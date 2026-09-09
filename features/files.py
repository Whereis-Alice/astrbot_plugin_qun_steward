"""群文件上传 / 删除 / 浏览 / 直链 / 移动 / 改名 / 容量 / 整理。

路径写法沿用上游：
- 「文件夹名」
- 「文件.zip」
- 「文件夹名/文件.zip」
- 「序号」或「文件夹序号/文件序号」（序号来自「查看群文件」的列表）

相比上游补齐了协议端返回缺字段时的兜底、每一步的失败提示，以及一批新能力：
拿下载直链、跨文件夹移动、改名、容量统计，还有按规则批量清理（默认清「没放进
任何文件夹的散落文件」，删除前必须二次确认）。
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.message.components import File, Image, Reply, Video

from ..core.card import Bar, Card, Note, Stat, Stats
from ..core.config import LOG_TAG
from ..core.protocol import (
    LLBOT,
    as_dict,
    call_action,
    call_action_variants,
    detect_backend,
    explain_action_error,
    extract_failure,
    unwrap,
)
from ..core.utils import (
    download_file,
    format_datetime,
    format_size,
    load_bytes,
    parse_int,
    sanitize_filename,
    timestamp_seconds,
)
from .base import Feature, FeatureContext

#: 群文件夹名长度上限（QQ 侧限制）
FOLDER_NAME_LIMIT = 30

#: 一次「整理群文件」最多删多少个，防手滑
TIDY_LIMIT = 50

#: 二次确认的有效期（秒）
TIDY_CONFIRM_TTL = 180
#: 非空文件夹删除确认的有效期（秒）
FOLDER_CONFIRM_TTL = 180

#: 群文件根目录在协议端的表示
ROOT_DIR = "/"

Entry = tuple[str, str]


def _is_file_source(value: Any) -> bool:
    """判断兼容字段是否像可下载来源，而不是协议端内部 file id。"""
    if value in (None, ""):
        return False
    text = str(value).strip()
    if not text:
        return False
    if text.startswith(("http://", "https://", "file://", "base64://", "data:")):
        return True
    try:
        return Path(text).is_file()
    except (OSError, ValueError):
        return False


class _FileReadError(RuntimeError):
    """读取群文件目录失败或返回了无法安全操作的数据。"""


class FilesFeature(Feature):
    """群文件相关操作。"""

    def __init__(self, ctx: FeatureContext) -> None:
        super().__init__(ctx)
        # (group_id, operator_id) -> (确认截止时间, 规则原文)，用于「整理群文件」
        # 的二次确认。把操作者纳入 key，避免同一群的另一位管理员误确认。
        self._pending_tidy: dict[tuple[str, str], tuple[float, str]] = {}
        # (group_id, operator_id) -> (确认截止时间, folder_id, folder_name)，用于
        # 删除非空文件夹。
        self._pending_folder_delete: dict[tuple[str, str], tuple[float, str, str]] = {}

    # ------------------------------------------------------------ 基础查询 --- #
    async def _fetch_root(self, event: AstrMessageEvent) -> tuple[dict[str, Any], bool]:
        """读取根目录，并返回（数据，是否成功）。"""
        try:
            data = await event.bot.get_group_root_files(group_id=int(event.get_group_id()))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 获取群文件根目录失败：{exc}")
            return {"folders": [], "files": []}, False
        if reason := extract_failure(data):
            logger.warning(f"{LOG_TAG} 获取群文件根目录被协议端拒绝：{reason}")
            return {"folders": [], "files": []}, False
        if not isinstance(data, (dict, list)):
            logger.warning(f"{LOG_TAG} 获取群文件根目录返回了异常数据：{type(data).__name__}")
            return {"folders": [], "files": []}, False
        return self._normalize(data), True

    async def _root(self, event: AstrMessageEvent) -> dict[str, Any]:
        """根目录列表；读取失败必须显式抛出，不能伪装成空目录。"""
        data, ok = await self._fetch_root(event)
        if not ok:
            raise _FileReadError("读取根目录失败")
        return data

    async def _fetch_folder_contents(
        self, event: AstrMessageEvent, folder_id: str
    ) -> tuple[dict[str, Any], bool]:
        """读取文件夹内容，并返回（数据，是否成功）。"""
        if not str(folder_id or "").strip():
            return {"folders": [], "files": []}, False
        try:
            data = await event.bot.get_group_files_by_folder(
                group_id=int(event.get_group_id()), folder_id=folder_id
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 获取群文件夹内容失败 folder={folder_id}: {exc}")
            return {"folders": [], "files": []}, False
        if reason := extract_failure(data):
            logger.warning(f"{LOG_TAG} 获取群文件夹内容被协议端拒绝 folder={folder_id}: {reason}")
            return {"folders": [], "files": []}, False
        if not isinstance(data, (dict, list)):
            logger.warning(
                f"{LOG_TAG} 获取群文件夹内容返回了异常数据 folder={folder_id}："
                f"{type(data).__name__}"
            )
            return {"folders": [], "files": []}, False
        return self._normalize(data), True

    async def _in_folder(self, event: AstrMessageEvent, folder_id: str) -> dict[str, Any]:
        """读取文件夹内容；失败必须显式抛出，避免误判为空目录。"""
        data, ok = await self._fetch_folder_contents(event, folder_id)
        if not ok:
            raise _FileReadError(f"读取文件夹【{folder_id}】内容失败")
        return data

    @staticmethod
    def _normalize(data: Any) -> dict[str, Any]:
        """统一成 ``{folders: [], files: []}``，兼容不同协议端的嵌套包装。

        NapCat 的根目录通常是 ``files`` / ``folders`` 两个数组，但不同版本和
        桥接层还会出现 ``items: [{fileInfo: ...}, {folderInfo: ...}]``、
        ``data.result`` 多层包装，以及 snake_case / camelCase 混用。这里采用
        「先识别容器，再识别条目」的递归解析，而不是把所有字典都当成文件；
        这样空的 ``folderInfo`` / ``fileInfo`` 元数据不会被误展示，更不会在
        「整理群文件」时触发误删。
        """
        value = unwrap(data)
        folders: list[dict[str, Any]] = []
        files: list[dict[str, Any]] = []
        indexes: dict[str, dict[str, int]] = {"folder": {}, "file": {}}

        folder_aliases = {
            "folder_id": (
                "folderId",
                "folderID",
                "directory_id",
                "directoryId",
                "id",
            ),
            "folder_name": ("folderName", "directoryName", "name"),
            "total_file_count": (
                "totalFileCount",
                "total_file_count",
                "total_count",
                "totalCount",
                "file_count",
                "fileCount",
            ),
        }
        file_aliases = {
            "file_id": ("fileId", "fileID", "fid", "id"),
            "file_name": ("fileName", "filename", "name"),
            "file_size": ("fileSize", "size"),
            "upload_time": ("uploadTime", "uploadedTime"),
            "modify_time": ("modifyTime", "modifiedTime"),
            "dead_time": ("deadTime", "expireTime", "expiresAt"),
            "download_times": ("downloadTimes", "downloadedTimes"),
            "uploader_name": ("uploaderName", "creatorName"),
            "uploader": ("uploaderUin", "uploaderId", "userId", "user_id"),
            "busid": ("busId", "busid"),
        }

        folder_wrappers = ("folderInfo", "folder_info")
        file_wrappers = ("fileInfo", "file_info")
        folder_containers = (
            "folders",
            "folder_list",
            "folderList",
            "directories",
            "directory_list",
            "directoryList",
        )
        file_containers = (
            "files",
            "file_list",
            "fileList",
        )
        mixed_containers = ("items", "list", "entries", "children")
        nested_containers = ("data", "result", "payload", "group_item", "groupItem")

        def source_item(item: dict[str, Any], kind: str) -> dict[str, Any]:
            """取出 fileInfo/folderInfo 内层，同时保留外层的辅助字段。"""
            wrappers = folder_wrappers if kind == "folder" else file_wrappers
            inner = next(
                (item[key] for key in wrappers if isinstance(item.get(key), dict)),
                None,
            )
            if inner is None:
                return dict(item)
            outer = {key: val for key, val in item.items() if key not in wrappers}
            return {**outer, **inner}

        def canonical(item: Any, kind: str) -> dict[str, Any]:
            if not isinstance(item, dict):
                return {}
            result = source_item(item, kind)
            aliases = folder_aliases if kind == "folder" else file_aliases
            for name, candidates in aliases.items():
                if result.get(name) in (None, ""):
                    for candidate in candidates:
                        if result.get(candidate) not in (None, ""):
                            result[name] = result[candidate]
                            break
            # 没有任何可定位信息的空包装不是一个真实条目。
            id_key = "folder_id" if kind == "folder" else "file_id"
            name_key = "folder_name" if kind == "folder" else "file_name"
            if result.get(id_key) in (None, "") and result.get(name_key) in (None, ""):
                return {}
            return result

        def add(kind: str, item: Any) -> None:
            if isinstance(item, list):
                for child in item:
                    add(kind, child)
                return
            normalized = canonical(item, kind)
            if not normalized:
                return
            id_key = "folder_id" if kind == "folder" else "file_id"
            name_key = "folder_name" if kind == "folder" else "file_name"
            identifier = str(normalized.get(id_key) or "").strip()
            # 没有 ID 的兼容响应只能按名称去重；有 ID 时绝不把两个不同条目合并。
            dedup_key = identifier or str(normalized.get(name_key) or "").casefold()
            target = folders if kind == "folder" else files
            if dedup_key and dedup_key in indexes[kind]:
                existing = target[indexes[kind][dedup_key]]
                for key, val in normalized.items():
                    if existing.get(key) in (None, "") and val not in (None, ""):
                        existing[key] = val
                return
            indexes[kind][dedup_key] = len(target) if dedup_key else -1
            target.append(normalized)

        def explicit_kind(item: dict[str, Any], hint: str | None) -> str | None:
            raw_type = item.get("type") or item.get("kind") or item.get("entryType")
            if isinstance(raw_type, str):
                lowered = raw_type.strip().lower()
                if lowered in {"folder", "directory", "dir", "文件夹", "目录"}:
                    return "folder"
                if lowered in {"file", "文件"}:
                    return "file"
            if hint in {"folder", "file"}:
                return hint
            if any(key in item for key in folder_wrappers):
                return "folder"
            if any(key in item for key in file_wrappers):
                return "file"
            if any(
                key in item
                for key in (
                    "folder_id",
                    "folderId",
                    "folderName",
                    "directory_id",
                    "directoryId",
                    "directoryName",
                )
            ):
                return "folder"
            if any(
                key in item
                for key in (
                    "file_id",
                    "fileId",
                    "fileName",
                    "fileSize",
                    "uploadTime",
                    "downloadTimes",
                    "busId",
                )
            ):
                return "file"
            # 只有通用 id/name 时按文件处理。文件是可读对象，文件夹则必须
            # 有明确的 folder 字段，避免把普通元数据误当成可删除目录。
            if any(key in item for key in ("id", "name")):
                return "file"
            return None

        def visit(current: Any, hint: str | None = None, depth: int = 0) -> None:
            if depth > 8 or current is None:
                return
            if isinstance(current, list):
                for child in current:
                    visit(child, hint, depth + 1)
                return
            if not isinstance(current, dict):
                return

            handled = False
            # 精确包装优先处理，items 里可以同时放文件和文件夹。
            for key in folder_wrappers:
                if key in current:
                    add("folder", current[key])
                    handled = True
            for key in file_wrappers:
                if key in current:
                    add("file", current[key])
                    handled = True

            for key in folder_containers:
                if key in current:
                    visit(current[key], "folder", depth + 1)
                    handled = True
            for key in file_containers:
                if key in current:
                    visit(current[key], "file", depth + 1)
                    handled = True
            for key in mixed_containers:
                if key in current:
                    visit(current[key], None, depth + 1)
                    handled = True
            for key in nested_containers:
                nested = current.get(key)
                if isinstance(nested, (dict, list)):
                    visit(nested, hint, depth + 1)
                    handled = True

            kind = explicit_kind(current, hint)
            # 含容器键的响应壳不是条目本身；但带明确 fileInfo/folderInfo 的
            # 外层已经在上面处理过，不需要再次添加。
            is_container = handled and any(
                key in current
                for key in (
                    *folder_containers,
                    *file_containers,
                    *mixed_containers,
                    *nested_containers,
                )
            )
            has_wrapper = handled and any(
                key in current for key in (*folder_wrappers, *file_wrappers)
            )
            if kind and not is_container and not has_wrapper:
                add(kind, current)

        visit(value)
        return {"folders": folders, "files": files}

    @staticmethod
    def _folder_name(folder: dict[str, Any]) -> str:
        return str(folder.get("folder_name") or folder.get("folderName") or "未命名文件夹")

    @staticmethod
    def _folder_id(folder: dict[str, Any]) -> str:
        return str(folder.get("folder_id") or folder.get("folderId") or folder.get("id") or "")

    @staticmethod
    def _file_name(file: dict[str, Any]) -> str:
        return str(file.get("file_name") or file.get("fileName") or "未命名文件")

    @staticmethod
    def _file_id(file: dict[str, Any]) -> str:
        return str(file.get("file_id") or file.get("fileId") or file.get("id") or "")

    def _listing(self, data: dict[str, Any], title: str) -> tuple[str, dict[int, Entry]]:
        """渲染目录文本，同时返回「序号 -> (类型, 名称)」映射。"""
        lines = [title] if title else []
        mapping: dict[int, Entry] = {}
        index = 1
        for folder in data["folders"]:
            name = self._folder_name(folder)
            count = folder.get("total_file_count")
            suffix = f"（{count} 个文件）" if count is not None else ""
            lines.append(f"▶{index}. {name}{suffix}")
            mapping[index] = ("folder", name)
            index += 1
        for file in data["files"]:
            name = self._file_name(file)
            lines.append(f"📄{index}. {name}")
            mapping[index] = ("file", name)
            index += 1
        if len(lines) <= (1 if title else 0):
            lines.append("（空目录）")
        return "\n".join(lines), mapping

    async def _find_folder(self, event: AstrMessageEvent, name: str) -> dict[str, Any] | None:
        root = await self._root(event)
        return self._find_folder_in_root(root, name)

    def _find_folder_in_root(
        self, root: dict[str, Any], name: str
    ) -> dict[str, Any] | None:
        """在已读取的根目录中按名称、展示序号或 folder_id 定位文件夹。"""
        target = str(name or "").strip()
        if not target:
            return None
        # 名字优先：这样名为「1」的文件夹仍然可以被精确找到。
        found = next((f for f in root["folders"] if self._folder_name(f) == target), None)
        if found:
            return found
        if target.isdigit():
            _, mapping = self._listing(root, "")
            entry = mapping.get(int(target))
            if entry and entry[0] == "folder":
                return next(
                    (f for f in root["folders"] if self._folder_name(f) == entry[1]), None
                )
        return next((f for f in root["folders"] if self._folder_id(f) == target), None)

    async def _find_file(
        self, event: AstrMessageEvent, folder_name: str, file_name: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """在指定文件夹里找文件，返回 (文件夹, 文件)。"""
        folder = await self._find_folder(event, folder_name)
        if not folder:
            return None, None
        data = await self._in_folder(event, self._folder_id(folder))
        file = next(
            (f for f in data["files"] if self._file_name(f) == file_name),
            None,
        )
        return folder, file

    # ------------------------------------------------------------ 路径解析 --- #
    async def _parse_path(
        self, event: AstrMessageEvent, path: str
    ) -> tuple[str | None, str | None]:
        """把用户输入解析成 (文件夹名, 文件名)。"""
        text = (path or "").strip()
        if not text:
            return None, None
        root = await self._root(event)
        _, mapping = self._listing(root, "")

        def by_index(token: str, kind: str | None = None) -> str | None:
            if not token.isdigit():
                return None
            entry = mapping.get(int(token))
            if not entry:
                return None
            if kind and entry[0] != kind:
                return None
            return entry[1]

        if "/" in text:
            left, right = text.split("/", 1)
            folder_name = by_index(left, "folder") or left
            if right.isdigit():
                folder = await self._find_folder(event, folder_name)
                if folder:
                    data = await self._in_folder(event, self._folder_id(folder))
                    _, sub_mapping = self._listing(data, "")
                    entry = sub_mapping.get(int(right))
                    if entry and entry[0] == "file":
                        return folder_name, entry[1]
            return folder_name, right

        if text.isdigit():
            entry = mapping.get(int(text))
            if not entry:
                return None, None
            return (entry[1], None) if entry[0] == "folder" else (None, entry[1])

        if "." in text:
            return None, text
        return text, None

    # ------------------------------------------------------------ 文件详情 --- #
    def _file_detail(self, file: dict[str, Any]) -> str:
        lines = [f"【📄 {self._file_name(file)}】"]
        lines.append(
            f"文件大小：{format_size(file.get('file_size') or file.get('fileSize') or file.get('size'))}"
        )
        uploader_name = (
            file.get("uploader_name")
            or file.get("uploaderName")
            or file.get("creatorName")
            or "未知"
        )
        uploader = (
            file.get("uploader")
            or file.get("uploaderUin")
            or file.get("uploaderId")
            or "未知"
        )
        lines.append(
            f"上传者：{uploader_name}({uploader})"
        )
        download_times = (
            file.get("download_times")
            if file.get("download_times") not in (None, "")
            else file.get("downloadTimes")
        )
        lines.append(f"下载次数：{download_times if download_times not in (None, '') else '未知'}")
        upload_time = file.get("upload_time") or file.get("uploadTime")
        upload_seconds = timestamp_seconds(upload_time)
        if upload_seconds:
            lines.append(f"上传时间：{format_datetime(upload_seconds)}")
        dead_time = file.get("dead_time")
        if dead_time in (None, ""):
            dead_time = file.get("deadTime") or file.get("expireTime")
        parsed_dead_time = timestamp_seconds(dead_time)
        if parsed_dead_time is None:
            expiry = "未知"
        else:
            expiry = "永久有效" if parsed_dead_time <= 0 else format_datetime(parsed_dead_time)
        lines.append("过期时间：" + expiry)
        modify_time = file.get("modify_time") or file.get("modifyTime")
        modify_seconds = timestamp_seconds(modify_time)
        if modify_seconds:
            lines.append(f"修改时间：{format_datetime(modify_seconds)}")
        return "\n".join(lines)

    # ------------------------------------------------------------ 上传 --- #
    async def _download_quoted(self, event: AstrMessageEvent, file_name: str) -> Any:
        """把被引用消息里的文件下载到本地缓存目录。"""
        chain = event.message_obj.message or []
        if not chain or not isinstance(chain[0], Reply):
            return None
        reply_chain = getattr(chain[0], "chain", None) or []
        segment = reply_chain[0] if reply_chain else None
        if not isinstance(segment, (File, Image, Video)):
            return None
        target = self.config.file_dir / sanitize_filename(file_name, "upload.bin")
        if isinstance(segment, File):
            # File.file 是同步属性：异步环境里访问它会发出警告，而且 file_
            # 可能只是协议端的内部 ID。优先走 AstrBot 推荐的异步解析入口，
            # 让它决定是返回原 URL 还是先下载到临时文件。
            source = ""
            try:
                source = await segment.get_file(allow_return_url=True)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"{LOG_TAG} 解析引用文件失败：{exc}")
            if not source:
                # 兼容旧版/自定义组件：只接受明确的 URL、编码数据或确实存在
                # 的本地路径，绝不把内部 file id 直接交给本地下载器。
                candidates = (
                    getattr(segment, "url", None),
                    getattr(segment, "file_", None),
                    getattr(segment, "path", None),
                )
                source = next((item for item in candidates if _is_file_source(item)), "")
        else:
            source = (
                getattr(segment, "path", None)
                or getattr(segment, "url", None)
                or getattr(segment, "file", None)
            )
        if not source:
            return None
        source = str(source)
        logger.info(f"{LOG_TAG} 正在下载待上传文件：{source}")
        try:
            data = await load_bytes(source)
            if data is None:
                # 某些自定义组件只在 file 字段里放下载地址，保留旧的
                # download_file 路径作为兼容兜底。
                await download_file(source, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"{LOG_TAG} 下载待上传文件失败：{exc}")
            with contextlib.suppress(OSError):
                target.unlink(missing_ok=True)
            return None
        if not target.exists():
            return None
        return target

    async def _ensure_folder(
        self, event: AstrMessageEvent, folder_name: str
    ) -> dict[str, Any] | None:
        """文件夹不存在就建一个。"""
        existing = await self._find_folder(event, folder_name)
        if existing:
            return existing
        safe_name = sanitize_filename(folder_name, "新建文件夹")[:FOLDER_NAME_LIMIT]
        try:
            await event.bot.create_group_file_folder(
                group_id=int(event.get_group_id()), folder_name=safe_name, parent_id="/"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"{LOG_TAG} 创建群文件夹失败 name={safe_name}: {exc}")
            return None
        return await self._find_folder(event, safe_name)

    async def upload(self, event: AstrMessageEvent, path: Any = None) -> str:
        try:
            folder_name, file_name = await self._parse_path(event, str(path or ""))
        except _FileReadError as exc:
            return f"读取群文件目录失败，为安全起见未上传：{exc}"
        if not file_name:
            return "路径里没有文件名，写法：上传群文件 [文件夹名/]文件名.后缀"

        local_path = await self._download_quoted(event, file_name)
        if not local_path:
            return "请引用一条包含文件的消息，再发送该指令"

        try:
            folder_id = None
            if folder_name:
                folder = await self._ensure_folder(event, folder_name)
                if not folder:
                    return f"无法创建或找到群文件夹【{folder_name}】"
                folder_id = self._folder_id(folder)
                if not folder_id:
                    return f"群文件夹【{folder_name}】没有可用的 folder_id，上传已取消"

            await event.bot.upload_group_file(
                group_id=int(event.get_group_id()),
                file=str(local_path),
                name=file_name,
                folder_id=folder_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"{LOG_TAG} 上传群文件失败：{exc}")
            await self.log(
                event, "file_upload", detail=str(exc), success=False, target_id=file_name
            )
            return f"上传失败：{exc}"
        finally:
            # 缓存文件不留在磁盘上
            with contextlib.suppress(OSError):
                local_path.unlink(missing_ok=True)

        await self.log(event, "file_upload", target_id=file_name, detail=str(path or ""))
        location = f"{folder_name}/{file_name}" if folder_name else file_name
        return f"群文件已上传：{location}"

    # ------------------------------------------------------------ 删除 --- #
    async def delete(self, event: AstrMessageEvent, path: Any = None) -> str:
        if not event.get_group_id():
            return "删除群文件只能在群里使用"
        try:
            folder_name, file_name = await self._parse_path(event, str(path or ""))
        except _FileReadError as exc:
            return f"读取群文件目录失败，为安全起见未删除：{exc}"
        if not folder_name and not file_name:
            return "请指定要删除的文件夹或文件，可先用「查看群文件」看序号"
        group_id = int(event.get_group_id())

        if file_name:
            if folder_name:
                try:
                    folder, file = await self._find_file(event, folder_name, file_name)
                except _FileReadError as exc:
                    return f"读取文件夹【{folder_name}】失败，为安全起见未删除：{exc}"
                if not folder or not file:
                    return f"未找到 {folder_name}/{file_name}"
            else:
                try:
                    root = await self._root(event)
                except _FileReadError as exc:
                    return f"读取群文件根目录失败，为安全起见未删除：{exc}"
                file = next(
                    (f for f in root["files"] if self._file_name(f) == file_name),
                    None,
                )
                if not file:
                    return f"未找到群文件：📄{file_name}"
            file_id = self._file_id(file)
            if not file_id:
                return f"群文件【{file_name}】没有可用的 file_id，为安全起见未删除"
            try:
                response = await event.bot.delete_group_file(group_id=group_id, file_id=file_id)
                if reason := extract_failure(response):
                    raise RuntimeError(reason)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"{LOG_TAG} 删除群文件失败：{exc}")
                await self.log(
                    event,
                    "file_delete",
                    target_id=file_name,
                    detail=str(exc),
                    success=False,
                )
                return f"删除失败：{exc}"
            await self.log(event, "file_delete", target_id=file_name)
            return f"已删除群文件：📄{file_name}"

        try:
            folder = await self._find_folder(event, str(folder_name))
        except _FileReadError as exc:
            return f"读取群文件目录失败，为安全起见未删除：{exc}"
        if not folder:
            return f"群文件夹【{folder_name}】不存在"
        # 「删除群文件」也可能解析到文件夹，统一转到带二次确认的入口，
        # 避免通过旧指令绕过非空目录保护。
        return await self.delete_folder(event, str(folder_name))

    @staticmethod
    def _folder_count(folder: dict[str, Any], data: dict[str, Any]) -> int:
        """估算文件夹内容数量，优先取协议端统计，再用实际列表补足。"""
        reported = folder.get("total_file_count")
        if reported in (None, ""):
            reported = folder.get("totalFileCount")
        reported_count = max(0, parse_int(reported, 0) or 0)
        listed_count = len(data.get("files") or []) + len(data.get("folders") or [])
        return max(reported_count, listed_count)

    async def _delete_folder_now(
        self, event: AstrMessageEvent, folder: dict[str, Any], display_name: str
    ) -> str:
        """通过统一动作层删除文件夹，兼容 SnowLuma 与旧版 NapCat 动作名。"""
        group_id = event.get_group_id()
        folder_id = self._folder_id(folder)
        if not folder_id:
            return "这个文件夹没有可用的 folder_id，无法删除"
        result = await call_action(
            event,
            ("delete_group_file_folder", "delete_group_folder"),
            group_id=int(group_id),
            folder_id=folder_id,
        )
        if not result.ok:
            logger.error(f"{LOG_TAG} 删除群文件夹失败：{result.error}")
            await self.log(
                event,
                "file_delete",
                target_id=display_name,
                detail=result.error,
                success=False,
            )
            return f"删除群文件夹失败：{explain_action_error(result, '删除群文件夹')}"
        await self.log(event, "file_delete", target_id=display_name, detail="文件夹")
        return f"已删除群文件夹：▶{display_name}"

    async def delete_folder(self, event: AstrMessageEvent, raw: Any = None) -> str:
        """删除文件夹；非空文件夹先预览，收到「确认」后才执行。"""
        group_id = str(event.get_group_id() or "")
        if not group_id:
            return "该指令只能在群里使用"
        operator_id = str(event.get_sender_id() or "")
        pending_key = (group_id, operator_id)

        text = str(raw or "").strip()
        tokens = text.split()
        confirm = bool(tokens and tokens[-1].lower() in {"确认", "执行", "yes"})
        if confirm:
            text = text.rsplit(maxsplit=1)[0] if len(tokens) > 1 else ""

        pending = self._pending_folder_delete.get(pending_key)
        if pending and pending[0] < time.time():
            self._pending_folder_delete.pop(pending_key, None)
            pending = None

        folder: dict[str, Any] | None = None
        display_name = text
        root: dict[str, Any] = {"folders": [], "files": []}
        if text:
            root, root_ok = await self._fetch_root(event)
            if not root_ok:
                return "读取群文件目录失败，为安全起见未删除，请稍后重试"
            folder = self._find_folder_in_root(root, text)
            if not folder:
                return f"群文件夹【{text}】不存在，可用名称或「查看群文件」中的序号"
            display_name = self._folder_name(folder)
        elif pending:
            folder_id = pending[1]
            root, root_ok = await self._fetch_root(event)
            if not root_ok:
                return "读取群文件目录失败，为安全起见未删除，请稍后重试"
            folder = next(
                (item for item in root["folders"] if self._folder_id(item) == folder_id),
                None,
            )
            display_name = pending[2]
            if not folder:
                self._pending_folder_delete.pop(pending_key, None)
                return f"待删除的文件夹【{display_name}】已经不存在"
        else:
            return "用法：删除群文件夹 <名称或序号>；非空文件夹需再发送「删除群文件夹 确认」"

        assert folder is not None
        folder_id = self._folder_id(folder)
        if not folder_id:
            return "这个文件夹没有可用的 folder_id，无法删除"
        contents, contents_ok = await self._fetch_folder_contents(event, folder_id)
        if not contents_ok:
            return "无法确认文件夹内容，为安全起见未删除，请稍后重试"
        count = self._folder_count(folder, contents)
        if count > 0 and not confirm:
            self._pending_folder_delete[pending_key] = (
                time.time() + FOLDER_CONFIRM_TTL,
                folder_id,
                display_name,
            )
            listed = [
                self._file_name(item) for item in contents.get("files", [])[:5]
            ] + [
                self._folder_name(item) for item in contents.get("folders", [])[:5]
            ]
            preview = "；内容示例：" + "、".join(listed[:5]) if listed else ""
            return (
                f"文件夹【{display_name}】不是空的，里面约有 {count} 项内容{preview}。\n"
                f"如确认删除（不可恢复），请在 {FOLDER_CONFIRM_TTL // 60} 分钟内发送"
                "「删除群文件夹 确认」"
            )
        if count > 0 and (not confirm or not pending or pending[1] != folder_id):
            return "为了避免误删，请先发送不带「确认」的命令查看预览"
        self._pending_folder_delete.pop(pending_key, None)
        return await self._delete_folder_now(event, folder, display_name)

    @staticmethod
    def _validate_folder_name(name: Any) -> tuple[str, str]:
        """校验 QQ 文件夹名，返回（清洗后的名字，错误信息）。"""
        text = str(name or "").strip()
        if not text:
            return "", "新文件夹名不能为空"
        if text in {".", ".."}:
            return "", "新文件夹名不能是 . 或 .."
        if text != sanitize_filename(text):
            return "", "文件夹名里不能有 / \\ : * ? \" < > | 这些字符"
        if len(text) > FOLDER_NAME_LIMIT:
            return "", f"文件夹名最多 {FOLDER_NAME_LIMIT} 个字符"
        return text, ""

    async def rename_folder(self, event: AstrMessageEvent, raw: Any = None) -> str:
        """重命名群文件夹 <原名称或序号> <新名称>。"""
        group_id = event.get_group_id()
        if not group_id:
            return "重命名群文件夹只能在群里使用"
        text = str(raw or "").strip()
        if not text:
            return "用法：重命名群文件夹 <原名称或序号> <新名称>"

        source = ""
        new_name = ""
        for separator in ("->", "=>", "→"):
            if separator in text:
                source, new_name = (part.strip() for part in text.split(separator, 1))
                break
        if not source:
            parts = text.split()
            if len(parts) < 2:
                return "用法：重命名群文件夹 <原名称或序号> <新名称>"
            source, new_name = " ".join(parts[:-1]), parts[-1]

        safe_name, error = self._validate_folder_name(new_name)
        if error:
            return error
        root, root_ok = await self._fetch_root(event)
        if not root_ok:
            return "读取群文件目录失败，为安全起见未改名，请稍后重试"
        folder = self._find_folder_in_root(root, source)
        if not folder:
            return f"群文件夹【{source}】不存在，可用名称或「查看群文件」中的序号"
        old_name = self._folder_name(folder)
        if old_name == safe_name:
            return "新文件夹名和原来一样"
        folder_id = self._folder_id(folder)
        if not folder_id:
            return "这个文件夹没有可用的 folder_id，无法改名"
        if any(
            other is not folder and self._folder_name(other).casefold() == safe_name.casefold()
            for other in root["folders"]
        ):
            return f"群里已经有同名文件夹【{safe_name}】"

        result = await call_action(
            event,
            ("rename_group_file_folder",),
            group_id=int(group_id),
            folder_id=folder_id,
            new_folder_name=safe_name,
        )
        if not result.ok:
            await self.log(
                event,
                "folder_rename",
                target_id=old_name,
                detail=result.error,
                success=False,
            )
            return f"重命名群文件夹失败：{result.error}"
        await self.log(
            event, "folder_rename", target_id=old_name, detail=f"改名为 {safe_name}"
        )
        return f"已将群文件夹【{old_name}】重命名为【{safe_name}】"

    # ------------------------------------------------------------ 浏览 --- #
    async def view(self, event: AstrMessageEvent, path: Any = None) -> str:
        text = str(path or "").strip()
        if not text:
            root, root_ok = await self._fetch_root(event)
            if not root_ok:
                return "读取群文件根目录失败，请稍后重试；为安全起见没有显示为空目录"
            listing, _ = self._listing(root, "【群文件根目录】")
            return listing + "\n\n用「查看群文件 序号」进入文件夹或查看文件详情"

        try:
            folder_name, file_name = await self._parse_path(event, text)
        except _FileReadError as exc:
            return f"读取群文件目录失败：{exc}"

        if folder_name and file_name:
            try:
                _, file = await self._find_file(event, folder_name, file_name)
            except _FileReadError as exc:
                return f"读取文件夹【{folder_name}】失败：{exc}"
            if not file:
                return f"未找到群文件：📄{file_name}"
            return self._file_detail(file)

        if folder_name:
            try:
                folder = await self._find_folder(event, folder_name)
            except _FileReadError as exc:
                return f"读取群文件目录失败：{exc}"
            if folder:
                data, data_ok = await self._fetch_folder_contents(
                    event, self._folder_id(folder)
                )
                if not data_ok:
                    return f"读取文件夹【{folder_name}】失败，请稍后重试"
                listing, _ = self._listing(data, f"【{folder_name}】")
                return listing
            # 名字对不上文件夹，再当根目录文件试一次
            try:
                root = await self._root(event)
            except _FileReadError as exc:
                return f"读取群文件根目录失败：{exc}"
            file = next(
                (f for f in root["files"] if self._file_name(f) == folder_name),
                None,
            )
            if file:
                return self._file_detail(file)
            return f"未找到【{folder_name}】"

        if file_name:
            try:
                root = await self._root(event)
            except _FileReadError as exc:
                return f"读取群文件根目录失败：{exc}"
            file = next(
                (f for f in root["files"] if self._file_name(f) == file_name),
                None,
            )
            if file:
                return self._file_detail(file)
            return f"未找到群文件：📄{file_name}"
        return "未找到对应的群文件或文件夹"

    # ------------------------------------------------------------ 定位 --- #
    async def _locate_file(
        self, event: AstrMessageEvent, path: str
    ) -> tuple[dict[str, Any] | None, str, str]:
        """按路径找文件，返回 (文件, 所在文件夹 ID, 展示路径)；根目录用 ROOT_DIR 表示。"""
        folder_name, file_name = await self._parse_path(event, path)
        if not file_name:
            return None, "", ""
        if folder_name:
            folder, file = await self._find_file(event, folder_name, file_name)
            parent = self._folder_id(folder) if folder else ""
            return file, parent, f"{folder_name}/{file_name}"
        root = await self._root(event)
        file = next((f for f in root["files"] if self._file_name(f) == file_name), None)
        return file, ROOT_DIR, file_name

    # ------------------------------------------------------------ 直链 --- #
    @staticmethod
    def _extract_file_url(value: Any, depth: int = 0) -> str:
        """从协议端的多层返回中提取下载地址。"""
        if depth > 5:
            return ""
        if isinstance(value, str):
            text = value.strip()
            if text.lower().startswith(("http://", "https://", "file://")):
                return text
            return ""
        if isinstance(value, list):
            for item in value:
                if url := FilesFeature._extract_file_url(item, depth + 1):
                    return url
            return ""
        if not isinstance(value, dict):
            return ""
        preferred = (
            "url",
            "download_url",
            "downloadUrl",
            "file_url",
            "fileUrl",
            "link",
            "href",
            "data",
            "result",
        )
        for key in preferred:
            if key in value and (
                url := FilesFeature._extract_file_url(value[key], depth + 1)
            ):
                return url
        for key, nested in value.items():
            if ("url" in str(key).lower() or "link" in str(key).lower()) and (
                url := FilesFeature._extract_file_url(nested, depth + 1)
            ):
                return url
        return ""

    async def link(self, event: AstrMessageEvent, path: Any = None) -> str:
        """取群文件的下载直链，方便转存到别处。"""
        if not event.get_group_id():
            return "群文件直链只能在群里使用"
        text = str(path or "").strip()
        if not text:
            return "用法：群文件直链 <[文件夹名/]文件名>，也可以用「查看群文件」里的序号"
        try:
            file, _, display = await self._locate_file(event, text)
        except _FileReadError as exc:
            return f"读取群文件目录失败：{exc}"
        if not file:
            return f"未找到群文件：{display or text}"
        file_id = self._file_id(file)
        if not file_id:
            return f"群文件【{display or text}】没有可用的 file_id，无法获取直链"

        result = await call_action(
            event,
            ("get_group_file_url",),
            group_id=int(event.get_group_id()),
            file_id=file_id,
            busid=file.get("busid"),
        )
        if not result.ok:
            return f"取直链失败：{result.error}"
        url = self._extract_file_url(result.data)
        if not url:
            return "协议端没有返回下载地址"
        return f"📄{display}\n{url}\n（直链有有效期，过期重新获取即可）"

    # ------------------------------------------------------------ 移动 --- #
    async def move(self, event: AstrMessageEvent, raw: Any = None) -> str:
        """移动群文件 <源路径> <目标文件夹|根目录>。"""
        if not event.get_group_id():
            return "移动群文件只能在群里使用"
        parts = str(raw or "").split()
        if len(parts) < 2:
            return "用法：移动群文件 <[文件夹名/]文件名> <目标文件夹名>，目标写「根目录」则移到最外层"
        target_name = parts[-1]
        source = " ".join(parts[:-1])

        try:
            file, parent, display = await self._locate_file(event, source)
        except _FileReadError as exc:
            return f"读取群文件目录失败，为安全起见未移动：{exc}"
        if not file:
            return f"未找到群文件：{display or source}"
        file_id = self._file_id(file)
        if not file_id:
            return f"群文件【{display or source}】没有可用的 file_id，为安全起见未移动"
        if not parent:
            return f"无法确认群文件【{display or source}】所在目录，为安全起见未移动"

        if target_name in {"根目录", "/", "根"}:
            target_id = ROOT_DIR
            target_label = "根目录"
        else:
            try:
                folder = await self._find_folder(event, target_name)
            except _FileReadError as exc:
                return f"读取群文件目录失败，为安全起见未移动：{exc}"
            if not folder:
                return f"目标文件夹【{target_name}】不存在，可先用「查看群文件」确认名字"
            target_id = self._folder_id(folder)
            target_label = target_name
        if target_id == parent:
            return f"它已经在【{target_label}】里了"

        backend = await detect_backend(event.bot)
        # llbot 仍沿用 parent_directory / target_directory；NapCat 与
        # SnowLuma 的当前实现使用 current_parent_directory /
        # target_parent_directory。先发对应端的正式参数，再用另一套做兼容兜底。
        if backend == LLBOT:
            param_sets = (
                {"parent_directory": parent, "target_directory": target_id},
                {
                    "current_parent_directory": parent,
                    "target_parent_directory": target_id,
                },
            )
        else:
            param_sets = (
                {
                    "current_parent_directory": parent,
                    "target_parent_directory": target_id,
                },
                {"parent_directory": parent, "target_directory": target_id},
            )
        variants = tuple(
            (
                "move_group_file",
                {
                    "group_id": int(event.get_group_id()),
                    "file_id": file_id,
                    **directories,
                },
            )
            for directories in param_sets
        )
        result = await call_action_variants(event, variants)
        if not result.ok:
            await self.log(
                event, "file_move", target_id=display, detail=result.error, success=False
            )
            return f"移动失败：{result.error}"
        await self.log(event, "file_move", target_id=display, detail=f"移动到 {target_label}")
        return f"已把 📄{display} 移动到【{target_label}】"

    # ------------------------------------------------------------ 改名 --- #
    async def rename(self, event: AstrMessageEvent, raw: Any = None) -> str:
        """重命名群文件 <路径> <新文件名>。"""
        if not event.get_group_id():
            return "重命名群文件只能在群里使用"
        parts = str(raw or "").split()
        if len(parts) < 2:
            return "用法：重命名群文件 <[文件夹名/]文件名> <新文件名>"
        new_name = parts[-1]
        source = " ".join(parts[:-1])
        if new_name != sanitize_filename(new_name):
            return "新文件名里不能有 / \\ : * ? \" < > | 这些字符"

        try:
            file, parent, display = await self._locate_file(event, source)
        except _FileReadError as exc:
            return f"读取群文件目录失败，为安全起见未改名：{exc}"
        if not file:
            return f"未找到群文件：{display or source}"
        file_id = self._file_id(file)
        if not file_id:
            return f"群文件【{display or source}】没有可用的 file_id，为安全起见未改名"
        if not parent:
            return f"无法确认群文件【{display or source}】所在目录，为安全起见未改名"
        if self._file_name(file) == new_name:
            return "新名字和原来一样"

        result = await call_action(
            event,
            ("rename_group_file",),
            group_id=int(event.get_group_id()),
            file_id=file_id,
            current_parent_directory=parent,
            new_name=new_name,
        )
        if not result.ok:
            await self.log(
                event, "file_rename", target_id=display, detail=result.error, success=False
            )
            return f"改名失败：{result.error}"
        await self.log(event, "file_rename", target_id=display, detail=f"改名为 {new_name}")
        return f"已把 📄{display} 改名为 📄{new_name}"

    # ------------------------------------------------------------ 容量 --- #
    async def usage(self, event: AstrMessageEvent) -> str | Card:
        """群文件容量统计。"""
        group_id = event.get_group_id()
        if not group_id:
            return "群文件容量只能在群里使用"
        result = await call_action(
            event, ("get_group_file_system_info",), group_id=int(group_id)
        )
        if not result.ok:
            return f"读取群文件容量失败：{result.error}"
        info = as_dict(result.data)
        for key in ("file_system_info", "fileSystemInfo", "info"):
            nested = info.get(key)
            if isinstance(nested, dict):
                info = nested
                break
        used = max(
            0,
            parse_int(info.get("used_space") or info.get("usedSpace"), 0) or 0,
        )
        total = max(
            0,
            parse_int(info.get("total_space") or info.get("totalSpace"), 0) or 0,
        )
        count = max(
            0,
            parse_int(info.get("file_count") or info.get("fileCount"), 0) or 0,
        )
        limit = max(
            0,
            parse_int(info.get("limit_count") or info.get("limitCount"), 0) or 0,
        )

        card = Card(title="群文件容量", subtitle=f"群号 {group_id}", badge="群文件")
        if total > 0:
            card.add(
                Bar(
                    label="已用空间",
                    ratio=min(1.0, used / total),
                    value=f"{format_size(used)} / {format_size(total)}",
                    note=f"剩余 {format_size(max(0, total - used))}",
                )
            )
        else:
            card.add(Bar(label="已用空间", ratio=0.0, value=format_size(used), note="协议端没返回总容量"))

        stats = [Stat(label="文件数", value=str(count), note=f"上限 {limit}" if limit else "")]
        if limit:
            stats.append(
                Stat(
                    label="名额占用",
                    value=f"{min(100, count * 100 // limit)}%",
                    tone="warn" if count * 10 >= limit * 9 else "brand",
                )
            )
        card.add(Stats(items=stats))
        if total > 0 and used * 10 >= total * 9:
            card.add(Note(text="容量快满了，可以用「整理群文件」清一批旧文件", tone="warn"))
        return card

    # ------------------------------------------------------------ 整理 --- #
    @staticmethod
    def _tidy_rule(tokens: list[str]) -> tuple[str, int, int]:
        """解析整理规则，返回 (规则名, 天数, 字节阈值)。

        规则名取值：loose（散落在根目录、没归进文件夹的文件）、expired（协议端标记
        已过期）、days（上传超过 N 天）、size（大于 N MB）。
        """
        days = 0
        size = 0
        rule = "loose"
        for token in tokens:
            if token in {"过期", "已过期"}:
                rule = "expired"
            elif token in {"散落", "根目录", "未归档"}:
                rule = "loose"
            elif match := re.fullmatch(r"(\d+)\s*天", token):
                rule, days = "days", int(match.group(1))
            elif match := re.fullmatch(r"(?:大于)?(\d+)\s*(?:M|MB|m|mb)", token):
                rule, size = "size", int(match.group(1)) * 1024 * 1024
        return rule, days, size

    @staticmethod
    def _rule_label(rule: str, days: int, size: int) -> str:
        return {
            "loose": "根目录里没归进文件夹的散落文件",
            "expired": "协议端标记为已过期的文件",
            "days": f"上传超过 {days} 天的文件",
            "size": f"大于 {format_size(size)} 的文件",
        }[rule]

    async def _tidy_candidates(
        self, event: AstrMessageEvent, rule: str, days: int, size: int
    ) -> list[dict[str, Any]]:
        """按规则收集待清理文件：loose 只看根目录，其余规则连文件夹一起扫。"""
        root, root_ok = await self._fetch_root(event)
        if not root_ok:
            raise _FileReadError("读取根目录失败")
        files: list[dict[str, Any]] = list(root["files"])
        if rule != "loose":
            for folder in root["folders"]:
                folder_id = self._folder_id(folder)
                if not folder_id:
                    raise _FileReadError(
                        f"文件夹【{self._folder_name(folder)}】没有可用的 folder_id"
                    )
                data, data_ok = await self._fetch_folder_contents(event, folder_id)
                if not data_ok:
                    raise _FileReadError(
                        f"读取文件夹【{self._folder_name(folder)}】内容失败"
                    )
                for file in data["files"]:
                    item = dict(file)
                    item["_folder"] = self._folder_name(folder)
                    files.append(item)

        now = time.time()
        picked: list[dict[str, Any]] = []
        for file in files:
            if rule == "expired":
                dead = timestamp_seconds(
                    file.get("dead_time")
                    or file.get("deadTime")
                    or file.get("expireTime"),
                    None,
                )
                if dead is None:
                    continue
                if not dead or dead > now:
                    continue
            elif rule == "days":
                uploaded = timestamp_seconds(
                    file.get("upload_time")
                    or file.get("uploadTime")
                    or file.get("modify_time")
                    or file.get("modifyTime"),
                    None,
                )
                if uploaded is None:
                    continue
                if not uploaded or now - uploaded < days * 86400:
                    continue
            elif rule == "size":
                volume = parse_int(
                    file.get("file_size") or file.get("fileSize") or file.get("size"),
                    None,
                )
                if volume is None or volume < size:
                    continue
            if not self._file_id(file):
                location = self._file_name(file)
                if file.get("_folder"):
                    location = f"{file['_folder']}/{location}"
                raise _FileReadError(f"文件【{location}】缺少 file_id")
            picked.append(file)
        picked.sort(
            key=lambda item: timestamp_seconds(
                item.get("upload_time") or item.get("uploadTime"), 0
            )
            or 0
        )
        return picked

    @staticmethod
    def _tidy_line(index: int, file: dict[str, Any]) -> str:
        prefix = f"{file['_folder']}/" if file.get("_folder") else ""
        parts = [f"{index}. {prefix}{FilesFeature._file_name(file)}"]
        volume = parse_int(
            file.get("file_size") or file.get("fileSize") or file.get("size"), 0
        ) or 0
        if volume:
            parts.append(format_size(volume))
        uploaded = timestamp_seconds(
            file.get("upload_time") or file.get("uploadTime"), 0
        ) or 0
        if uploaded:
            parts.append(format_datetime(uploaded))
        return " · ".join(parts)

    async def tidy(self, event: AstrMessageEvent, raw: Any = None) -> str:
        """整理群文件：先预览，再发一次「整理群文件 确认」才真删。"""
        group_id = str(event.get_group_id())
        if not group_id:
            return "该指令只能在群里使用"
        operator_id = str(event.get_sender_id() or "")
        pending_key = (group_id, operator_id)
        tokens = str(raw or "").split()
        confirm = any(token in {"确认", "执行", "yes"} for token in tokens)
        tokens = [token for token in tokens if token not in {"确认", "执行", "yes"}]

        pending: tuple[float, str] | None = None
        if confirm:
            # 无论确认命令是否重复写了规则，都必须先存在同一操作者创建的
            # 预览。否则「整理群文件 过期 确认」会绕过二次确认直接删除。
            pending = self._pending_tidy.get(pending_key)
            if not pending or pending[0] < time.time():
                self._pending_tidy.pop(pending_key, None)
                return "没有属于你的待确认整理任务（或已超时），请先不带「确认」发一次预览"
            stored_tokens = pending[1].split()
            if tokens and tokens != stored_tokens:
                return "确认内容和上次预览不一致，请按上次预览的规则重新发送确认"
            tokens = stored_tokens

        rule, days, size = self._tidy_rule(tokens)
        label = self._rule_label(rule, days, size)
        try:
            candidates = await self._tidy_candidates(event, rule, days, size)
        except _FileReadError as exc:
            self._pending_tidy.pop(pending_key, None)
            return f"读取群文件失败，为安全起见未执行整理：{exc}"
        if not candidates:
            self._pending_tidy.pop(pending_key, None)
            return f"没有符合条件的文件（规则：{label}）"

        if not confirm:
            self._pending_tidy[pending_key] = (
                time.time() + TIDY_CONFIRM_TTL,
                " ".join(tokens),
            )
            head = [
                f"【整理群文件预览】规则：{label}",
                f"命中 {len(candidates)} 个文件"
                + (f"，本次最多删 {TIDY_LIMIT} 个" if len(candidates) > TIDY_LIMIT else ""),
                "",
            ]
            body = [self._tidy_line(i, file) for i, file in enumerate(candidates[:TIDY_LIMIT], 1)]
            tail = [
                "",
                f"确认删除请在 {TIDY_CONFIRM_TTL // 60} 分钟内发送「整理群文件 确认」",
                "群文件删除不可撤销，请先看清列表",
            ]
            return "\n".join(head + body + tail)

        self._pending_tidy.pop(pending_key, None)
        interval = self.config.safety.float("batch_interval", 0.4)
        done: list[str] = []
        failed: list[str] = []
        for file in candidates[:TIDY_LIMIT]:
            name = self._file_name(file)
            file_id = self._file_id(file)
            if not file_id:
                failed.append(name + "（缺少 file_id，未操作）")
                continue
            try:
                response = await event.bot.delete_group_file(
                    group_id=int(group_id), file_id=file_id
                )
                if reason := extract_failure(response):
                    raise RuntimeError(reason)
                done.append(name)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"{LOG_TAG} 整理群文件时删除失败 {name}: {exc}")
                failed.append(name)
            if interval > 0:
                await asyncio.sleep(interval)

        await self.log(
            event,
            "file_tidy",
            detail=f"规则={label} 成功={len(done)} 失败={len(failed)}",
            success=not failed,
        )
        summary = [f"整理完成（规则：{label}）", f"已删除 {len(done)} 个文件"]
        if failed:
            summary.append(f"{len(failed)} 个删除失败：" + "、".join(failed[:5]))
        remaining = len(candidates) - TIDY_LIMIT
        if remaining > 0:
            summary.append(f"还有 {remaining} 个符合条件，可再执行一次")
        return "\n".join(summary)

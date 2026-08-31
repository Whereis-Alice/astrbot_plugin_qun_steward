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
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.message.components import File, Image, Reply, Video

from ..core.config import LOG_TAG
from ..core.protocol import as_dict, call_action
from ..core.utils import download_file, format_datetime, format_size, sanitize_filename
from .base import Feature, FeatureContext

#: 群文件夹名长度上限（QQ 侧限制）
FOLDER_NAME_LIMIT = 30

#: 一次「整理群文件」最多删多少个，防手滑
TIDY_LIMIT = 50

#: 二次确认的有效期（秒）
TIDY_CONFIRM_TTL = 180

#: 群文件根目录在协议端的表示
ROOT_DIR = "/"

Entry = tuple[str, str]


class FilesFeature(Feature):
    """群文件相关操作。"""

    def __init__(self, ctx: FeatureContext) -> None:
        super().__init__(ctx)
        # group_id -> (确认截止时间, 规则原文)，用于「整理群文件」的二次确认
        self._pending_tidy: dict[str, tuple[float, str]] = {}

    # ------------------------------------------------------------ 基础查询 --- #
    async def _root(self, event: AstrMessageEvent) -> dict[str, Any]:
        """根目录列表，失败时返回空结构而不是抛异常。"""
        try:
            data = await event.bot.get_group_root_files(group_id=int(event.get_group_id()))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 获取群文件根目录失败：{exc}")
            return {"folders": [], "files": []}
        return self._normalize(data)

    async def _in_folder(self, event: AstrMessageEvent, folder_id: str) -> dict[str, Any]:
        try:
            data = await event.bot.get_group_files_by_folder(
                group_id=int(event.get_group_id()), folder_id=folder_id
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 获取群文件夹内容失败 folder={folder_id}: {exc}")
            return {"folders": [], "files": []}
        return self._normalize(data)

    @staticmethod
    def _normalize(data: Any) -> dict[str, Any]:
        """统一成 {folders: [], files: []}，兼容协议端少字段。"""
        if not isinstance(data, dict):
            return {"folders": [], "files": []}
        return {
            "folders": list(data.get("folders") or []),
            "files": list(data.get("files") or []),
        }

    def _listing(self, data: dict[str, Any], title: str) -> tuple[str, dict[int, Entry]]:
        """渲染目录文本，同时返回「序号 -> (类型, 名称)」映射。"""
        lines = [title] if title else []
        mapping: dict[int, Entry] = {}
        index = 1
        for folder in data["folders"]:
            name = str(folder.get("folder_name") or "未命名文件夹")
            count = folder.get("total_file_count")
            suffix = f"（{count} 个文件）" if count is not None else ""
            lines.append(f"▶{index}. {name}{suffix}")
            mapping[index] = ("folder", name)
            index += 1
        for file in data["files"]:
            name = str(file.get("file_name") or "未命名文件")
            lines.append(f"📄{index}. {name}")
            mapping[index] = ("file", name)
            index += 1
        if len(lines) <= (1 if title else 0):
            lines.append("（空目录）")
        return "\n".join(lines), mapping

    async def _find_folder(self, event: AstrMessageEvent, name: str) -> dict[str, Any] | None:
        root = await self._root(event)
        return next(
            (f for f in root["folders"] if str(f.get("folder_name")) == name),
            None,
        )

    async def _find_file(
        self, event: AstrMessageEvent, folder_name: str, file_name: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """在指定文件夹里找文件，返回 (文件夹, 文件)。"""
        folder = await self._find_folder(event, folder_name)
        if not folder:
            return None, None
        data = await self._in_folder(event, str(folder.get("folder_id")))
        file = next(
            (f for f in data["files"] if str(f.get("file_name")) == file_name),
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
                    data = await self._in_folder(event, str(folder.get("folder_id")))
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
        lines = [f"【📄 {file.get('file_name', '未知')}】"]
        lines.append(f"文件大小：{format_size(file.get('size'))}")
        lines.append(
            f"上传者：{file.get('uploader_name', '未知')}({file.get('uploader', '未知')})"
        )
        lines.append(f"下载次数：{file.get('download_times', '未知')}")
        if upload_time := file.get("upload_time"):
            lines.append(f"上传时间：{format_datetime(upload_time)}")
        dead_time = file.get("dead_time") or 0
        lines.append(
            "过期时间：" + ("永久有效" if int(dead_time) == 0 else format_datetime(dead_time))
        )
        if modify_time := file.get("modify_time"):
            lines.append(f"修改时间：{format_datetime(modify_time)}")
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
        url = getattr(segment, "url", None) or getattr(segment, "file", None)
        if not url:
            return None
        target = self.config.file_dir / sanitize_filename(file_name, "upload.bin")
        logger.info(f"{LOG_TAG} 正在下载待上传文件：{url}")
        try:
            await download_file(str(url), target)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"{LOG_TAG} 下载待上传文件失败：{exc}")
            return None
        return target if target.exists() else None

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
        folder_name, file_name = await self._parse_path(event, str(path or ""))
        if not file_name:
            return "路径里没有文件名，写法：上传群文件 [文件夹名/]文件名.后缀"

        local_path = await self._download_quoted(event, file_name)
        if not local_path:
            return "请引用一条包含文件的消息，再发送该指令"

        folder_id = None
        if folder_name:
            folder = await self._ensure_folder(event, folder_name)
            if not folder:
                return f"无法创建或找到群文件夹【{folder_name}】"
            folder_id = folder.get("folder_id")

        try:
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
        folder_name, file_name = await self._parse_path(event, str(path or ""))
        if not folder_name and not file_name:
            return "请指定要删除的文件夹或文件，可先用「查看群文件」看序号"
        group_id = int(event.get_group_id())

        if file_name:
            if folder_name:
                folder, file = await self._find_file(event, folder_name, file_name)
                if not folder or not file:
                    return f"未找到 {folder_name}/{file_name}"
            else:
                root = await self._root(event)
                file = next(
                    (f for f in root["files"] if str(f.get("file_name")) == file_name),
                    None,
                )
                if not file:
                    return f"未找到群文件：📄{file_name}"
            try:
                await event.bot.delete_group_file(
                    group_id=group_id, file_id=str(file.get("file_id"))
                )
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

        folder = await self._find_folder(event, str(folder_name))
        if not folder:
            return f"群文件夹【{folder_name}】不存在"
        try:
            await event.bot.delete_group_folder(
                group_id=group_id, folder_id=str(folder.get("folder_id"))
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"{LOG_TAG} 删除群文件夹失败：{exc}")
            await self.log(
                event,
                "file_delete",
                target_id=str(folder_name),
                detail=str(exc),
                success=False,
            )
            return f"删除失败：{exc}"
        await self.log(event, "file_delete", target_id=str(folder_name), detail="文件夹")
        return f"已删除群文件夹：▶{folder_name}"

    # ------------------------------------------------------------ 浏览 --- #
    async def view(self, event: AstrMessageEvent, path: Any = None) -> str:
        text = str(path or "").strip()
        if not text:
            root = await self._root(event)
            listing, _ = self._listing(root, "【群文件根目录】")
            return listing + "\n\n用「查看群文件 序号」进入文件夹或查看文件详情"

        folder_name, file_name = await self._parse_path(event, text)

        if folder_name and file_name:
            _, file = await self._find_file(event, folder_name, file_name)
            if not file:
                return f"未找到群文件：📄{file_name}"
            return self._file_detail(file)

        if folder_name:
            folder = await self._find_folder(event, folder_name)
            if folder:
                data = await self._in_folder(event, str(folder.get("folder_id")))
                listing, _ = self._listing(data, f"【{folder_name}】")
                return listing
            # 名字对不上文件夹，再当根目录文件试一次
            root = await self._root(event)
            file = next(
                (f for f in root["files"] if str(f.get("file_name")) == folder_name),
                None,
            )
            if file:
                return self._file_detail(file)
            return f"未找到【{folder_name}】"

        if file_name:
            root = await self._root(event)
            file = next(
                (f for f in root["files"] if str(f.get("file_name")) == file_name),
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
            parent = str(folder.get("folder_id") or ROOT_DIR) if folder else ROOT_DIR
            return file, parent, f"{folder_name}/{file_name}"
        root = await self._root(event)
        file = next((f for f in root["files"] if str(f.get("file_name")) == file_name), None)
        return file, ROOT_DIR, file_name

    # ------------------------------------------------------------ 直链 --- #
    async def link(self, event: AstrMessageEvent, path: Any = None) -> str:
        """取群文件的下载直链，方便转存到别处。"""
        text = str(path or "").strip()
        if not text:
            return "用法：群文件直链 <[文件夹名/]文件名>，也可以用「查看群文件」里的序号"
        file, _, display = await self._locate_file(event, text)
        if not file:
            return f"未找到群文件：{display or text}"

        result = await call_action(
            event,
            ("get_group_file_url",),
            group_id=int(event.get_group_id()),
            file_id=str(file.get("file_id")),
            busid=file.get("busid"),
        )
        if not result.ok:
            return f"取直链失败：{result.error}"
        url = str(as_dict(result.data).get("url") or "")
        if not url:
            return "协议端没有返回下载地址"
        return f"📄{display}\n{url}\n（直链有有效期，过期重新获取即可）"

    # ------------------------------------------------------------ 移动 --- #
    async def move(self, event: AstrMessageEvent, raw: Any = None) -> str:
        """移动群文件 <源路径> <目标文件夹|根目录>。"""
        parts = str(raw or "").split()
        if len(parts) < 2:
            return "用法：移动群文件 <[文件夹名/]文件名> <目标文件夹名>，目标写「根目录」则移到最外层"
        target_name = parts[-1]
        source = " ".join(parts[:-1])

        file, parent, display = await self._locate_file(event, source)
        if not file:
            return f"未找到群文件：{display or source}"

        if target_name in {"根目录", "/", "根"}:
            target_id = ROOT_DIR
            target_label = "根目录"
        else:
            folder = await self._find_folder(event, target_name)
            if not folder:
                return f"目标文件夹【{target_name}】不存在，可先用「查看群文件」确认名字"
            target_id = str(folder.get("folder_id"))
            target_label = target_name
        if target_id == parent:
            return f"它已经在【{target_label}】里了"

        result = await call_action(
            event,
            ("move_group_file",),
            group_id=int(event.get_group_id()),
            file_id=str(file.get("file_id")),
            parent_directory=parent,
            target_directory=target_id,
        )
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
        parts = str(raw or "").split()
        if len(parts) < 2:
            return "用法：重命名群文件 <[文件夹名/]文件名> <新文件名>"
        new_name = parts[-1]
        source = " ".join(parts[:-1])
        if new_name != sanitize_filename(new_name):
            return "新文件名里不能有 / \\ : * ? \" < > | 这些字符"

        file, parent, display = await self._locate_file(event, source)
        if not file:
            return f"未找到群文件：{display or source}"
        if str(file.get("file_name")) == new_name:
            return "新名字和原来一样"

        result = await call_action(
            event,
            ("rename_group_file",),
            group_id=int(event.get_group_id()),
            file_id=str(file.get("file_id")),
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
    async def usage(self, event: AstrMessageEvent) -> str:
        """群文件容量统计。"""
        result = await call_action(
            event, ("get_group_file_system_info",), group_id=int(event.get_group_id())
        )
        if not result.ok:
            return f"读取群文件容量失败：{result.error}"
        info = as_dict(result.data)
        used = int(info.get("used_space") or 0)
        total = int(info.get("total_space") or 0)
        count = int(info.get("file_count") or 0)
        limit = int(info.get("limit_count") or 0)

        lines = ["【群文件容量】"]
        if total > 0:
            lines.append(f"已用 {format_size(used)} / {format_size(total)}（{used * 100 // total}%）")
        else:
            lines.append(f"已用 {format_size(used)}")
        lines.append(f"文件数 {count}" + (f" / {limit}" if limit else ""))
        if total > 0 and used * 10 >= total * 9:
            lines.append("容量快满了，可以用「整理群文件」清一批旧文件")
        return "\n".join(lines)

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
        root = await self._root(event)
        files: list[dict[str, Any]] = list(root["files"])
        if rule != "loose":
            for folder in root["folders"]:
                data = await self._in_folder(event, str(folder.get("folder_id")))
                for file in data["files"]:
                    item = dict(file)
                    item["_folder"] = str(folder.get("folder_name") or "")
                    files.append(item)

        now = time.time()
        picked: list[dict[str, Any]] = []
        for file in files:
            if rule == "expired":
                dead = int(file.get("dead_time") or 0)
                if not dead or dead > now:
                    continue
            elif rule == "days":
                uploaded = int(file.get("upload_time") or file.get("modify_time") or 0)
                if not uploaded or now - uploaded < days * 86400:
                    continue
            elif rule == "size":
                if int(file.get("file_size") or file.get("size") or 0) < size:
                    continue
            picked.append(file)
        picked.sort(key=lambda item: int(item.get("upload_time") or 0))
        return picked

    @staticmethod
    def _tidy_line(index: int, file: dict[str, Any]) -> str:
        prefix = f"{file['_folder']}/" if file.get("_folder") else ""
        parts = [f"{index}. {prefix}{file.get('file_name') or '未命名'}"]
        volume = int(file.get("file_size") or file.get("size") or 0)
        if volume:
            parts.append(format_size(volume))
        uploaded = int(file.get("upload_time") or 0)
        if uploaded:
            parts.append(format_datetime(uploaded))
        return " · ".join(parts)

    async def tidy(self, event: AstrMessageEvent, raw: Any = None) -> str:
        """整理群文件：先预览，再发一次「整理群文件 确认」才真删。"""
        group_id = str(event.get_group_id())
        if not group_id:
            return "该指令只能在群里使用"
        tokens = str(raw or "").split()
        confirm = any(token in {"确认", "执行", "yes"} for token in tokens)
        tokens = [token for token in tokens if token not in {"确认", "执行", "yes"}]

        if confirm and not tokens:
            pending = self._pending_tidy.get(group_id)
            if not pending or pending[0] < time.time():
                return "没有待确认的整理任务（或已超时），请先不带「确认」发一次预览"
            tokens = pending[1].split()

        rule, days, size = self._tidy_rule(tokens)
        label = self._rule_label(rule, days, size)
        candidates = await self._tidy_candidates(event, rule, days, size)
        if not candidates:
            self._pending_tidy.pop(group_id, None)
            return f"没有符合条件的文件（规则：{label}）"

        if not confirm:
            self._pending_tidy[group_id] = (
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

        self._pending_tidy.pop(group_id, None)
        interval = self.config.safety.float("batch_interval", 0.4)
        done: list[str] = []
        failed: list[str] = []
        for file in candidates[:TIDY_LIMIT]:
            name = str(file.get("file_name") or "未命名")
            try:
                await event.bot.delete_group_file(
                    group_id=int(group_id), file_id=str(file.get("file_id"))
                )
                done.append(name)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"{LOG_TAG} 整理群文件时删除失败 {name}: {exc}")
                failed.append(name)
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

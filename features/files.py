"""群文件上传 / 删除 / 浏览。

路径写法沿用上游：
- 「文件夹名」
- 「文件.zip」
- 「文件夹名/文件.zip」
- 「序号」或「文件夹序号/文件序号」（序号来自「查看群文件」的列表）

相比上游补齐了协议端返回缺字段时的兜底，以及每一步的失败提示。
"""

from __future__ import annotations

import contextlib
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.message.components import File, Image, Reply, Video

from ..core.config import LOG_TAG
from ..core.utils import download_file, format_datetime, format_size, sanitize_filename
from .base import Feature

#: 群文件夹名长度上限（QQ 侧限制）
FOLDER_NAME_LIMIT = 30

Entry = tuple[str, str]


class FilesFeature(Feature):
    """群文件相关操作。"""

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
                event, "upload_group_file", detail=str(exc), success=False, target_id=file_name
            )
            return f"上传失败：{exc}"
        finally:
            # 缓存文件不留在磁盘上
            with contextlib.suppress(OSError):
                local_path.unlink(missing_ok=True)

        await self.log(event, "upload_group_file", target_id=file_name, detail=str(path or ""))
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
                    "delete_group_file",
                    target_id=file_name,
                    detail=str(exc),
                    success=False,
                )
                return f"删除失败：{exc}"
            await self.log(event, "delete_group_file", target_id=file_name)
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
                "delete_group_file",
                target_id=str(folder_name),
                detail=str(exc),
                success=False,
            )
            return f"删除失败：{exc}"
        await self.log(event, "delete_group_file", target_id=str(folder_name), detail="文件夹")
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

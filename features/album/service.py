"""群相册上传与随机图功能。

命令入口是「上传群相册 [相册名] [数量]」：
- 引用一条文本消息 -> 渲染成「我朋友说」图片
- 引用/附带图片 -> 直接使用原图
- 末尾带数量 -> 把被引用消息往上的 N 条文本拼成长图

上传成功后可选把图片留档到本地，供「随机图关键词」二次使用。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ...core.config import LOG_TAG
from ...core.protocol import (
    album_name_of,
    backend_label,
    create_album,
    detect_backend,
    find_album,
    list_albums,
    upload_album_image,
)
from ...core.utils import (
    extract_image_url,
    format_datetime,
    get_nickname,
    get_reply_message_id,
    get_reply_text,
    get_replyer_id,
    load_bytes,
    parse_int,
    sanitize_filename,
)
from ..base import Feature, FeatureContext
from .cloud import AlbumCloud, PickedImage
from .draw import MemeRenderer, detect_image_ext
from .fonts import FontResolver

#: 支持随机发送的图片扩展名
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

#: 拉取历史消息时的最大扫描条数，防止在大群里翻上万条
MAX_SCAN = 200

#: 单次拼接的消息条数硬上限（配置值也会被压到这个范围内）
STITCH_HARD_LIMIT = 50

#: 头像并发下载上限
AVATAR_CONCURRENCY = 5

#: 「查看群相册」单页最多列出多少项
BROWSE_PAGE = 30

#: 相册项数可能藏在这些字段里，用来在相册列表上顺手标出张数
_COUNT_KEYS = ("total", "count", "media_count", "mediaCount", "photo_count", "totalCount")


class AlbumFeature(Feature):
    """群相册相关能力。"""

    def __init__(self, ctx: FeatureContext) -> None:
        super().__init__(ctx)
        self.fonts = FontResolver(self.config)
        self.renderer = MemeRenderer(self.fonts, self.config.resource_dir)
        self.cloud = AlbumCloud(self.config)
        # group_id -> {"album_name": str, "album_id": str}，命中后省一次列表查询
        self._default_cache: dict[str, dict[str, str]] = {}
        # group_id -> {关键词: album_id}
        self._keywords: dict[str, dict[str, str]] = {}
        self._uploading: set[str] = set()
        # group_id -> 最近一次「查看群相册」看的 album_id，方便「删相册图 3」省掉相册名
        self._last_browsed: dict[str, str] = {}

    # ------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        """插件启动时预热字体与随机图关键词。"""
        try:
            await self.fonts.ensure_downloaded()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 字体预热失败: {exc}")
        self.refresh_keywords()

    @property
    def backup_root(self) -> Path:
        return self.config.album_dir / "backup"

    @property
    def backup_enabled(self) -> bool:
        return self.config.album.bool("backup_media", False)

    @property
    def show_title(self) -> bool:
        return self.config.album.bool("show_title", True)

    def max_stitch_count(self) -> int:
        value = self.config.album.int("max_stitch_count", 20)
        return max(2, min(value, STITCH_HARD_LIMIT))

    def random_groups(self) -> list[str]:
        return [str(item).strip() for item in self.config.album.list("random_album_groups") if item]

    # ------------------------------------------------------------ 关键词表

    def refresh_keywords(self) -> None:
        """扫描留档目录，重建「相册名 -> album_id」关键词表。"""
        keywords: dict[str, dict[str, str]] = {}
        root = self.backup_root
        if not root.is_dir():
            self._keywords = keywords
            return
        for group_dir in root.iterdir():
            if not group_dir.is_dir():
                continue
            meta = self._read_album_meta(group_dir.name)
            table: dict[str, str] = {}
            for album_id, info in meta.items():
                name = str((info or {}).get("name") or "").strip()
                if not name:
                    continue
                table[sanitize_filename(name)] = album_id
            if table:
                keywords[group_dir.name] = table
        self._keywords = keywords
        logger.debug(f"{LOG_TAG} 随机图关键词已刷新，覆盖 {len(keywords)} 个群")

    def keyword_snapshot(self) -> dict[str, list[str]]:
        """WebUI 展示用：每个群可用的随机图关键词。"""
        return {gid: sorted(table) for gid, table in self._keywords.items()}

    def _meta_path(self, group_id: str) -> Path:
        return self.backup_root / str(group_id) / "_albums.json"

    def _read_album_meta(self, group_id: str) -> dict[str, dict[str, Any]]:
        path = self._meta_path(group_id)
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text("utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 读取相册留档索引失败 {path}: {exc}")
            return {}
        return data if isinstance(data, dict) else {}

    def _write_album_meta(self, group_id: str, album_id: str, album_name: str) -> None:
        """记录 album_id -> 名称；检测到改名时留一条日志。"""
        meta = self._read_album_meta(group_id)
        previous = str((meta.get(album_id) or {}).get("name") or "")
        if previous and previous != album_name:
            logger.info(f"{LOG_TAG} 相册改名 group={group_id} {previous} -> {album_name}")
        meta[album_id] = {"name": album_name, "updated_at": int(time.time())}
        path = self._meta_path(group_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
        except OSError as exc:
            logger.warning(f"{LOG_TAG} 写入相册留档索引失败 {path}: {exc}")

    # ------------------------------------------------------------ 参数解析

    @staticmethod
    def parse_args(message: str) -> tuple[str, int | None]:
        """解析「上传群相册 [相册名] [数量]」，返回 (相册名, 拼接条数)。"""
        parts = (message or "").strip().split()
        if len(parts) <= 1:
            return "", None
        if len(parts) >= 3 and parts[-1].isdigit():
            return " ".join(parts[1:-1]), int(parts[-1])
        # 「上传群相册 5」这种只带数字的写法，视为不指定相册、拼接 5 条
        if len(parts) == 2 and parts[1].isdigit():
            return "", int(parts[1])
        return " ".join(parts[1:]), None

    def _configured_album(self, group_id: str) -> str:
        """从配置里取该群的默认相册名。"""
        for entry in self.config.album.list("default_albums"):
            if not isinstance(entry, dict):
                continue
            if str(entry.get("group_id") or "").strip() == group_id:
                return str(entry.get("album_name") or "").strip()
        return ""

    # ------------------------------------------------------------ 等级门槛

    async def _check_level(self, event: AstrMessageEvent) -> tuple[bool, int]:
        """相册功能的群等级门槛；群主/管理员直接放行。"""
        threshold = self.config.album.int("level_threshold", 0)
        if threshold <= 0:
            return True, 0
        try:
            info = await event.bot.get_group_member_info(
                group_id=int(event.get_group_id()),
                user_id=int(event.get_sender_id()),
                no_cache=True,
            )
            level = int(info.get("level") or 0)
            if str(info.get("role") or "") in ("owner", "admin"):
                return True, level
            return level >= threshold, level
        except Exception as exc:  # noqa: BLE001
            # 拿不到等级时放行，避免协议端异常直接锁死功能；但要留痕便于排查
            logger.warning(f"{LOG_TAG} 获取群等级失败，本次放行门槛检查: {exc}")
            return True, 0

    # ------------------------------------------------------------ 图片生成

    async def _member_info(self, event: AstrMessageEvent, user_id: int) -> dict[str, Any]:
        try:
            info = await event.bot.get_group_member_info(
                group_id=int(event.get_group_id()), user_id=user_id, no_cache=True
            )
            return {
                "role": str(info.get("role") or "member"),
                "level": int(info.get("level") or 0),
                "title": str(info.get("title") or ""),
                "nickname": str(info.get("card") or info.get("nickname") or user_id),
            }
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"{LOG_TAG} 获取群成员资料失败 user={user_id}: {exc}")
            return {"role": "member", "level": 0, "title": "", "nickname": str(user_id)}

    async def _avatar(self, user_id: str) -> bytes | None:
        uid = str(user_id)
        if not uid.isdigit():
            return None
        return await load_bytes(f"https://q4.qlogo.cn/headimg_dl?dst_uin={uid}&spec=640")

    async def _render_one(self, event: AstrMessageEvent, user_id: str, text: str) -> bytes | None:
        avatar = await self._avatar(user_id)
        info = await self._member_info(event, int(user_id)) if user_id.isdigit() else {}
        try:
            return await asyncio.to_thread(
                self.renderer.render,
                name=str(info.get("nickname") or user_id),
                avatar_bytes=avatar,
                text=text,
                role=str(info.get("role") or "member"),
                title=str(info.get("title") or ""),
                level=int(info.get("level") or 0),
                show_title=self.show_title,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"{LOG_TAG} 渲染图片失败 user={user_id}: {exc}", exc_info=True)
            return None

    async def _render_reply(self, event: AstrMessageEvent) -> bytes | None:
        """把被引用的一条文本渲染成单图。"""
        text = (get_reply_text(event) or "").strip()
        replyer = get_replyer_id(event)
        if not text or not replyer:
            return None
        return await self._render_one(event, replyer, text)

    async def _render_stitched(self, event: AstrMessageEvent, count: int) -> bytes | None:
        """把被引用消息及其上方共 count 条文本拼成长图。"""
        messages = await self.collect_history(event, count)
        if not messages:
            return None
        images: list[bytes] = []
        for item in messages:
            rendered = await self._render_one(event, str(item["user_id"]), str(item["text"]))
            if rendered:
                images.append(rendered)
        if not images:
            return None
        return await asyncio.to_thread(self.renderer.stitch, images)

    async def collect_history(self, event: AstrMessageEvent, count: int) -> list[dict[str, Any]]:
        """取被引用消息及其之上的若干条纯文本消息（正序返回）。"""
        target_id = get_reply_message_id(event)
        if not target_id:
            return []
        group_id = int(event.get_group_id())
        wanted = max(1, min(count, self.max_stitch_count()))

        try:
            payload = await event.bot.get_group_msg_history(
                group_id=group_id, message_seq=0, count=MAX_SCAN, reverseOrder=False
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 获取群历史消息失败 group={group_id}: {exc}")
            return []

        raw = payload.get("messages") if isinstance(payload, dict) else payload
        if not isinstance(raw, list) or not raw:
            return []

        index = next(
            (i for i, msg in enumerate(raw) if str(msg.get("message_id")) == str(target_id)),
            -1,
        )
        if index < 0:
            logger.warning(
                f"{LOG_TAG} 最近 {MAX_SCAN} 条消息里没找到被引用消息，"
                "请引用较新的消息，或减小拼接数量"
            )
            return []

        collected: list[dict[str, Any]] = []
        for i in range(index, -1, -1):
            message = raw[i]
            sender = str(message.get("user_id") or (message.get("sender") or {}).get("user_id") or "")
            text = await self._plain_text(event, message.get("message"))
            if sender and text.strip():
                collected.append({"user_id": sender, "text": text})
            if len(collected) >= wanted:
                break
        collected.reverse()
        return collected

    async def _plain_text(self, event: AstrMessageEvent, raw: Any) -> str:
        """把 OneBot 消息段数组拍平成纯文本。"""
        if isinstance(raw, str):
            return raw
        if not isinstance(raw, list):
            return ""
        chunks: list[str] = []
        for segment in raw:
            if not isinstance(segment, dict):
                continue
            seg_type = segment.get("type")
            data = segment.get("data") or {}
            if seg_type == "text":
                chunks.append(str(data.get("text") or ""))
            elif seg_type == "at":
                qq = str(data.get("qq") or "")
                name = str(data.get("name") or "")
                if not name and qq.isdigit():
                    name = await get_nickname(event, qq)
                if name or qq:
                    chunks.append(f"@{name or qq} ")
            elif seg_type == "file":
                chunks.append(f"[文件: {data.get('file') or '未知文件'}]")
        return "".join(chunks)

    # ------------------------------------------------------------ 上传主流程

    async def upload(self, event: AstrMessageEvent) -> str:
        """处理「上传群相册」指令。

        返回给用户的提示文本；返回空串表示无需回复（上传成功时 QQ 会自带卡片提示）。
        """
        group_id = str(event.get_group_id())
        if not group_id:
            return "群相册只能在群里使用。"
        if group_id in self._uploading:
            return "上一张还在上传，稍等一下再试。"

        allowed, level = await self._check_level(event)
        if not allowed:
            threshold = self.config.album.int("level_threshold", 0)
            return f"你的群等级（{level}）不足，需要达到 {threshold} 级才能使用该指令。"

        album_name, count = self.parse_args(event.message_str)
        self._uploading.add(group_id)
        try:
            return await self._upload_flow(event, group_id, album_name, count)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"{LOG_TAG} 相册上传失败 group={group_id}: {exc}", exc_info=True)
            await self.log(event, "album_upload", detail=str(exc), success=False)
            return f"上传失败：{exc}"
        finally:
            self._uploading.discard(group_id)

    async def _upload_flow(
        self, event: AstrMessageEvent, group_id: str, album_name: str, count: int | None
    ) -> str:
        resolved_from_config = False
        if not album_name:
            album_name = self._configured_album(group_id)
            resolved_from_config = bool(album_name)

        album_id = ""
        used_cache = False
        created = False
        if album_name:
            cached = self._default_cache.get(group_id)
            if cached and cached.get("album_name") == album_name:
                album_id = cached.get("album_id", "")
                used_cache = bool(album_id)
            if not album_id:
                album = await find_album(event, group_id, album_name)
                if album is None and resolved_from_config:
                    # 相册名是管理员在配置里写死的，协议端支持时直接建一个，
                    # 免得每次换季新建相册都要手动去 QQ 里点一遍
                    album = await create_album(event, group_id, album_name)
                    created = album is not None
                if album is None:
                    return f"相册【{album_name}】不存在，请先在群相册里创建，或检查名字是否写错。"
                album_id = str(album.get("album_id") or "")
                album_name = album_name_of(album) or album_name
                if resolved_from_config:
                    self._default_cache[group_id] = {
                        "album_name": album_name,
                        "album_id": album_id,
                    }
        else:
            albums = await list_albums(event, group_id)
            if not albums:
                return "没有找到任何群相册，请先在群里创建一个相册。"
            album_id = str(albums[0].get("album_id") or "")
            album_name = album_name_of(albums[0])

        if not album_id:
            return "相册 ID 解析失败，可能是协议端返回格式不兼容，请查看日志。"

        image = await self._build_image(event, count)
        if image is None:
            return (
                "没找到可上传的内容。用法：引用一张图片或一段文字后发送「上传群相册」，"
                "也可以写成「上传群相册 相册名 5」把连续 5 条消息拼成长图。"
            )

        ext = detect_image_ext(image)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if self.backup_enabled:
            save_path = self.backup_root / group_id / album_id / f"{stamp}.{ext}"
            keep_local = True
        else:
            save_path = self.config.album_dir / f"{group_id}_{stamp}.{ext}"
            keep_local = False
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_bytes(image)

        try:
            backend = await self._upload_with_retry(
                event, group_id, album_id, album_name, save_path, used_cache
            )
        finally:
            if not keep_local:
                save_path.unlink(missing_ok=True)

        if keep_local:
            self._write_album_meta(group_id, album_id, album_name)
            self.refresh_keywords()
        # 云端缓存里少了刚上传的这张，随机图/列图要能立刻看到
        self.cloud.invalidate(group_id, album_id)

        await self.log(
            event,
            "album_upload",
            detail=f"相册={album_name} 协议端={backend_label(backend)}",
        )
        # 上传成功后 QQ 客户端自己会弹相册卡片，插件不再重复播报，返回空串即静默。
        # 唯一例外是「相册是插件顺手新建的」，这件事客户端不会提示，值得说一句。
        if created:
            return f"已新建相册【{album_name}】并上传成功。"
        return ""

    async def _build_image(self, event: AstrMessageEvent, count: int | None) -> bytes | None:
        """按参数决定图片来源：拼接长图 / 原图 / 单条文字渲染。"""
        if count:
            return await self._render_stitched(event, count)
        url = extract_image_url(event)
        if url:
            image = await load_bytes(url)
            if image:
                return image
        return await self._render_reply(event)

    async def _upload_with_retry(
        self,
        event: AstrMessageEvent,
        group_id: str,
        album_id: str,
        album_name: str,
        save_path: Path,
        used_cache: bool,
    ) -> str:
        """上传失败且用了缓存 album_id 时，清缓存重查一次再传。"""
        backend = await detect_backend(event.bot)
        try:
            return await upload_album_image(
                event, group_id, album_id, album_name, save_path, backend
            )
        except Exception:
            if not used_cache:
                raise
            logger.info(f"{LOG_TAG} 缓存的相册 ID 可能失效，重新查询后重试 group={group_id}")
            self._default_cache.pop(group_id, None)
            album = await find_album(event, group_id, album_name)
            if album is None:
                raise
            return await upload_album_image(
                event,
                group_id,
                str(album.get("album_id") or ""),
                album_name_of(album) or album_name,
                save_path,
                backend,
            )

    # ------------------------------------------------------------ 相册定位

    def match_keyword(self, group_id: str, word: str) -> str | None:
        """判断一句话的首个词是否命中该群的本地留档关键词。"""
        table = self._keywords.get(str(group_id))
        if not table:
            return None
        return table.get(sanitize_filename(word))

    async def resolve_album(
        self, event: AstrMessageEvent, group_id: str, name: str
    ) -> tuple[str, str]:
        """把相册名解析成 (album_id, 相册名)，解析不出来返回 ("", "")。

        先查本地留档的关键词表（零网络开销），再问协议端，这样即使从没开过留档
        也能按名字找到相册。
        """
        word = (name or "").strip()
        if not word:
            return "", ""
        local = self.match_keyword(group_id, word)
        if local:
            return local, self._album_display(group_id, local) or word
        if not self.cloud.enabled:
            return "", ""
        album_id = await self.cloud.album_id_of(event, group_id, word)
        if not album_id:
            return "", ""
        display = await self.cloud.album_name_of_id(event, group_id, album_id)
        return album_id, display or word

    def _album_display(self, group_id: str, album_id: str) -> str:
        info = self._read_album_meta(group_id).get(str(album_id)) or {}
        return str(info.get("name") or "")

    # ------------------------------------------------------------ 随机发图

    def random_image(self, group_id: str, album_id: str) -> Path | None:
        """从留档目录里随机挑一张图。"""
        directory = self.backup_root / str(group_id) / str(album_id)
        if not directory.is_dir():
            return None
        candidates = [
            item
            for item in directory.iterdir()
            if item.is_file() and item.suffix.lower() in IMAGE_EXTS
        ]
        if not candidates:
            return None
        return random.choice(candidates)

    async def pick_image(
        self, event: AstrMessageEvent, group_id: str, album_id: str
    ) -> PickedImage | None:
        """随机取一张图：本地留档优先（快且省流量），没有就走云端相册。"""
        local = self.random_image(group_id, album_id)
        if local is not None:
            return PickedImage(path=local)
        if not self.cloud.enabled:
            return None
        url = await self.cloud.random_url(event, group_id, album_id)
        return PickedImage(url=url) if url else None

    async def random_keyword(self, event: AstrMessageEvent) -> PickedImage | None:
        """被动监听入口：命中相册名则返回一张随机图。"""
        group_id = str(event.get_group_id())
        if not group_id or group_id not in self.random_groups():
            return None
        if not getattr(event, "is_at_or_wake_command", False):
            return None
        parts = (event.message_str or "").strip().split()
        if not parts:
            return None
        album_id, _ = await self.resolve_album(event, group_id, parts[0])
        if not album_id:
            return None
        return await self.pick_image(event, group_id, album_id)

    async def random_command(
        self, event: AstrMessageEvent, name: str
    ) -> tuple[PickedImage | None, str]:
        """「随机图 [相册名]」指令：返回 (图片, 提示文本)，提示文本非空即失败原因。"""
        group_id = str(event.get_group_id())
        if not group_id:
            return None, "该指令只能在群里使用。"
        word = (name or "").strip()
        if not word:
            return None, "用法：随机图 <相册名>，相册名就是群相册里的名字。"
        album_id, display = await self.resolve_album(event, group_id, word)
        if not album_id:
            return None, f"没找到相册【{word}】，用「查看群相册」看看有哪些相册。"
        picked = await self.pick_image(event, group_id, album_id)
        if picked is None:
            return None, f"相册【{display or word}】里还没有可发的图片。"
        return picked, ""

    # ------------------------------------------------------------ 云端浏览

    @staticmethod
    def _album_count(album: dict[str, Any]) -> str:
        for key in _COUNT_KEYS:
            value = album.get(key)
            if isinstance(value, int) and value >= 0:
                return f" · {value} 项"
            if isinstance(value, str) and value.isdigit():
                return f" · {int(value)} 项"
        return ""

    async def browse(self, event: AstrMessageEvent, name: str) -> str:
        """「查看群相册 [相册名]」：不带名字列相册，带名字列这本相册里的图。"""
        group_id = str(event.get_group_id())
        if not group_id:
            return "该指令只能在群里使用。"
        word = (name or "").strip()
        if not word:
            return await self._browse_albums(event, group_id)
        return await self._browse_medias(event, group_id, word)

    async def _browse_albums(self, event: AstrMessageEvent, group_id: str) -> str:
        albums = await self.cloud.albums(event, group_id, refresh=True)
        if not albums:
            return (
                "没读到群相册列表。可能是协议端不支持相册接口，"
                f"当前协议端：{backend_label(await detect_backend(event.bot))}。"
            )
        lines = [f"群相册（共 {len(albums)} 本）"]
        for index, album in enumerate(albums, 1):
            lines.append(
                f"{index}. {album_name_of(album) or '未命名'}{self._album_count(album)}"
            )
        lines.append("")
        lines.append("看图片：查看群相册 <相册名>")
        return "\n".join(lines)

    async def _browse_medias(self, event: AstrMessageEvent, group_id: str, word: str) -> str:
        album_id, display = await self.resolve_album(event, group_id, word)
        if not album_id:
            return f"没找到相册【{word}】，先用「查看群相册」看看有哪些相册。"
        medias = await self.cloud.medias(event, group_id, album_id, refresh=True)
        if not medias:
            return f"相册【{display or word}】是空的，或者协议端不支持读取相册内容。"
        self._last_browsed[group_id] = album_id

        shown = medias[:BROWSE_PAGE]
        lines = [f"相册【{display or word}】共 {len(medias)} 项"]
        if len(medias) > len(shown):
            lines[0] += f"（下面只列前 {len(shown)} 项）"
        for index, item in enumerate(shown, 1):
            tag = "视频" if item.get("is_video") else "图片"
            stamp = format_datetime(item.get("upload_time")) if item.get("upload_time") else ""
            title = str(item.get("name") or "").strip() or tag
            lines.append(f"{index}. {title}" + (f" · {stamp}" if stamp else ""))
        lines.append("")
        lines.append(f"删图：删相册图 {display or word} <序号>")
        return "\n".join(lines)

    async def remove_media(self, event: AstrMessageEvent, raw: str) -> tuple[str, str]:
        """「删相册图 [相册名] <序号>」：返回 (回复文本, 审计详情)。"""
        group_id = str(event.get_group_id())
        if not group_id:
            return "该指令只能在群里使用。", ""
        parts = (raw or "").split()
        if not parts:
            return "用法：删相册图 [相册名] <序号>，序号看「查看群相册 <相册名>」。", ""

        index = parse_int(parts[-1])
        if index is None or index <= 0:
            return "最后一个参数要是图片序号，比如「删相册图 金句 3」。", ""
        name = " ".join(parts[:-1]).strip()

        if name:
            album_id, display = await self.resolve_album(event, group_id, name)
            if not album_id:
                return f"没找到相册【{name}】。", ""
        else:
            album_id = self._last_browsed.get(group_id, "")
            if not album_id:
                return "先用「查看群相册 <相册名>」列一次图片，再按序号删。", ""
            display = await self.cloud.album_name_of_id(event, group_id, album_id)

        medias = await self.cloud.medias(event, group_id, album_id)
        if index > len(medias):
            return f"相册【{display}】只有 {len(medias)} 项，没有第 {index} 项。", ""
        target = medias[index - 1]
        media_id = str(target.get("media_id") or "")
        if not media_id:
            return "这一项没有可用的删除标识，协议端返回的字段不兼容。", ""

        error = await self.cloud.delete(event, group_id, album_id, media_id)
        title = str(target.get("name") or "").strip() or f"第 {index} 项"
        if error:
            return f"删除失败：{error}", f"相册={display} 项={title} 失败={error}"
        return f"已从相册【{display}】删除 {title}。", f"相册={display} 项={title}"

    # ------------------------------------------------------------ WebUI 支撑

    async def album_options(self, group_id: str) -> list[dict[str, str]]:
        """给 WebUI 提供某个群的相册候选（需要协议端在线）。"""
        client = await self.groups.client_for(str(group_id))
        if client is None:
            return []

        class _Shim:
            """list_albums 只用到 bot / get_group_id，这里做个最小壳。"""

            def __init__(self, bot: Any, gid: str) -> None:
                self.bot = bot
                self._gid = gid

            def get_group_id(self) -> str:
                return self._gid

        albums = await list_albums(_Shim(client, str(group_id)), group_id)  # type: ignore[arg-type]
        return [{"album_id": item["album_id"], "name": item["name"]} for item in albums]

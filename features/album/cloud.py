"""群相册的云端读写：列相册、列图、删图、随机取一张。

本地留档（backup_media）默认是关的，随机图和删图都得靠协议端接口。云端接口
一次往返不便宜，被动关键词又是每条消息都要判一次，所以这里统一做短时缓存：
相册名表和图片列表各缓存 CACHE_TTL 秒，上传/删除后主动失效。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ...core.config import LOG_TAG, StewardConfig
from ...core.protocol import (
    album_name_of,
    del_album_media,
    list_album_media,
    list_albums,
)

#: 相册名表 / 图片列表的缓存存活时间（秒）
CACHE_TTL = 300

#: 单本相册最多拉取多少项
MEDIA_LIMIT = 200


@dataclass(slots=True)
class PickedImage:
    """一张待发送的图片：来自本地留档（path）或云端直链（url），二者只有一个有值。"""

    path: Path | None = None
    url: str = ""

    @property
    def source(self) -> str:
        return str(self.path) if self.path else self.url


class AlbumCloud:
    """协议端相册的只读浏览 + 删图，带短时缓存。"""

    def __init__(self, config: StewardConfig) -> None:
        self.config = config
        # group_id -> (过期时间, 相册列表)
        self._albums: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        # (group_id, album_id) -> (过期时间, 媒体列表)
        self._medias: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}

    @property
    def enabled(self) -> bool:
        """云端兜底总开关：关掉后只用本地留档。"""
        return self.config.album.bool("cloud_random", True)

    # ------------------------------------------------------------ 缓存维护

    def invalidate(self, group_id: Any, album_id: Any = None) -> None:
        """上传/删除后让缓存失效，下一次查询重新拉取。"""
        gid = str(group_id)
        self._albums.pop(gid, None)
        if album_id is None:
            for key in [k for k in self._medias if k[0] == gid]:
                self._medias.pop(key, None)
        else:
            self._medias.pop((gid, str(album_id)), None)

    def clear(self) -> None:
        self._albums.clear()
        self._medias.clear()

    # ------------------------------------------------------------ 相册列表

    async def albums(
        self, event: AstrMessageEvent, group_id: Any, *, refresh: bool = False
    ) -> list[dict[str, Any]]:
        """拉取相册列表（带缓存）。"""
        gid = str(group_id)
        cached = self._albums.get(gid)
        if cached and not refresh and cached[0] > time.time():
            return cached[1]
        albums = await list_albums(event, group_id)
        if albums:
            self._albums[gid] = (time.time() + CACHE_TTL, albums)
        return albums

    async def album_id_of(self, event: AstrMessageEvent, group_id: Any, name: str) -> str:
        """按相册名找 album_id，先精确再包含匹配，找不到返回空串。"""
        target = (name or "").strip()
        if not target:
            return ""
        albums = await self.albums(event, group_id)
        for album in albums:
            if album_name_of(album) == target:
                return str(album.get("album_id") or "")
        for album in albums:
            if target in album_name_of(album):
                return str(album.get("album_id") or "")
        return ""

    async def album_name_of_id(
        self, event: AstrMessageEvent, group_id: Any, album_id: Any
    ) -> str:
        aid = str(album_id)
        for album in await self.albums(event, group_id):
            if str(album.get("album_id") or "") == aid:
                return album_name_of(album)
        return ""

    # ------------------------------------------------------------ 图片列表

    async def medias(
        self,
        event: AstrMessageEvent,
        group_id: Any,
        album_id: Any,
        *,
        refresh: bool = False,
    ) -> list[dict[str, Any]]:
        """拉取一本相册里的媒体（带缓存）。"""
        key = (str(group_id), str(album_id))
        cached = self._medias.get(key)
        if cached and not refresh and cached[0] > time.time():
            return cached[1]
        medias = await list_album_media(event, group_id, album_id, limit=MEDIA_LIMIT)
        if medias:
            self._medias[key] = (time.time() + CACHE_TTL, medias)
        return medias

    async def random_url(
        self, event: AstrMessageEvent, group_id: Any, album_id: Any
    ) -> str:
        """云端随机取一张图的直链；取不到返回空串（视频会被跳过）。"""
        medias = await self.medias(event, group_id, album_id)
        candidates = [
            item for item in medias if item.get("url") and not item.get("is_video")
        ]
        if not candidates:
            return ""
        return str(random.choice(candidates)["url"])

    async def delete(
        self, event: AstrMessageEvent, group_id: Any, album_id: Any, media_id: str
    ) -> str:
        """删除一项，返回空串表示成功。"""
        error = await del_album_media(event, group_id, album_id, media_id)
        if not error:
            self.invalidate(group_id, album_id)
            logger.debug(f"{LOG_TAG} 相册缓存已失效 group={group_id} album={album_id}")
        return error

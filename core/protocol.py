"""协议端（OneBot 实现）探测与差异适配。

QQ 群相册相关接口在 NapCat / LLOneBot / SnowLuma 上的动作名和参数各不相同，
这里集中处理，业务层只调用统一入口。
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .config import LOG_TAG

#: 已知协议端标识
NAPCAT = "napcat"
LLBOT = "llbot"
SNOWLUMA = "snowluma"

#: app_name 关键字 -> 内部标识
_APP_NAME_MAP: dict[str, str] = {
    "llonebot": LLBOT,
    "snowluma": SNOWLUMA,
    "napcat": NAPCAT,
}

_BACKEND_LABELS: dict[str, str] = {
    NAPCAT: "NapCat",
    LLBOT: "LLOneBot / llbot",
    SNOWLUMA: "SnowLuma",
}

# 按 client 实例缓存探测结果，避免每次上传都打一次 get_version_info
_backend_cache: dict[int, str] = {}


def backend_label(backend: str) -> str:
    return _BACKEND_LABELS.get(backend, backend)


def clear_backend_cache() -> None:
    _backend_cache.clear()


async def detect_backend(client: Any) -> str:
    """探测协议端类型，探测失败时按 NapCat 处理（兼容性最好的一档）。"""
    if client is None:
        return NAPCAT
    key = id(client)
    cached = _backend_cache.get(key)
    if cached:
        return cached

    app_name = ""
    try:
        payload = await client.api.call_action("get_version_info")
        if isinstance(payload, dict):
            data = payload.get("data")
            source = data if isinstance(data, dict) else payload
            app_name = str(source.get("app_name") or source.get("appname") or "")
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"{LOG_TAG} get_version_info 失败，按 NapCat 处理: {exc}")

    lowered = app_name.lower()
    backend = NAPCAT
    for keyword, name in _APP_NAME_MAP.items():
        if keyword in lowered:
            backend = name
            break
    _backend_cache[key] = backend
    logger.info(f"{LOG_TAG} 协议端识别为 {backend_label(backend)}（app_name={app_name or '未知'}）")
    return backend


def normalize_album_list(payload: Any) -> list[dict[str, Any]]:
    """把各协议端的相册列表响应统一成 [{album_id, name, ...}] 结构。"""
    raw: Any = payload
    if isinstance(raw, dict):
        for key in ("data", "album_list", "list", "albums"):
            value = raw.get(key)
            if isinstance(value, list):
                raw = value
                break
            if isinstance(value, dict):
                nested = value.get("album_list") or value.get("list") or value.get("albums")
                if isinstance(nested, list):
                    raw = nested
                    break
    if not isinstance(raw, list):
        return []

    albums: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        album = dict(item)
        if "album_id" not in album:
            for key in ("id", "albumId", "album_no"):
                if key in album:
                    album["album_id"] = album[key]
                    break
        album["album_id"] = str(album.get("album_id") or "")
        album["name"] = str(album.get("name") or album.get("album_name") or "")
        albums.append(album)
    return albums


def album_name_of(album: dict[str, Any]) -> str:
    return str(album.get("name") or album.get("album_name") or "")


async def list_albums(event: AstrMessageEvent, group_id: Any) -> list[dict[str, Any]]:
    """拉取群相册列表。"""
    client = event.bot
    backend = await detect_backend(client)
    gid = int(group_id)
    try:
        if backend in (LLBOT, SNOWLUMA):
            payload = await client.api.call_action("get_group_album_list", group_id=gid)
        else:
            payload = await client.get_qun_album_list(group_id=gid)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"{LOG_TAG} 获取群相册列表失败 group={gid} backend={backend}: {exc}")
        return []
    return normalize_album_list(payload)


async def find_album(
    event: AstrMessageEvent, group_id: Any, album_name: str
) -> dict[str, Any] | None:
    """按名称精确匹配相册，找不到时退化为包含匹配。"""
    albums = await list_albums(event, group_id)
    target = album_name.strip()
    for album in albums:
        if album_name_of(album) == target:
            return album
    for album in albums:
        if target and target in album_name_of(album):
            return album
    return None


def _file_candidates(image_path: Path, backend: str) -> list[Any]:
    """生成 file 参数的候选形式，按各协议端最可能成功的顺序排列。"""
    raw_path = str(image_path.resolve())
    file_uri = image_path.resolve().as_uri()
    try:
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    except OSError as exc:
        raise RuntimeError(f"读取图片失败：{exc}") from exc
    b64 = f"base64://{encoded}"

    if backend == LLBOT:
        return [[raw_path], [file_uri], [b64]]
    return [raw_path, b64, file_uri]


async def upload_album_image(
    event: AstrMessageEvent,
    group_id: Any,
    album_id: str,
    album_name: str,
    image_path: Path,
    backend: str | None = None,
) -> str:
    """把本地图片上传到群相册，返回实际生效的协议端标识。

    不同协议端对 file 参数的接受形式差异很大（本地路径 / file:// / base64），
    逐个候选尝试，全部失败时抛出最后一个错误。
    """
    client = event.bot
    resolved = backend or await detect_backend(client)
    gid = int(group_id)
    candidates = _file_candidates(image_path, resolved)
    last_error: Exception | None = None

    for candidate in candidates:
        try:
            if resolved == LLBOT:
                await client.api.call_action(
                    "upload_group_album",
                    group_id=gid,
                    album_id=str(album_id),
                    files=candidate,
                )
            elif resolved == SNOWLUMA:
                await client.api.call_action(
                    "upload_image_to_qun_album",
                    group_id=gid,
                    album_id=str(album_id),
                    album_name=album_name,
                    file=candidate,
                )
            else:
                await client.upload_image_to_qun_album(
                    group_id=gid,
                    album_id=str(album_id),
                    album_name=album_name,
                    file=candidate,
                )
            return resolved
        except Exception as exc:  # noqa: BLE001 - 换一种 file 形式重试
            last_error = exc
            logger.debug(f"{LOG_TAG} 相册上传候选失败 backend={resolved}: {exc}")

    if last_error is not None:
        raise last_error
    raise RuntimeError("没有可用的图片参数形式，上传未执行")

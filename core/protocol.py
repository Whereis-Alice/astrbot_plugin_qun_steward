"""协议端（OneBot 实现）探测与差异适配。

QQ 群相册相关接口在 NapCat / LLOneBot(llbot) / SnowLuma 上的动作名、参数名甚至
"成功"的判定方式都不一样，这里集中处理，业务层只调用统一入口。

三端差异（依据各自官方 API 文档 / 动作清单）：

============ ============================= ==================================
协议端        列出相册                       上传图片
============ ============================= ==================================
NapCat       get_qun_album_list            upload_image_to_qun_album(file)
llbot        get_group_album_list          upload_group_album(files=[...])
SnowLuma     get_qun_album_list            upload_image_to_qun_album(file)
============ ============================= ==================================

其中 llbot 的 upload_group_album 即使全部失败也会返回 status=ok / retcode=0，
真正的结果在 data.fail_count / data.fail_indexes 里，必须单独判断，否则会把
失败当成功。
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

#: 列出相册的动作名，按优先级排列（前一个失败或没数据就试下一个）
_ALBUM_LIST_ACTIONS: dict[str, tuple[str, ...]] = {
    NAPCAT: ("get_qun_album_list",),
    LLBOT: ("get_group_album_list", "get_qun_album_list"),
    SNOWLUMA: ("get_qun_album_list", "get_group_album_list"),
}
_DEFAULT_LIST_ACTIONS: tuple[str, ...] = ("get_qun_album_list", "get_group_album_list")

#: 支持"新建相册"的协议端及其动作名
_ALBUM_CREATE_ACTIONS: dict[str, str] = {
    LLBOT: "create_group_album",
}

# 按 client 实例缓存探测结果，避免每次上传都打一次 get_version_info
_backend_cache: dict[int, str] = {}


def backend_label(backend: str) -> str:
    return _BACKEND_LABELS.get(backend, backend)


def clear_backend_cache() -> None:
    _backend_cache.clear()


def supports_album_create(backend: str) -> bool:
    """该协议端是否提供新建相册的接口。"""
    return backend in _ALBUM_CREATE_ACTIONS


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


def extract_failure(payload: Any) -> str:
    """从协议端响应里提取失败原因，返回空串表示成功。

    覆盖两种失败形态：
    - 常规 OneBot 失败：status=failed 或 retcode != 0
    - llbot 式"假成功"：status=ok 但 data.fail_count / fail_indexes 非空
    """
    if not isinstance(payload, dict):
        return ""

    status = str(payload.get("status") or "").lower()
    retcode = payload.get("retcode")
    if status in {"failed", "error"} or (isinstance(retcode, int) and retcode != 0):
        message = payload.get("message") or payload.get("wording") or payload.get("msg")
        if message:
            return str(message)
        return f"协议端返回 status={status or '未知'} retcode={retcode}"

    data = payload.get("data")
    if not isinstance(data, dict):
        return ""
    fail_count = data.get("fail_count")
    if isinstance(fail_count, int) and fail_count > 0:
        return f"协议端上传失败 fail_count={fail_count}"
    fail_indexes = data.get("fail_indexes")
    if isinstance(fail_indexes, list) and fail_indexes:
        return f"协议端上传失败 fail_indexes={fail_indexes}"
    success_count = data.get("success_count")
    if isinstance(success_count, int) and success_count <= 0 and fail_count is not None:
        return "协议端上传失败 success_count=0"
    return ""


def normalize_album_list(payload: Any) -> list[dict[str, Any]]:
    """把各协议端的相册列表响应统一成 [{album_id, name, ...}] 结构。

    llbot 返回蛇形的 album_id / name，SnowLuma 返回驼峰的 id / name，
    这里把常见别名一并抹平。
    """
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
    """拉取群相册列表，按协议端优先级依次尝试可用动作名。"""
    client = event.bot
    backend = await detect_backend(client)
    gid = int(group_id)
    actions = _ALBUM_LIST_ACTIONS.get(backend, _DEFAULT_LIST_ACTIONS)

    for action in actions:
        try:
            payload = await client.api.call_action(action, group_id=gid)
        except Exception as exc:  # noqa: BLE001 - 动作不存在时换下一个
            logger.debug(f"{LOG_TAG} {action} 调用失败 group={gid} backend={backend}: {exc}")
            continue
        reason = extract_failure(payload)
        if reason:
            logger.debug(f"{LOG_TAG} {action} 返回失败 group={gid}: {reason}")
            continue
        albums = normalize_album_list(payload)
        if albums:
            return albums

    logger.warning(f"{LOG_TAG} 未获取到群相册列表 group={gid} backend={backend_label(backend)}")
    return []


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


async def create_album(
    event: AstrMessageEvent, group_id: Any, album_name: str, desc: str = ""
) -> dict[str, Any] | None:
    """新建群相册；协议端不支持或失败时返回 None。

    各端创建接口的返回体差异很大，创建后统一回查一次列表来拿 album_id。
    """
    client = event.bot
    backend = await detect_backend(client)
    action = _ALBUM_CREATE_ACTIONS.get(backend)
    if not action:
        return None

    gid = int(group_id)
    try:
        payload = await client.api.call_action(action, group_id=gid, name=album_name, desc=desc)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"{LOG_TAG} 新建群相册失败 group={gid} name={album_name}: {exc}")
        return None
    reason = extract_failure(payload)
    if reason:
        logger.warning(f"{LOG_TAG} 新建群相册被拒绝 group={gid} name={album_name}: {reason}")
        return None

    logger.info(f"{LOG_TAG} 已新建群相册 group={gid} name={album_name}")
    return await find_album(event, group_id, album_name)


def _file_candidates(image_path: Path, backend: str) -> list[str]:
    """生成 file 参数的候选形式，按各协议端最可能成功的顺序排列。"""
    resolved = image_path.resolve()
    raw_path = str(resolved)
    file_uri = resolved.as_uri()
    try:
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    except OSError as exc:
        raise RuntimeError(f"读取图片失败：{exc}") from exc
    b64 = f"base64://{encoded}"

    if backend == LLBOT:
        # llbot 文档示例用的是 file:///D:/temp/1.png，裸路径经常被拒
        return [file_uri, b64, raw_path]
    return [raw_path, b64, file_uri]


def _candidate_label(candidate: str) -> str:
    """给日志用的简短描述，避免把整段 base64 打进日志。"""
    if candidate.startswith("base64://"):
        return "base64"
    if candidate.startswith("file://"):
        return "file uri"
    return "本地路径"


def _upload_request(
    backend: str, group_id: int, album_id: str, album_name: str, candidate: str
) -> tuple[str, dict[str, Any]]:
    """按协议端拼出上传动作名与参数。"""
    if backend == LLBOT:
        # llbot 收 files 数组，且没有 album_name 参数
        return "upload_group_album", {
            "group_id": group_id,
            "album_id": album_id,
            "files": [candidate],
        }
    return "upload_image_to_qun_album", {
        "group_id": group_id,
        "album_id": album_id,
        "album_name": album_name,
        "file": candidate,
    }


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
    last_error: Exception | None = None

    for candidate in _file_candidates(image_path, resolved):
        action, params = _upload_request(resolved, gid, str(album_id), album_name, candidate)
        try:
            payload = await client.api.call_action(action, **params)
        except Exception as exc:  # noqa: BLE001 - 换一种 file 形式重试
            last_error = exc
            logger.debug(
                f"{LOG_TAG} 相册上传候选异常 backend={resolved} "
                f"形式={_candidate_label(candidate)}: {exc}"
            )
            continue
        reason = extract_failure(payload)
        if not reason:
            return resolved
        last_error = RuntimeError(reason)
        logger.debug(
            f"{LOG_TAG} 相册上传候选被拒 backend={resolved} "
            f"形式={_candidate_label(candidate)}: {reason}"
        )

    if last_error is not None:
        raise last_error
    raise RuntimeError("没有可用的图片参数形式，上传未执行")

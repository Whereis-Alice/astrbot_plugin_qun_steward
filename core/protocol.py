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
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
    # Some builds / integrations expose the shortened product name directly.
    "llbot": LLBOT,
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
# SnowLuma's NapCat-compatible album endpoint is cursor-paginated.  Keep a
# generous but finite guard so a malformed cursor cannot create an endless loop.
_ALBUM_PAGE_LIMIT = 20

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
    _unsupported.clear()


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

    def as_int(value: Any) -> int | None:
        """把协议端常见的数字字符串安全地转成整数。"""
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().lstrip("+-").isdigit():
            try:
                return int(value.strip())
            except ValueError:
                return None
        return None

    def message_of(*objects: Any) -> str:
        keys = (
            "message",
            "wording",
            "msg",
            "ret_msg",
            "retMsg",
            "error",
            "error_message",
            "errorMessage",
        )
        for obj in objects:
            if not isinstance(obj, dict):
                continue
            for key in keys:
                value = obj.get(key)
                if value not in (None, ""):
                    return str(value)
        return ""

    data = payload.get("data")
    data_dict = data if isinstance(data, dict) else {}
    status = str(payload.get("status") or data_dict.get("status") or "").lower()
    retcode = payload.get("retcode")
    if retcode is None:
        retcode = data_dict.get("retcode")
    retcode_int = as_int(retcode)
    if status in {"failed", "failure", "error", "err"} or (
        retcode_int is not None and retcode_int != 0
    ):
        message = message_of(payload, data_dict)
        if message:
            return message
        return f"协议端返回 status={status or '未知'} retcode={retcode}"

    # llbot's upload endpoint can return status=ok / retcode=0 while reporting
    # per-file failures inside data.  These values are strings in some builds.
    fail_count = data_dict.get("fail_count")
    fail_count_int = as_int(fail_count)
    if fail_count_int is not None and fail_count_int > 0:
        return f"协议端上传失败 fail_count={fail_count_int}"

    fail_indexes = data_dict.get("fail_indexes")
    if isinstance(fail_indexes, (list, tuple, set)) and fail_indexes:
        return f"协议端上传失败 fail_indexes={list(fail_indexes)}"
    if isinstance(fail_indexes, str) and fail_indexes.strip() not in {"", "[]"}:
        return f"协议端上传失败 fail_indexes={fail_indexes.strip()}"

    success_count = data_dict.get("success_count")
    success_count_int = as_int(success_count)
    if (
        success_count_int is not None
        and success_count_int <= 0
        and fail_count is not None
    ):
        return "协议端上传失败 success_count=0"
    return ""


# --------------------------------------------------------------- 通用动作调用层

#: 用于给用户做友好提示的“不支持”关键词。
#:
#: 这组词不能直接拿来做能力缓存：严格校验的协议端也可能用
#: ``unsupported parameter`` 表示“动作存在，但这一组参数不对”。
_UNSUPPORTED_MARKERS: tuple[str, ...] = (
    "unsupported",
    "not supported",
    "unknown action",
    "no such action",
    "unimplemented",
    "not implemented",
    "retcode=1404",
    "retcode=404",
    "不支持",
    "未实现",
    "未支持",
    "没有此接口",
)

#: 明确表示动作/方法不存在的关键词。只有命中这组词，才会写入动作能力缓存。
_ACTION_MISSING_MARKERS: tuple[str, ...] = (
    "unknown action",
    "unknown method",
    "no such action",
    "action not found",
    "method not found",
    "unimplemented",
    "not implemented",
    "retcode=1404",
    "retcode=404",
    "接口不存在",
    "动作不存在",
    "没有此接口",
    "没有此动作",
    "未知动作",
    "不支持动作",
    "未实现动作",
)

#: 出现这些词时，``unsupported`` 更可能是在说参数校验失败，而不是动作缺失。
_PARAMETER_ERROR_MARKERS: tuple[str, ...] = (
    "parameter",
    "param",
    "argument",
    "keyword",
    "field",
    "schema",
    "validation",
    "required",
    "invalid",
    "missing",
    "参数",
    "字段",
    "校验",
    "验证",
    "必填",
    "缺少",
    "格式",
)


@dataclass(slots=True)
class ActionResult:
    """一次跨端动作调用的结果。

    action 是实际生效的动作名（便于日志排障），data 已剥掉 OneBot 响应的外层
    包装，error 非空即失败。
    """

    action: str = ""
    data: Any = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.action)


def explain_action_error(result: ActionResult, capability: str) -> str:
    """把动作调用失败转成适合直接展示给用户的文案。

    不同 OneBot 实现对「没有这个动作」的返回并不统一，有的返回英文异常，
    有的返回中文错误。功能模块不应该把这些实现细节原样塞进群聊，因此统一
    将已识别的“不支持”错误收敛成稳定的中文提示；权限不足等真实业务错误仍
    保留协议端给出的原因，方便排查。
    """
    reason = str(result.error or "").strip()
    # 只有能确认“动作不存在”时才把错误收敛成“不支持”。例如
    # ``unsupported parameter`` 代表动作存在但参数不兼容，直接展示原因更有
    # 助于排查，也不会掩盖调用方的参数问题。
    if not reason or _is_action_unavailable(reason):
        return f"当前协议端不支持{capability}"
    return reason


#: (协议端, 动作名) -> True 表示该端不支持，后续直接跳过，省一次往返
_unsupported: dict[tuple[str, str], bool] = {}


def clear_action_cache() -> None:
    _unsupported.clear()


def _is_unsupported(reason: str) -> bool:
    lowered = reason.lower()
    return any(marker in lowered for marker in _UNSUPPORTED_MARKERS)


def _is_action_unavailable(reason: Any) -> bool:
    """判断失败是否足以证明“动作不存在”。

    协议端错误文案并不统一，不能要求每个实现都返回同一错误码；但也不能把
    ``unsupported parameter`` 这类参数错误缓存成“动作不存在”，否则同一动作的
    下一组兼容参数永远不会有机会执行。
    """
    lowered = str(reason or "").strip().lower()
    if not lowered:
        return False
    if any(marker in lowered for marker in _ACTION_MISSING_MARKERS):
        return True
    if any(marker in lowered for marker in _PARAMETER_ERROR_MARKERS):
        return False

    # 有些实现会把 action / method / api 一起写出来；只在“不支持”明确
    # 修饰动作本身时缓存。单独的 ``unsupported`` / ``不支持`` 太含糊，不能
    # 让一次业务失败污染后续所有参数变体。
    action_words = r"(?:action|method|api|endpoint|operation|接口|动作|方法)"
    unsupported_words = r"(?:unsupported|not supported|unavailable|不支持|不可用)"
    return bool(
        re.search(rf"{unsupported_words}\s+{action_words}\b", lowered)
        or re.search(rf"{action_words}\s+{unsupported_words}\b", lowered)
    )


def is_unsupported_error(reason: Any) -> bool:
    """公开判断动作是否因为「接口不存在」而失败。

    功能模块在批量接口不可用时可以安全回退到逐项接口；权限不足、参数错误
    等真实业务失败不应被误判成“不支持”。
    """
    return _is_action_unavailable(reason)


def unwrap(payload: Any) -> Any:
    """剥掉 OneBot 响应外层的 data 包装；已经是裸数据时原样返回。"""
    if isinstance(payload, dict) and "data" in payload and ({"status", "retcode"} & set(payload)):
        return payload.get("data")
    return payload


def as_dict(payload: Any) -> dict[str, Any]:
    value = unwrap(payload)
    return value if isinstance(value, dict) else {}


def as_list(payload: Any) -> list[Any]:
    """把响应取成列表，兼容 {list:[...]} / {items:[...]} 这类包装。"""
    value = unwrap(payload)
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("list", "items", "data", "result", "msgs", "messages"):
            nested = value.get(key)
            if isinstance(nested, list):
                return nested
    return []


async def call_action(
    event: AstrMessageEvent, candidates: Sequence[str], **params: Any
) -> ActionResult:
    """依次尝试候选动作名，返回第一个成功的结果。"""
    return await call_action_variants(
        event,
        tuple((action, params) for action in candidates),
    )


async def call_action_variants(
    event: AstrMessageEvent,
    variants: Sequence[tuple[str, Mapping[str, Any]]],
) -> ActionResult:
    """按「动作名 + 参数」候选组合依次调用，返回第一个成功的结果。

    同一能力在不同 OneBot 实现里有时不只是动作名不同，参数名也会不同（例如
    删除群公告的 ``fid`` / ``notice_id``）。这时调用方可以给出多个完整组合，
    而不用把一个错误的参数集合发送给严格校验的协议端。值为 ``None`` 的参数
    会被丢弃；动作“不支持”的结论仍按协议端缓存，参数错误则不会误缓存成不支持。
    """
    client = getattr(event, "bot", None)
    if client is None:
        return ActionResult(error="当前平台不支持该操作")

    backend = await detect_backend(client)
    last_error = ""

    for action, params in variants:
        if _unsupported.get((backend, action)):
            continue
        payload_params = {key: value for key, value in params.items() if value is not None}
        try:
            payload = await client.api.call_action(action, **payload_params)
        except Exception as exc:  # noqa: BLE001 - 换下一个候选动作
            reason = str(exc) or exc.__class__.__name__
        else:
            reason = extract_failure(payload)
            if not reason:
                return ActionResult(action=action, data=unwrap(payload))
        # 只有明确的“动作不存在”才缓存。参数不兼容必须让后续 variant 继续尝试。
        if _is_action_unavailable(reason):
            _unsupported[(backend, action)] = True
        last_error = reason
        logger.debug(
            f"{LOG_TAG} 动作 {action} 不可用 backend={backend} "
            f"params={tuple(payload_params)}: {reason}"
        )

    return ActionResult(error=last_error or "协议端不支持该操作")


def normalize_album_list(payload: Any) -> list[dict[str, Any]]:
    """把各协议端的相册列表响应统一成 [{album_id, name, ...}] 结构。

    llbot 返回蛇形的 album_id / name，SnowLuma 返回驼峰的 id / name，
    这里把常见别名一并抹平。
    """
    raw, _, _ = _album_container(payload)
    if not isinstance(raw, list):
        return []

    albums: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        album = dict(item)
        if not str(album.get("album_id") or "").strip():
            for key in ("id", "albumId", "album_no"):
                if album.get(key) not in (None, ""):
                    album["album_id"] = album[key]
                    break
        album["album_id"] = str(album.get("album_id") or "")
        album["name"] = str(
            album.get("name")
            or album.get("album_name")
            or album.get("albumName")
            or ""
        )
        albums.append(album)
    return albums


_ALBUM_HAS_MORE_KEYS: tuple[str, ...] = (
    "has_more",
    "hasMore",
    "next_has_more",
    "nextHasMore",
)


def _album_container(payload: Any) -> tuple[list[Any], str, bool | None]:
    """找到相册数组及其分页元数据，兼容多层 data/result 包装。"""
    value = unwrap(payload)

    def visit(
        current: Any,
        inherited_attach: str = "",
        inherited_has_more: bool | None = None,
        depth: int = 0,
    ) -> tuple[list[Any], str, bool | None]:
        if depth > 5:
            return [], inherited_attach, inherited_has_more
        if isinstance(current, list):
            return current, inherited_attach, inherited_has_more
        if not isinstance(current, dict):
            return [], inherited_attach, inherited_has_more

        attach = str(
            current.get("attach_info")
            or current.get("attachInfo")
            or current.get("next_attach_info")
            or current.get("nextAttachInfo")
            or inherited_attach
            or ""
        )
        has_more = inherited_has_more
        for key in _ALBUM_HAS_MORE_KEYS:
            if key in current:
                has_more = _bool_value(current.get(key))
                break

        for key in ("album_list", "albumList", "albums", "list"):
            nested = current.get(key)
            if isinstance(nested, list):
                return nested, attach, has_more

        for key in ("data", "result", "payload", "response"):
            nested = current.get(key)
            if isinstance(nested, (dict, list)):
                found, nested_attach, nested_more = visit(
                    nested, attach, has_more, depth + 1
                )
                if found:
                    return found, nested_attach or attach, (
                        nested_more if nested_more is not None else has_more
                    )
        return [], attach, has_more

    return visit(value)


def _normalize_album_list_page(payload: Any) -> tuple[list[dict[str, Any]], str, bool | None]:
    """归一化一页相册列表，并保留下一页游标与 has_more。"""
    raw, attach, has_more = _album_container(payload)
    if not isinstance(raw, list):
        return [], attach, has_more
    return normalize_album_list(raw), attach, has_more


def album_name_of(album: dict[str, Any]) -> str:
    return str(album.get("name") or album.get("album_name") or "")


async def list_albums(event: AstrMessageEvent, group_id: Any) -> list[dict[str, Any]]:
    """拉取群相册列表，按协议端优先级依次尝试可用动作名并自动翻页。"""
    client = event.bot
    backend = await detect_backend(client)
    gid = int(group_id)
    actions = _ALBUM_LIST_ACTIONS.get(backend, _DEFAULT_LIST_ACTIONS)

    for action in actions:
        albums: list[dict[str, Any]] = []
        seen: set[str] = set()
        attach = ""
        succeeded = False
        for _ in range(_ALBUM_PAGE_LIMIT):
            page_params: dict[str, Any] = {"group_id": gid}
            # LLOneBot 的 get_group_album_list 当前只声明 group_id；相册「媒体
            # 列表」虽然支持 attach_info，但相册目录本身没有这个分页参数。
            # 因此这里只取协议端返回的相册目录，不向 llbot 发送游标字段。
            if attach and backend != LLBOT:
                page_params["attach_info"] = attach
            result = await call_action(
                event,
                (action,),
                **page_params,
            )
            if not result.ok:
                logger.debug(
                    f"{LOG_TAG} {action} 调用失败 group={gid} backend={backend}: "
                    f"{result.error}"
                )
                break
            succeeded = True
            page, next_attach, has_more = _normalize_album_list_page(result.data)
            for album in page:
                album_id = str(album.get("album_id") or "")
                key = album_id or (
                    f"{album_name_of(album)}\x00"
                    f"{album.get('create_time') or album.get('createTime') or ''}"
                )
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                albums.append(album)

            if has_more is False:
                break
            if backend == LLBOT:
                break
            if not next_attach or next_attach == attach:
                break
            attach = next_attach

        if albums:
            return albums
        # Keep the old alias fallback behavior: an implemented endpoint can still
        # return an empty/unsupported view while the sibling action has data.
        if succeeded:
            logger.debug(f"{LOG_TAG} {action} 返回空相册列表 group={gid}")

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


# ------------------------------------------------------------------ 相册云端读写

#: 列出相册内图片/视频的动作名候选
_ALBUM_MEDIA_LIST_ACTIONS: tuple[str, ...] = (
    "get_group_album_media_list",
    "get_qun_album_media_list",
)

#: 删除相册内图片/视频的动作名候选
_ALBUM_MEDIA_DEL_ACTIONS: tuple[str, ...] = (
    "del_group_album_media",
    "del_qun_album_media",
    "delete_group_album_media",
)

#: 媒体项里可能承载"唯一标识"的字段名（删除接口要用它，QQ 侧叫 lloc）
_MEDIA_ID_KEYS: tuple[str, ...] = (
    "lloc",
    "media_id",
    "mediaId",
    "id",
    "pic_id",
    "picId",
    "photo_id",
    "sloc",
)

#: 媒体项里可能承载图片地址的字段名，靠前的优先（优先原图）
_MEDIA_URL_KEYS: tuple[str, ...] = (
    "origin_url",
    "originUrl",
    "raw_url",
    "rawUrl",
    "big_url",
    "bigUrl",
    "download_url",
    "downloadUrl",
    "url",
    "pic_url",
    "picUrl",
    "thumb_url",
    "cover",
)

# NapCat 的相册媒体接口会把「是否还有下一页」单独放在响应里；不能只看
# attach_info 是否为空，因为最后一页有时仍会带一个游标。
_MEDIA_HAS_MORE_KEYS: tuple[str, ...] = (
    "next_has_more",
    "nextHasMore",
    "has_more",
    "hasMore",
)

#: 分页拉取相册时的单页上限与最多翻页数，防止大相册把内存和耗时拖爆
_MEDIA_PAGE_LIMIT = 10


def _pick_str(item: dict[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return ""


def _is_http_url(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower().startswith(("http://", "https://"))


def _media_url_variant_score(value: Any) -> tuple[int, int, int]:
    """给相册多规格 URL 排序：原图标记 > spec > 像素面积。"""
    if not isinstance(value, dict):
        return (0, 0, 0)

    raw = any(
        _bool_value(value.get(key)) is True
        for key in ("raw", "is_raw", "origin", "original", "full")
    )
    try:
        spec = int(value.get("spec") or 0)
    except (TypeError, ValueError):
        spec = 0
    try:
        width = int(value.get("width") or 0)
        height = int(value.get("height") or 0)
    except (TypeError, ValueError):
        width = height = 0
    return (100000 if raw else 0, spec, max(0, width * height))


def _url_from_value(value: Any, *, prefer_raw: bool = False, depth: int = 0) -> str:
    """从字符串、字典或数组里递归找一个可下载的 URL。

    QQ 新版接口的 ``photo_url`` 不是固定的字符串：它可能是字符串数组、
    ``[{"url": ...}]``，也可能和 ``default_url`` 并存。这里不把某一种
    响应形状写死，并把带 origin/raw/full 标记的地址放在最前面。
    """
    if depth > 5:
        return ""
    if _is_http_url(value):
        return value.strip()
    if isinstance(value, list):
        candidates = list(value)
        if prefer_raw:
            candidates.sort(
                key=lambda item: _media_url_variant_score(item),
                reverse=True,
            )
        for item in candidates:
            if found := _url_from_value(item, prefer_raw=prefer_raw, depth=depth + 1):
                return found
        return ""
    if not isinstance(value, dict):
        return ""

    raw_keys = (
        "origin_url",
        "originUrl",
        "raw_url",
        "rawUrl",
        "original_url",
        "originalUrl",
        "full_url",
        "fullUrl",
    )
    normal_keys = (
        "url",
        "download_url",
        "downloadUrl",
        "big_url",
        "bigUrl",
        "big",
        "photo_url",
        "photoUrl",
        "photo_urls",
        "photoUrls",
        "default_url",
        "defaultUrl",
        "video_url",
        "videoUrl",
        "sloc",
        "lloc",
        "cover",
    )
    keys = raw_keys + normal_keys if prefer_raw else normal_keys + raw_keys
    for key in keys:
        if key in value and (
            found := _url_from_value(value[key], prefer_raw=prefer_raw, depth=depth + 1)
        ):
            return found
    # 最后的宽松兜底，兼容桥接层自定义的 url 字段，但不把所有元数据都递归一遍。
    for key, nested in value.items():
        if "url" in str(key).lower() and (
            found := _url_from_value(nested, prefer_raw=prefer_raw, depth=depth + 1)
        ):
            return found
    return ""


def _media_url(item: dict[str, Any]) -> str:
    """从字段并不固定的媒体项里挑出一个可下载的 http 地址。

    SnowLuma 的图片和视频都包在 ``image`` / ``video`` 里；图片还可能同时
    带多个规格的 ``photo_url`` 与一个 ``default_url``。优先使用视频直链，
    图片在声明有原图时优先选择默认高质量地址，再回退到最高规格地址。
    """
    image = item.get("image")
    video = item.get("video")
    if isinstance(video, dict):
        for key in ("url", "video_url", "videoUrl", "cover"):
            if found := _url_from_value(video.get(key), prefer_raw=True):
                return found

    if isinstance(image, dict):
        has_raw = image.get("has_raw")
        if has_raw is None:
            has_raw = image.get("hasRaw")
        raw_flag = _bool_value(has_raw)
        if raw_flag is True:
            for key in ("default_url", "defaultUrl"):
                if found := _url_from_value(image.get(key), prefer_raw=True):
                    return found
            for key in ("photo_url", "photoUrl", "photo_urls", "photoUrls"):
                if found := _url_from_value(image.get(key), prefer_raw=True):
                    return found
        if found := _url_from_value(image, prefer_raw=raw_flag is True):
            return found
    for key in _MEDIA_URL_KEYS:
        if key in item and (
            found := _url_from_value(item[key], prefer_raw=key in {"origin_url", "raw_url"})
        ):
            return found
    return _url_from_value(item)


def _bool_value(value: Any) -> bool | None:
    """把协议端的 bool / 0、1 / 字符串统一起来。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y", "on", "有", "是"}:
            return True
        if text in {"0", "false", "no", "n", "off", "无", "否"}:
            return False
    return None


def _media_container(payload: Any) -> tuple[list[Any], str, bool | None]:
    """找到媒体数组及其分页元数据，兼容多层 data/result 包装。"""
    value = unwrap(payload)
    inherited_attach = ""
    inherited_has_more: bool | None = None

    def visit(current: Any, depth: int = 0) -> tuple[list[Any], str, bool | None]:
        if depth > 5:
            return [], inherited_attach, inherited_has_more
        if isinstance(current, list):
            return current, inherited_attach, inherited_has_more
        if not isinstance(current, dict):
            return [], inherited_attach, inherited_has_more

        attach = str(
            current.get("nextAttachInfo")
            or current.get("next_attach_info")
            or current.get("attachInfo")
            or current.get("attach_info")
            or inherited_attach
            or ""
        )
        has_more = inherited_has_more
        for key in _MEDIA_HAS_MORE_KEYS:
            if key in current:
                has_more = _bool_value(current.get(key))
                break

        for key in ("mediaList", "media_list", "medias", "media", "list", "items"):
            nested = current.get(key)
            if isinstance(nested, list):
                return nested, attach, has_more

        # ``data`` / ``result`` 既可能是下一层响应，也可能是一个媒体数组。
        for key in ("data", "result", "payload", "response"):
            nested = current.get(key)
            if isinstance(nested, (dict, list)):
                found, nested_attach, nested_more = visit(nested, depth + 1)
                if found:
                    return found, nested_attach or attach, (
                        nested_more if nested_more is not None else has_more
                    )

        # 直接传入一个媒体项时也尽量兼容，避免把它当成不可用响应。
        if any(key in current for key in ("image", "video", "lloc", "media_id", "mediaId")):
            return [current], attach, has_more
        return [], attach, has_more

    return visit(value)


def _normalize_album_media_page(payload: Any) -> tuple[list[dict[str, Any]], str, bool | None]:
    """归一化一页相册媒体，第三个返回值表示协议端明确的 has_more。"""
    raw_list, attach, has_more = _media_container(payload)
    medias: list[dict[str, Any]] = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue

        image = item.get("image") if isinstance(item.get("image"), dict) else {}
        video = item.get("video") if isinstance(item.get("video"), dict) else {}
        source = image or video or item
        media_id = _pick_str(
            image,
            ("lloc", "media_id", "mediaId", "id", "sloc"),
        )
        if video and not media_id:
            # SnowLuma's delete action accepts the video's id and resolves its
            # cover lloc internally.  Prefer that id over the cover's lloc so
            # video deletion remains reliable across paginated responses.
            media_id = _pick_str(video, ("id", "video_id", "videoId", "lloc"))
            if not media_id:
                cover = video.get("cover")
                if isinstance(cover, dict):
                    cover_image = (
                        cover.get("image")
                        if isinstance(cover.get("image"), dict)
                        else cover
                    )
                    media_id = _pick_str(
                        cover_image,
                        ("lloc", "media_id", "mediaId", "id", "sloc"),
                    )
        if not media_id:
            media_id = _pick_str(item, _MEDIA_ID_KEYS)

        url = _media_url(item)
        is_video = bool(video) or bool(
            item.get("duration")
            or item.get("video_url")
            or item.get("videoUrl")
            or item.get("video_type")
            or item.get("videoType")
        )
        if not url and not media_id:
            continue

        name = _pick_str(
            source,
            ("name", "title", "file_name", "fileName", "desc", "description"),
        ) or _pick_str(item, ("name", "title", "file_name", "fileName"))
        upload_time = (
            _pick_str(item, ("upload_time", "uploadTime", "created_at", "createdAt"))
            or _pick_str(source, ("upload_time", "uploadTime", "created_at", "createdAt"))
            or 0
        )
        normalized: dict[str, Any] = {
            "media_id": media_id,
            "url": url,
            "name": name,
            "upload_time": upload_time,
            "is_video": is_video,
            "raw": item,
        }
        batch_id = _pick_str(item, ("batch_id", "batchId"))
        if batch_id:
            normalized["batch_id"] = batch_id
        medias.append(normalized)
    return medias, attach, has_more


def normalize_album_media(payload: Any) -> tuple[list[dict[str, Any]], str]:
    """统一媒体列表响应，返回 (媒体项列表, 下一页游标)。

    各端字段命名不一致（mediaList / media_list / 直接一个数组），且图片项本身的
    字段也不固定，这里只保证 media_id / url / name / is_video 四个键可用，
    原始字段保留在 raw 里，便于以后扩展。
    """
    medias, attach, _ = _normalize_album_media_page(payload)
    return medias, attach


async def list_album_media(
    event: AstrMessageEvent, group_id: Any, album_id: Any, *, limit: int = 200
) -> list[dict[str, Any]]:
    """分页拉取一本相册里的媒体，最多取 limit 项。

    协议端不支持时返回空列表，调用方据此回退到本地留档。
    """
    gid = int(group_id)
    aid = str(album_id)
    limit = max(0, int(limit))
    if limit == 0:
        return []
    medias: list[dict[str, Any]] = []
    seen: set[str] = set()
    attach = ""

    for _ in range(_MEDIA_PAGE_LIMIT):
        page_params: dict[str, Any] = {"group_id": gid, "album_id": aid}
        # The current LLOneBot implementation accepts the optional
        # ``attach_info`` field too.  Older builds that do not understand it
        # will return a parameter error; because call_action does not cache
        # parameter errors as a missing action, the first page still remains a
        # safe fallback for those builds.
        if attach:
            page_params["attach_info"] = attach
        result = await call_action(event, _ALBUM_MEDIA_LIST_ACTIONS, **page_params)
        if not result.ok:
            if not medias:
                logger.debug(f"{LOG_TAG} 拉取相册媒体失败 group={gid} album={aid}: {result.error}")
            break
        page, next_attach, has_more = _normalize_album_media_page(result.data)
        for item in page:
            key = str(item.get("media_id") or item.get("url") or "")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            medias.append(item)
        if len(medias) >= limit:
            break
        # 明确的 next_has_more=False 优先级最高，即便响应还带着旧游标也结束。
        if has_more is False:
            break
        if not next_attach or next_attach == attach:
            break
        # 空页只有在协议端明确声明“还有下一页”时才继续，避免把异常响应
        # 当成可分页数据反复请求。
        if not page and has_more is not True:
            break
        attach = next_attach

    return medias[:limit]


async def del_album_media(
    event: AstrMessageEvent, group_id: Any, album_id: Any, media_id: str
) -> str:
    """删除相册里的一张图/一个视频，返回空串表示成功。"""
    result = await call_action(
        event,
        _ALBUM_MEDIA_DEL_ACTIONS,
        group_id=int(group_id),
        album_id=str(album_id),
        lloc=str(media_id),
    )
    if result.ok:
        logger.info(f"{LOG_TAG} 已删除相册媒体 group={group_id} album={album_id}")
        return ""
    return result.error or "协议端未返回结果"

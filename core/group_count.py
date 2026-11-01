"""欢迎 / 告别中的群人数：刷新名单，并消除成员变动通知与缓存更新的时差。"""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .config import LOG_TAG
from .protocol import extract_failure, unwrap
from .utils import parse_int

_COUNT_KEYS = ("member_count", "memberCount", "current_member_count", "currentMemberCount")
_REFRESH_DELAY = 0.4


async def _fresh_data(bot: Any, action: str, group_id: str) -> Any:
    """使用无缓存查询；兼容不接受 no_cache 关键字的客户端。"""
    getter = getattr(bot, action, None)
    if not callable(getter):
        return None
    try:
        try:
            payload = await getter(group_id=int(group_id), no_cache=True)
        except TypeError:
            payload = await getter(group_id=int(group_id))
        failure = extract_failure(payload)
        if failure:
            logger.debug(f"{LOG_TAG} {action} 失败 group={group_id}：{failure}")
            return None
        return unwrap(payload)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"{LOG_TAG} {action} 失败 group={group_id}：{exc}")
        return None


def _count(data: Any) -> int | None:
    if not isinstance(data, dict):
        return None
    for key in _COUNT_KEYS:
        raw = data.get(key)
        value = None if isinstance(raw, bool) else parse_int(raw)
        if value is not None and value > 0:
            return value
    return None


def _member_ids(data: Any) -> set[str] | None:
    """标准 OneBot 全量名单；无效 / 空响应不能当成零人的群。"""
    if not isinstance(data, list) or not data:
        return None
    members: set[str] = set()
    for member in data:
        if not isinstance(member, dict):
            return None
        raw = member.get("user_id")
        uid = None if isinstance(raw, bool) else parse_int(raw)
        if uid is None or uid <= 0:
            return None
        members.add(str(uid))
    return members


async def member_count(event: AstrMessageEvent, group_id: str) -> int | None:
    """获取当前群人数；变动通知仅对名单里尚未同步的当事人做一次校正。

    NapCat 的 get_group_info 可能忽略 no_cache，成员名单也可能先返回旧缓存。
    入群时取「名单 ∪ 新人」，退群时取「名单 − 离群成员」，已经同步的名单
    不会再次加减；欢迎测试和验证回复等普通消息不做入 / 退群校正。
    """
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    raw = raw if isinstance(raw, dict) else {}
    notice = raw.get("notice_type") if str(raw.get("group_id")) == group_id else None
    subject = parse_int(raw.get("user_id"))
    changed = notice in {"group_increase", "group_decrease"} and subject is not None and subject > 0
    members: set[str] | None = None
    if changed:
        subject_id = str(subject)
        joining = notice == "group_increase"
        for attempt in range(2):
            updated = _member_ids(await _fresh_data(event.bot, "get_group_member_list", group_id))
            if updated is None:
                break
            members = updated
            if (subject_id in members) == joining:
                break
            if attempt == 0:
                await asyncio.sleep(_REFRESH_DELAY)
        if members is not None:
            if joining:
                members.add(subject_id)
            else:
                members.discard(subject_id)
            return len(members)
    else:
        members = _member_ids(await _fresh_data(event.bot, "get_group_member_list", group_id))
        if members is not None:
            return len(members)

    # 实时群信息优先于通知载荷中的人数，后者也可能是成员变动前的快照。
    count = _count(await _fresh_data(event.bot, "get_group_info", group_id))
    if count is not None:
        return count
    return _count(raw) or _count(raw.get("group"))

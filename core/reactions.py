"""消息表情回应（贴表情）的跨端封装。

投票禁言用它来计票：给投票消息贴 👍 / 👎，成员点一下就算一票，比刷指令干净。
各协议端的动作名和返回结构不一致，这里统一成「贴一个表情」「查某几个表情分别
被谁点了」两个入口。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .config import LOG_TAG
from .protocol import as_list, call_action

#: 贴表情
_SET_ACTIONS: tuple[str, ...] = (
    "set_msg_emoji_like",
    "set_group_reaction",
    "set_emoji_like",
)

#: 一次性拿到某条消息上所有表情的点击情况
_BULK_ACTIONS: tuple[str, ...] = ("get_msg_emoji_likes",)

#: 只能按单个表情查时的回退动作
_DETAIL_ACTIONS: tuple[str, ...] = ("get_emoji_likes", "fetch_emoji_like")

#: 用户列表可能藏在这些键里
_USER_LIST_KEYS: tuple[str, ...] = ("users", "emoji_like_list", "list", "likes")


def _user_ids(payload: Any) -> set[str]:
    """从各种形状的点赞名单里抠出 user_id 集合。"""
    raw: Any = payload
    if isinstance(raw, dict):
        for key in _USER_LIST_KEYS:
            value = raw.get(key)
            if isinstance(value, list):
                raw = value
                break
    if not isinstance(raw, list):
        return set()
    users: set[str] = set()
    for item in raw:
        if isinstance(item, dict):
            uid = item.get("user_id") or item.get("uin") or item.get("userId")
        else:
            uid = item
        text = str(uid or "").strip()
        if text.isdigit():
            users.add(text)
    return users


async def add_reaction(event: AstrMessageEvent, message_id: Any, emoji_id: str) -> str:
    """给消息贴一个表情，返回空串表示成功。"""
    result = await call_action(
        event,
        _SET_ACTIONS,
        message_id=str(message_id),
        emoji_id=str(emoji_id),
        set=True,
    )
    if result.ok:
        return ""
    logger.debug(f"{LOG_TAG} 贴表情失败 message={message_id} emoji={emoji_id}: {result.error}")
    return result.error or "协议端不支持贴表情"


async def reaction_users(
    event: AstrMessageEvent, message_id: Any, emoji_ids: Sequence[str]
) -> dict[str, set[str]]:
    """查这几个表情分别被哪些 QQ 号点过。

    优先一次拉全量；有的协议端只返回数量不返回名单，这时按表情逐个回退查询。
    """
    wanted = [str(item) for item in emoji_ids]
    found: dict[str, set[str]] = {item: set() for item in wanted}

    bulk = await call_action(event, _BULK_ACTIONS, message_id=str(message_id))
    if bulk.ok:
        for item in as_list(bulk.data):
            if not isinstance(item, dict):
                continue
            emoji_id = str(item.get("emoji_id") or item.get("emojiId") or "")
            if emoji_id in found:
                found[emoji_id] |= _user_ids(item)
        if any(found.values()):
            return found

    for emoji_id in wanted:
        detail = await call_action(
            event, _DETAIL_ACTIONS, message_id=str(message_id), emoji_id=emoji_id
        )
        if detail.ok:
            found[emoji_id] |= _user_ids(detail.data)
    return found

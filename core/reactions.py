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
from .protocol import call_action, call_action_variants, unwrap

#: 贴表情
_SET_ACTIONS: tuple[str, ...] = (
    "set_msg_emoji_like",
    "set_group_reaction",
    "set_emoji_like",
)

#: 一次性拿到某条消息上所有表情的点击情况
_BULK_ACTIONS: tuple[str, ...] = ("get_msg_emoji_likes",)

#: 用户列表可能藏在这些键里。LLOneBot 的官方响应使用 camelCase 的
#: ``emojiLikesList``，不能只按 SnowLuma 的蛇形字段解析。
_USER_LIST_KEYS: tuple[str, ...] = (
    "users",
    "emoji_like_list",
    "emojiLikesList",
    "emoji_likes_list",
    "emojiLikes",
    "list",
    "likes",
)


def _user_ids(payload: Any) -> set[str]:
    """从各种形状的点赞名单里抠出 user_id 集合。"""
    users: set[str] = set()

    def visit(raw: Any, depth: int = 0) -> None:
        if depth > 5 or raw is None:
            return
        if isinstance(raw, dict):
            # 先处理明确的用户列表。LLOneBot 官方响应使用
            # ``emojiLikesList``，SnowLuma/NapCat 常见蛇形别名。
            found_list = False
            for key in _USER_LIST_KEYS:
                value = raw.get(key)
                if isinstance(value, (list, tuple, set)):
                    found_list = True
                    visit(value, depth + 1)
            if found_list:
                return
            # 兼容 {data: ...} / {result: ...} 等额外响应壳。
            for key in ("data", "result", "payload", "response"):
                if key in raw:
                    visit(raw[key], depth + 1)
            return
        if isinstance(raw, (list, tuple, set)):
            for item in raw:
                if isinstance(item, dict):
                    uid = (
                        item.get("user_id")
                        or item.get("userId")
                        or item.get("uin")
                        or item.get("tinyId")
                        or item.get("tiny_id")
                        or item.get("qq")
                    )
                    text = str(uid or "").strip()
                    if text.isdigit():
                        users.add(text)
                    else:
                        # 列表元素也可能继续包了一层用户对象。
                        visit(item, depth + 1)
                else:
                    text = str(item or "").strip()
                    if text.isdigit():
                        users.add(text)

    visit(unwrap(payload))
    return users


def _bulk_entries(payload: Any) -> list[dict[str, Any]]:
    """把「一条消息的全部表情」响应取成条目列表。"""
    raw = unwrap(payload)
    # 直接调用私有解析器的测试、以及少数桥接层会把 OneBot 外壳再包一层，
    # 这里不要只依赖 call_action 已经剥过一次；递归找常见 data/result 容器。
    for _ in range(5):
        if not isinstance(raw, dict):
            break
        nested = next(
            (
                raw[key]
                for key in ("data", "result", "payload", "response")
                if isinstance(raw.get(key), (dict, list))
            ),
            None,
        )
        if nested is None:
            break
        raw = nested
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        for key in ("reactions", "emoji_likes", "emojiLikes", "list", "items"):
            value = raw.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        # 某些桥接层直接把 emoji id 映射到用户数组。
        entries: list[dict[str, Any]] = []
        for emoji_id, users in raw.items():
            if isinstance(users, (list, tuple, set)):
                entries.append({"emoji_id": emoji_id, "users": users})
        return entries
    return []


def _emoji_id(item: dict[str, Any]) -> str:
    for key in ("emoji_id", "emojiId", "id", "emoji", "type"):
        value = item.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


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
        for item in _bulk_entries(bulk.data):
            emoji_id = _emoji_id(item)
            if emoji_id in found:
                found[emoji_id] |= _user_ids(item)

    for emoji_id in wanted:
        # 即使 bulk 接口返回了部分表情，也要补查没有名单的表情；否则
        # 「赞」有票、「踩」无票时会被误判成 bulk 已经完整返回。
        if found[emoji_id]:
            continue
        cookie = ""
        for _ in range(10):
            detail = await call_action_variants(
                event,
                (
                    (
                        "get_emoji_likes",
                        {"message_id": str(message_id), "emoji_id": emoji_id},
                    ),
                    (
                        "fetch_emoji_like",
                        {
                            "message_id": str(message_id),
                            "emojiId": emoji_id,
                            "count": 1000,
                            "cookie": cookie,
                        },
                    ),
                    (
                        "fetch_emoji_like",
                        {
                            "message_id": str(message_id),
                            "emoji_id": emoji_id,
                            "count": 1000,
                            "cookie": cookie,
                        },
                    ),
                ),
            )
            if not detail.ok:
                break
            found[emoji_id] |= _user_ids(detail.data)
            data = unwrap(detail.data)
            if not isinstance(data, dict):
                break
            is_last = data.get("isLastPage")
            if is_last is True:
                break
            next_cookie = str(data.get("cookie") or "").strip()
            if not next_cookie or next_cookie == cookie:
                break
            cookie = next_cookie
    return found

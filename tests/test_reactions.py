"""贴表情计票的跨端响应解析测试。"""

from __future__ import annotations

from typing import Any

import pytest
from astrbot_plugin_qun_steward.core import reactions


def test_user_ids_supports_llbot_emoji_likes_list_and_tiny_id() -> None:
    payload = {"data": {"emojiLikesList": [{"tinyId": "10001"}, {"tinyId": 10002}]}}
    assert reactions._user_ids(payload) == {"10001", "10002"}


def test_bulk_entries_supports_camel_case_emoji_id() -> None:
    payload = {"data": [{"emojiId": 76, "emojiLikesList": [{"tinyId": "9"}]}]}
    entries = reactions._bulk_entries(payload)
    assert reactions._emoji_id(entries[0]) == "76"
    assert reactions._user_ids(entries[0]) == {"9"}


class _Api:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_action(self, action: str, **params: Any) -> Any:
        self.calls.append((action, params))
        if action == "get_version_info":
            return {"data": {"app_name": "LLOneBot"}}
        if action == "get_msg_emoji_likes":
            raise RuntimeError("bulk unavailable")
        if action == "fetch_emoji_like":
            emoji_id = str(params.get("emojiId") or params.get("emoji_id"))
            if emoji_id == "76":
                return {"data": {"emojiLikesList": [{"tinyId": "1"}], "isLastPage": True}}
            return {
                "data": {
                    "emojiLikesList": [{"tinyId": "2"}],
                    "isLastPage": True,
                }
            }
        raise RuntimeError(f"unsupported {action}")


class _Event:
    def __init__(self) -> None:
        self.bot = type("Bot", (), {"api": _Api()})()


@pytest.mark.asyncio
async def test_reaction_users_paginates_and_fills_both_emojis() -> None:
    event = _Event()
    result = await reactions.reaction_users(event, "m1", ("76", "77"))
    assert result == {"76": {"1"}, "77": {"2"}}
    fetches = [name for name, _ in event.bot.api.calls if name == "fetch_emoji_like"]
    assert len(fetches) == 2

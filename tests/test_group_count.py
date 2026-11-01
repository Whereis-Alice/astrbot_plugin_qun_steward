"""人数回归：重现 QQ 已有 349 人、欢迎却仍报 348 人的缓存时差。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_qun_steward.core import group_count
from astrbot_plugin_qun_steward.core.group_count import member_count

GID = "10001"
UID = "20002"


def _members(count: int, *, includes_subject: bool) -> list[dict[str, int]]:
    users = [{"user_id": 30000 + index} for index in range(count)]
    if includes_subject:
        users[-1] = {"user_id": int(UID)}
    return users


def _event(bot: Any, notice: str | None = "group_increase", **raw: Any) -> Any:
    payload = {"group_id": int(GID), "user_id": int(UID), "member_count": 348, **raw}
    if notice:
        payload["notice_type"] = notice
    return SimpleNamespace(bot=bot, message_obj=SimpleNamespace(raw_message=payload))


@pytest.fixture(autouse=True)
def fast_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(group_count, "_REFRESH_DELAY", 0)


@pytest.mark.parametrize("already_updated", [False, True])
async def test_join_includes_new_member_exactly_once(already_updated: bool) -> None:
    members = _members(349 if already_updated else 348, includes_subject=already_updated)
    getter = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": members})
    bot = SimpleNamespace(
        get_group_member_list=getter,
        get_group_info=AsyncMock(return_value={"member_count": 348}),
    )

    assert await member_count(_event(bot), GID) == 349
    assert getter.call_count == (1 if already_updated else 2)
    getter.assert_called_with(group_id=int(GID), no_cache=True)
    bot.get_group_info.assert_not_called()


async def test_rechecks_list_while_backend_refreshes_in_background() -> None:
    getter = AsyncMock(side_effect=[
        _members(348, includes_subject=False),
        _members(350, includes_subject=True),
    ])
    bot = SimpleNamespace(get_group_member_list=getter)

    assert await member_count(_event(bot), GID) == 350
    assert getter.call_count == 2


async def test_retains_first_valid_list_when_refresh_fails() -> None:
    getter = AsyncMock(side_effect=[
        _members(348, includes_subject=False),
        RuntimeError("刷新失败"),
    ])

    assert await member_count(_event(SimpleNamespace(get_group_member_list=getter)), GID) == 349


@pytest.mark.parametrize("already_updated", [False, True])
async def test_leave_removes_departed_member_exactly_once(already_updated: bool) -> None:
    getter = AsyncMock(return_value=_members(
        348 if already_updated else 349,
        includes_subject=not already_updated,
    ))
    bot = SimpleNamespace(get_group_member_list=getter)

    assert await member_count(_event(bot, "group_decrease", member_count=349), GID) == 348
    assert getter.call_count == (1 if already_updated else 2)


async def test_duplicate_ids_do_not_inflate_total() -> None:
    members = _members(349, includes_subject=True)
    bot = SimpleNamespace(get_group_member_list=AsyncMock(return_value=members + members[:3]))

    assert await member_count(_event(bot), GID) == 349


@pytest.mark.parametrize("notice", [None, "group_increase"])
async def test_fresh_info_wins_over_stale_notice_when_list_unavailable(notice: str | None) -> None:
    getter = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"member_count": "349"}})
    bot = SimpleNamespace(get_group_info=getter)

    assert await member_count(_event(bot, notice), GID) == 349
    getter.assert_called_once_with(group_id=int(GID), no_cache=True)


async def test_preview_does_not_add_operator_to_current_count() -> None:
    bot = SimpleNamespace(
        get_group_member_list=AsyncMock(return_value=_members(349, includes_subject=False)),
        get_group_info=AsyncMock(return_value={"member_count": 348}),
    )

    assert await member_count(_event(bot, None), GID) == 349
    bot.get_group_member_list.assert_awaited_once()
    bot.get_group_info.assert_not_called()


async def test_notice_from_other_group_is_not_applied() -> None:
    bot = SimpleNamespace(get_group_member_list=AsyncMock(return_value=_members(349, includes_subject=False)))

    assert await member_count(_event(bot, group_id=99999), GID) == 349


async def test_client_without_no_cache_argument_is_supported() -> None:
    calls = []

    async def getter(*, group_id: int) -> list[dict[str, int]]:
        calls.append(group_id)
        return _members(349, includes_subject=True)

    assert await member_count(_event(SimpleNamespace(get_group_member_list=getter)), GID) == 349
    assert calls == [int(GID)]


@pytest.mark.parametrize("bad_list", [
    [], None, {}, [{"nickname": "缺少 QQ 号"}], [{"user_id": True}],
    {"status": "failed", "retcode": 100, "data": _members(349, includes_subject=True)},
])
async def test_invalid_or_failed_list_falls_back_to_fresh_info(bad_list: Any) -> None:
    bot = SimpleNamespace(
        get_group_member_list=AsyncMock(return_value=bad_list),
        get_group_info=AsyncMock(return_value={"memberCount": 349}),
    )

    assert await member_count(_event(bot), GID) == 349


async def test_api_failure_uses_notice_count_without_guessing_plus_one() -> None:
    bot = SimpleNamespace(get_group_info=AsyncMock(side_effect=RuntimeError("协议端离线")))

    assert await member_count(_event(bot), GID) == 348


async def test_nested_notice_count_is_supported_as_last_fallback() -> None:
    event = _event(SimpleNamespace(), member_count=None, group={"currentMemberCount": "349"})

    assert await member_count(event, GID) == 349


async def test_missing_count_is_unknown_instead_of_zero() -> None:
    event = _event(SimpleNamespace(), member_count=None)

    assert await member_count(event, GID) is None

"""投票禁言的表情票合并、竞态与生命周期测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from astrbot_plugin_qun_steward.features.base import FeatureContext
from astrbot_plugin_qun_steward.features.vote import VoteFeature, VoteRecord

GROUP_ID = "10001"
TARGET_ID = "20002"


class _Event:
    def __init__(self, *, sender_id: str = "30003", self_id: str = "99999") -> None:
        self.sent: list[Any] = []
        self._sender_id = sender_id
        self._self_id = self_id
        self.bot = SimpleNamespace(
            get_group_member_info=self._member_info,
            get_stranger_info=self._stranger_info,
            set_group_ban=self._set_group_ban,
        )
        self.bans: list[dict[str, Any]] = []

    def get_group_id(self) -> str:
        return GROUP_ID

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_sender_name(self) -> str:
        return "投票者"

    def get_self_id(self) -> str:
        return self._self_id

    async def _member_info(self, **kwargs: Any) -> dict[str, str]:
        return {"card": "目标用户" if str(kwargs.get("user_id")) == TARGET_ID else "投票者"}

    async def _stranger_info(self, **_kwargs: Any) -> dict[str, str]:
        return {"nickname": "目标用户"}

    async def _set_group_ban(self, **kwargs: Any) -> None:
        self.bans.append(kwargs)

    async def send(self, value: Any) -> None:
        self.sent.append(value)

    def plain_result(self, value: str) -> str:
        return value


def _feature(make_config: Any, store: Any, database: Any) -> VoteFeature:
    async def record(**_kwargs: Any) -> None:
        return None

    ctx = FeatureContext(
        context=None,  # type: ignore[arg-type]
        config=make_config(),
        store=store,
        permissions=None,  # type: ignore[arg-type]
        audit=SimpleNamespace(record=record),  # type: ignore[arg-type]
        undo=None,  # type: ignore[arg-type]
        groups=None,  # type: ignore[arg-type]
        db=database,
        to_image=None,  # type: ignore[arg-type]
    )
    return VoteFeature(ctx)


def _record(*, threshold: int = 5) -> VoteRecord:
    return VoteRecord(
        group_id=GROUP_ID,
        target_id=TARGET_ID,
        ban_time=60,
        threshold=threshold,
        expire_at=10**10,
        message_id="message-1",
    )


@pytest.mark.asyncio
async def test_poll_replaces_removed_emoji_votes(
    make_config: Any, store: Any, database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    feature = _feature(make_config, store, database)
    record = _record()
    feature._votes[GROUP_ID] = record
    event = _Event()
    rounds = 0

    async def fake_sleep(_seconds: float) -> None:
        nonlocal rounds
        rounds += 1
        if rounds > 2:
            raise asyncio.CancelledError

    async def fake_reaction_users(*_args: Any) -> dict[str, set[str]]:
        if rounds == 1:
            return {"76": {"40004"}, "77": set()}
        return {"76": set(), "77": set()}

    monkeypatch.setattr("astrbot_plugin_qun_steward.features.vote.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(
        "astrbot_plugin_qun_steward.features.vote.reaction_users", fake_reaction_users
    )

    await feature._poll_reactions(event, record)

    assert record.emoji_votes == {}
    assert record.votes == {}


@pytest.mark.asyncio
async def test_command_and_emoji_votes_are_merged_without_losing_command_vote(
    make_config: Any, store: Any, database: Any
) -> None:
    feature = _feature(make_config, store, database)
    record = _record()
    record.command_votes["40004"] = True
    record.emoji_votes["50005"] = False
    feature._merge_votes(record)
    assert record.votes == {"40004": True, "50005": False}

    # 取消表情后，只移除表情来源，指令票仍然有效。
    record.emoji_votes = {}
    feature._merge_votes(record)
    assert record.votes == {"40004": True}


@pytest.mark.asyncio
async def test_concurrent_threshold_checks_settle_only_once(
    make_config: Any, store: Any, database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    feature = _feature(make_config, store, database)
    record = _record(threshold=1)
    record.votes["40004"] = True
    feature._votes[GROUP_ID] = record
    event = _Event()
    executions = 0

    async def fake_execute(*_args: Any, **_kwargs: Any) -> str:
        nonlocal executions
        executions += 1
        await asyncio.sleep(0)
        return "已结算"

    monkeypatch.setattr(feature, "_execute", fake_execute)
    results = await asyncio.gather(
        feature._check_threshold(event, record), feature._check_threshold(event, record)
    )

    assert executions == 1
    assert results.count("已结算") == 1
    assert sum(bool(result) for result in results) == 1
    assert feature.status(GROUP_ID) is None


@pytest.mark.asyncio
async def test_polling_task_can_finish_and_send_result_without_self_cancellation(
    make_config: Any, store: Any, database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    feature = _feature(make_config, store, database)
    record = _record(threshold=1)
    feature._votes[GROUP_ID] = record
    event = _Event()
    rounds = 0

    async def fake_sleep(_seconds: float) -> None:
        nonlocal rounds
        rounds += 1
        if rounds > 1:
            raise AssertionError("轮询在结算后不应继续等待")

    async def fake_reaction_users(*_args: Any) -> dict[str, set[str]]:
        return {"76": {"40004"}, "77": set()}

    async def fake_execute(*_args: Any, **_kwargs: Any) -> str:
        return "已结算"

    monkeypatch.setattr("astrbot_plugin_qun_steward.features.vote.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(
        "astrbot_plugin_qun_steward.features.vote.reaction_users", fake_reaction_users
    )
    monkeypatch.setattr(feature, "_execute", fake_execute)

    task = feature._spawn(feature._poll_reactions(event, record))
    await task

    assert not task.cancelled()
    assert event.sent == ["已结算"]


@pytest.mark.asyncio
async def test_shutdown_cancels_and_waits_tracked_tasks(
    make_config: Any, store: Any, database: Any
) -> None:
    feature = _feature(make_config, store, database)
    finished = asyncio.Event()

    async def worker() -> None:
        try:
            await asyncio.sleep(60)
        finally:
            finished.set()

    task = feature._spawn(worker())
    await asyncio.sleep(0)
    await feature.shutdown()

    assert task.done()
    assert finished.is_set()
    assert not feature._tasks


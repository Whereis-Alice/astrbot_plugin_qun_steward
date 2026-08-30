"""撤销栈：入栈上限、过期回收、回滚成败。"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from astrbot_plugin_qun_steward.core.undo import MAX_PER_GROUP, UndoStack

GID = "10001"
ConfigFactory = Callable[..., Any]


def _stack(make_config: ConfigFactory, **safety: Any) -> UndoStack:
    return UndoStack(make_config(safety=safety) if safety else make_config())


async def _noop() -> None:
    return None


def _push(stack: UndoStack, description: str, revert: Any = None) -> None:
    stack.push(
        GID,
        operator_id="10000",
        action="ban",
        description=description,
        revert=revert or _noop,
    )


class TestBasics:
    def test_window_reads_config(self, make_config: ConfigFactory) -> None:
        assert _stack(make_config).window == 900
        assert _stack(make_config, undo_window=60).window == 60

    def test_peek_returns_latest(self, make_config: ConfigFactory) -> None:
        stack = _stack(make_config)
        _push(stack, "第一条")
        _push(stack, "第二条")
        entry = stack.peek(GID)
        assert entry is not None
        assert entry.description == "第二条"
        # peek 不出栈
        assert stack.peek(GID) is not None

    def test_pending_is_newest_first(self, make_config: ConfigFactory) -> None:
        stack = _stack(make_config)
        _push(stack, "旧")
        _push(stack, "新")
        assert [item.description for item in stack.pending(GID)] == ["新", "旧"]

    def test_pop_removes_entry(self, make_config: ConfigFactory) -> None:
        stack = _stack(make_config)
        _push(stack, "唯一一条")
        assert stack.pop(GID) is not None
        assert stack.pop(GID) is None

    def test_groups_are_isolated(self, make_config: ConfigFactory) -> None:
        stack = _stack(make_config)
        _push(stack, "A 群的操作")
        assert stack.peek("20002") is None

    def test_clear_single_and_all(self, make_config: ConfigFactory) -> None:
        stack = _stack(make_config)
        _push(stack, "一条")
        stack.clear(GID)
        assert stack.peek(GID) is None
        _push(stack, "又一条")
        stack.clear()
        assert stack.pending(GID) == []

    def test_stack_is_capped(self, make_config: ConfigFactory) -> None:
        stack = _stack(make_config)
        for index in range(MAX_PER_GROUP + 5):
            _push(stack, "操作" + str(index))
        pending = stack.pending(GID)
        assert len(pending) == MAX_PER_GROUP
        # 保留的是最近的那些
        assert pending[0].description == "操作" + str(MAX_PER_GROUP + 4)


class TestExpiry:
    def test_expired_entries_are_dropped(self, make_config: ConfigFactory) -> None:
        stack = _stack(make_config, undo_window=60)
        _push(stack, "很久以前的操作")
        entry = stack.peek(GID)
        assert entry is not None
        entry.created_at = time.time() - 3600
        assert stack.peek(GID) is None
        assert stack.pending(GID) == []

    def test_zero_window_disables_expiry(self, make_config: ConfigFactory) -> None:
        stack = _stack(make_config, undo_window=0)
        _push(stack, "永不过期")
        entry = stack.peek(GID)
        assert entry is not None
        entry.created_at = time.time() - 86400
        assert stack.peek(GID) is not None


class TestUndo:
    async def test_undo_runs_revert(self, make_config: ConfigFactory) -> None:
        calls: list[str] = []

        async def revert() -> None:
            calls.append("done")

        stack = _stack(make_config)
        _push(stack, "禁言 张三 60 秒", revert)
        ok, message = await stack.undo(GID)
        assert ok is True
        assert calls == ["done"]
        assert message == "已撤销：禁言 张三 60 秒"
        # 撤销过的操作不该还能再撤一次
        assert stack.peek(GID) is None

    async def test_undo_without_entry(self, make_config: ConfigFactory) -> None:
        stack = _stack(make_config, undo_window=900)
        ok, message = await stack.undo(GID)
        assert ok is False
        assert "15 分钟" in message

    async def test_undo_reports_revert_failure(self, make_config: ConfigFactory) -> None:
        async def revert() -> None:
            raise RuntimeError("协议端掉线了")

        stack = _stack(make_config)
        _push(stack, "解除禁言 张三", revert)
        ok, message = await stack.undo(GID)
        assert ok is False
        assert "撤销失败" in message
        assert "协议端掉线了" in message

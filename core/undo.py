"""危险操作撤销栈。

管理员手滑禁言 / 拉黑 / 上管之后，可以用「撤销」指令回滚最近一次操作。
只保存在内存里：撤销窗口很短（默认 15 分钟），重启后失效是可接受的。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

from .config import LOG_TAG, StewardConfig

#: 每个群最多保留的可撤销操作数
MAX_PER_GROUP = 20

RevertFn = Callable[[], Awaitable[None]]


@dataclass
class UndoEntry:
    """一条可回滚的操作。"""

    group_id: str
    operator_id: str
    action: str
    description: str
    revert: RevertFn
    created_at: float = field(default_factory=time.time)

    def expired(self, window: int) -> bool:
        return window > 0 and (time.time() - self.created_at) > window


class UndoStack:
    """按群维护的撤销栈。"""

    def __init__(self, config: StewardConfig) -> None:
        self._config = config
        self._stacks: dict[str, list[UndoEntry]] = {}

    @property
    def window(self) -> int:
        return self._config.safety.int("undo_window", 900)

    def push(
        self,
        group_id: Any,
        *,
        operator_id: Any,
        action: str,
        description: str,
        revert: RevertFn,
    ) -> None:
        """登记一条可撤销操作。"""
        gid = str(group_id)
        stack = self._stacks.setdefault(gid, [])
        stack.append(
            UndoEntry(
                group_id=gid,
                operator_id=str(operator_id),
                action=action,
                description=description,
                revert=revert,
            )
        )
        del stack[:-MAX_PER_GROUP]
        self._prune(gid)

    def peek(self, group_id: Any) -> UndoEntry | None:
        gid = str(group_id)
        self._prune(gid)
        stack = self._stacks.get(gid)
        return stack[-1] if stack else None

    def pop(self, group_id: Any) -> UndoEntry | None:
        gid = str(group_id)
        self._prune(gid)
        stack = self._stacks.get(gid)
        return stack.pop() if stack else None

    def pending(self, group_id: Any) -> list[UndoEntry]:
        gid = str(group_id)
        self._prune(gid)
        return list(reversed(self._stacks.get(gid, [])))

    def clear(self, group_id: Any | None = None) -> None:
        if group_id is None:
            self._stacks.clear()
        else:
            self._stacks.pop(str(group_id), None)

    async def undo(self, group_id: Any) -> tuple[bool, str]:
        """撤销最近一次操作，返回 (是否成功, 提示文案)。"""
        entry = self.pop(group_id)
        if entry is None:
            window_min = max(1, self.window // 60)
            return False, f"最近 {window_min} 分钟内没有可撤销的操作"
        try:
            await entry.revert()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 撤销失败 {entry.description}: {exc}")
            return False, f"撤销失败：{entry.description}（{exc}）"
        return True, f"已撤销：{entry.description}"

    def _prune(self, gid: str) -> None:
        stack = self._stacks.get(gid)
        if not stack:
            return
        window = self.window
        alive = [entry for entry in stack if not entry.expired(window)]
        if alive:
            self._stacks[gid] = alive
        else:
            self._stacks.pop(gid, None)

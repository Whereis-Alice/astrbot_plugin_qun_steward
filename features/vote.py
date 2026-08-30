"""投票禁言。

相比上游：
- 每群可同时存在的投票仍为 1 个，但结算任务会被登记，插件卸载时统一取消；
- 支持配置是否允许被投票者自己投票；
- 投票发起、通过、否决都会写审计日志。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..core.config import LOG_TAG
from ..core.utils import get_ats, get_nickname
from .base import Feature, FeatureContext


@dataclass
class VoteRecord:
    """一场进行中的禁言投票。"""

    group_id: str
    target_id: str
    ban_time: int
    threshold: int
    expire_at: float
    votes: dict[str, bool] = field(default_factory=dict)
    task: asyncio.Task[None] | None = None

    @property
    def agree(self) -> int:
        return sum(1 for v in self.votes.values() if v)

    @property
    def disagree(self) -> int:
        return sum(1 for v in self.votes.values() if not v)


class VoteFeature(Feature):
    """群成员投票禁言。"""

    def __init__(self, ctx: FeatureContext) -> None:
        super().__init__(ctx)
        self._votes: dict[str, VoteRecord] = {}

    def _settings(self, group_id: Any) -> tuple[int, int, bool]:
        group_cfg = self.store.value(group_id, "vote_ban")
        group_cfg = group_cfg if isinstance(group_cfg, dict) else {}
        section = self.config.vote_ban
        ttl = int(group_cfg.get("ttl") or section.int("ttl", 120))
        threshold = int(group_cfg.get("threshold") or section.int("threshold", 3))
        allow_self = bool(
            group_cfg.get("allow_self_vote", section.bool("allow_self_vote", False))
        )
        return max(10, ttl), max(1, threshold), allow_self

    async def start(self, event: AstrMessageEvent, ban_time: Any = None) -> str:
        targets = get_ats(event)
        if not targets:
            return "请 @ 要投票禁言的人"
        group_id = event.get_group_id()
        if group_id in self._votes:
            return "群内已有正在进行的禁言投票"

        target_id = targets[0]
        seconds = self.config.resolve_ban_time(
            self.store.value(group_id, "random_ban_time"), ban_time
        )
        ttl, threshold, _ = self._settings(group_id)

        record = VoteRecord(
            group_id=group_id,
            target_id=target_id,
            ban_time=seconds,
            threshold=threshold,
            expire_at=time.time() + ttl,
        )
        self._votes[group_id] = record
        record.task = asyncio.create_task(self._settle_later(event, record, ttl))

        nickname = await get_nickname(event, target_id)
        await self.log(
            event, "vote_ban", target_id=target_id, detail=f"发起投票，禁言{seconds}秒，阈值{threshold}"
        )
        return (
            f"已发起对 {nickname} 的禁言投票（禁言{seconds}秒）\n"
            f"发送「赞同禁言 / 反对禁言」表态，任一方满 {threshold} 票立即结算，{ttl} 秒后按多数票结算"
        )

    async def cast(self, event: AstrMessageEvent, agree: bool) -> str:
        group_id = event.get_group_id()
        record = self._votes.get(group_id)
        if not record:
            return "当前没有进行中的禁言投票"

        voter_id = str(event.get_sender_id())
        _, _, allow_self = self._settings(group_id)
        if voter_id == record.target_id and not allow_self:
            return "被投票的人不能自己投票"

        record.votes[voter_id] = agree
        nickname = await get_nickname(event, record.target_id)

        if record.agree >= record.threshold:
            self._finish(group_id)
            return await self._execute(event, record, nickname, passed=True, reason="投票通过")
        if record.disagree >= record.threshold:
            self._finish(group_id)
            await self.log(event, "vote_ban", target_id=record.target_id, detail="投票被否决")
            return f"禁言投票被否决，{nickname}安全了"
        return (
            f"禁言【{nickname}】：\n"
            f"赞同（{record.agree}/{record.threshold}）\n"
            f"反对（{record.disagree}/{record.threshold}）"
        )

    async def _settle_later(
        self, event: AstrMessageEvent, record: VoteRecord, ttl: int
    ) -> None:
        try:
            await asyncio.sleep(ttl)
        except asyncio.CancelledError:
            return
        if self._votes.get(record.group_id) is not record:
            return
        self._finish(record.group_id)
        nickname = await get_nickname(event, record.target_id)
        try:
            if record.agree > record.disagree:
                message = await self._execute(
                    event, record, nickname, passed=True, reason="投票时间到"
                )
            else:
                await self.log(
                    event, "vote_ban", target_id=record.target_id, detail="超时按多数票否决"
                )
                message = f"投票时间到！禁言被否决，{nickname}安全了"
            await event.send(event.plain_result(message))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 投票结算失败 group={record.group_id}: {exc}")

    async def _execute(
        self,
        event: AstrMessageEvent,
        record: VoteRecord,
        nickname: str,
        *,
        passed: bool,
        reason: str,
    ) -> str:
        if not passed:
            return f"{reason}！禁言被否决，{nickname}安全了"
        try:
            await event.bot.set_group_ban(
                group_id=int(record.group_id),
                user_id=int(record.target_id),
                duration=record.ban_time,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 投票禁言执行失败 group={record.group_id}: {exc}")
            await self.log(
                event, "vote_ban", target_id=record.target_id, detail=str(exc), success=False
            )
            return f"{reason}，但我没有权限禁言{nickname}"
        await self.log(
            event, "vote_ban", target_id=record.target_id, detail=f"通过，禁言{record.ban_time}秒"
        )
        return f"{reason}！已禁言{nickname} {record.ban_time} 秒"

    def _finish(self, group_id: str) -> None:
        record = self._votes.pop(group_id, None)
        if record and record.task and not record.task.done():
            record.task.cancel()

    def status(self, group_id: Any) -> dict[str, Any] | None:
        record = self._votes.get(str(group_id))
        if not record:
            return None
        return {
            "group_id": record.group_id,
            "target_id": record.target_id,
            "ban_time": record.ban_time,
            "threshold": record.threshold,
            "agree": record.agree,
            "disagree": record.disagree,
            "expire_at": record.expire_at,
        }

    async def shutdown(self) -> None:
        """插件卸载时取消所有结算任务。"""
        for group_id in list(self._votes):
            self._finish(group_id)

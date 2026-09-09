"""投票禁言。

相比上游：
- 支持「贴表情计票」：投票消息发出后机器人自己先贴上 👍 / 👎，群友点一下就算
  一票，不用刷指令；协议端不支持贴表情时自动退回指令计票；
- 每群同时只有 1 场投票，结算任务与轮询任务都会登记，插件卸载时统一取消；
- 支持配置是否允许被投票者自己投票；
- 投票发起、通过、否决都会写审计日志。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..core.config import LOG_TAG
from ..core.reactions import add_reaction, reaction_users
from ..core.utils import get_ats, get_nickname
from .base import Feature, FeatureContext

#: 贴表情计票的轮询间隔（秒）
POLL_INTERVAL = 5

#: 贴表情模式
MODE_EMOJI = "贴表情"
MODE_COMMAND = "发指令"


@dataclass
class VoteRecord:
    """一场进行中的禁言投票。"""

    group_id: str
    target_id: str
    ban_time: int
    threshold: int
    expire_at: float
    votes: dict[str, bool] = field(default_factory=dict)
    #: 指令票和表情票分开保存。这样取消表情后可以准确移除表情票，
    #: 同时不影响「两者都行」模式下同一用户已经发出的指令票。
    command_votes: dict[str, bool] = field(default_factory=dict, repr=False)
    emoji_votes: dict[str, bool] = field(default_factory=dict, repr=False)
    #: 投票公告的消息 ID，贴表情计票时才有
    message_id: str = ""
    task: asyncio.Task[None] | None = None
    poll: asyncio.Task[None] | None = None
    #: 结算只允许成功认领一次；轮询与超时任务可能同时醒来。
    settle_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    settled: bool = False

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
        # 投票任务不是由 main.py 的通用任务集合创建的：一场投票可能在任意
        # 群消息处理器里启动。因此这里单独持有所有任务，卸载时即使任务已经
        # 从 _votes 里移除、正在执行最后一段网络调用，也能被统一等待/取消。
        self._tasks: set[asyncio.Task[Any]] = set()

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        """创建并追踪一条投票后台任务。"""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ------------------------------------------------------------ 配置读取

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

    @property
    def _mode(self) -> str:
        return self.config.vote_ban.str("mode", "两者都行")

    @property
    def _emojis(self) -> tuple[str, str]:
        section = self.config.vote_ban
        return section.str("agree_emoji", "76"), section.str("disagree_emoji", "77")

    # ------------------------------------------------------------ 发起投票

    async def start(self, event: AstrMessageEvent, ban_time: Any = None) -> str:
        targets = get_ats(event)
        if not targets:
            return "请 @ 要投票禁言的人"
        group_id = event.get_group_id()
        if not group_id:
            return "投票禁言只能在群里使用"
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
        nickname = await get_nickname(event, target_id)
        await self.log(
            event,
            "vote_ban",
            target_id=target_id,
            detail=f"发起投票，禁言{seconds}秒，阈值{threshold}",
        )

        self._votes[group_id] = record
        record.task = self._spawn(self._settle_later(event, record, ttl))

        emoji_ready = False
        if self._mode != MODE_COMMAND:
            emoji_ready = await self._setup_emoji_vote(
                event, record, nickname, seconds, threshold, ttl
            )
        if emoji_ready:
            # 公告已经由 _setup_emoji_vote 自己发出去了，避免重复播报
            return ""
        if self._mode == MODE_EMOJI:
            # 严格表情模式不能悄悄改成指令计票；清理刚创建的超时任务，
            # 把失败原因直接交给发起人。
            self._finish(group_id)
            return "贴表情计票启动失败：当前协议端无法给投票消息贴上两个表情"
        # _setup_emoji_vote 已经发出公告但只有一个表情成功时，公告本身包含
        # 指令回退说明，不再额外发第二条几乎相同的消息。
        if record.message_id:
            return ""
        return self._announcement(nickname, seconds, threshold, ttl, emoji=False)

    def _announcement(
        self, nickname: str, seconds: int, threshold: int, ttl: int, *, emoji: bool
    ) -> str:
        if emoji:
            how = "给这条消息贴 👍 表示赞同、贴 👎 表示反对"
            if self._mode == "两者都行":
                how += "；若表情回应不可用，也可发送「赞同禁言 / 反对禁言」"
        else:
            how = "发送「赞同禁言 / 反对禁言」表态"
        return (
            f"已发起对 {nickname} 的禁言投票（禁言{seconds}秒）\n"
            f"{how}，任一方满 {threshold} 票立即结算，{ttl} 秒后按多数票结算"
        )

    async def _setup_emoji_vote(
        self,
        event: AstrMessageEvent,
        record: VoteRecord,
        nickname: str,
        seconds: int,
        threshold: int,
        ttl: int,
    ) -> bool:
        """自己发公告拿到 message_id，贴上两个表情并启动轮询；不可用时返回 False。"""
        text = self._announcement(nickname, seconds, threshold, ttl, emoji=True)
        message_id = await self._send_own(event, text)
        if not message_id:
            return False

        # 记住公告已经发出。即使第二个表情贴失败，「两者都行」也可以让
        # 发起人继续用指令计票，而不应再重复发送一条公告。
        record.message_id = message_id
        agree_emoji, disagree_emoji = self._emojis
        # 两个表情必须都能贴上。只贴成功一个会让群友误以为投票可用，
        # 但轮询永远收不到另一方的票，最终只能等超时；这种情况整体回退。
        for emoji_id in (agree_emoji, disagree_emoji):
            if error := await add_reaction(event, message_id, emoji_id):
                logger.debug(
                    f"{LOG_TAG} 投票表情初始化不完整 message={message_id} "
                    f"emoji={emoji_id}: {error}"
                )
                return False

        record.poll = self._spawn(self._poll_reactions(event, record))
        return True

    async def _send_own(self, event: AstrMessageEvent, text: str) -> str:
        """用协议端接口发消息，好处是能拿到 message_id 用来贴表情。"""
        try:
            payload = await event.bot.send_group_msg(
                group_id=int(event.get_group_id()), message=text
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"{LOG_TAG} 发送投票公告失败，退回指令计票: {exc}")
            return ""
        if isinstance(payload, dict):
            message_id = payload.get("message_id") or payload.get("messageId")
            if message_id is not None:
                return str(message_id)
        return ""

    # ------------------------------------------------------------ 计票

    async def cast(self, event: AstrMessageEvent, agree: bool) -> str:
        group_id = event.get_group_id()
        record = self._votes.get(group_id)
        if not record:
            return "当前没有进行中的禁言投票"
        if self._mode == MODE_EMOJI:
            return "本群改用贴表情计票：给上面那条投票消息贴 👍 或 👎 即可"

        voter_id = str(event.get_sender_id())
        _, _, allow_self = self._settings(group_id)
        if voter_id == record.target_id and not allow_self:
            return "被投票的人不能自己投票"

        record.command_votes[voter_id] = agree
        self._merge_votes(record)
        return await self._check_threshold(event, record) or self._progress(
            record, await get_nickname(event, record.target_id)
        )

    def _progress(self, record: VoteRecord, nickname: str) -> str:
        return (
            f"禁言【{nickname}】：\n"
            f"赞同（{record.agree}/{record.threshold}）\n"
            f"反对（{record.disagree}/{record.threshold}）"
        )

    async def _check_threshold(self, event: AstrMessageEvent, record: VoteRecord) -> str:
        """够票就结算并返回结论文本，还没够票返回空串。"""
        if record.agree < record.threshold and record.disagree < record.threshold:
            return ""
        if not await self._claim_settlement(record):
            return ""
        nickname = await get_nickname(event, record.target_id)
        self._finish(record.group_id)
        if record.agree >= record.threshold:
            return await self._execute(event, record, nickname, passed=True, reason="投票通过")
        await self.log(event, "vote_ban", target_id=record.target_id, detail="投票被否决")
        return f"禁言投票被否决，{nickname}安全了"

    async def _poll_reactions(self, event: AstrMessageEvent, record: VoteRecord) -> None:
        """定时读取投票消息上的表情，把点了表情的人折算成票。"""
        agree_emoji, disagree_emoji = self._emojis
        self_id = str(event.get_self_id() or "")
        _, _, allow_self = self._settings(record.group_id)
        try:
            while True:
                await asyncio.sleep(POLL_INTERVAL)
                if self._votes.get(record.group_id) is not record:
                    return
                try:
                    found = await reaction_users(
                        event, record.message_id, (agree_emoji, disagree_emoji)
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 下一轮继续尝试
                    # 网络抖动或协议端暂时限流不应让整场表情投票静默失效；
                    # 超时任务仍会兜底结算，下一轮再重新读取即可。
                    logger.warning(
                        f"{LOG_TAG} 读取投票表情失败 group={record.group_id}：{exc}"
                    )
                    continue
                yes = found.get(agree_emoji, set())
                no = found.get(disagree_emoji, set())
                excluded = {self_id} if self_id else set()
                if not allow_self:
                    excluded.add(record.target_id)
                emoji_votes: dict[str, bool] = {}
                # 两个都点的人算弃权，避免一个人顶两票
                for uid in (yes - no) - excluded:
                    emoji_votes[uid] = True
                for uid in (no - yes) - excluded:
                    emoji_votes[uid] = False
                # 用本轮完整名单替换旧的表情票。成员取消表情后不再出现在
                # yes/no 中，旧票随之消失；指令票仍由 command_votes 保留。
                record.emoji_votes = emoji_votes
                self._merge_votes(record)
                if message := await self._check_threshold(event, record):
                    await event.send(event.plain_result(message))
                    return
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            # 解析/发送结果等非预期错误也只结束轮询，不影响超时结算任务。
            logger.warning(f"{LOG_TAG} 贴表情计票失败 group={record.group_id}: {exc}")

    # ------------------------------------------------------------ 结算

    async def _settle_later(
        self, event: AstrMessageEvent, record: VoteRecord, ttl: int
    ) -> None:
        try:
            await asyncio.sleep(ttl)
        except asyncio.CancelledError:
            return
        if self._votes.get(record.group_id) is not record:
            return
        # 超时结算与最后一轮表情轮询可能同时到达；先原子地认领结算权，
        # 再取消另一条任务，避免重复禁言或重复发送结果。
        if not await self._claim_settlement(record, force=True):
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

    async def _claim_settlement(self, record: VoteRecord, *, force: bool = False) -> bool:
        """原子地认领一场投票的结算权。"""
        async with record.settle_lock:
            if record.settled or self._votes.get(record.group_id) is not record:
                return False
            if not force and record.agree < record.threshold and record.disagree < record.threshold:
                return False
            record.settled = True
            return True

    @staticmethod
    def _merge_votes(record: VoteRecord) -> None:
        """合并两种来源的票；同一用户同时操作时以当前表情票为准。"""
        record.votes = {**record.command_votes, **record.emoji_votes}

    def _finish(self, group_id: str) -> list[asyncio.Task[None]]:
        """从活动表移除投票并取消后台任务，返回实际取消的任务。"""
        record = self._votes.pop(group_id, None)
        if not record:
            return []
        record.settled = True
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        cancelled: list[asyncio.Task[None]] = []
        for task in (record.task, record.poll):
            # 不能取消当前正在执行的结算/轮询任务：CancelledError 会在它
            # 下一次 await 时注入，导致结算结果还没发出去就被吞掉。
            if task and task is not current and not task.done():
                task.cancel()
                cancelled.append(task)
        return cancelled

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
        """插件卸载时取消所有结算与轮询任务。"""
        for group_id in list(self._votes):
            self._finish(group_id)

        # _finish 只处理仍挂在活动记录上的任务；这里再扫一遍追踪集合，
        # 覆盖「任务已经认领结算、记录已弹出，但还在 await 协议端」的窗口。
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        tasks = [task for task in self._tasks if task is not current and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

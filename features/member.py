"""群成员信息与批量清理。

清理群友是本插件破坏性最强的操作，这里做了三重保护：
1. 先出报告图，再等「确认清理」；
2. 同一个群同时只允许跑一个清理任务；
3. 踢人按 safety.batch_interval 节流，并受 safety.max_batch_kick 上限限制。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.message.components import At
from astrbot.core.utils.session_waiter import SessionController, session_waiter

from ..core.config import LOG_TAG
from ..core.group_cache import role_label
from ..core.utils import format_date, get_nickname, parse_int
from .base import Feature

#: 群友信息图片最多渲染多少行，超出只出统计，避免生成超大图片
MAX_LIST_ROWS = 800
#: 等待确认清理的秒数
CONFIRM_TIMEOUT = 60


class MemberFeature(Feature):
    """群友信息 / 清理群友。"""

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        # 正在执行清理任务的群，避免并发重复踢人
        self._clearing: set[str] = set()

    # ------------------------------------------------------------ 群友信息 --- #
    async def member_list(self, event: AstrMessageEvent) -> None:
        """输出群友清单图片。"""
        group_id = event.get_group_id()
        await event.send(event.plain_result("正在整理群友信息，请稍等…"))
        try:
            members = await event.bot.get_group_member_list(group_id=int(group_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 获取群成员列表失败 group={group_id}: {exc}")
            await event.send(event.plain_result(f"获取群成员信息失败：{exc}"))
            return
        members = list(members or [])
        if not members:
            await event.send(event.plain_result("没有拿到任何群成员信息"))
            return

        rows = sorted(members, key=lambda m: parse_int(m.get("join_time"), 0) or 0)
        owners = sum(1 for m in members if m.get("role") == "owner")
        admins = sum(1 for m in members if m.get("role") == "admin")

        lines = [
            f"## 群 {group_id} 成员清单",
            "",
            f"共 **{len(members)}** 人 · 群主 {owners} 人 · 管理员 {admins} 人",
            "",
            "| # | 进群时间 | 等级 | 身份 | QQ | 昵称 |",
            "| --: | --- | --: | --- | --- | --- |",
        ]
        for index, member in enumerate(rows[:MAX_LIST_ROWS], start=1):
            nickname = str(member.get("card") or member.get("nickname") or "（无昵称）")
            lines.append(
                f"| {index} | {format_date(member.get('join_time'))}"
                f" | {parse_int(member.get('level'), 0) or 0}"
                f" | {role_label(member.get('role'))}"
                f" | {member.get('user_id', '')}"
                f" | {nickname.replace('|', '/')} |"
            )
        if len(rows) > MAX_LIST_ROWS:
            lines.append("")
            lines.append(f"> 人数过多，仅显示最早进群的 {MAX_LIST_ROWS} 人。")

        url = await self.to_image("\n".join(lines))
        await event.send(event.image_result(url))
        await self.log(event, "member_list", detail=f"{len(members)} 人")

    # ------------------------------------------------------------ 清理群友 --- #
    def _collect_candidates(
        self, members: list[dict[str, Any]], inactive_days: int, under_level: int
    ) -> list[dict[str, Any]]:
        """筛出「长期不发言且等级低」的普通成员。"""
        threshold = int(time.time()) - inactive_days * 86400
        result: list[dict[str, Any]] = []
        for member in members:
            if member.get("role") in {"owner", "admin"}:
                continue
            last_sent = parse_int(member.get("last_sent_time"), 0) or 0
            level = parse_int(member.get("level"), 0) or 0
            if last_sent < threshold and level < under_level:
                result.append(member)
        result.sort(key=lambda m: parse_int(m.get("last_sent_time"), 0) or 0)
        return result

    async def clear_members(
        self,
        event: AstrMessageEvent,
        inactive_days: Any = 30,
        under_level: Any = 10,
    ) -> None:
        """清理群友。先出报告，再等确认。"""
        group_id = event.get_group_id()
        sender_id = event.get_sender_id()
        days = max(1, parse_int(inactive_days, 30) or 30)
        level_limit = max(0, parse_int(under_level, 10) or 0)

        if group_id in self._clearing:
            await event.send(event.plain_result("本群已有一个清理任务在等待确认，请先处理完"))
            event.stop_event()
            return

        try:
            members = await event.bot.get_group_member_list(group_id=int(group_id))
        except Exception as exc:  # noqa: BLE001
            await event.send(event.plain_result(f"获取群成员信息失败：{exc}"))
            event.stop_event()
            return

        candidates = self._collect_candidates(list(members or []), days, level_limit)
        if not candidates:
            await event.send(event.plain_result("没有符合条件的群友，无需清理"))
            event.stop_event()
            return

        max_kick = max(1, self.config.safety.int("max_batch_kick", 50) or 50)
        truncated = len(candidates) > max_kick
        targets = candidates[:max_kick]

        lines = [
            f"## 待清理群友（{len(targets)} 人）",
            "",
            f"筛选条件：**{days}** 天内未发言，且群等级低于 **{level_limit}** 级",
            "",
            "| 最后发言 | 等级 | QQ | 昵称 |",
            "| --- | --: | --- | --- |",
        ]
        for member in targets:
            nickname = str(member.get("card") or member.get("nickname") or "（无昵称）")
            lines.append(
                f"| {format_date(member.get('last_sent_time'))}"
                f" | {parse_int(member.get('level'), 0) or 0}"
                f" | {member.get('user_id', '')}"
                f" | {nickname.replace('|', '/')} |"
            )
        if truncated:
            lines.append("")
            lines.append(
                f"> 命中 {len(candidates)} 人，超过单次上限 "
                f"{max_kick}，本次只处理前 {max_kick} 人。"
            )
        lines.append("")
        lines.append("### 请回复 **确认清理** 或 **取消清理**")

        url = await self.to_image("\n".join(lines))
        await event.send(event.image_result(url))

        # 默认不 @ 全体候选人，免得刷屏 + 打扰；需要时可在配置里打开
        if self.config.safety.bool("at_targets_on_clear", False):
            await event.send(
                event.chain_result([At(qq=str(m.get("user_id"))) for m in targets])
            )

        self._clearing.add(group_id)
        try:
            await self._wait_confirm(event, group_id, sender_id, targets)
        finally:
            self._clearing.discard(group_id)
            event.stop_event()

    async def _wait_confirm(
        self,
        event: AstrMessageEvent,
        group_id: str,
        sender_id: str,
        targets: list[dict[str, Any]],
    ) -> None:
        feature = self

        @session_waiter(timeout=CONFIRM_TIMEOUT)  # type: ignore[misc]
        async def waiter(controller: SessionController, sub_event: AstrMessageEvent) -> None:
            # 只认同一个群里、同一个人发的确认
            if group_id != sub_event.get_group_id() or sender_id != sub_event.get_sender_id():
                return
            text = (sub_event.message_str or "").strip()
            if text == "取消清理":
                await sub_event.send(sub_event.plain_result("清理任务已取消"))
                controller.stop()
                return
            if text == "确认清理":
                await feature._do_kick(sub_event, group_id, targets)
                controller.stop()

        try:
            await waiter(event)
        except TimeoutError:
            await event.send(event.plain_result("等待确认超时，清理任务已取消"))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"{LOG_TAG} 清理群友任务出错：{exc}")
            await event.send(event.plain_result(f"清理任务出错：{exc}"))

    async def _do_kick(
        self,
        event: AstrMessageEvent,
        group_id: str,
        targets: list[dict[str, Any]],
    ) -> None:
        interval = self.config.safety.float("batch_interval", 0.4)
        success = 0
        failed: list[str] = []
        for index, member in enumerate(targets):
            user_id = str(member.get("user_id") or "")
            if not user_id:
                continue
            if index and interval > 0:
                await asyncio.sleep(interval)
            try:
                await event.bot.set_group_kick(
                    group_id=int(group_id), user_id=int(user_id), reject_add_request=False
                )
            except Exception as exc:  # noqa: BLE001
                name = await get_nickname(event, user_id)
                logger.error(f"{LOG_TAG} 踢出 {user_id} 失败：{exc}")
                failed.append(f"{name}({user_id})")
                continue
            success += 1
        await self.log(
            event,
            "clear_member",
            detail=f"成功 {success} 人，失败 {len(failed)} 人",
            success=not failed,
        )
        summary = [f"清理完成：成功 {success} 人"]
        if failed:
            summary.append(f"失败 {len(failed)} 人：" + "、".join(failed[:10]))
            if len(failed) > 10:
                summary.append(f"（另有 {len(failed) - 10} 人失败，详见日志）")
        await event.send(event.plain_result("\n".join(summary)))

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
from ..core.messaging import send_forward, split_text
from ..core.utils import format_date, get_nickname, md_cell, parse_int, timestamp_seconds
from .base import Feature

#: 群友信息图片最多渲染多少行，超出只出统计，避免生成超大图片
MAX_LIST_ROWS = 800
#: 等待确认清理的秒数
CONFIRM_TIMEOUT = 60


def _member_value(member: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """读取群成员字段，兼容不同协议端的命名。"""
    for key in keys:
        value = member.get(key)
        if value not in (None, ""):
            return value
    return default


def _normalize_members(payload: Any) -> list[dict[str, Any]]:
    """把成员列表响应规整成字典数组，兼容适配器的常见包装。"""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("members", "member_list", "memberList", "list", "items", "data", "result"):
        nested = payload.get(key)
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
        if isinstance(nested, dict):
            found = _normalize_members(nested)
            if found:
                return found
    return []


def _role_of(member: dict[str, Any]) -> str:
    raw = _member_value(member, "role", "member_role", "memberRole", default="")
    numeric = parse_int(raw, None)
    # LLOneBot 的底层群成员枚举是 2=普通成员、3=管理员、4=群主；
    # 它的标准 OneBot 输出会转成字符串，这里同时兼容扩展接口的原值。
    if numeric == 2:
        return "member"
    if numeric == 3:
        return "admin"
    if numeric == 4:
        return "owner"
    return str(raw).strip().lower()


def _is_privileged(member: dict[str, Any]) -> bool:
    return _role_of(member) in {
        "owner",
        "admin",
        "administrator",
        "群主",
        "管理员",
    }


class MemberFeature(Feature):
    """群友信息 / 清理群友。"""

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        # 正在执行清理任务的群，避免并发重复踢人
        self._clearing: set[str] = set()

    @staticmethod
    def _stop(event: AstrMessageEvent) -> None:
        """兼容没有实现 stop_event 的最小适配器 / 测试替身。"""
        stop = getattr(event, "stop_event", None)
        if callable(stop):
            stop()

    async def _send_long_list(self, event: AstrMessageEvent, text: str) -> None:
        """按配置发送长列表：合并转发 → 长图 → 纯文本逐级回退。"""
        mode = self.config.output.str("long_list_mode", "合并转发")
        if mode == "纯文本":
            await event.send(event.plain_result(text))
            return

        if mode == "合并转发" and event.get_group_id():
            blocks = split_text(text, self.config.output.int("node_lines", 15))
            if len(blocks) > 1:
                try:
                    if await send_forward(event, blocks, summary="群成员列表"):
                        return
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"{LOG_TAG} 群成员列表合并转发失败，改用长图：{exc}")

        try:
            url = await self.to_image(text)
            if url:
                await event.send(event.image_result(url))
                return
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 群成员列表渲染失败，回退纯文本：{exc}")
        await event.send(event.plain_result(text))

    # ------------------------------------------------------------ 群友信息 --- #
    async def member_list(self, event: AstrMessageEvent) -> None:
        """输出群友清单图片。"""
        group_id = event.get_group_id()
        if not group_id:
            await event.send(event.plain_result("群友信息只能在群里使用"))
            return
        await event.send(event.plain_result("正在整理群友信息，请稍等…"))
        try:
            members = await event.bot.get_group_member_list(group_id=int(group_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 获取群成员列表失败 group={group_id}: {exc}")
            await event.send(event.plain_result(f"获取群成员信息失败：{exc}"))
            return
        members = _normalize_members(members)
        if not members:
            await event.send(event.plain_result("没有拿到任何群成员信息"))
            return

        rows = sorted(
            members,
            key=lambda m: timestamp_seconds(
                _member_value(m, "join_time", "joinTime"), 0
            )
            or 0,
        )
        owners = sum(1 for m in members if _role_of(m) == "owner")
        admins = sum(1 for m in members if _role_of(m) == "admin")

        lines = [
            f"## 群 {group_id} 成员清单",
            "",
            f"共 **{len(members)}** 人 · 群主 {owners} 人 · 管理员 {admins} 人",
            "",
            "| # | 进群时间 | 等级 | 身份 | QQ | 昵称 |",
            "| --: | --- | --: | --- | --- | --- |",
        ]
        for index, member in enumerate(rows[:MAX_LIST_ROWS], start=1):
            nickname = str(
                _member_value(member, "card", "nickname", "nick", default="（无昵称）")
            )
            lines.append(
                f"| {index} | {format_date(_member_value(member, 'join_time', 'joinTime'))}"
                f" | {parse_int(_member_value(member, 'level'), 0) or 0}"
                f" | {role_label(_member_value(member, 'role'))}"
                f" | {_member_value(member, 'user_id', 'userId', 'uin', default='')}"
                f" | {md_cell(nickname, 24)} |"
            )
        if len(rows) > MAX_LIST_ROWS:
            lines.append("")
            lines.append(f"> 人数过多，仅显示最早进群的 {MAX_LIST_ROWS} 人。")

        await self._send_long_list(event, "\n".join(lines))
        await self.log(event, "member_list", detail=f"{len(members)} 人")

    # ------------------------------------------------------------ 清理群友 --- #
    def _collect_candidates(
        self, members: list[dict[str, Any]], inactive_days: int, under_level: int
    ) -> list[dict[str, Any]]:
        """筛出「长期不发言且等级低」的普通成员。"""
        threshold = int(time.time()) - inactive_days * 86400
        result: list[dict[str, Any]] = []
        for member in members:
            # 缺少身份、等级、最后发言时间或 QQ 号时不纳入候选。清理是破坏性
            # 操作，宁可少清理，也不能把协议端返回不完整的成员误踢出去。
            if _is_privileged(member) or _role_of(member) not in {
                "member",
                "normal",
                "普通成员",
            }:
                continue
            user_id = str(_member_value(member, "user_id", "userId", "uin", default="") or "")
            if not user_id.isdigit():
                continue
            raw_last_sent = _member_value(
                member, "last_sent_time", "lastSentTime", default=None
            )
            raw_level = _member_value(member, "level", "qq_level", "qqLevel", default=None)
            last_sent = timestamp_seconds(raw_last_sent, None)
            level = parse_int(raw_level, None)
            if last_sent is None or level is None:
                continue
            if last_sent < threshold and level < under_level:
                result.append(member)
        result.sort(
            key=lambda m: parse_int(
                timestamp_seconds(
                    _member_value(m, "last_sent_time", "lastSentTime"), 0
                ),
            )
            or 0
        )
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
        if not group_id:
            await event.send(event.plain_result("群友清理只能在群里使用"))
            self._stop(event)
            return
        days = max(1, parse_int(inactive_days, 30) or 30)
        level_limit = max(0, parse_int(under_level, 10) or 0)

        if group_id in self._clearing:
            await event.send(event.plain_result("本群已有一个清理任务在等待确认，请先处理完"))
            self._stop(event)
            return

        # 在第一次网络请求前就占位，避免两个管理员同时发起清理时都通过检查。
        self._clearing.add(group_id)

        try:
            try:
                members = await event.bot.get_group_member_list(group_id=int(group_id))
            except Exception as exc:  # noqa: BLE001
                await event.send(event.plain_result(f"获取群成员信息失败：{exc}"))
                return

            candidates = self._collect_candidates(
                _normalize_members(members),
                days,
                level_limit,
            )
            if not candidates:
                await event.send(event.plain_result("没有符合条件的群友，无需清理"))
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
                nickname = str(
                    _member_value(member, "card", "nickname", "nick", default="（无昵称）")
                )
                lines.append(
                    f"| {format_date(_member_value(member, 'last_sent_time', 'lastSentTime'))}"
                    f" | {parse_int(_member_value(member, 'level'), 0) or 0}"
                    f" | {_member_value(member, 'user_id', 'userId', 'uin', default='')}"
                    f" | {md_cell(nickname, 24)} |"
                )
            if truncated:
                lines.append("")
                lines.append(
                    f"> 命中 {len(candidates)} 人，超过单次上限 "
                    f"{max_kick}，本次只处理前 {max_kick} 人。"
                )
            lines.append("")
            lines.append("### 请回复 **确认清理** 或 **取消清理**")

            await self._send_long_list(event, "\n".join(lines))

            # 默认不 @ 全体候选人，免得刷屏 + 打扰；需要时可在配置里打开
            if self.config.safety.bool("at_targets_on_clear", False):
                await event.send(
                    event.chain_result(
                        [
                            At(
                                qq=str(
                                    _member_value(m, "user_id", "userId", "uin", default="")
                                )
                            )
                            for m in targets
                            if _member_value(m, "user_id", "userId", "uin", default="")
                        ]
                    )
                )

            await self._wait_confirm(event, group_id, sender_id, targets)
        finally:
            self._clearing.discard(group_id)
            self._stop(event)

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
            user_id = str(
                _member_value(member, "user_id", "userId", "uin", default="") or ""
            )
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

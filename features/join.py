"""进群审核、进群欢迎、退群通知。

与上游相比的主要改动：
- 进群申请落库（join_request 表）并分配群内短序号，因此可以直接
  「批准 3」，不必再引用通知消息、从文本里切冒号。
- 引用通知消息的老用法依然兼容。
- 只有一条待办时可以省略序号。
"""

from __future__ import annotations

import re
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..core.config import LOG_TAG
from ..core.protocol import call_action, unwrap
from ..core.utils import (
    apply_delta,
    format_datetime,
    get_nickname,
    get_reply_text,
    list_text,
    parse_bool,
    parse_int,
    split_tokens,
    switch_text,
)
from .base import Feature, rest_of

#: 待办列表里最多展示多少条
MAX_PENDING_SHOWN = 20
#: 待办超过这个秒数就不再参与「只有一条时自动选中」，避免误批陈旧申请
STALE_SECONDS = 7 * 86400

#: 协议端的待审进群列表（NapCat / llbot / SnowLuma 命名不一，依次尝试）
_SYSTEM_MSG_ACTIONS: tuple[str, ...] = ("get_group_system_msg",)
#: 被忽略/未处理的加群通知，作为上面的兜底数据源
_IGNORED_ACTIONS: tuple[str, ...] = (
    "get_group_ignored_notifies",
    "get_group_ignore_add_request",
)
#: 一次对账最多拉多少条
SYNC_LIMIT = 50


class JoinFeature(Feature):
    """进群 / 退群相关的配置指令与事件处理。"""

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        # 防爆破计数，重启清零即可，不必持久化
        self._fail: dict[str, int] = {}

    # ------------------------------------------------------------ 小工具 --- #
    async def _add_block(self, group_id: str, user_id: str) -> None:
        """把用户加进本群进群黑名单。"""
        current = [str(item) for item in (self.store.value(group_id, "block_ids") or [])]
        if user_id not in current:
            current.append(user_id)
            await self.store.set(group_id, "block_ids", current)

    async def _notify(self, event: AstrMessageEvent, group_id: str, text: str) -> None:
        """按 admin_audit 决定私聊超管还是群内发送。"""
        to_admin = bool(self.store.value(group_id, "admin_audit", self.config.admin_audit))
        if not to_admin:
            await event.send(event.plain_result(text))
            return
        admins = self.config.admins_id
        if not admins:
            logger.warning(f"{LOG_TAG} admin_audit 已开启但未配置超管，回退到群内通知")
            await event.send(event.plain_result(text))
            return
        for admin_id in admins:
            try:
                await event.bot.send_private_msg(user_id=int(admin_id), message=text)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"{LOG_TAG} 通知超管 {admin_id} 失败：{exc}")

    # ------------------------------------------------------- 配置类指令 --- #
    async def toggle_review(self, event: AstrMessageEvent, mode: Any = None) -> str:
        group_id = event.get_group_id()
        value = parse_bool(mode)
        if value is None:
            return f"本群进群审核：{switch_text(self.store.value(group_id, 'join_switch'))}"
        await self.store.set(group_id, "join_switch", value)
        await self.log(event, "join_switch", detail=switch_text(value))
        return f"本群进群审核已{switch_text(value)}"

    async def _edit_words(self, event: AstrMessageEvent, field: str, label: str) -> str:
        group_id = event.get_group_id()
        raw = rest_of(event)
        if not raw:
            return f"本群{label}：{list_text(self.store.value(group_id, field))}"
        current = [str(item) for item in (self.store.value(group_id, field) or [])]
        new_words, added, removed = apply_delta(current, split_tokens(raw))
        await self.store.set(group_id, field, new_words)
        await self.log(event, field, detail=" ".join(new_words))
        if not added and not removed:
            return f"本群{label}已设为：{list_text(new_words)}"
        parts = [f"本群{label}已更新"]
        if added:
            parts.append("新增：" + "、".join(added))
        if removed:
            parts.append("移除：" + "、".join(removed))
        parts.append("当前：" + list_text(new_words))
        return "\n".join(parts)

    async def set_accept_words(self, event: AstrMessageEvent) -> str:
        return await self._edit_words(event, "join_accept_words", "进群白词")

    async def set_reject_words(self, event: AstrMessageEvent) -> str:
        return await self._edit_words(event, "join_reject_words", "进群黑词")

    async def toggle_no_match_reject(self, event: AstrMessageEvent, mode: Any = None) -> str:
        group_id = event.get_group_id()
        value = parse_bool(mode)
        if value is None:
            status = switch_text(self.store.value(group_id, "join_no_match_reject"))
            return f"本群「未命中白词自动驳回」：{status}"
        await self.store.set(group_id, "join_no_match_reject", value)
        await self.log(event, "join_no_match_reject", detail=switch_text(value))
        return f"本群「未命中白词自动驳回」已{switch_text(value)}"

    async def set_min_level(self, event: AstrMessageEvent, level: Any = None) -> str:
        group_id = event.get_group_id()
        value = parse_int(level)
        if value is None:
            return f"本群进群等级门槛：{self.store.value(group_id, 'join_min_level')} 级"
        value = max(0, value)
        await self.store.set(group_id, "join_min_level", value)
        await self.log(event, "join_min_level", detail=str(value))
        if value <= 0:
            return "已解除本群的进群等级限制"
        return f"本群进群等级门槛已设为：{value} 级（隐藏等级的用户按人工审核处理）"

    async def set_max_time(self, event: AstrMessageEvent, times: Any = None) -> str:
        group_id = event.get_group_id()
        value = parse_int(times)
        if value is None:
            return f"本群进群可尝试次数：{self.store.value(group_id, 'join_max_time')} 次"
        value = max(0, value)
        await self.store.set(group_id, "join_max_time", value)
        await self.log(event, "join_max_time", detail=str(value))
        if value <= 0:
            return "已解除本群的进群尝试次数限制"
        return f"本群进群尝试次数上限已设为：{value} 次，超出后自动拉黑"

    async def manage_block_ids(self, event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        raw = rest_of(event)
        if not raw:
            return f"本群进群黑名单：{list_text(self.store.value(group_id, 'block_ids'))}"
        current = [str(item) for item in (self.store.value(group_id, "block_ids") or [])]
        tokens = [tok for tok in split_tokens(raw) if re.fullmatch(r"[+-]?\d+", tok)]
        if not tokens:
            return "请给出 QQ 号，例如：进群黑名单 +10001 -10002"
        new_ids, added, removed = apply_delta(current, tokens)
        await self.store.set(group_id, "block_ids", new_ids)
        await self.log(event, "block_ids", detail=" ".join(new_ids))
        parts = ["本群进群黑名单"]
        if added:
            parts.append("新增：" + "、".join(added))
        if removed:
            parts.append("移除：" + "、".join(removed))
        if not added and not removed:
            parts.append("已覆写为：" + list_text(new_ids))
        else:
            parts.append("当前共 " + str(len(new_ids)) + " 人")
        return "\n".join(parts)

    async def set_join_ban(self, event: AstrMessageEvent, seconds: Any = None) -> str:
        group_id = event.get_group_id()
        value = parse_int(seconds)
        if value is None:
            return f"本群进群禁言：{self.store.value(group_id, 'join_ban_time')} 秒"
        value = max(0, value)
        await self.store.set(group_id, "join_ban_time", value)
        await self.log(event, "join_ban_time", detail=str(value))
        if value <= 0:
            return "已关闭本群进群禁言"
        return f"本群进群禁言已设为：{value} 秒"

    async def set_welcome(self, event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        raw = rest_of(event)
        if not raw:
            text = str(self.store.value(group_id, "join_welcome") or "")
            return "本群进群欢迎语：\n" + (text or "（未设置）")
        if raw in {"关", "关闭", "取消", "清空"}:
            await self.store.set(group_id, "join_welcome", "")
            await self.log(event, "join_welcome", detail="清空")
            return "已关闭本群进群欢迎"
        await self.store.set(group_id, "join_welcome", raw)
        await self.log(event, "join_welcome", detail=raw)
        tip = "" if "{nickname}" in raw else "\n提示：欢迎语里写 {nickname} 可自动替换为新成员昵称"
        return f"本群进群欢迎语已设为：\n{raw}{tip}"

    async def toggle_leave_notify(self, event: AstrMessageEvent, mode: Any = None) -> str:
        group_id = event.get_group_id()
        value = parse_bool(mode)
        if value is None:
            return f"本群退群通知：{switch_text(self.store.value(group_id, 'leave_notify'))}"
        await self.store.set(group_id, "leave_notify", value)
        await self.log(event, "leave_notify", detail=switch_text(value))
        return f"本群退群通知已{switch_text(value)}"

    async def toggle_leave_block(self, event: AstrMessageEvent, mode: Any = None) -> str:
        group_id = event.get_group_id()
        value = parse_bool(mode)
        if value is None:
            return f"本群退群拉黑：{switch_text(self.store.value(group_id, 'leave_block'))}"
        await self.store.set(group_id, "leave_block", value)
        await self.log(event, "leave_block", detail=switch_text(value))
        return f"本群退群拉黑已{switch_text(value)}"

    # ------------------------------------------------------- 审核判定 --- #
    async def should_approve(
        self,
        group_id: str,
        user_id: str,
        comment: str | None = None,
        user_level: int | None = None,
    ) -> tuple[bool | None, str]:
        """返回 (是否放行, 原因)。None 表示交人工审核。"""
        block_ids = [str(item) for item in (self.store.value(group_id, "block_ids") or [])]
        if user_id in block_ids:
            return False, "黑名单用户"

        min_level = parse_int(self.store.value(group_id, "join_min_level"), 0) or 0
        if min_level > 0 and user_level is not None and user_level < min_level:
            return False, f"QQ等级过低({user_level}<{min_level})"

        if comment:
            answer = comment.split("\n答案：", 1)[1] if "\n答案：" in comment else comment
            lowered = answer.lower()
            reject_words = [str(w) for w in (self.store.value(group_id, "join_reject_words") or [])]
            for word in reject_words:
                if word and word.lower() in lowered:
                    if parse_bool(self.store.value(group_id, "reject_word_block"), False):
                        await self._add_block(group_id, user_id)
                        return False, f"命中进群黑词「{word}」，已拉黑"
                    return False, f"命中进群黑词「{word}」"
            accept_words = [str(w) for w in (self.store.value(group_id, "join_accept_words") or [])]
            for word in accept_words:
                if word and word.lower() in lowered:
                    return True, f"命中进群白词「{word}」"

        max_fail = parse_int(self.store.value(group_id, "join_max_time"), 3) or 0
        if max_fail > 0:
            key = f"{group_id}_{user_id}"
            self._fail[key] = self._fail.get(key, 0) + 1
            if self._fail[key] >= max_fail:
                await self._add_block(group_id, user_id)
                return False, f"进群尝试次数达上限({max_fail}次)，已拉黑"

        if parse_bool(self.store.value(group_id, "join_no_match_reject"), False):
            return False, "未命中进群关键词"
        return None, "人工审核"

    # --------------------------------------------------- 待办队列（表） --- #
    async def _next_seq(self, group_id: str) -> int:
        row = await self.db.fetch_one(
            "SELECT MAX(seq) AS m FROM join_request WHERE group_id = ? AND status = 'pending'",
            (group_id,),
        )
        current = (row["m"] if row else None) or 0
        return int(current) + 1

    async def _save_request(
        self,
        *,
        group_id: str,
        user_id: str,
        flag: str,
        nickname: str,
        comment: str,
        level: int | None,
    ) -> int:
        """写入/更新待办，返回群内短序号。"""
        existing = await self.db.fetch_one(
            "SELECT seq FROM join_request WHERE flag = ?", (flag,)
        )
        seq = int(existing["seq"] or 0) if existing else 0
        if not seq:
            seq = await self._next_seq(group_id)
        await self.db.execute(
            "INSERT INTO join_request"
            " (flag, group_id, user_id, nickname, comment, level, created_at,"
            "  status, handled_by, seq)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', '', ?)"
            " ON CONFLICT(flag) DO UPDATE SET"
            " nickname=excluded.nickname, comment=excluded.comment, level=excluded.level,"
            " created_at=excluded.created_at, status='pending', handled_by='',"
            " seq=excluded.seq",
            (
                flag,
                group_id,
                user_id,
                nickname,
                comment,
                -1 if level is None else int(level),
                time.time(),
                seq,
            ),
        )
        return seq

    async def _mark_handled(self, flag: str, status: str, handled_by: str) -> None:
        await self.db.execute(
            "UPDATE join_request SET status = ?, handled_by = ? WHERE flag = ?",
            (status, handled_by, flag),
        )

    async def pending(self, group_id: str | None = None) -> list[dict[str, Any]]:
        """待审进群列表，新的在前。"""
        if group_id:
            rows = await self.db.fetch_all(
                "SELECT * FROM join_request WHERE group_id = ? AND status = 'pending'"
                " ORDER BY created_at DESC",
                (group_id,),
            )
        else:
            rows = await self.db.fetch_all(
                "SELECT * FROM join_request WHERE status = 'pending'"
                " ORDER BY created_at DESC"
            )
        return [dict(row) for row in rows]

    # --------------------------------------------------- 与协议端对账 --- #
    async def _remote_pending(
        self, event: AstrMessageEvent, group_id: str
    ) -> tuple[list[dict[str, Any]], str]:
        """拉协议端的待审进群列表，返回（归一化后的条目, 错误提示）。

        首选 get_group_system_msg；协议端没有这个动作时退回「被忽略的加群通知」，
        两者结构基本一致，字段名做了兼容。
        """
        result = await call_action(
            event,
            _SYSTEM_MSG_ACTIONS,
            group_id=int(group_id) if group_id else None,
            only_pending=True,
            count=SYNC_LIMIT,
        )
        if not result.ok:
            result = await call_action(event, _IGNORED_ACTIONS)
        if not result.ok:
            return [], result.error or "协议端不支持查询待审进群列表"
        items = _normalize_requests(result.data)
        if group_id:
            items = [item for item in items if item["group_id"] in ("", group_id)]
        return items, ""

    async def sync_pending(
        self, event: AstrMessageEvent, group_id: str = ""
    ) -> tuple[int, int, str]:
        """把协议端的待审列表和插件待办对账，返回（新增, 关闭, 错误）。

        两种情况会漏账：机器人离线期间进的申请没进过库；申请被别的管理员在手机上
        处理掉了但库里还是 pending。对账后「待审进群」才是真实的。
        """
        gid = str(group_id or event.get_group_id() or "")
        remote, error = await self._remote_pending(event, gid)
        if error:
            return 0, 0, error

        remote_flags = {item["flag"] for item in remote if item["flag"]}
        local = await self.pending(gid or None)
        local_flags = {str(row["flag"]) for row in local}

        added = 0
        for item in remote:
            if not item["flag"] or item["flag"] in local_flags:
                continue
            await self._save_request(
                group_id=item["group_id"] or gid,
                user_id=item["user_id"],
                flag=item["flag"],
                nickname=item["nickname"],
                comment=item["comment"],
                level=None,
            )
            added += 1

        closed = 0
        for row in local:
            if str(row["flag"]) not in remote_flags:
                await self._mark_handled(str(row["flag"]), "expired", "协议端对账")
                closed += 1

        if added or closed:
            logger.info(f"{LOG_TAG} 进群待办对账 group={gid} 新增={added} 关闭={closed}")
        return added, closed, ""

    def _format_pending(self, items: list[dict[str, Any]]) -> str:
        lines = [f"待审进群申请（共 {len(items)} 条）"]
        for item in items[:MAX_PENDING_SHOWN]:
            level = item.get("level", -1)
            level_text = "等级隐藏" if level is None or int(level) < 0 else f"{level} 级"
            lines.append(
                f"[{item['seq']}] {item['nickname']}({item['user_id']})"
                f" · {level_text} · {format_datetime(item['created_at'])}"
            )
            if item.get("comment"):
                lines.append(f"    验证信息：{item['comment']}")
        if len(items) > MAX_PENDING_SHOWN:
            lines.append(f"…… 另有 {len(items) - MAX_PENDING_SHOWN} 条未显示")
        lines.append("用「批准 序号」或「驳回 序号 理由」处理")
        return "\n".join(lines)

    async def list_pending(self, event: AstrMessageEvent) -> str:
        """「待审进群」：先和协议端对账，再列出真实的待办。"""
        group_id = event.get_group_id()
        added, closed, error = await self.sync_pending(event, group_id)
        items = await self.pending(group_id)
        notes: list[str] = []
        if added:
            notes.append(f"补录 {added} 条离线期间的申请")
        if closed:
            notes.append(f"清掉 {closed} 条已在客户端处理过的")
        if not items:
            tail = "（" + "，".join(notes) + "）" if notes else ""
            return f"当前没有待审的进群申请{tail}"
        text = self._format_pending(items)
        if notes:
            text += "\n对账：" + "，".join(notes)
        elif error:
            text += "\n提示：当前协议端不支持待审列表对账，列表可能不含离线期间的申请"
        return text

    # ------------------------------------------------------------ 审批 --- #
    def _flag_from_reply(self, event: AstrMessageEvent) -> str:
        """兼容老用法：引用【进群申请】通知消息。"""
        text = get_reply_text(event) or ""
        if "【进群申请】" not in text:
            return ""
        match = re.search(r"flag[：:]\s*(\S+)", text)
        return match.group(1) if match else ""

    async def _pick_request(
        self, group_id: str, tokens: list[str]
    ) -> tuple[dict[str, Any] | None, list[str], str]:
        """从参数里挑出目标申请，返回 (申请, 剩余参数, 错误提示)。"""
        items = await self.pending(group_id)
        if tokens and tokens[0].isdigit():
            token = tokens[0]
            for item in items:
                if str(item["seq"]) == token:
                    return item, tokens[1:], ""
            for item in items:
                if str(item["user_id"]) == token:
                    return item, tokens[1:], ""
            # 纯数字但对不上：可能只是理由的一部分，继续走下面的兜底
        if not items:
            return None, tokens, "当前没有待审的进群申请"
        if len(items) == 1 and time.time() - float(items[0]["created_at"]) < STALE_SECONDS:
            return items[0], tokens, ""
        return None, tokens, self._format_pending(items)

    async def _send_approval(
        self, event: AstrMessageEvent, flag: str, agree: bool, reason: str
    ) -> None:
        """同意/拒绝一条申请。

        主动申请是 sub_type=add，别人邀请进群是 sub_type=invite。上游只发 add，
        导致邀请类申请永远处理失败，这里在 add 失败后再按 invite 试一次。
        """
        last: Exception | None = None
        for sub_type in ("add", "invite"):
            try:
                await event.bot.set_group_add_request(
                    flag=flag, sub_type=sub_type, approve=agree, reason=reason
                )
                return
            except Exception as exc:  # noqa: BLE001 - 换另一种 sub_type 再试
                last = exc
                logger.debug(f"{LOG_TAG} 审批失败 flag={flag} sub_type={sub_type}: {exc}")
        raise last if last else RuntimeError("处理进群申请失败")

    async def handle_approval(
        self, event: AstrMessageEvent, agree: bool, extra: str = ""
    ) -> str:
        """处理「批准」/「驳回」。"""
        group_id = event.get_group_id()
        tokens = split_tokens(extra)
        flag = self._flag_from_reply(event)
        record: dict[str, Any] | None = None
        if flag:
            row = await self.db.fetch_one("SELECT * FROM join_request WHERE flag = ?", (flag,))
            record = dict(row) if row else None
        else:
            record, tokens, error = await self._pick_request(group_id, tokens)
            if record is None:
                return error
            flag = str(record["flag"])
        if not flag:
            return "未能定位到进群申请，试试「待审进群」查看列表"

        reason = " ".join(tokens).strip()
        nickname = str(record["nickname"]) if record else "该用户"
        target_id = str(record["user_id"]) if record else ""
        try:
            await self._send_approval(event, flag, agree, reason)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"{LOG_TAG} 处理进群申请失败 flag={flag}: {exc}")
            await self._mark_handled(flag, "expired", event.get_sender_id())
            await self.log(
                event,
                "join_approve" if agree else "join_reject",
                target_id=target_id,
                detail=str(exc),
                success=False,
            )
            return "处理失败：这条申请可能已经被处理过或已过期"

        await self._mark_handled(
            flag, "approved" if agree else "rejected", event.get_sender_id()
        )
        await self.log(
            event,
            "join_approve" if agree else "join_reject",
            target_id=target_id,
            detail=reason or ("同意" if agree else "拒绝"),
        )
        if agree:
            self._fail.pop(f"{group_id}_{target_id}", None)
            return f"已同意 {nickname}({target_id}) 进群"
        suffix = f"\n理由：{reason}" if reason else ""
        return f"已拒绝 {nickname}({target_id}) 进群{suffix}"

    # ------------------------------------------------------- 事件监听 --- #
    async def event_monitoring(self, event: AstrMessageEvent) -> str | None:
        """监听进群申请 / 进群 / 退群。返回需要在群里说的话。"""
        raw = getattr(event.message_obj, "raw_message", None)
        if not isinstance(raw, dict):
            return None
        group_id = str(raw.get("group_id") or "")
        if not group_id:
            return None
        user_id = str(raw.get("user_id") or "")
        post_type = raw.get("post_type")
        notice_type = raw.get("notice_type")

        if post_type == "request" and raw.get("request_type") == "group":
            if raw.get("sub_type") != "add":
                return None
            await self._handle_join_request(event, raw, group_id, user_id)
            return None

        if (
            post_type == "notice"
            and notice_type == "group_decrease"
            and raw.get("sub_type") == "leave"
        ):
            if not parse_bool(self.store.value(group_id, "leave_notify"), False):
                return None
            nickname = await get_nickname(event, user_id)
            message = f"{nickname}({user_id}) 主动退群了"
            if parse_bool(self.store.value(group_id, "leave_block"), False):
                await self._add_block(group_id, user_id)
                message += "，已加入进群黑名单"
            await self.audit.record(
                group_id=group_id,
                action="leave",
                target_id=user_id,
                detail=message,
                source="event",
            )
            return message

        if notice_type == "group_increase" and user_id != str(event.get_self_id()):
            welcome = str(self.store.value(group_id, "join_welcome") or "")
            reply: str | None = None
            if welcome:
                nickname = await get_nickname(event, user_id)
                try:
                    reply = welcome.format(nickname=nickname)
                except (KeyError, IndexError, ValueError):
                    # 欢迎语里写了不支持的占位符，原样发出去而不是报错
                    reply = welcome
            ban_time = parse_int(self.store.value(group_id, "join_ban_time"), 0) or 0
            if ban_time > 0:
                try:
                    await event.bot.set_group_ban(
                        group_id=int(group_id), user_id=int(user_id), duration=ban_time
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"{LOG_TAG} 进群禁言失败 group={group_id}: {exc}")
            return reply
        return None

    async def _handle_join_request(
        self,
        event: AstrMessageEvent,
        raw: dict[str, Any],
        group_id: str,
        user_id: str,
    ) -> None:
        if not parse_bool(self.store.value(group_id, "join_switch"), True):
            return
        comment = str(raw.get("comment") or "")
        flag = str(raw.get("flag") or "")
        nickname = "未知昵称"
        level: int | None = None
        try:
            info = await event.bot.get_stranger_info(user_id=int(user_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 获取陌生人信息失败 user={user_id}: {exc}")
            info = {}
        if info:
            nickname = str(info.get("nickname") or nickname)
            if not info.get("isHideQQLevel"):
                level = parse_int(info.get("qqLevel") or info.get("level"))

        approve, reason = await self.should_approve(group_id, user_id, comment, level)
        if approve is True:
            self._fail.pop(f"{group_id}_{user_id}", None)

        seq = await self._save_request(
            group_id=group_id,
            user_id=user_id,
            flag=flag,
            nickname=nickname,
            comment=comment,
            level=level,
        )

        auto_text = ""
        if approve is not None:
            try:
                await event.bot.set_group_add_request(
                    flag=flag,
                    sub_type="add",
                    approve=approve,
                    reason="" if approve else reason,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"{LOG_TAG} 自动审核失败 flag={flag}: {exc}")
                await self._mark_handled(flag, "expired", "auto")
                return
            await self._mark_handled(flag, "approved" if approve else "rejected", "auto")
            await self.audit.record(
                group_id=group_id,
                action="join_approve" if approve else "join_reject",
                operator_id="auto",
                operator_name="自动审核",
                target_id=user_id,
                detail=reason,
                source="event",
            )
            if not approve and reason.startswith("黑名单用户"):
                return
            auto_text = f"自动{'批准' if approve else '驳回'}：{reason}"

        lines = ["【进群申请】" if auto_text else "【进群申请】待处理"]
        lines.append(f"昵称：{nickname}")
        lines.append(f"QQ：{user_id}")
        lines.append(f"flag：{flag}")
        if level is not None:
            lines.append(f"等级：{level}")
        if comment:
            lines.append(comment)
        if auto_text:
            lines.append(auto_text)
        else:
            lines.append(f"处理：批准 {seq} / 驳回 {seq} 理由")
        await self._notify(event, group_id, "\n".join(lines))

    async def purge_requests(self, retain_days: int = 30) -> int:
        """清理已处理的历史申请，返回删除条数。"""
        cutoff = time.time() - max(1, retain_days) * 86400
        rows = await self.db.fetch_all(
            "SELECT COUNT(*) AS c FROM join_request"
            " WHERE status != 'pending' AND created_at < ?",
            (cutoff,),
        )
        removed = int(rows[0]["c"]) if rows else 0
        if removed:
            await self.db.execute(
                "DELETE FROM join_request WHERE status != 'pending' AND created_at < ?",
                (cutoff,),
            )
        return removed


def _normalize_requests(payload: Any) -> list[dict[str, Any]]:
    """把各协议端的待审进群响应统一成 [{flag, group_id, user_id, nickname, comment}]。

    NapCat 返回 {"join_requests": [...], "InvitedRequest": [...]}，llbot / SnowLuma
    直接返回数组，字段名有 requester_uin / requester_nick / message 等多种写法。
    已经处理过（checked=true）的条目会被丢掉。
    """
    raw = unwrap(payload)
    entries: list[Any] = []
    if isinstance(raw, list):
        entries = raw
    elif isinstance(raw, dict):
        for value in raw.values():
            if isinstance(value, list):
                entries.extend(value)

    items: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("checked"):
            continue
        flag = str(
            entry.get("flag")
            or entry.get("request_id")
            or entry.get("requestId")
            or entry.get("seq")
            or ""
        ).strip()
        user_id = str(
            entry.get("requester_uin")
            or entry.get("user_id")
            or entry.get("requesterUin")
            or entry.get("uin")
            or ""
        ).strip()
        if not flag or not user_id:
            continue
        nickname = str(
            entry.get("requester_nick")
            or entry.get("nickname")
            or entry.get("requesterNick")
            or ""
        ).strip()
        comment = str(entry.get("message") or entry.get("comment") or "").strip()
        invitor = str(entry.get("invitor_uin") or entry.get("invitorUin") or "").strip()
        if invitor and invitor != "0":
            invitor_nick = str(entry.get("invitor_nick") or "").strip() or invitor
            comment = f"由 {invitor_nick} 邀请入群" + (f"｜{comment}" if comment else "")
        items.append(
            {
                "flag": flag,
                "group_id": str(entry.get("group_id") or entry.get("groupId") or "").strip(),
                "user_id": user_id,
                "nickname": nickname or user_id,
                "comment": comment,
            }
        )
    return items

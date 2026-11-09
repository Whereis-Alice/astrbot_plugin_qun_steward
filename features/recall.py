"""消息撤回。"""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..core.config import LOG_TAG
from ..core.permission import PermLevel
from ..core.protocol import as_dict, extract_failure
from ..core.utils import get_ats, get_reply_message_id, parse_int
from .base import Feature

#: 一次最多扫描的历史消息条数，避免把整群历史全拉下来
MAX_SCAN = 200
DEFAULT_SCAN = 10


class RecallFeature(Feature):
    """撤回消息：引用撤回单条，或 @某人 批量撤回其近期消息。"""

    async def recall(self, event: AstrMessageEvent) -> str:
        message_id = get_reply_message_id(event)
        if message_id:
            return await self._recall_one(event, message_id)
        return await self._recall_recent(event)

    async def recall_message(self, event: AstrMessageEvent, message_id: Any = "") -> str:
        """按 ID 撤回单条消息；工具入口已校验平台、群和配置中的最低权限。"""
        if message_id in (None, ""):
            message_id = get_reply_message_id(event)
        if message_id in (None, ""):
            return "未指定消息 ID，也没有引用消息。请提供目标消息的真实 message_id，不能猜测 ID。"
        # OneBot 的消息 ID 可能是有符号哈希，负数也是合法值。
        mid = None if isinstance(message_id, bool) else parse_int(message_id)
        if mid is None or mid == 0:
            return "消息 ID 必须是非零整数，可使用协议端提供的负数 ID。"
        try:
            payload = await event.bot.get_msg(message_id=mid)
        except Exception as exc:  # noqa: BLE001
            return await self._reject_tool_recall(event, mid, f"获取目标消息失败：{exc}")
        if failure := extract_failure(payload):
            return await self._reject_tool_recall(event, mid, f"获取目标消息失败：{failure}")

        message = as_dict(payload)
        group_id = parse_int(message.get("group_id"))
        if str(message.get("message_type") or "").lower() == "private":
            return await self._reject_tool_recall(event, mid, "只能撤回当前 QQ 群的消息，不能撤回私聊消息。")
        if group_id is None or group_id <= 0:
            return await self._reject_tool_recall(event, mid, "无法确认目标消息所属的群，未执行撤回。")
        if group_id != parse_int(event.get_group_id()):
            return await self._reject_tool_recall(event, mid, "目标消息不属于当前群，不能跨群撤回。")
        if "message_id" in message and parse_int(message["message_id"]) != mid:
            return await self._reject_tool_recall(event, mid, "协议端返回的消息 ID 不匹配，未执行撤回。")

        sender = as_dict(message.get("sender"))
        author_id = sender.get("user_id") or message.get("user_id")
        author = None if isinstance(author_id, bool) else parse_int(author_id)
        if author is None or author <= 0:
            return await self._reject_tool_recall(event, mid, "无法确认目标消息的作者，未执行撤回。")
        if reason := await self._check_recall_author(event, str(author)):
            return await self._reject_tool_recall(event, mid, reason)
        if message.get("status") == "deleted":
            return "该消息已经撤回，无需重复操作。"

        failure = await self._recall_one(event, str(mid), source="llm")
        return failure or f"已撤回消息 {mid}。"

    async def _reject_tool_recall(self, event: AstrMessageEvent, mid: int, reason: str) -> str:
        await self.log(event, "recall", target_id=str(mid), detail=reason, success=False, source="llm")
        return reason

    async def _check_recall_author(self, event: AstrMessageEvent, author_id: str) -> str | None:
        """插件调用权限与 QQ 实际撤回权限分开判断，不接受模型提供的作者身份。"""
        if author_id == str(event.get_self_id()):
            return None  # 机器人撤回自己发的消息不要求群管理员身份。
        bot_level = await self.permissions.self_level(event)
        if bot_level is PermLevel.UNKNOWN:
            return "无法确认机器人的群身份，未执行撤回。"
        if bot_level < PermLevel.ADMIN:
            return "机器人不是群管理员，只能撤回机器人自己发送的消息。"
        author_level = await self.permissions.group_level_of(event, author_id)
        if author_level is PermLevel.UNKNOWN:
            return "无法确认消息作者的群身份，未执行撤回。"
        if author_level is PermLevel.OWNER:
            return "消息作者是群主，机器人无法撤回群主的消息。"
        if bot_level is PermLevel.ADMIN and author_level is PermLevel.ADMIN:
            return "机器人和消息作者都是管理员，无法撤回；只有群主可以撤回管理员的消息。"
        if author_id == str(event.get_sender_id()):
            return None  # 普通成员可以请求撤回自己发的消息。
        caller_level = await self.permissions.level_of(event, event.get_sender_id())
        if caller_level < PermLevel.ADMIN:
            return "你只能请求撤回自己或机器人的消息；撤回其他群员的消息需要管理员权限。"
        protected_level = await self.permissions.level_of(event, author_id)
        if protected_level is PermLevel.UNKNOWN:
            return "无法确认消息作者的权限，未执行撤回。"
        if caller_level is not PermLevel.SUPERUSER and caller_level <= protected_level:
            return "你无权撤回同级或更高身份成员的消息。"
        return None

    async def _recall_one(
        self, event: AstrMessageEvent, message_id: str, *, source: str = "command"
    ) -> str:
        try:
            response = await event.bot.delete_msg(message_id=int(message_id))
            if failure := extract_failure(response):
                raise RuntimeError(failure)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 撤回失败 message_id={message_id}: {exc}")
            await self.log(
                event, "recall", target_id=message_id, detail=str(exc), success=False, source=source
            )
            return f"撤回失败：{exc}。可能是权限不足、消息已撤回或超出 QQ 允许的撤回时限。"
        await self.log(
            event, "recall", target_id=message_id,
            detail="指定消息撤回" if source == "llm" else "引用撤回", source=source
        )
        return ""

    async def _recall_recent(self, event: AstrMessageEvent) -> str:
        targets = {str(uid) for uid in (get_ats(event) or [event.get_self_id()])}
        parts = (event.message_str or "").split()
        tail = parts[-1] if parts else ""
        scan = int(tail) if tail.isdigit() else DEFAULT_SCAN
        scan = max(1, min(scan, MAX_SCAN))

        client = event.bot
        try:
            payload = await client.api.call_action(
                "get_group_msg_history",
                group_id=int(event.get_group_id()),
                message_seq=0,
                count=scan,
                reverseOrder=True,
            )
        except Exception as exc:  # noqa: BLE001
            return f"获取历史消息失败：{exc}"

        messages = (payload or {}).get("messages") or []
        candidates = [
            msg
            for msg in reversed(messages)
            if isinstance(msg, dict)
            and str((msg.get("sender") or {}).get("user_id") or "") in targets
        ]
        if not candidates:
            return f"最近 {scan} 条消息里没有找到目标用户的发言"

        semaphore = asyncio.Semaphore(10)
        deleted = 0

        async def delete_one(message: dict) -> None:
            nonlocal deleted
            async with semaphore:
                try:
                    await client.delete_msg(message_id=message.get("message_id"))
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"{LOG_TAG} 撤回单条失败: {exc}")
                else:
                    deleted += 1

        await asyncio.gather(*(delete_one(msg) for msg in candidates))
        await self.log(
            event,
            "recall",
            target_id=",".join(sorted(targets)),
            detail=f"扫描{scan}条，撤回{deleted}条",
        )
        return f"已从最近 {scan} 条消息中撤回 {deleted} 条"

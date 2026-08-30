"""消息撤回。"""

from __future__ import annotations

import asyncio

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..core.config import LOG_TAG
from ..core.utils import get_ats, get_reply_message_id
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

    async def _recall_one(self, event: AstrMessageEvent, message_id: str) -> str:
        try:
            await event.bot.delete_msg(message_id=int(message_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 撤回失败 message_id={message_id}: {exc}")
            await self.log(event, "recall", target_id=message_id, detail=str(exc), success=False)
            return "我无权撤回这条消息"
        await self.log(event, "recall", target_id=message_id, detail="引用撤回")
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

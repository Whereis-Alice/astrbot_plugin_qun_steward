"""群公告发布与查看。"""

from __future__ import annotations

import textwrap
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..core.config import LOG_TAG
from ..core.utils import download_file, extract_image_url, format_datetime
from .base import Feature, rest_of


class NoticeFeature(Feature):
    """群公告。修复了上游「纯文字公告不会真的发出去」的问题。"""

    async def publish(
        self,
        event: AstrMessageEvent,
        content: str = "",
        image_url: str | None = None,
    ) -> str:
        text = (content or rest_of(event)).strip()
        if not text:
            return "未指定群公告内容"

        group_id = event.get_group_id()
        url = image_url or extract_image_url(event)
        image_path = ""
        if url:
            filename = f"{group_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            saved = await download_file(url, self.config.notice_dir / filename)
            if not saved:
                return "公告配图下载失败，请稍后重试"
            image_path = str(saved)

        payload: dict[str, object] = {"group_id": int(group_id), "content": text}
        if image_path:
            payload["image"] = image_path

        try:
            # 无论有没有配图都要真正调用接口（上游只在有图时才发）
            await event.bot.api.call_action("_send_group_notice", **payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 发布群公告失败 group={group_id}: {exc}")
            await self.log(event, "notice", detail=str(exc), success=False)
            return f"群公告发布失败：{exc}"

        await self.log(event, "notice", detail=text[:120] + ("…" if len(text) > 120 else ""))
        return "群公告已发布" + ("（含配图）" if image_path else "")

    async def view(self, event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        try:
            payload = await event.bot.api.call_action(
                "_get_group_notice", group_id=int(group_id)
            )
        except Exception as exc:  # noqa: BLE001
            return f"获取群公告失败：{exc}"

        notices = payload if isinstance(payload, list) else (payload or {}).get("data") or []
        blocks: list[str] = []
        for notice in notices:
            if not isinstance(notice, dict):
                continue
            sender_id = notice.get("sender_id", "")
            when = format_datetime(notice.get("publish_time"))
            message = notice.get("message")
            text = ""
            if isinstance(message, dict):
                text = str(message.get("text") or "")
            elif isinstance(message, str):
                text = message
            text = text.replace("&#10;", "\n\n")
            blocks.append(
                f"【{when}-{sender_id}】\n\n{textwrap.indent(text, '    ')}"
            )
        return "\n\n\n".join(blocks) or "当前群没有群公告"

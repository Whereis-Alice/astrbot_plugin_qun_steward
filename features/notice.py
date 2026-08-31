"""群公告的发布、查看与删除。"""

from __future__ import annotations

import textwrap
from datetime import datetime
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..core.config import LOG_TAG
from ..core.protocol import as_list, call_action
from ..core.utils import download_file, extract_image_url, format_datetime, parse_int
from .base import Feature, rest_of

#: 发布 / 查看 / 删除群公告的动作名（各端都沿用带下划线前缀的 go-cqhttp 扩展名）
_PUBLISH_ACTIONS: tuple[str, ...] = ("_send_group_notice",)
_LIST_ACTIONS: tuple[str, ...] = ("_get_group_notice",)
_DELETE_ACTIONS: tuple[str, ...] = ("_del_group_notice",)


def _notice_text(notice: dict[str, Any]) -> str:
    """公告正文可能是字符串，也可能是 {text: ...} 结构。"""
    message = notice.get("message")
    text = ""
    if isinstance(message, dict):
        text = str(message.get("text") or "")
    elif isinstance(message, str):
        text = message
    return text.replace("&#10;", "\n\n")


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

        # 无论有没有配图都要真正调用接口（上游只在有图时才发）
        result = await call_action(
            event,
            _PUBLISH_ACTIONS,
            group_id=int(group_id),
            content=text,
            image=image_path or None,
        )
        if not result.ok:
            logger.warning(f"{LOG_TAG} 发布群公告失败 group={group_id}: {result.error}")
            await self.log(event, "notice", detail=result.error, success=False)
            return f"群公告发布失败：{result.error}"

        await self.log(event, "notice", detail=text[:120] + ("…" if len(text) > 120 else ""))
        return "群公告已发布" + ("（含配图）" if image_path else "")

    async def _notices(self, event: AstrMessageEvent) -> tuple[list[dict[str, Any]], str]:
        """拉取公告列表，返回 (公告列表, 错误说明)。"""
        group_id = event.get_group_id()
        result = await call_action(event, _LIST_ACTIONS, group_id=int(group_id))
        if not result.ok:
            return [], result.error
        notices = [item for item in as_list(result.data) if isinstance(item, dict)]
        return notices, ""

    async def view(self, event: AstrMessageEvent) -> str:
        notices, error = await self._notices(event)
        if error:
            return f"获取群公告失败：{error}"

        blocks: list[str] = []
        for index, notice in enumerate(notices, 1):
            sender_id = notice.get("sender_id", "")
            when = format_datetime(notice.get("publish_time"))
            tag = "（新成员公告）" if notice.get("send_to_new_members") else ""
            body = textwrap.indent(_notice_text(notice), "    ")
            blocks.append(f"{index}. 【{when}-{sender_id}】{tag}\n\n{body}")
        if not blocks:
            return "当前群没有群公告"
        return "\n\n\n".join(blocks) + "\n\n提示：可用「删除群公告 序号」删掉其中一条"

    async def delete(self, event: AstrMessageEvent, index: Any = "") -> str:
        """按「群公告」列表里的序号删除一条公告。"""
        position = parse_int(index, 0) or 0
        if position <= 0:
            return "请指定要删除的公告序号，例如「删除群公告 1」（序号见「群公告」）"

        notices, error = await self._notices(event)
        if error:
            return f"获取群公告失败：{error}"
        if position > len(notices):
            return f"本群只有 {len(notices)} 条公告，找不到第 {position} 条"

        notice = notices[position - 1]
        notice_id = str(notice.get("notice_id") or notice.get("noticeId") or "")
        fid = str(notice.get("fid") or notice.get("notice_id") or "")
        if not notice_id and not fid:
            return "这条公告没有可用的标识，无法删除"

        group_id = event.get_group_id()
        result = await call_action(
            event,
            _DELETE_ACTIONS,
            group_id=int(group_id),
            notice_id=notice_id or None,
            fid=fid or None,
        )
        summary = _notice_text(notice).strip().splitlines()
        preview = summary[0][:30] if summary else ""
        if not result.ok:
            await self.log(event, "notice_del", detail=result.error, success=False)
            return f"删除群公告失败：{result.error}"

        await self.log(event, "notice_del", detail=preview or f"第 {position} 条")
        return f"已删除第 {position} 条群公告" + (f"：{preview}" if preview else "")

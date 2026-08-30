"""精华消息：设精、移精、查看群精华。"""

from __future__ import annotations

from typing import Any

from astrbot.api.event import AstrMessageEvent

from ..core.utils import format_datetime, get_reply_message_id
from .base import Feature


def _plain_text(content: Any) -> str:
    """把精华消息的 content 字段压成一行可读文本。"""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for seg in content:
        if not isinstance(seg, dict):
            continue
        seg_type = str(seg.get("type") or "")
        data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
        if seg_type == "text":
            parts.append(str(data.get("text") or "").strip())
        elif seg_type == "image":
            parts.append("[图片]")
        elif seg_type == "face":
            parts.append("[表情]")
        elif seg_type == "at":
            parts.append(f"@{data.get('qq', '')}")
        elif seg_type == "video":
            parts.append("[视频]")
        elif seg_type == "record":
            parts.append("[语音]")
        elif seg_type == "file":
            parts.append("[文件]")
        elif seg_type == "forward":
            parts.append("[合并转发]")
        else:
            parts.append(f"[{seg_type or '未知'}]")
    return " ".join(p for p in parts if p)


class EssenceFeature(Feature):
    """精华消息管理。"""

    async def set_essence(
        self, event: AstrMessageEvent, enable: bool, message_id: Any = ""
    ) -> str:
        mid = str(message_id or "") or (get_reply_message_id(event) or "")
        if not mid:
            return "请引用要操作的消息，再发送该指令"
        try:
            if enable:
                await event.bot.set_essence_msg(message_id=int(mid))
            else:
                await event.bot.delete_essence_msg(message_id=int(mid))
        except Exception as exc:  # noqa: BLE001
            action = "essence_add" if enable else "essence_del"
            await self.log(event, action, target_id=mid, detail=str(exc), success=False)
            return f"操作失败：{exc}"
        await self.log(event, "essence_add" if enable else "essence_del", target_id=mid)
        return "已设为精华消息" if enable else "已取消精华消息"

    async def list_essence(self, event: AstrMessageEvent) -> str:
        """返回排版好的群精华列表（Markdown，交给 text_to_image 渲染）。"""
        group_id = event.get_group_id()
        try:
            payload = await event.bot.get_essence_msg_list(group_id=int(group_id))
        except Exception as exc:  # noqa: BLE001
            return f"获取群精华失败：{exc}"

        items = payload if isinstance(payload, list) else (payload or {}).get("data") or []
        if not items:
            return "没有群精华消息"

        lines = [f"## 群精华（共 {len(items)} 条）", ""]
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            sender = str(item.get("sender_nick") or item.get("sender_id") or "未知")
            sender_id = item.get("sender_id") or ""
            operator = str(item.get("operator_nick") or item.get("operator_id") or "未知")
            when = format_datetime(item.get("sender_time") or item.get("operator_time"))
            text = _plain_text(item.get("content")) or "（无文字内容）"
            lines.append(f"**{index}. {sender}**（{sender_id}）")
            lines.append(f"- 时间：{when}")
            lines.append(f"- 设精人：{operator}")
            lines.append(f"- 内容：{text}")
            lines.append("")
        return "\n".join(lines)

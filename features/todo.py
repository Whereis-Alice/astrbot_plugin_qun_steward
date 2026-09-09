"""群待办：实时查询、设置、完成与取消。

群待办的唯一可靠引用是协议端返回的 message_id。列表里的展示序号只是给人
看的短编号，完成 / 取消时会先重新拉取列表，再把序号映射回真实消息 ID，避免
列表变化后误操作另一条消息。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from astrbot.api.event import AstrMessageEvent

from ..core.card import Card, Note, Stat, Stats, Table
from ..core.protocol import call_action, explain_action_error, unwrap
from ..core.utils import format_datetime, get_reply_message_id, md_cell, parse_int
from .base import Feature

#: SnowLuma、部分 NapCat 扩展使用的群待办接口。
_LIST_ACTIONS: tuple[str, ...] = ("get_group_todo_list",)
_SET_ACTIONS: tuple[str, ...] = ("set_group_todo",)
_COMPLETE_ACTIONS: tuple[str, ...] = ("complete_group_todo",)
_CANCEL_ACTIONS: tuple[str, ...] = ("cancel_group_todo",)

#: 列表太大时只取前面一部分，避免协议端异常数据拖垮回复。
MAX_TODOS = 100

_LIST_KEYS: tuple[str, ...] = (
    "group_todo_list",
    "groupTodoList",
    "todo_list",
    "todoList",
    "todos",
    "list",
    "items",
    "result",
    "data",
    "messages",
)

_MESSAGE_ID_KEYS: tuple[str, ...] = ("message_id", "messageId", "msg_id", "msgId")


def _first(item: dict[str, Any], keys: Iterable[str]) -> Any:
    """返回第一个非空字段。"""
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None


def _message_text(value: Any) -> str:
    """把待办中的消息摘要拍平为可读文本。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        direct = _first(value, ("text", "content", "message"))
        if direct is not None and direct is not value:
            return _message_text(direct)
        data = value.get("data")
        if data is not value:
            return _message_text(data)
        return ""
    if not isinstance(value, list):
        return ""

    chunks: list[str] = []
    for segment in value:
        if isinstance(segment, str):
            chunks.append(segment)
            continue
        if not isinstance(segment, dict):
            continue
        data = segment.get("data")
        if isinstance(data, dict):
            text = _first(data, ("text", "content"))
        else:
            text = _first(segment, ("text", "content", "message"))
        if text is not None:
            chunks.append(str(text))
    return "".join(chunks).strip()


def _raw_todos(payload: Any) -> list[Any]:
    """从常见的 OneBot 包装中找出待办数组。"""
    value = unwrap(payload)
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return []
    for key in _LIST_KEYS:
        nested = value.get(key)
        if isinstance(nested, list):
            return nested
        if isinstance(nested, dict):
            found = _raw_todos(nested)
            if found:
                return found
    return []


def normalize_todos(payload: Any) -> list[dict[str, Any]]:
    """统一待办字段，兼容 snake_case、camelCase 与不同的摘要结构。

    没有消息 ID 的条目无法被完成或取消，直接忽略，避免给用户展示一个实际上
    无法操作的假序号。
    """
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in _raw_todos(payload):
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        message_id = _first(item, _MESSAGE_ID_KEYS)
        if isinstance(message_id, dict):
            message_id = _first(message_id, _MESSAGE_ID_KEYS)
        if message_id is None and isinstance(item.get("message"), dict):
            message_id = _first(item["message"], _MESSAGE_ID_KEYS)
        message_id = str(message_id or "").strip()
        if not message_id or message_id in seen:
            continue

        text = _first(item, ("text", "message_text", "messageText", "summary", "title"))
        if text is None:
            text = _message_text(item.get("message"))
        elif not isinstance(text, str):
            text = _message_text(text)
        text = str(text or "").strip()

        item.update(
            {
                "message_id": message_id,
                "message_seq": _first(item, ("message_seq", "messageSeq", "sequence", "seq")),
                "message_random": _first(
                    item, ("message_random", "messageRandom", "random")
                ),
                "text": text,
                "create_time": _first(item, ("create_time", "createTime", "created_at", "createdAt")),
                "update_time": _first(item, ("update_time", "updateTime", "updated_at", "updatedAt")),
            }
        )
        normalized.append(item)
        seen.add(message_id)
        if len(normalized) >= MAX_TODOS:
            break
    return normalized


def _message_param(message_id: Any) -> int | str:
    """SnowLuma 的 message_id 是整数；保留非数字 ID 兼容其它实现。"""
    text = str(message_id or "").strip()
    parsed = parse_int(text, None)
    return parsed if parsed is not None else text


def _todo_label(item: dict[str, Any]) -> str:
    text = str(item.get("text") or "").strip()
    if text:
        return md_cell(text, 90)
    return "（无文本摘要，可引用原消息查看）"


class TodoFeature(Feature):
    """群待办业务。"""

    async def _fetch(
        self, event: AstrMessageEvent
    ) -> tuple[list[dict[str, Any]], str]:
        group_id = event.get_group_id()
        if not group_id:
            return [], "请在群里使用该指令"
        result = await call_action(
            event, _LIST_ACTIONS, group_id=int(group_id)
        )
        if not result.ok:
            return [], explain_action_error(result, "群待办")
        return normalize_todos(result.data), ""

    async def show(self, event: AstrMessageEvent) -> str | Card:
        """实时读取并展示当前群待办。"""
        todos, error = await self._fetch(event)
        if error:
            return f"获取群待办失败：{error}"
        if not todos:
            return "本群当前没有群待办"

        group_id = event.get_group_id()
        card = Card(
            title="群待办",
            subtitle=f"群号 {group_id} · 实时查询",
            badge="待办",
            footer="完成 / 取消请使用此列表中的序号",
        )
        card.add(Stats(items=[Stat(label="待办数量", value=str(len(todos)), tone="brand")]))

        rows: list[list[str]] = []
        for index, item in enumerate(todos, 1):
            created = item.get("create_time")
            when = format_datetime(created) if created else "未知时间"
            rows.append([str(index), _todo_label(item), when])
        card.add(Table(headers=["序号", "事项", "创建时间"], rows=rows))
        card.add(
            Note(
                text="设置：引用一条群消息后发送「设群待办」；完成 / 取消使用上面的序号。",
                tone="muted",
            )
        )
        return card

    async def set(self, event: AstrMessageEvent) -> str:
        """把当前消息引用的群消息加入待办。"""
        group_id = event.get_group_id()
        if not group_id:
            return "群待办只能在群里使用"
        try:
            message_id = get_reply_message_id(event)
        except (AttributeError, TypeError):
            message_id = None
        if not message_id:
            return "请先引用一条要加入待办的群消息，再发送「设群待办」"

        result = await call_action(
            event,
            _SET_ACTIONS,
            group_id=int(group_id),
            message_id=_message_param(message_id),
        )
        if not result.ok:
            await self.log(event, "todo_add", target_id=message_id, detail=result.error, success=False)
            return f"设置群待办失败：{explain_action_error(result, '群待办')}"
        await self.log(event, "todo_add", target_id=message_id)
        return "已把引用的消息加入群待办"

    async def _select(
        self, event: AstrMessageEvent, index: Any
    ) -> tuple[dict[str, Any] | None, str]:
        position = parse_int(index)
        if position is None or position <= 0:
            return None, "请指定待办序号，例如「完成群待办 1」（序号见「群待办」）"
        todos, error = await self._fetch(event)
        if error:
            return None, f"获取群待办失败：{error}"
        if position > len(todos):
            return None, f"本群当前只有 {len(todos)} 条待办，找不到第 {position} 条"
        return todos[position - 1], ""

    async def _mutate(
        self,
        event: AstrMessageEvent,
        index: Any,
        actions: tuple[str, ...],
        audit_action: str,
        operation_label: str,
        success_text: str,
    ) -> str:
        item, error = await self._select(event, index)
        if error:
            return error
        assert item is not None
        group_id = event.get_group_id()
        message_id = str(item["message_id"])
        result = await call_action(
            event,
            actions,
            group_id=int(group_id),
            message_id=_message_param(message_id),
        )
        if not result.ok:
            await self.log(
                event, audit_action, target_id=message_id, detail=result.error, success=False
            )
            return f"{operation_label}失败：{explain_action_error(result, '群待办')}"
        await self.log(event, audit_action, target_id=message_id, detail=_todo_label(item))
        return f"{success_text}：{_todo_label(item)}"

    async def complete(self, event: AstrMessageEvent, index: Any = "") -> str:
        """按「群待办」列表序号完成一项。"""
        return await self._mutate(
            event, index, _COMPLETE_ACTIONS, "todo_complete", "完成群待办", "已完成群待办"
        )

    async def cancel(self, event: AstrMessageEvent, index: Any = "") -> str:
        """按「群待办」列表序号取消一项。"""
        return await self._mutate(
            event, index, _CANCEL_ACTIONS, "todo_cancel", "取消群待办", "已取消群待办"
        )


__all__ = ["TodoFeature", "normalize_todos"]

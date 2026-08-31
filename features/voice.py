"""AI 声聊：让机器人用 QQ 官方音色把一段文字念出来。

协议端提供两个动作：get_ai_characters 列出可用音色，send_group_ai_record 直接把
合成好的语音发到群里。三端动作名略有差异，统一走 core.protocol.call_action 降级，
不支持的协议端会得到一句明确提示而不是报错。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api.event import AstrMessageEvent

from ..core.protocol import as_list, call_action
from .base import Feature, FeatureContext

#: 列出可用音色
_CHARACTER_ACTIONS: tuple[str, ...] = ("get_ai_characters",)
#: 发送 AI 语音
_RECORD_ACTIONS: tuple[str, ...] = ("send_group_ai_record", "send_group_ai_voice")

#: 音色列表缓存时间（秒）；音色表几乎不变，缓存久一点省往返
CACHE_TTL = 1800

#: 单次朗读的文本上限，太长协议端会直接失败
MAX_TEXT = 300


class VoiceFeature(Feature):
    """群内 AI 声聊。"""

    def __init__(self, ctx: FeatureContext) -> None:
        super().__init__(ctx)
        # group_id -> (过期时间, [(音色名, 音色 ID)])
        self._cache: dict[str, tuple[float, list[tuple[str, str]]]] = {}

    # ------------------------------------------------------------ 配置

    @property
    def _chat_type(self) -> int:
        return max(1, self.config.voice.int("chat_type", 1))

    @property
    def _default_character(self) -> str:
        return self.config.voice.str("default_character", "").strip()

    # ------------------------------------------------------------ 音色表

    async def characters(
        self, event: AstrMessageEvent, *, refresh: bool = False
    ) -> list[tuple[str, str]]:
        """取本群可用音色 [(名称, ID)]，取不到返回空列表。"""
        group_id = str(event.get_group_id() or "")
        cached = self._cache.get(group_id)
        if cached and not refresh and cached[0] > time.time():
            return cached[1]

        result = await call_action(
            event,
            _CHARACTER_ACTIONS,
            group_id=int(group_id) if group_id else None,
            chat_type=self._chat_type,
        )
        items = _flatten_characters(result.data)
        if items:
            self._cache[group_id] = (time.time() + CACHE_TTL, items)
        return items

    async def list_characters(self, event: AstrMessageEvent) -> str:
        """「声聊音色」：列出可选音色，带序号方便直接引用。"""
        if not event.get_group_id():
            return "AI 声聊只能在群里使用"
        items = await self.characters(event, refresh=True)
        if not items:
            return "当前协议端没有返回可用音色，可能是不支持 AI 声聊"
        current = self._default_character
        lines = [f"可用音色共 {len(items)} 个："]
        for index, (name, character_id) in enumerate(items, start=1):
            mark = "（默认）" if current and current in (name, character_id) else ""
            lines.append(f"{index}. {name}{mark}")
        lines.append("")
        lines.append("用法：声聊 <台词>，或 声聊 <序号或音色名> <台词> 临时换音色")
        return "\n".join(lines)

    async def _resolve(
        self, event: AstrMessageEvent, raw: str
    ) -> tuple[str, str, str]:
        """把参数拆成（音色 ID, 音色名, 台词）。

        第一个词命中序号或音色名时当作临时音色，否则整段都是台词。
        """
        items = await self.characters(event)
        text = raw.strip()
        head, _, tail = text.partition(" ")
        head, tail = head.strip(), tail.strip()

        picked = ""
        if items and tail:
            if head.isdigit() and 1 <= int(head) <= len(items):
                picked = items[int(head) - 1][1]
            else:
                picked = next(
                    (cid for name, cid in items if head in (name, cid)), ""
                )
            if picked:
                text = tail

        if not picked:
            wanted = self._default_character
            if wanted:
                picked = next(
                    (cid for name, cid in items if wanted in (name, cid)), wanted
                )
            elif items:
                picked = items[0][1]

        label = next((name for name, cid in items if cid == picked), picked)
        return picked, label, text

    # ------------------------------------------------------------ 朗读

    async def speak(self, event: AstrMessageEvent, raw: str) -> str:
        """「声聊 <台词>」：合成语音并发到群里。成功返回空串（语音本身就是回复）。"""
        group_id = event.get_group_id()
        if not group_id:
            return "AI 声聊只能在群里使用"

        character, label, text = await self._resolve(event, raw)
        if not text:
            return "请给出要念的台词，例如：声聊 今天天气不错"
        if len(text) > MAX_TEXT:
            return f"台词太长了（{len(text)}/{MAX_TEXT} 字），分几次念吧"
        if not character:
            return "没有可用音色，先用「声聊音色」看看协议端支持哪些"

        result = await call_action(
            event,
            _RECORD_ACTIONS,
            group_id=int(group_id),
            character=character,
            text=text,
            chat_type=self._chat_type,
        )
        if not result.ok:
            await self.log(
                event, "voice", detail=f"{label}: {result.error}", success=False
            )
            return f"声聊失败：{result.error}"
        await self.log(event, "voice", detail=f"{label} 念了 {len(text)} 字")
        return ""


def _flatten_characters(payload: Any) -> list[tuple[str, str]]:
    """把 [{type, characters:[{character_id, character_name}]}] 摊平成 [(名称, ID)]。

    有的协议端直接返回一层音色数组，这里两种结构都接。
    """
    items: list[tuple[str, str]] = []
    seen: set[str] = set()

    def take(entry: Any) -> None:
        if not isinstance(entry, dict):
            return
        character_id = str(
            entry.get("character_id") or entry.get("characterId") or entry.get("id") or ""
        ).strip()
        if not character_id or character_id in seen:
            return
        name = str(
            entry.get("character_name")
            or entry.get("characterName")
            or entry.get("name")
            or character_id
        ).strip()
        seen.add(character_id)
        items.append((name or character_id, character_id))

    for group in as_list(payload):
        if isinstance(group, dict) and isinstance(
            nested := (group.get("characters") or group.get("characterList")), list
        ):
            for entry in nested:
                take(entry)
        else:
            take(group)
    return items

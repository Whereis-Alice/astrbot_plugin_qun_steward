"""违禁词与刷屏治理。

相比上游做了这些改进：
- 支持「包含匹配 / 整词匹配 / 正则匹配」三种命中方式，避免「草莓」被「草」误伤；
- 支持豁免等级与白名单，管理员和指定成员不会被自己配置的词误伤；
- 支持「撤回并禁言 / 仅撤回 / 仅提醒」三档处置动作；
- 刷屏计数表会定期清理，不再无限增长。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..core.config import LOG_TAG
from ..core.permission import PermLevel
from ..core.utils import (
    apply_delta,
    get_nickname,
    list_text,
    parse_bool,
    parse_int,
    split_tokens,
    switch_text,
)
from .base import Feature, FeatureContext, rest_of

#: 匹配方式
MATCH_CONTAIN = "包含匹配"
MATCH_WHOLE = "整词匹配"
MATCH_REGEX = "正则匹配"

#: 处置动作
ACTION_BAN = "撤回并禁言"
ACTION_RECALL = "仅撤回"
ACTION_WARN = "仅提醒"

#: 整词匹配时视作「词内字符」的范围（含 CJK）
_WORD_CHAR = r"[0-9A-Za-z_\u4e00-\u9fff]"

#: 刷屏计数表的空闲回收时间
_IDLE_TTL = 600.0


class WordFeature(Feature):
    """违禁词检测 + 刷屏检测。"""

    def __init__(self, ctx: FeatureContext) -> None:
        super().__init__(ctx)
        self._builtin_words: list[str] | None = None
        self._pattern_cache: dict[tuple[str, tuple[str, ...]], list[re.Pattern[str]]] = {}
        # group_id -> user_id -> 最近发言时间戳
        self._timestamps: dict[str, dict[str, list[float]]] = {}
        # group_id -> user_id -> 最近一次因刷屏被处置的时间
        self._last_banned: dict[str, dict[str, float]] = {}

    # ---------------------------------------------------------- 内置词库 --- #
    @property
    def builtin_words(self) -> list[str]:
        """内置敏感词库，首次使用时才加载。"""
        if self._builtin_words is None:
            try:
                raw = json.loads(self.config.lexicon_path.read_text(encoding="utf-8"))
                data = raw.get("words") if isinstance(raw, dict) else raw
                self._builtin_words = [str(w) for w in (data or []) if str(w).strip()]
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"{LOG_TAG} 内置词库加载失败: {exc}")
                self._builtin_words = []
        return self._builtin_words

    # -------------------------------------------------------------- 指令 --- #
    async def word_ban_time(self, event: AstrMessageEvent, seconds: Any = None) -> str:
        gid = event.get_group_id()
        value = parse_int(seconds)
        if value is None:
            current = parse_int(self.store.value(gid, "word_ban_time"), 0) or 0
            return f"本群禁词禁言时长：{current} 秒"
        value = max(0, value)
        await self.store.set(gid, "word_ban_time", value)
        await self.log(event, "config", detail=f"禁词禁言时长={value}")
        return f"本群禁词禁言时长已设为：{value} 秒" if value > 0 else "本群禁词禁言已关闭"

    async def spamming_ban_time(self, event: AstrMessageEvent, seconds: Any = None) -> str:
        gid = event.get_group_id()
        value = parse_int(seconds)
        if value is None:
            current = parse_int(self.store.value(gid, "spamming_ban_time"), 0) or 0
            return f"本群刷屏禁言时长：{current} 秒"
        value = max(0, value)
        await self.store.set(gid, "spamming_ban_time", value)
        await self.log(event, "config", detail=f"刷屏禁言时长={value}")
        return f"本群刷屏禁言时长已设为：{value} 秒" if value > 0 else "本群刷屏禁言已关闭"

    async def manage_words(self, event: AstrMessageEvent) -> str:
        """查看 / 覆写 / 增删本群违禁词。"""
        gid = event.get_group_id()
        raw = rest_of(event)
        current = [str(w) for w in (self.store.value(gid, "custom_ban_words") or [])]

        if not raw:
            return f"本群违禁词（{len(current)} 个）：{list_text(current)}"

        tokens = split_tokens(raw)
        if all(not tok.startswith(("+", "-")) for tok in tokens):
            await self.store.set(gid, "custom_ban_words", tokens)
            await self.log(event, "config", detail=f"违禁词覆写为 {len(tokens)} 个")
            return f"本群违禁词已覆写为：{' '.join(tokens)}"

        merged, added, removed = apply_delta(current, tokens)
        await self.store.set(gid, "custom_ban_words", merged)
        await self.log(event, "config", detail=f"违禁词 +{len(added)} -{len(removed)}")

        lines = ["本群违禁词"]
        if added:
            lines.append(f"新增：{'、'.join(added)}")
        if removed:
            lines.append(f"移除：{'、'.join(removed)}")
        if not added and not removed:
            lines.append("无变动")
        lines.append(f"当前共 {len(merged)} 个")
        return "\n".join(lines)

    async def toggle_builtin(self, event: AstrMessageEvent, mode: Any = None) -> str:
        gid = event.get_group_id()
        parsed = parse_bool(mode)
        if parsed is None:
            current = bool(self.store.value(gid, "builtin_ban"))
            return f"本群内置禁词：{switch_text(current)}（共 {len(self.builtin_words)} 个词）"
        await self.store.set(gid, "builtin_ban", parsed)
        await self.log(event, "config", detail=f"内置禁词={switch_text(parsed)}")
        suffix = f"，词库共 {len(self.builtin_words)} 个词" if parsed else ""
        return f"本群内置禁词已{switch_text(parsed)}{suffix}"

    # ------------------------------------------------------ 违禁词检测 --- #
    def _patterns(self, mode: str, words: list[str]) -> list[re.Pattern[str]]:
        key = (mode, tuple(words))
        cached = self._pattern_cache.get(key)
        if cached is not None:
            return cached
        compiled: list[re.Pattern[str]] = []
        for word in words:
            if not word:
                continue
            try:
                if mode == MATCH_REGEX:
                    compiled.append(re.compile(word, re.IGNORECASE))
                elif mode == MATCH_WHOLE:
                    compiled.append(
                        re.compile(
                            rf"(?<!{_WORD_CHAR}){re.escape(word)}(?!{_WORD_CHAR})",
                            re.IGNORECASE,
                        )
                    )
                else:
                    compiled.append(re.compile(re.escape(word), re.IGNORECASE))
            except re.error as exc:
                logger.warning(f"{LOG_TAG} 违禁词规则无效，已跳过：{word}（{exc}）")
        # 缓存条数有限，防止配置频繁变动时无限增长
        if len(self._pattern_cache) > 64:
            self._pattern_cache.clear()
        self._pattern_cache[key] = compiled
        return compiled

    async def _is_exempt(self, event: AstrMessageEvent) -> bool:
        gid = event.get_group_id()
        sender_id = str(event.get_sender_id())
        whitelist = {str(x) for x in (self.store.value(gid, "word_whitelist") or [])}
        if sender_id in whitelist:
            return True
        exempt = PermLevel.from_label(
            self.store.value(gid, "word_exempt_level"), PermLevel.ADMIN
        )
        if exempt is PermLevel.UNKNOWN:
            return False
        level = await self.permissions.level_of(event, sender_id)
        return level >= exempt

    async def check_ban_words(self, event: AstrMessageEvent) -> str | None:
        """命中违禁词时执行处置，返回需要回复的提示（无需回复则返回 None）。"""
        gid = event.get_group_id()
        message = event.message_str or ""
        if not message.strip():
            return None

        custom = [str(w) for w in (self.store.value(gid, "custom_ban_words") or [])]
        use_builtin = bool(self.store.value(gid, "builtin_ban"))
        if not custom and not use_builtin:
            return None
        if await self._is_exempt(event):
            return None

        mode = str(self.store.value(gid, "word_match_mode") or MATCH_CONTAIN)
        hit = self._first_hit(message, mode, custom)
        source = "自定义"
        if hit is None and use_builtin:
            hit = self._first_hit(message, MATCH_CONTAIN, self.builtin_words)
            source = "内置"
        if hit is None:
            return None

        return await self._punish(event, hit, source)

    def _first_hit(self, message: str, mode: str, words: list[str]) -> str | None:
        for pattern, word in zip(self._patterns(mode, words), words, strict=False):
            if pattern.search(message):
                return word
        return None

    async def _punish(self, event: AstrMessageEvent, word: str, source: str) -> str | None:
        gid = event.get_group_id()
        sender_id = str(event.get_sender_id())
        action = str(self.store.value(gid, "word_action") or ACTION_BAN)

        if action == ACTION_WARN:
            await self.log(
                event, "word_ban", target_id=sender_id, detail=f"{source}词命中：{word}（仅提醒）"
            )
            return f"请注意发言（命中{source}违禁词）"

        try:
            await event.bot.delete_msg(message_id=int(event.message_obj.message_id))
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"{LOG_TAG} 违禁词撤回失败 group={gid}: {exc}")

        detail = f"{source}词命中：{word}"
        if action == ACTION_RECALL:
            await self.log(event, "word_ban", target_id=sender_id, detail=f"{detail}（仅撤回）")
            return None

        ban_time = parse_int(self.store.value(gid, "word_ban_time"), 0) or 0
        if ban_time <= 0:
            await self.log(event, "word_ban", target_id=sender_id, detail=f"{detail}（未禁言）")
            return None
        try:
            await event.bot.set_group_ban(
                group_id=int(gid), user_id=int(sender_id), duration=ban_time
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 违禁词禁言失败 group={gid}: {exc}")
            await self.log(
                event, "word_ban", target_id=sender_id, detail=f"{detail}，禁言失败", success=False
            )
            return None
        await self.log(event, "word_ban", target_id=sender_id, detail=f"{detail}，禁言{ban_time}秒")
        return None

    # ---------------------------------------------------------- 刷屏检测 --- #
    async def check_spamming(self, event: AstrMessageEvent) -> str | None:
        gid = event.get_group_id()
        ban_time = parse_int(self.store.value(gid, "spamming_ban_time"), 0) or 0
        if ban_time <= 0:
            return None

        sender_id = str(event.get_sender_id())
        count = max(2, parse_int(self.store.value(gid, "spamming_count"), 5) or 5)
        interval = float(self.store.value(gid, "spamming_interval") or 0.5)
        now = time.time()

        # 处置冷却：刚被禁过就不再重复计数
        if now - self._last_banned.setdefault(gid, {}).get(sender_id, 0.0) < ban_time:
            return None

        bucket = self._timestamps.setdefault(gid, {}).setdefault(sender_id, [])
        bucket.append(now)
        del bucket[:-count]
        if len(bucket) < count:
            return None
        gaps = [bucket[i + 1] - bucket[i] for i in range(len(bucket) - 1)]
        if not all(gap < interval for gap in gaps):
            return None

        # 先打标记再执行，避免并发重复禁言
        self._last_banned[gid][sender_id] = now
        bucket.clear()
        if await self._is_exempt(event):
            return None

        try:
            await event.bot.set_group_ban(
                group_id=int(gid), user_id=int(sender_id), duration=ban_time
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 刷屏禁言失败 group={gid}: {exc}")
            await self.log(
                event, "spamming_ban", target_id=sender_id, detail=str(exc), success=False
            )
            return None
        nickname = await get_nickname(event, sender_id)
        await self.log(event, "spamming_ban", target_id=sender_id, detail=f"禁言{ban_time}秒")
        return f"检测到{nickname}刷屏，已禁言{ban_time}秒"

    def prune(self) -> None:
        """清理空闲的刷屏计数，避免内存无限增长。由定时任务调用。"""
        now = time.time()
        for gid in list(self._timestamps):
            users = self._timestamps[gid]
            for uid in list(users):
                stamps = users[uid]
                if not stamps or now - stamps[-1] > _IDLE_TTL:
                    users.pop(uid, None)
            if not users:
                self._timestamps.pop(gid, None)
        for gid in list(self._last_banned):
            users = self._last_banned[gid]
            for uid in list(users):
                if now - users[uid] > _IDLE_TTL:
                    users.pop(uid, None)
            if not users:
                self._last_banned.pop(gid, None)

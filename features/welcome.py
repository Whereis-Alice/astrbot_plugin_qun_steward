"""增强入群欢迎与可选算术验证。

设计原则：
* 旧 ``join_welcome`` 继续可用，新 ``welcome_templates`` 非空时优先；
* 消息一律使用 AstrBot message components，不拼 CQ 码；
* 延迟发送和验证超时任务都由本模块持有，插件卸载时统一取消；
* 验证答案必须是纯数字，避免把「今天是 3 号」这类话误判成回答。
"""

from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..core.config import LOG_TAG
from ..core.protocol import unwrap
from ..core.utils import (
    get_nickname,
    list_text,
    parse_bool,
    parse_int,
    split_tokens,
    switch_text,
)
from .base import Feature, resolve_targets, rest_of

# {at} 不是字符串占位符，而要生成真正的 At 组件。先替换成不会出现在
# 正常欢迎语里的哨兵，等其它占位符渲染完成后再切回消息组件。
_AT_TOKEN = "\x00WELCOME_AT\x00"
_CLEAR_WORDS = frozenset({"关", "关闭", "取消", "清空", "清除"})
_VERIFY_ACTIONS = ("踢出", "踢出并拉黑", "仅提醒")
_LOCAL_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/])")
_WINDOWS_FILE_URI = re.compile(r"^/([A-Za-z]:[\\/].*)$")


class _SafeValues(dict[str, str]):
    """渲染欢迎语时保留未知占位符。"""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


@dataclass(slots=True)
class _PendingVerification:
    """一位新人的待验证状态。"""

    question: str
    answer: int
    attempts: int = 0
    task: asyncio.Task[Any] | None = field(default=None, repr=False)


class WelcomeFeature(Feature):
    """增强欢迎、图片混排、延迟发送和入群验证。"""

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self._pending: dict[tuple[str, str], _PendingVerification] = {}
        self._sequence: dict[str, int] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------ 生命周期 --- #

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        """启动并持有后台任务，避免任务被事件循环提前回收。"""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def shutdown(self) -> None:
        """取消延迟发送和验证超时任务。"""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        self._tasks.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for pending in self._pending.values():
            if pending.task is not None and not pending.task.done():
                pending.task.cancel()
        self._pending.clear()

    # ------------------------------------------------------------ 事件入口 --- #

    async def handle_notice(self, event: AstrMessageEvent) -> list[Any] | str | None:
        """处理 OneBot group_increase 通知；其他事件原样放行。"""
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if not isinstance(raw, dict) or raw.get("notice_type") != "group_increase":
            return None
        group_id = str(raw.get("group_id") or "")
        user_id = str(raw.get("user_id") or "")
        if not group_id or not user_id or user_id == str(event.get_self_id()):
            return None
        return await self.handle_increase(event, group_id, user_id)

    async def handle_increase(
        self, event: AstrMessageEvent, group_id: str, user_id: str
    ) -> list[Any] | str | None:
        """处理 group_increase：验证优先，未开启验证时直接欢迎。"""
        if parse_bool(self.store.value(group_id, "welcome_verify"), False):
            return await self._start_verification(event, group_id, user_id)

        # 开启验证时不能先禁言：新人被禁言后无法回答问题。这里宁可跳过
        # 「进群禁言」，也不要生成一个永远无法通过的验证。
        await self._apply_join_ban(event, group_id, user_id)
        return await self._welcome_or_schedule(event, group_id, user_id)

    async def check_reply(self, event: AstrMessageEvent) -> list[Any] | str | None:
        """检查一条群消息是否是待验证新人的答案。"""
        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        key = (str(group_id), str(user_id))
        pending = self._pending.get(key)
        if pending is None:
            return None

        text = (event.message_str or "").strip()
        if not re.fullmatch(r"[+-]?\d+", text):
            return None
        if parse_int(text) != pending.answer:
            pending.attempts += 1
            limit = self._verify_attempts(group_id)
            if 0 < limit <= pending.attempts:
                return await self._finish_failure(
                    event,
                    group_id,
                    user_id,
                    f"连续答错 {pending.attempts} 次",
                    "welcome_verify_fail",
                )
            remaining = "，还可尝试 " + str(limit - pending.attempts) + " 次" if limit > 0 else ""
            return f"答案不对，请直接回复数字再试一次{remaining}。"

        self._pop_pending(key)
        await self._unban(event, group_id, user_id)
        await self.audit.record(
            group_id=group_id,
            action="welcome_verify_pass",
            operator_id="auto",
            operator_name="入群验证",
            target_id=user_id,
            detail=pending.question + " = " + str(pending.answer),
            source="event",
        )
        result = await self._welcome_or_schedule(event, group_id, user_id)
        if result is None:
            return "验证通过，欢迎加入本群！"
        if isinstance(result, list):
            return result
        delay = self._delay(group_id)
        return f"验证通过，欢迎语将在 {delay} 秒后发送。"

    # ------------------------------------------------------------ 欢迎构建 --- #

    async def _welcome_or_schedule(
        self, event: AstrMessageEvent, group_id: str, user_id: str
    ) -> list[Any] | None:
        """构建欢迎消息；配置了延迟时转入后台发送。"""
        chain = await self.build_welcome(event, group_id, user_id)
        if not chain:
            return None
        delay = self._delay(group_id)
        if delay <= 0:
            return chain
        self._spawn(self._send_later(event, chain, delay))
        return None

    async def _send_later(
        self, event: AstrMessageEvent, chain: list[Any], delay: int
    ) -> None:
        try:
            await asyncio.sleep(delay)
            await event.send(event.chain_result(chain))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 延迟欢迎发送失败：{exc}")

    async def build_welcome(
        self, event: AstrMessageEvent, group_id: str, user_id: str
    ) -> list[Any]:
        """按当前配置构建欢迎消息链。"""
        template = self._select_template(group_id)
        if not template:
            return []

        values = await self._placeholder_values(event, group_id, user_id, template)
        protected = template.replace("{at}", _AT_TOKEN)
        try:
            rendered = protected.format_map(_SafeValues(values))
        except (AttributeError, IndexError, KeyError, ValueError):
            rendered = protected

        chain: list[Any] = []
        chunks = rendered.split(_AT_TOKEN)
        for index, chunk in enumerate(chunks):
            if chunk:
                chain.append(Comp.Plain(chunk))
            if index < len(chunks) - 1:
                chain.append(Comp.At(qq=int(user_id)))

        chain.extend(self._image_components(group_id))
        return chain

    def _select_template(self, group_id: str) -> str:
        templates = [str(item) for item in self.store.value(group_id, "welcome_templates") or []]
        templates = [item for item in templates if item.strip()]
        if templates:
            mode = str(self.store.value(group_id, "welcome_mode") or "随机")
            if mode == "顺序":
                index = self._sequence.get(group_id, 0) % len(templates)
                self._sequence[group_id] = index + 1
                return templates[index]
            return random.choice(templates)
        return str(self.store.value(group_id, "join_welcome") or "")

    async def _placeholder_values(
        self,
        event: AstrMessageEvent,
        group_id: str,
        user_id: str,
        template: str,
    ) -> dict[str, str]:
        values = {
            "user_id": user_id,
            "group_id": group_id,
        }
        if any(key in template for key in ("{nickname}", "{昵称}")):
            values["nickname"] = await get_nickname(event, user_id)
            values["昵称"] = values["nickname"]
        if any(key in template for key in ("{group_name}", "{群名}")):
            values["group_name"] = await self._group_name(event, group_id)
            values["群名"] = values["group_name"]
        if any(key in template for key in ("{member_count}", "{人数}")):
            count = await self._member_count(event, group_id)
            values["member_count"] = str(count)
            values["人数"] = values["member_count"]
        if any(key in template for key in ("{join_time}", "{时间}")):
            values["join_time"] = self._now_text()
            values["时间"] = values["join_time"]
        return values

    @staticmethod
    def _now_text() -> str:
        try:
            return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
        except ZoneInfoNotFoundError:
            return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    async def _group_name(self, event: AstrMessageEvent, group_id: str) -> str:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if isinstance(raw, dict):
            for key in ("group_name", "groupName", "name"):
                value = raw.get(key)
                if value not in (None, ""):
                    return str(value).strip()
            group = raw.get("group")
            if isinstance(group, dict):
                for key in ("group_name", "groupName", "name"):
                    value = group.get(key)
                    if value not in (None, ""):
                        return str(value).strip()

        getter = getattr(event.bot, "get_group_info", None)
        if callable(getter):
            try:
                info = unwrap(await getter(group_id=int(group_id), no_cache=False))
                if isinstance(info, dict):
                    for key in ("group_name", "groupName", "name"):
                        value = info.get(key)
                        if value not in (None, ""):
                            return str(value).strip()
            except TypeError:
                try:
                    info = unwrap(await getter(group_id=int(group_id)))
                    if isinstance(info, dict):
                        for key in ("group_name", "groupName", "name"):
                            value = info.get(key)
                            if value not in (None, ""):
                                return str(value).strip()
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"{LOG_TAG} 获取群名失败 group={group_id}：{exc}")
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"{LOG_TAG} 获取群名失败 group={group_id}：{exc}")
        return group_id

    async def _member_count(self, event: AstrMessageEvent, group_id: str) -> int:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if isinstance(raw, dict):
            for key in ("member_count", "memberCount", "current_member_count", "currentMemberCount"):
                value = parse_int(raw.get(key))
                if value is not None and value > 0:
                    return value

        getter = getattr(event.bot, "get_group_info", None)
        if callable(getter):
            try:
                info = unwrap(await getter(group_id=int(group_id), no_cache=False))
                if isinstance(info, dict):
                    for key in ("member_count", "memberCount", "current_member_count"):
                        value = parse_int(info.get(key))
                        if value is not None and value > 0:
                            return value
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"{LOG_TAG} 获取群人数失败 group={group_id}：{exc}")
        return 0

    def _image_components(self, group_id: str) -> list[Any]:
        images = [str(item).strip() for item in self.store.value(group_id, "welcome_images") or []]
        components: list[Any] = []
        for image in images:
            if not image:
                continue
            try:
                if image.startswith("file://"):
                    path = unquote(image[7:])
                    path = _WINDOWS_FILE_URI.sub(r"\1", path)
                    components.append(Comp.Image.fromFileSystem(path))
                elif _LOCAL_PATH.fullmatch(image) or Path(image).is_absolute():
                    components.append(Comp.Image.fromFileSystem(image))
                else:
                    components.append(Comp.Image.fromURL(image))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"{LOG_TAG} 欢迎图片无效 {image}：{exc}")
        return components

    # ------------------------------------------------------------ 禁言工具 --- #

    def _delay(self, group_id: str) -> int:
        return max(0, min(parse_int(self.store.value(group_id, "welcome_delay"), 0) or 0, 300))

    async def _apply_join_ban(
        self, event: AstrMessageEvent, group_id: str, user_id: str
    ) -> None:
        seconds = parse_int(self.store.value(group_id, "join_ban_time"), 0) or 0
        if seconds <= 0:
            return
        try:
            await event.bot.set_group_ban(
                group_id=int(group_id), user_id=int(user_id), duration=seconds
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 进群禁言失败 group={group_id} user={user_id}：{exc}")

    async def _unban(self, event: AstrMessageEvent, group_id: str, user_id: str) -> None:
        """验证通过后尽力解除禁言；没设置禁言时调用 0 也无害。"""
        try:
            await event.bot.set_group_ban(
                group_id=int(group_id), user_id=int(user_id), duration=0
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"{LOG_TAG} 解除进群禁言失败 user={user_id}：{exc}")

    # ------------------------------------------------------------ 算术验证 --- #

    async def _start_verification(
        self, event: AstrMessageEvent, group_id: str, user_id: str
    ) -> list[Any]:
        question, answer = self._make_question()
        timeout = self._verify_timeout_seconds(group_id)
        attempts = self._verify_attempts(group_id)
        key = (group_id, user_id)
        old = self._pending.get(key)
        if old is not None and old.task is not None:
            old.task.cancel()

        pending = _PendingVerification(question=question, answer=answer)
        pending.task = self._spawn(
            self._verify_timeout(event, group_id, user_id, timeout)
        )
        self._pending[key] = pending
        limit = f"，最多答错 {attempts} 次" if attempts > 0 else ""
        return [
            Comp.At(qq=int(user_id)),
            Comp.Plain(
                f" 欢迎加入本群！请先完成入群验证（{timeout} 秒内{limit}）：\n"
                f"{question} = ?\n直接回复纯数字答案即可。"
            ),
        ]

    @staticmethod
    def _make_question() -> tuple[str, int]:
        left = random.randint(1, 20)
        right = random.randint(1, 20)
        operation = random.choice(("+", "-", "×"))
        if operation == "+":
            return f"{left} + {right}", left + right
        if operation == "-":
            left, right = max(left, right), min(left, right)
            return f"{left} - {right}", left - right
        left, right = random.randint(2, 9), random.randint(2, 9)
        return f"{left} × {right}", left * right

    async def _verify_timeout(
        self, event: AstrMessageEvent, group_id: str, user_id: str, timeout: int
    ) -> None:
        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            raise
        if (group_id, user_id) not in self._pending:
            return
        message = await self._finish_failure(
            event, group_id, user_id, f"超时 {timeout} 秒未答对", "welcome_verify_timeout"
        )
        try:
            await event.send(event.plain_result(message))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 验证超时通知发送失败：{exc}")

    async def _finish_failure(
        self,
        event: AstrMessageEvent,
        group_id: str,
        user_id: str,
        reason: str,
        action: str,
    ) -> str:
        self._pop_pending((group_id, user_id))
        timeout_action = self._verify_action(group_id)
        detail = reason
        success = True
        if timeout_action in {"踢出", "踢出并拉黑"}:
            reject = timeout_action == "踢出并拉黑"
            try:
                await event.bot.set_group_kick(
                    group_id=int(group_id),
                    user_id=int(user_id),
                    reject_add_request=reject,
                )
                if reject:
                    await self._add_join_block(group_id, user_id)
                detail += f"；已{timeout_action}"
            except Exception as exc:  # noqa: BLE001
                success = False
                detail += f"；{timeout_action}失败：{exc}"
        else:
            detail += "；请管理员人工处理"

        await self.audit.record(
            group_id=group_id,
            action=action,
            operator_id="auto",
            operator_name="入群验证",
            target_id=user_id,
            detail=detail,
            source="event",
            success=success,
        )
        return f"入群验证未通过：{user_id} {reason}。" + detail.partition("；")[2]

    async def _add_join_block(self, group_id: str, user_id: str) -> None:
        current = [str(item) for item in self.store.value(group_id, "block_ids") or []]
        if user_id not in current:
            current.append(user_id)
            await self.store.set(group_id, "block_ids", current)

    def _pop_pending(self, key: tuple[str, str]) -> None:
        pending = self._pending.pop(key, None)
        current = asyncio.current_task()
        if (
            pending is not None
            and pending.task is not None
            and not pending.task.done()
            and pending.task is not current
        ):
            pending.task.cancel()

    def _verify_timeout_seconds(self, group_id: str) -> int:
        return max(15, min(parse_int(self.store.value(group_id, "welcome_verify_timeout"), 120) or 120, 3600))

    def _verify_attempts(self, group_id: str) -> int:
        return max(0, min(parse_int(
            self.store.value(group_id, "welcome_verify_max_attempts"), 3
        ) or 0, 10))

    def _verify_action(self, group_id: str) -> str:
        action = str(self.store.value(group_id, "welcome_verify_timeout_action") or "踢出")
        return action if action in _VERIFY_ACTIONS else "踢出"

    # ------------------------------------------------------------ 配置指令 --- #

    async def set_legacy_text(self, event: AstrMessageEvent) -> str:
        """兼容旧指令：单条欢迎语写入 join_welcome。"""
        group_id = event.get_group_id()
        raw = rest_of(event)
        if not raw:
            text = str(self.store.value(group_id, "join_welcome") or "")
            return "本群兼容欢迎语：\n" + (text or "（未设置）")
        if raw in _CLEAR_WORDS:
            await self.store.set(group_id, "join_welcome", "")
            await self.log(event, "join_welcome", detail="清空")
            return "已清空兼容欢迎语"
        await self.store.set(group_id, "join_welcome", raw)
        await self.log(event, "join_welcome", detail=raw)
        placeholders = ("{at}", "{nickname}", "{昵称}", "{group_name}", "{群名}")
        tip = "" if any(item in raw for item in placeholders) else (
            "\n提示：可用 {at}（@新人）、{nickname}（昵称）、{group_name}（群名）占位。"
        )
        return f"兼容欢迎语已设置。多模板请用「欢迎模板」。{tip}"

    async def set_templates(self, event: AstrMessageEvent) -> str:
        return await self._edit_string_list(event, "welcome_templates", "欢迎模板")

    async def set_images(self, event: AstrMessageEvent) -> str:
        return await self._edit_string_list(event, "welcome_images", "欢迎图片")

    async def _edit_string_list(
        self, event: AstrMessageEvent, field: str, label: str
    ) -> str:
        group_id = event.get_group_id()
        raw = rest_of(event)
        current = [str(item) for item in self.store.value(group_id, field) or []]
        if not raw:
            items = current or []
            body = "\n".join(f"{index}. {item}" for index, item in enumerate(items, 1))
            return f"本群{label}：\n" + (body or "（空）")
        if raw in {"+", "-"}:
            return f"请在「{raw}」后面写内容，多条用「||」分隔。"
        if raw in _CLEAR_WORDS:
            await self.store.set(group_id, field, [])
            await self.log(event, field, detail="清空")
            return f"已清空本群{label}"

        added: list[str] = []
        removed: list[str] = []
        if raw.startswith("+"):
            added = [item.strip() for item in raw[1:].split("||") if item.strip()]
            current.extend(item for item in added if item not in current)
        elif raw.startswith("-"):
            token = raw[1:].strip()
            index = parse_int(token)
            if index is not None and 1 <= index <= len(current):
                removed = [current.pop(index - 1)]
            elif token in current:
                current.remove(token)
                removed = [token]
            else:
                return f"没找到要移除的{label}，可用序号或完整内容。"
        else:
            current = [item.strip() for item in raw.split("||") if item.strip()]

        await self.store.set(group_id, field, current)
        await self.log(event, field, detail=f"{len(current)} 项")
        if added:
            return f"已新增 {len(added)} 条{label}，当前共 {len(current)} 条。"
        if removed:
            return f"已移除{label}：{removed[0]}，当前共 {len(current)} 条。"
        return f"已覆写{label}，当前共 {len(current)} 条。"

    async def set_mode(self, event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        raw = rest_of(event)
        if not raw:
            return f"本群欢迎模式：{self.store.value(group_id, 'welcome_mode')}"
        if raw not in {"随机", "顺序"}:
            return "欢迎模式只支持「随机」或「顺序」。"
        await self.store.set(group_id, "welcome_mode", raw)
        await self.log(event, "welcome_mode", detail=raw)
        return f"本群欢迎模式已设为：{raw}"

    async def set_delay(self, event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        raw = rest_of(event)
        value = parse_int(raw)
        if value is None:
            return f"本群欢迎延迟：{self._delay(group_id)} 秒"
        value = max(0, min(value, 300))
        await self.store.set(group_id, "welcome_delay", value)
        await self.log(event, "welcome_delay", detail=str(value))
        return f"本群欢迎延迟已设为：{value} 秒"

    async def toggle_verify(self, event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        tokens = split_tokens(rest_of(event))
        mode = parse_bool(tokens[0] if tokens else None)
        if mode is None:
            return (
                f"本群入群验证：{switch_text(self.store.value(group_id, 'welcome_verify'))}\n"
                f"超时：{self._verify_timeout_seconds(group_id)} 秒；"
                f"最多答错：{self._verify_attempts(group_id)} 次；"
                f"超时动作：{self._verify_action(group_id)}"
            )

        changes: dict[str, Any] = {"welcome_verify": mode}
        if len(tokens) > 1:
            timeout = parse_int(tokens[1])
            if timeout is None:
                return "验证超时时间必须是数字，例如：入群验证 开 180"
            changes["welcome_verify_timeout"] = max(15, min(timeout, 3600))
        await self.store.update(group_id, changes)
        await self.log(event, "welcome_verify", detail=switch_text(mode))
        if not mode:
            self._cancel_group_pending(group_id)
            return "已关闭入群验证，并取消本群未完成的验证任务。"
        return (
            f"本群入群验证已{switch_text(mode)}，"
            f"当前超时 {self._verify_timeout_seconds(group_id)} 秒。"
        )

    def _cancel_group_pending(self, group_id: str) -> None:
        for key in [key for key in self._pending if key[0] == group_id]:
            self._pop_pending(key)

    async def set_verify_action(self, event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        raw = rest_of(event)
        if not raw:
            return f"本群验证失败动作：{self._verify_action(group_id)}"
        if raw not in _VERIFY_ACTIONS:
            return "验证失败动作只支持：踢出、踢出并拉黑、仅提醒。"
        await self.store.set(group_id, "welcome_verify_timeout_action", raw)
        await self.log(event, "welcome_verify_timeout_action", detail=raw)
        return f"本群验证失败动作已设为：{raw}"

    async def set_verify_attempts(self, event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        raw = rest_of(event)
        value = parse_int(raw)
        if value is None:
            return f"本群最多答错次数：{self._verify_attempts(group_id)} 次（0 表示不限）"
        value = max(0, min(value, 10))
        await self.store.set(group_id, "welcome_verify_max_attempts", value)
        await self.log(event, "welcome_verify_max_attempts", detail=str(value))
        return f"本群最多答错次数已设为：{value} 次"

    async def test(self, event: AstrMessageEvent) -> list[Any] | str:
        """按当前配置预览欢迎消息，不发送真实入群事件。"""
        group_id = event.get_group_id()
        if not group_id:
            return "欢迎测试只能在群里使用。"
        targets = resolve_targets(event)
        user_id = targets[0] if targets else str(event.get_sender_id())
        chain = await self.build_welcome(event, group_id, user_id)
        if not chain:
            return "当前没有配置欢迎语。"
        delay = self._delay(group_id)
        if delay > 0:
            chain.insert(0, Comp.Plain(f"（测试预览，实际会延迟 {delay} 秒）\n"))
        return chain

    async def config_text(self, event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        if not group_id:
            return "欢迎配置只能在群里查看。"
        templates = [str(item) for item in self.store.value(group_id, "welcome_templates") or []]
        images = [str(item) for item in self.store.value(group_id, "welcome_images") or []]
        legacy = str(self.store.value(group_id, "join_welcome") or "")
        lines = [
            f"【欢迎配置】群 {group_id}",
            f"模板模式：{self.store.value(group_id, 'welcome_mode')}；延迟：{self._delay(group_id)} 秒",
            f"兼容单条欢迎语：{legacy or '（未设置）'}",
            "欢迎模板：" + (list_text(templates, "（空）") if templates else "（空，使用兼容单条欢迎语）"),
            "欢迎图片：" + list_text(images),
            (
                f"入群验证：{switch_text(self.store.value(group_id, 'welcome_verify'))}；"
                f"超时 {self._verify_timeout_seconds(group_id)} 秒；"
                f"最多答错 {self._verify_attempts(group_id)} 次；"
                f"失败动作 {self._verify_action(group_id)}"
            ),
        ]
        if parse_bool(self.store.value(group_id, "welcome_verify"), False):
            lines.append("提示：开启验证时会跳过「进群禁言」，否则新人无法回复答案。")
        return "\n".join(lines)


__all__ = ["WelcomeFeature"]

"""权限模型与鉴权。

上游插件把鉴权逻辑写成模块级函数并依赖全局配置，这里改成 PermissionResolver
依赖注入，方便单元测试，也便于按群覆写权限矩阵。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from enum import IntEnum
from functools import wraps
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .config import LOG_TAG, StewardConfig
from .store import GroupStore
from .utils import get_ats, parse_int

AIOCQHTTP_ADAPTER = "aiocqhttp"

_LABELS: dict[int, str] = {
    -1: "未知",
    0: "成员",
    1: "高等级成员",
    2: "管理员",
    3: "群主",
    4: "超管",
}


class PermLevel(IntEnum):
    """权限等级，数值越大权限越高。"""

    UNKNOWN = -1
    MEMBER = 0
    HIGH = 1
    ADMIN = 2
    OWNER = 3
    SUPERUSER = 4

    @property
    def label(self) -> str:
        return _LABELS[int(self)]

    def __str__(self) -> str:  # 便于直接拼进提示文案
        return self.label

    @classmethod
    def from_label(cls, text: Any, default: PermLevel | None = None) -> PermLevel:
        """把中文标签解析成等级；无法识别时返回 default（默认「管理员」）。"""
        fallback = default if default is not None else cls.ADMIN
        raw = str(text or "").strip()
        if not raw:
            return fallback
        if raw in {"未知", "无权限"}:
            return cls.UNKNOWN
        for value, label in _LABELS.items():
            if raw == label:
                return cls(value)
        alias = {
            "superuser": cls.SUPERUSER,
            "owner": cls.OWNER,
            "admin": cls.ADMIN,
            "high": cls.HIGH,
            "member": cls.MEMBER,
        }
        return alias.get(raw.lower(), fallback)


#: 供 _conf_schema.json 与 WebUI 共用的下拉选项
PERM_OPTIONS: list[str] = ["超管", "群主", "管理员", "高等级成员", "成员"]


class PermissionResolver:
    """解析调用者 / 机器人 / 操作目标的权限并给出拒绝理由。"""

    def __init__(self, config: StewardConfig, store: GroupStore) -> None:
        self._config = config
        self._store = store

    # ------------------------------------------------------------ 基础查询 --- #
    def level_threshold(self, group_id: Any) -> int:
        """「高等级成员」的等级门槛，群级优先。"""
        value = parse_int(
            self._store.value(group_id, "level_threshold"), self._config.level_threshold
        )
        return value if value and value > 0 else self._config.level_threshold

    def required_level(self, group_id: Any, perm_key: str | None) -> PermLevel:
        """某个功能所需的最低权限，群级 perms 覆写优先于全局。"""
        if not perm_key:
            return PermLevel.MEMBER
        group_perms = self._store.value(group_id, "perms")
        if isinstance(group_perms, dict) and perm_key in group_perms:
            return PermLevel.from_label(group_perms[perm_key])
        return PermLevel.from_label(self._config.perms.get(perm_key))

    def is_superuser(self, user_id: Any) -> bool:
        return str(user_id) in self._config.admins_id

    async def level_of(self, event: AstrMessageEvent, user_id: Any) -> PermLevel:
        """查询某人在当前群里的权限等级。"""
        uid = str(user_id)
        if self.is_superuser(uid):
            return PermLevel.SUPERUSER
        group_id = event.get_group_id()
        if not group_id:
            return PermLevel.UNKNOWN
        try:
            info = await event.bot.get_group_member_info(
                group_id=int(group_id), user_id=int(uid), no_cache=True
            )
        except Exception as exc:  # noqa: BLE001 - 协议端不可用时降级为「未知」
            logger.debug(f"{LOG_TAG} 获取群成员信息失败 group={group_id} user={uid}: {exc}")
            return PermLevel.UNKNOWN
        role = str((info or {}).get("role") or "")
        if role == "owner":
            return PermLevel.OWNER
        if role == "admin":
            return PermLevel.ADMIN
        level = parse_int((info or {}).get("level"), 0) or 0
        return PermLevel.HIGH if level >= self.level_threshold(group_id) else PermLevel.MEMBER

    async def self_level(self, event: AstrMessageEvent) -> PermLevel:
        """机器人自己在当前群里的权限等级（不受超管名单影响）。"""
        group_id = event.get_group_id()
        self_id = event.get_self_id()
        if not group_id or not self_id:
            return PermLevel.UNKNOWN
        try:
            info = await event.bot.get_group_member_info(
                group_id=int(group_id), user_id=int(self_id), no_cache=True
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"{LOG_TAG} 获取机器人群身份失败 group={group_id}: {exc}")
            return PermLevel.UNKNOWN
        role = str((info or {}).get("role") or "")
        if role == "owner":
            return PermLevel.OWNER
        if role == "admin":
            return PermLevel.ADMIN
        return PermLevel.MEMBER

    # ---------------------------------------------------------------- 鉴权 --- #
    async def check(
        self,
        event: AstrMessageEvent,
        *,
        bot_perm: PermLevel = PermLevel.ADMIN,
        perm_key: str | None = None,
        check_at: bool = True,
    ) -> str | None:
        """返回拒绝理由；None 表示放行。"""
        group_id = event.get_group_id()
        sender_id = str(event.get_sender_id())
        required = self.required_level(group_id, perm_key)

        user_level = await self.level_of(event, sender_id)
        if user_level < required:
            return f"你没{required.label}权限"

        bot_level = await self.self_level(event)
        if bot_level < bot_perm:
            return f"我没{bot_perm.label}权限"

        if not check_at:
            return None

        # 目标校验：机器人和调用者都必须严格高于目标，避免借机器人越权互殴。
        for target_id in get_ats(event):
            if target_id == sender_id:
                continue  # 自助操作（例如自我禁言）放行
            target_level = await self.level_of(event, target_id)
            if target_level is PermLevel.UNKNOWN:
                continue
            if bot_level <= target_level:
                return f"我动不了{target_level.label}"
            if user_level is not PermLevel.SUPERUSER and user_level <= target_level:
                return f"你动不了{target_level.label}"
        return None

    async def check_llm(
        self,
        event: AstrMessageEvent,
        perm_key: str | None,
        *,
        bot_perm: PermLevel = PermLevel.ADMIN,
        need_auth: bool = True,
    ) -> str | None:
        """LLM 函数工具的鉴权：先校验平台，再按需校验调用者权限。"""
        if event.get_platform_name() != AIOCQHTTP_ADAPTER:
            return "该工具仅支持通过 QQ 群聊调用"
        if not event.get_group_id():
            return "该工具仅支持在 QQ 群聊中调用"
        if not need_auth:
            # 机器人自行发起：只需确认机器人自身有权限
            bot_level = await self.self_level(event)
            if bot_level < bot_perm:
                return f"我没{bot_perm.label}权限"
            return None
        return await self.check(event, bot_perm=bot_perm, perm_key=perm_key, check_at=False)


_SKIP = "\x00skip"


def perm_required(
    bot_perm: PermLevel = PermLevel.ADMIN,
    perm_key: str | None = None,
    check_at: bool = True,
) -> Callable[[Any], Any]:
    """指令处理器装饰器。

    只在 aiocqhttp 群聊里生效；其他平台或私聊直接跳过（不报错，避免影响别的适配器）。
    被装饰方法所属实例需提供 permissions 属性（PermissionResolver）。
    """

    def decorator(func: Any) -> Any:
        is_asyncgen = inspect.isasyncgenfunction(func)

        async def guard(self: Any, event: AstrMessageEvent) -> str | None:
            if event.get_platform_name() != AIOCQHTTP_ADAPTER or not event.get_group_id():
                return _SKIP
            resolver: PermissionResolver = self.permissions
            return await resolver.check(
                event, bot_perm=bot_perm, perm_key=perm_key, check_at=check_at
            )

        if is_asyncgen:

            @wraps(func)
            async def agen_wrapper(self: Any, event: AstrMessageEvent, *args: Any, **kwargs: Any):
                reason = await guard(self, event)
                if reason == _SKIP:
                    return
                if reason:
                    event.stop_event()
                    yield event.plain_result(reason)
                    return
                async for item in func(self, event, *args, **kwargs):
                    yield item

            return agen_wrapper

        @wraps(func)
        async def coro_wrapper(self: Any, event: AstrMessageEvent, *args: Any, **kwargs: Any):
            reason = await guard(self, event)
            if reason == _SKIP:
                return None
            if reason:
                # 协程处理器的返回值不会被框架发送，这里直接写进事件结果
                event.set_result(event.plain_result(reason))
                event.stop_event()
                return None
            return await func(self, event, *args, **kwargs)

        return coro_wrapper

    return decorator

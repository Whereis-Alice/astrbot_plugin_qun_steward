"""功能模块共用的依赖容器与小工具。

指令注册全部集中在 main.py，业务逻辑放在各 Feature 类里，两边通过
FeatureContext 传递依赖，避免到处 import 全局单例。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context

from ..core.audit import AuditLog
from ..core.config import StewardConfig
from ..core.db import Database
from ..core.group_cache import GroupInfoCache
from ..core.permission import PermissionResolver
from ..core.store import GroupStore
from ..core.undo import UndoStack
from ..core.utils import get_ats, get_replyer_id


@dataclass
class FeatureContext:
    """所有功能模块共用的依赖。"""

    context: Context
    config: StewardConfig
    store: GroupStore
    permissions: PermissionResolver
    audit: AuditLog
    undo: UndoStack
    groups: GroupInfoCache
    db: Database
    #: Markdown -> 图片 URL，由 main.py 注入（Star.text_to_image），便于单测替换
    to_image: Callable[[str], Awaitable[str]]


def resolve_targets(event: AstrMessageEvent, allow_reply: bool = True) -> list[str]:
    """解析操作目标：优先 @，其次引用消息的发送者。"""
    targets = get_ats(event)
    if targets:
        return targets
    if allow_reply:
        replyer = get_replyer_id(event)
        if replyer:
            return [replyer]
    return []


def args_of(event: AstrMessageEvent) -> list[str]:
    """取指令后面的参数（已去掉指令本身）。"""
    parts = (event.message_str or "").split()
    return parts[1:] if parts else []


def rest_of(event: AstrMessageEvent) -> str:
    """取指令后面的整段原始文本，保留空格。"""
    return (event.message_str or "").partition(" ")[2].strip()


class Feature:
    """功能模块基类，只提供依赖访问和审计快捷方法。"""

    def __init__(self, ctx: FeatureContext) -> None:
        self.ctx = ctx

    @property
    def config(self) -> StewardConfig:
        return self.ctx.config

    @property
    def store(self) -> GroupStore:
        return self.ctx.store

    @property
    def permissions(self) -> PermissionResolver:
        return self.ctx.permissions

    @property
    def groups(self) -> GroupInfoCache:
        return self.ctx.groups

    @property
    def undo(self) -> UndoStack:
        return self.ctx.undo

    @property
    def audit(self) -> AuditLog:
        return self.ctx.audit

    @property
    def db(self) -> Database:
        return self.ctx.db

    async def to_image(self, markdown: str) -> str:
        """把 Markdown 渲染成图片并返回 URL。"""
        return await self.ctx.to_image(markdown)

    async def log(
        self,
        event: AstrMessageEvent,
        action: str,
        *,
        target_id: Any = "",
        detail: str = "",
        success: bool = True,
        source: str = "command",
    ) -> None:
        """记录一条审计日志，操作者信息从事件里取。"""
        await self.audit.record(
            group_id=event.get_group_id(),
            action=action,
            operator_id=event.get_sender_id(),
            operator_name=event.get_sender_name() or "",
            target_id=target_id,
            detail=detail,
            source=source,
            success=success,
        )

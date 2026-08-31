"""WebUI 数据服务层：只做数据组装与校验，不碰 HTTP。

这样拆开的好处是 API 层薄到可以一眼看完，而这里的逻辑可以直接单测。
"""

from __future__ import annotations

import json
import time
from typing import Any

from astrbot.api import logger

from ..core.audit import action_label
from ..core.config import DISPLAY_NAME, LOG_TAG, PLUGIN_NAME, StewardConfig
from ..core.permission import PERM_OPTIONS, PermLevel
from ..core.protocol import backend_label
from ..core.store import FIELD_LABELS
from ..core.utils import format_datetime, parse_bool, parse_int
from ..features.base import FeatureContext

#: 顶层可编辑配置分区（分区名 -> 中文标题）
SETTING_SECTIONS: dict[str, str] = {
    "vote_ban": "投票禁言",
    "safety": "安全与批量操作",
    "audit": "操作审计",
    "album": "群相册",
    "fonts": "字体",
    "output": "长列表输出",
    "voice": "AI 声聊",
}

#: 顶层标量配置（键 -> 中文标题）
SETTING_SCALARS: dict[str, str] = {
    "admin_audit": "管理员操作也记审计",
    "random_ban_time": "全局随机禁言区间",
    "level_threshold": "高等级成员门槛",
    "llm_get_msg_count": "LLM 取历史消息条数",
    "enable_llm_tools": "启用 LLM 工具",
}


def _coerce(meta: dict[str, Any], value: Any) -> Any:
    """按 _conf_schema.json 里声明的类型把前端传来的值规整一遍。"""
    kind = str(meta.get("type") or "string")
    if kind == "bool":
        parsed = parse_bool(value, False)
        return bool(parsed)
    if kind == "int":
        return parse_int(value, 0) or 0
    if kind == "float":
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
    if kind == "list":
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            return [tok for tok in value.replace(",", " ").replace("，", " ").split() if tok]
        return []
    if kind == "template_list":
        return value if isinstance(value, list) else []
    return "" if value is None else str(value)


class StewardWebService:
    """把插件内部状态整理成 WebUI 需要的 JSON 结构。"""

    def __init__(self, ctx: FeatureContext, plugin: Any) -> None:
        self.ctx = ctx
        self.plugin = plugin
        self._schema: dict[str, Any] | None = None

    # ------------------------------------------------------------ 基础访问器

    @property
    def config(self) -> StewardConfig:
        return self.ctx.config

    @property
    def store(self) -> Any:
        return self.ctx.store

    @property
    def schema(self) -> dict[str, Any]:
        """懒加载 _conf_schema.json，作为字段元信息的唯一来源。"""
        if self._schema is None:
            path = self.config.plugin_dir / "_conf_schema.json"
            try:
                self._schema = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"{LOG_TAG} 读取配置模板失败：{exc}")
                self._schema = {}
        return self._schema

    @property
    def field_schema(self) -> dict[str, Any]:
        return (self.schema.get("default") or {}).get("items") or {}

    def version(self) -> str:
        """从 metadata.yaml 读版本号，读不到就返回未知。"""
        path = self.config.plugin_dir / "metadata.yaml"
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("version:"):
                    return line.split(":", 1)[1].strip().strip('"').strip("'")
        except Exception:  # noqa: BLE001
            pass
        return "unknown"

    # ------------------------------------------------------------------ 概览

    async def overview(self) -> dict[str, Any]:
        groups = await self.ctx.groups.list_groups_with_bot_roles()
        pending = await self.plugin.join.pending()
        stats = await self.ctx.audit.stats(7)
        curfew = self.plugin.curfew.snapshot()
        return {
            "plugin": {
                "name": PLUGIN_NAME,
                "display_name": DISPLAY_NAME,
                "version": self.version(),
            },
            "groups": {
                "total": len(groups),
                "customized": sum(1 for g in groups if g.get("customized")),
                "bot_admin": sum(1 for g in groups if g.get("bot_role") in {"admin", "owner"}),
            },
            "audit": {
                "enabled": self.ctx.audit.enabled,
                "total": await self.ctx.audit.count(),
                "recent": stats,
            },
            "pending_joins": len(pending),
            "curfew": curfew,
            "backend": backend_label(None),
            "undo_window": self.ctx.undo.window,
            "generated_at": format_datetime(time.time()),
        }

    # ------------------------------------------------------------------ 群列表

    async def groups(self, force: bool = False) -> list[dict[str, Any]]:
        return await self.ctx.groups.list_groups_with_bot_roles(force=force)

    # ------------------------------------------------------------ 字段元信息

    def fields(self) -> list[dict[str, Any]]:
        """返回所有群配置字段的元信息，前端据此渲染表单。"""
        meta = self.field_schema
        out: list[dict[str, Any]] = []
        for field, label in FIELD_LABELS.items():
            item = meta.get(field) or {}
            out.append(
                {
                    "field": field,
                    "label": label,
                    "type": str(item.get("type") or "string"),
                    "description": str(item.get("description") or label),
                    "hint": str(item.get("hint") or ""),
                    "options": item.get("options") or [],
                    "default": self.store.defaults.get(field),
                }
            )
        return out

    # ------------------------------------------------------------ 群配置读写

    def group_config(self, group_id: str) -> dict[str, Any]:
        gid = str(group_id)
        return {
            "group_id": gid,
            "follows_default": self.store.follows_default(gid),
            "overrides": self.store.overrides(gid),
            "values": self.store.snapshot(gid),
            "text": self.store.export_lines(gid),
        }

    async def save_group_config(self, group_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        gid = str(group_id or "").strip()
        if not gid:
            raise ValueError("缺少群号")
        meta = self.field_schema
        cleaned: dict[str, Any] = {}
        unknown: list[str] = []
        for field, value in (changes or {}).items():
            if field not in FIELD_LABELS:
                unknown.append(field)
                continue
            cleaned[field] = _coerce(meta.get(field) or {}, value)
        if cleaned:
            await self.store.update(gid, cleaned)
        return {"updated": sorted(cleaned), "unknown": unknown, **self.group_config(gid)}

    async def import_group_text(self, group_id: str, text: str) -> dict[str, Any]:
        gid = str(group_id or "").strip()
        if not gid:
            raise ValueError("缺少群号")
        updated, unknown = await self.store.import_lines(gid, text or "")
        return {"updated": updated, "unknown": unknown, **self.group_config(gid)}

    async def reset_group(self, group_id: str | None) -> dict[str, Any]:
        gid = str(group_id or "").strip()
        await self.store.follow_default(gid or None)
        return {"reset": gid or "all"}

    # ------------------------------------------------------------ 默认模板读写

    def defaults(self) -> dict[str, Any]:
        return {"values": dict(self.store.defaults), "fields": self.fields()}

    async def save_defaults(self, changes: dict[str, Any]) -> dict[str, Any]:
        meta = self.field_schema
        raw = self.config.raw
        block = raw.get("default")
        if not isinstance(block, dict):
            raise ValueError("配置模板缺少 default 分区")
        updated: list[str] = []
        for field, value in (changes or {}).items():
            if field not in FIELD_LABELS:
                continue
            block[field] = _coerce(meta.get(field) or {}, value)
            updated.append(field)
        if updated:
            self.config.save()
            self.store.set_defaults(self.config.build_group_defaults())
        return {"updated": sorted(updated), "values": dict(self.store.defaults)}

    # ---------------------------------------------------------------- 权限矩阵

    def perms(self) -> dict[str, Any]:
        items = (self.schema.get("perms") or {}).get("items") or {}
        current = self.config.perms
        rows = []
        for key, meta in items.items():
            rows.append(
                {
                    "key": key,
                    "label": str(meta.get("description") or key),
                    "value": str(current.get(key) or meta.get("default") or "管理员"),
                    "default": str(meta.get("default") or "管理员"),
                }
            )
        return {"options": PERM_OPTIONS, "rows": rows}

    def save_perms(self, changes: dict[str, Any]) -> dict[str, Any]:
        items = (self.schema.get("perms") or {}).get("items") or {}
        raw = self.config.raw
        block = raw.get("perms")
        if not isinstance(block, dict):
            block = {}
            raw["perms"] = block
        updated: list[str] = []
        for key, value in (changes or {}).items():
            if key not in items:
                continue
            label = str(value)
            if label not in PERM_OPTIONS:
                raise ValueError(f"未知权限等级：{label}")
            block[key] = label
            updated.append(key)
        if updated:
            self.config.save()
        return {"updated": sorted(updated)}

    # ------------------------------------------------------------ 顶层配置读写

    def settings(self) -> dict[str, Any]:
        raw = self.config.raw
        sections: list[dict[str, Any]] = []
        for name, title in SETTING_SECTIONS.items():
            meta = (self.schema.get(name) or {}).get("items") or {}
            values = raw.get(name) if isinstance(raw.get(name), dict) else {}
            sections.append(
                {
                    "section": name,
                    "title": title,
                    "items": [
                        {
                            "key": key,
                            "label": str(item.get("description") or key),
                            "hint": str(item.get("hint") or ""),
                            "type": str(item.get("type") or "string"),
                            "options": item.get("options") or [],
                            "value": values.get(key, item.get("default")),
                        }
                        for key, item in meta.items()
                    ],
                }
            )
        scalars = []
        for key, title in SETTING_SCALARS.items():
            item = self.schema.get(key) or {}
            scalars.append(
                {
                    "key": key,
                    "label": str(item.get("description") or title),
                    "hint": str(item.get("hint") or ""),
                    "type": str(item.get("type") or "string"),
                    "value": raw.get(key, item.get("default")),
                }
            )
        return {"sections": sections, "scalars": scalars}

    def save_settings(self, section: str, changes: dict[str, Any]) -> dict[str, Any]:
        raw = self.config.raw
        updated: list[str] = []
        if section:
            if section not in SETTING_SECTIONS:
                raise ValueError(f"未知配置分区：{section}")
            meta = (self.schema.get(section) or {}).get("items") or {}
            block = raw.get(section)
            if not isinstance(block, dict):
                block = {}
                raw[section] = block
            for key, value in (changes or {}).items():
                if key not in meta:
                    continue
                block[key] = _coerce(meta[key], value)
                updated.append(f"{section}.{key}")
        else:
            for key, value in (changes or {}).items():
                if key not in SETTING_SCALARS:
                    continue
                raw[key] = _coerce(self.schema.get(key) or {}, value)
                updated.append(key)
        if updated:
            self.config.save()
        return {"updated": sorted(updated)}

    # ---------------------------------------------------------------- 违禁词

    def words(self, group_id: str) -> dict[str, Any]:
        gid = str(group_id)
        return {
            "group_id": gid,
            "custom": [str(w) for w in (self.store.value(gid, "custom_ban_words") or [])],
            "whitelist": [str(w) for w in (self.store.value(gid, "word_whitelist") or [])],
            "builtin_enabled": bool(self.store.value(gid, "builtin_ban")),
            "builtin_count": len(self.plugin.words.builtin_words),
            "match_mode": str(self.store.value(gid, "word_match_mode") or "包含匹配"),
            "action": str(self.store.value(gid, "word_action") or "撤回并禁言"),
            "exempt_level": str(self.store.value(gid, "word_exempt_level") or "管理员"),
            "ban_time": parse_int(self.store.value(gid, "word_ban_time"), 0) or 0,
        }

    # ---------------------------------------------------------------- 审计日志

    async def audit(
        self,
        *,
        group_id: str | None = None,
        action: str | None = None,
        keyword: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        rows = await self.ctx.audit.query(
            group_id=group_id or None,
            action=action or None,
            keyword=keyword or None,
            limit=max(1, min(200, limit)),
            offset=max(0, offset),
        )
        return {
            "total": await self.ctx.audit.count(group_id=group_id or None),
            "rows": [
                {
                    **dict(row),
                    "action_label": action_label(str(dict(row).get("action") or "")),
                    "created_text": format_datetime(dict(row).get("ts")),
                }
                for row in rows
            ],
        }

    # ------------------------------------------------------------ 进群待审队列

    async def pending_joins(self, group_id: str | None = None) -> list[dict[str, Any]]:
        items = await self.plugin.join.pending(group_id or None)
        for item in items:
            item["created_text"] = format_datetime(item.get("created_at"))
        return items

    # ---------------------------------------------------------------- 撤销栈

    def undo_stack(self, group_id: str) -> list[dict[str, Any]]:
        return [
            {
                "action": entry.action,
                "description": entry.description,
                "operator_id": entry.operator_id,
                "created_text": format_datetime(entry.created_at),
                "expired": entry.expired(self.ctx.undo.window),
            }
            for entry in self.ctx.undo.pending(group_id)
        ]

    async def run_undo(self, group_id: str) -> dict[str, Any]:
        gid = str(group_id or "").strip()
        if not gid:
            raise ValueError("缺少群号")
        ok, message = await self.ctx.undo.undo(gid)
        return {"success": ok, "message": message}

    # ---------------------------------------------------------------- 群相册

    async def album(self, group_id: str) -> dict[str, Any]:
        gid = str(group_id)
        options: list[dict[str, str]] = []
        try:
            options = await self.plugin.album.album_options(gid)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"{LOG_TAG} 获取相册列表失败 group={gid}: {exc}")
        return {
            "group_id": gid,
            "albums": options,
            "keywords": self.plugin.album.keyword_snapshot().get(gid, []),
            "backup_enabled": self.plugin.album.backup_enabled,
            "random_groups": self.plugin.album.random_groups,
            "show_title": self.plugin.album.show_title,
            "max_stitch_count": self.plugin.album.max_stitch_count,
        }

    # ---------------------------------------------------------------- 运行状态

    def runtime(self) -> dict[str, Any]:
        return {
            "curfew": self.plugin.curfew.snapshot(),
            "perm_levels": [
                {"value": level.value, "label": level.label}
                for level in (
                    PermLevel.MEMBER,
                    PermLevel.HIGH,
                    PermLevel.ADMIN,
                    PermLevel.OWNER,
                    PermLevel.SUPERUSER,
                )
            ],
            "keywords": self.plugin.album.keyword_snapshot(),
        }

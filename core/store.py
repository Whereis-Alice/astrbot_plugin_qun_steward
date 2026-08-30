"""群配置存储。

设计要点：
* 数据库里只保存"被显式改过的字段"（稀疏覆写），没有记录的群自动跟随默认值；
  这样改默认值能立刻影响所有未覆写的群，也不会因为一次接口抖动就丢配置。
* 内存里维护全量覆写缓存，读取是同步的，指令热路径不碰磁盘。
"""

from __future__ import annotations

import json
import time
from typing import Any

from astrbot.api import logger

from .config import LOG_TAG
from .db import Database

# 中文字段名 <-> 内部字段名。中文名用于「群务配置」指令的导入导出，
# 前 16 项与上游插件保持一致，方便老用户迁移配置文本。
FIELD_LABELS: dict[str, str] = {
    "join_switch": "进群审核",
    "join_min_level": "进群等级门槛",
    "join_max_time": "进群尝试次数",
    "join_accept_words": "进群白词",
    "join_reject_words": "进群黑词",
    "join_no_match_reject": "未中白词拒绝",
    "reject_word_block": "命中黑词拉黑",
    "block_ids": "进群黑名单",
    "join_welcome": "进群欢迎词",
    "join_ban_time": "进群禁言时长",
    "leave_notify": "主动退群通知",
    "leave_block": "主动退群拉黑",
    "builtin_ban": "启用内置禁词",
    "custom_ban_words": "自定义违禁词",
    "word_ban_time": "禁词禁言时长",
    "spamming_ban_time": "刷屏禁言时长",
    "word_match_mode": "禁词匹配方式",
    "word_action": "禁词处理动作",
    "word_exempt_level": "禁词豁免等级",
    "word_whitelist": "禁词白名单",
    "spamming_count": "刷屏判定条数",
    "spamming_interval": "刷屏判定间隔",
    "random_ban_time": "随机禁言区间",
    "admin_audit": "申请私聊超管",
    "level_threshold": "高等级成员门槛",
}
LABEL_FIELDS: dict[str, str] = {label: field for field, label in FIELD_LABELS.items()}

_TRUE_LABELS = frozenset({"开", "开启", "启用", "true", "1", "是"})


class GroupStore:
    """按群存取配置，读缓存 + 写穿透。"""

    def __init__(self, db: Database, defaults: dict[str, Any]) -> None:
        self._db = db
        self._defaults: dict[str, Any] = dict(defaults)
        self._overrides: dict[str, dict[str, Any]] = {}
        self._loaded = False

    # ------------------------------------------------------------ 生命周期 --- #
    async def load(self) -> None:
        rows = await self._db.fetch_all("SELECT group_id, data FROM group_config")
        overrides: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                parsed = json.loads(row["data"])
            except (TypeError, ValueError):
                logger.warning(f"{LOG_TAG} 群 {row['group_id']} 的配置无法解析，已忽略")
                continue
            if isinstance(parsed, dict):
                overrides[str(row["group_id"])] = parsed
        self._overrides = overrides
        self._loaded = True

    def set_defaults(self, defaults: dict[str, Any]) -> None:
        """默认值变更后刷新（例如用户在 WebUI 改了全局默认）。"""
        self._defaults = dict(defaults)

    @property
    def defaults(self) -> dict[str, Any]:
        return dict(self._defaults)

    # ---------------------------------------------------------------- 读取 --- #
    def snapshot(self, group_id: Any) -> dict[str, Any]:
        """群的生效配置（默认值 + 覆写），同步读取。"""
        merged = dict(self._defaults)
        merged.update(self._overrides.get(str(group_id), {}))
        return merged

    def value(self, group_id: Any, field: str, default: Any = None) -> Any:
        """读单个字段的生效值，同步。"""
        override = self._overrides.get(str(group_id), {})
        if field in override:
            return override[field]
        if field in self._defaults:
            return self._defaults[field]
        return default

    def overrides(self, group_id: Any) -> dict[str, Any]:
        """该群显式覆写过的字段。"""
        return dict(self._overrides.get(str(group_id), {}))

    def follows_default(self, group_id: Any) -> bool:
        return str(group_id) not in self._overrides

    def overridden_group_ids(self) -> list[str]:
        return sorted(self._overrides.keys())

    # ---------------------------------------------------------------- 写入 --- #
    async def set(self, group_id: Any, field: str, value: Any) -> None:
        await self.update(group_id, {field: value})

    async def update(self, group_id: Any, changes: dict[str, Any]) -> None:
        """写入若干字段。与默认值相同的字段会被移出覆写，保持记录精简。"""
        if not changes:
            return
        gid = str(group_id)
        override = dict(self._overrides.get(gid, {}))
        for field, value in changes.items():
            if field in self._defaults and self._defaults[field] == value:
                override.pop(field, None)
            else:
                override[field] = value
        await self._persist(gid, override)

    async def replace_overrides(self, group_id: Any, override: dict[str, Any]) -> None:
        """整表替换某群的覆写字段。"""
        gid = str(group_id)
        cleaned = {
            field: value
            for field, value in override.items()
            if not (field in self._defaults and self._defaults[field] == value)
        }
        await self._persist(gid, cleaned)

    async def follow_default(self, group_id: Any | None = None) -> None:
        """删除覆写，让群回到跟随默认值的状态；*group_id* 为 None 时清空全部。"""
        if group_id is None:
            self._overrides.clear()
            await self._db.execute("DELETE FROM group_config")
            return
        gid = str(group_id)
        self._overrides.pop(gid, None)
        await self._db.execute("DELETE FROM group_config WHERE group_id = ?", (gid,))

    async def _persist(self, gid: str, override: dict[str, Any]) -> None:
        if not override:
            self._overrides.pop(gid, None)
            await self._db.execute("DELETE FROM group_config WHERE group_id = ?", (gid,))
            return
        self._overrides[gid] = override
        await self._db.execute(
            "INSERT INTO group_config (group_id, data, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(group_id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
            (gid, json.dumps(override, ensure_ascii=False), time.time()),
        )

    # ------------------------------------------------------- 中文导入导出 --- #
    def export_lines(self, group_id: Any) -> str:
        """把群配置导出成「中文字段: 值」的多行文本。"""
        snapshot = self.snapshot(group_id)
        lines: list[str] = []
        for field, label in FIELD_LABELS.items():
            if field not in snapshot:
                continue
            lines.append(f"{label}: {self._to_text(snapshot[field])}")
        return "\n".join(lines)

    async def import_lines(self, group_id: Any, text: str) -> tuple[list[str], list[str]]:
        """解析「中文字段: 值」文本并写入，返回 (已更新字段中文名, 未识别行)。"""
        changes: dict[str, Any] = {}
        unknown: list[str] = []
        for raw_line in (text or "").replace("；", "\n").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            label, separator, value = self._split_line(line)
            if not separator:
                unknown.append(line)
                continue
            field = LABEL_FIELDS.get(label.strip())
            if not field:
                unknown.append(line)
                continue
            changes[field] = self._from_text(field, value.strip())
        if changes:
            await self.update(group_id, changes)
        return [FIELD_LABELS[field] for field in changes], unknown

    @staticmethod
    def _split_line(line: str) -> tuple[str, str, str]:
        for separator in ("：", ":", "="):
            if separator in line:
                label, _, value = line.partition(separator)
                return label, separator, value
        return line, "", ""

    @staticmethod
    def _to_text(value: Any) -> str:
        if isinstance(value, bool):
            return "开" if value else "关"
        if isinstance(value, (list, tuple)):
            return " ".join(str(item) for item in value)
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    def _from_text(self, field: str, text: str) -> Any:
        reference = self._defaults.get(field)
        if isinstance(reference, bool):
            return text.strip().lower() in _TRUE_LABELS
        if isinstance(reference, list):
            return [token for token in text.replace("、", " ").split() if token]
        if isinstance(reference, int):
            try:
                return int(float(text))
            except ValueError:
                return reference
        if isinstance(reference, float):
            try:
                return float(text)
            except ValueError:
                return reference
        return text

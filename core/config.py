"""插件配置访问层。

把 AstrBot 的裸 dict 配置包成显式、带类型的属性访问，避免各处散落
`self.conf.get("a", {}).get("b", 0)` 这类写法。
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from astrbot.api.star import Context, StarTools

from .utils import parse_int, parse_time_range

PLUGIN_NAME = "astrbot_plugin_qun_steward"
DISPLAY_NAME = "群务管家"
LOG_TAG = "[群务管家]"
DB_FILENAME = "qun_steward.db"

MAX_BAN_SECONDS = 2592000  # 30 天，QQ 单次禁言上限


class Section:
    """只读的配置子节，缺键时回落到给定默认值。"""

    __slots__ = ("_data", "_defaults")

    def __init__(self, data: Any, defaults: dict[str, Any] | None = None) -> None:
        self._data = data if isinstance(data, dict) else {}
        self._defaults = defaults or {}

    def get(self, key: str, default: Any = None) -> Any:
        value = self._data.get(key)
        if value is None:
            return self._defaults.get(key, default)
        return value

    def int(self, key: str, default: int = 0) -> int:
        return parse_int(self.get(key, default), default) or 0

    def float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.get(key, default))
        except (TypeError, ValueError):
            return default

    def bool(self, key: str, default: bool = False) -> bool:
        value = self.get(key, default)
        return bool(value) if isinstance(value, bool) else default

    def str(self, key: str, default: str = "") -> str:
        value = self.get(key, default)
        return str(value) if value is not None else default

    def list(self, key: str) -> list[Any]:
        value = self.get(key, [])
        return list(value) if isinstance(value, (list, tuple)) else []

    def as_dict(self) -> dict[str, Any]:
        return dict(self._data)


class StewardConfig:
    """插件全局配置 + 运行时路径。"""

    def __init__(self, raw: Any, context: Context) -> None:
        self._raw = raw
        self._context = context

        self.data_dir: Path = StarTools.get_data_dir(PLUGIN_NAME)
        self.plugin_dir: Path = Path(__file__).resolve().parent.parent
        self.resource_dir: Path = self.plugin_dir / "resources"

        self.db_path: Path = self.data_dir / DB_FILENAME
        self.lexicon_path: Path = self.resource_dir / "SensitiveLexicon.json"
        self.notice_dir: Path = self.data_dir / "notice"
        self.file_dir: Path = self.data_dir / "files"
        self.album_dir: Path = self.data_dir / "album"
        self.font_dir: Path = self.data_dir / "fonts"
        self.curfew_path: Path = self.data_dir / "curfew.json"

        for directory in (self.notice_dir, self.file_dir, self.album_dir, self.font_dir):
            directory.mkdir(parents=True, exist_ok=True)

    # ----------------------------------------------------------------- raw --- #
    @property
    def raw(self) -> Any:
        return self._raw

    def save(self) -> None:
        """把内存中的改动写回 AstrBot 配置文件。"""
        saver = getattr(self._raw, "save_config", None)
        if callable(saver):
            saver()

    # -------------------------------------------------------------- 顶层项 --- #
    @property
    def group_defaults(self) -> dict[str, Any]:
        """群级默认配置（`default` 节）。"""
        value = self._raw.get("default")
        return dict(value) if isinstance(value, dict) else {}

    @property
    def admin_audit(self) -> bool:
        return bool(self._raw.get("admin_audit", False))

    @property
    def random_ban_time(self) -> str:
        return str(self._raw.get("random_ban_time") or "30~300")

    @property
    def level_threshold(self) -> int:
        return parse_int(self._raw.get("level_threshold"), 50) or 50

    @property
    def llm_get_msg_count(self) -> int:
        return parse_int(self._raw.get("llm_get_msg_count"), 10) or 10

    @property
    def enable_llm_tools(self) -> bool:
        return bool(self._raw.get("enable_llm_tools", True))

    @property
    def vote_ban(self) -> Section:
        return Section(
            self._raw.get("vote_ban"),
            {"ttl": 120, "threshold": 3, "allow_self_vote": False, "mode": "两者都行"},
        )

    @property
    def safety(self) -> Section:
        return Section(
            self._raw.get("safety"),
            {
                "batch_interval": 0.4,
                "max_batch_kick": 50,
                "at_targets_on_clear": False,
                "undo_window": 900,
            },
        )

    @property
    def audit(self) -> Section:
        return Section(self._raw.get("audit"), {"enable": True, "retain_days": 30})

    @property
    def album(self) -> Section:
        return Section(
            self._raw.get("album"),
            {
                "default_albums": [],
                "backup_media": False,
                "random_album_groups": [],
                "level_threshold": 0,
                "show_title": True,
                "max_stitch_count": 20,
                "cloud_random": True,
            },
        )

    @property
    def output(self) -> Section:
        """长列表输出方式（合并转发 / 长图 / 纯文本）。"""
        return Section(
            self._raw.get("output"),
            {"long_list_mode": "合并转发", "node_lines": 15},
        )

    @property
    def voice(self) -> Section:
        """AI 声聊设置。"""
        return Section(
            self._raw.get("voice"),
            {"default_character": "", "chat_type": 1},
        )

    @property
    def fonts(self) -> Section:
        return Section(
            self._raw.get("fonts"),
            {
                "custom_font_path": "",
                "custom_font_bold_path": "",
                "auto_download": False,
                "download_manifest": "",
            },
        )

    @property
    def perms(self) -> dict[str, Any]:
        value = self._raw.get("perms")
        return dict(value) if isinstance(value, dict) else {}

    @property
    def admins_id(self) -> list[str]:
        """AstrBot 全局超管列表（只保留纯数字 QQ 号）。"""
        try:
            raw_admins = self._context.get_config().get("admins_id", [])
        except Exception:  # noqa: BLE001
            return []
        return [str(item) for item in raw_admins if str(item).isdigit()]

    @property
    def timezone(self) -> str:
        try:
            return str(self._context.get_config().get("timezone") or "Asia/Shanghai")
        except Exception:  # noqa: BLE001
            return "Asia/Shanghai"

    # ------------------------------------------------------------ 群级默认 --- #
    def build_group_defaults(self) -> dict[str, Any]:
        """群配置的完整默认值：`default` 节 + 可按群覆写的顶层项。"""
        defaults = self.group_defaults
        defaults.setdefault("random_ban_time", "")
        return {
            **defaults,
            "admin_audit": self.admin_audit,
            "level_threshold": self.level_threshold,
            "vote_ban": self.vote_ban.as_dict() or {"ttl": 120, "threshold": 3},
            "perms": self.perms,
        }

    # -------------------------------------------------------------- 禁言时长 --- #
    def ban_time_range(self, group_range: Any = None) -> tuple[int, int]:
        """取随机禁言区间：群级设置优先，其次全局设置。"""
        if str(group_range or "").strip():
            return parse_time_range(group_range)
        return parse_time_range(self.random_ban_time)

    def resolve_ban_time(self, group_range: Any, seconds: Any = None) -> int:
        """把用户输入的秒数规整成合法禁言时长；未指定则在随机区间内取值。"""
        parsed = parse_int(seconds)
        if parsed is None:
            low, high = self.ban_time_range(group_range)
            return random.randint(low, high)
        return max(0, min(parsed, MAX_BAN_SECONDS))

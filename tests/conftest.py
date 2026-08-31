"""测试共用夹具。

这里的测试只覆盖「纯逻辑」层：配置解析、群配置存储、权限模型、审计、撤销栈、
协议端响应归一化等。它们不需要真实的 AstrBot 运行时，因此依赖统一用最小替身注入。

插件本体通过 pyproject.toml 里的 pytest `pythonpath` 设置导入，无需手改 sys.path。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from astrbot_plugin_qun_steward.core.config import Section
from astrbot_plugin_qun_steward.core.db import Database
from astrbot_plugin_qun_steward.core.store import GroupStore

PLUGIN_DIR = Path(__file__).resolve().parents[1]

#: 与 _conf_schema.json 的 default 节保持一致的群级默认值（另含两个可按群覆写的顶层项）
GROUP_DEFAULTS: dict[str, Any] = {
    "join_switch": True,
    "join_min_level": 8,
    "join_max_time": 3,
    "join_accept_words": [],
    "join_reject_words": [],
    "join_no_match_reject": False,
    "reject_word_block": False,
    "block_ids": [],
    "join_welcome": "",
    "join_ban_time": 0,
    "leave_notify": False,
    "leave_block": False,
    "builtin_ban": False,
    "custom_ban_words": [],
    "word_ban_time": 360,
    "word_match_mode": "包含匹配",
    "word_action": "撤回并禁言",
    "word_exempt_level": "管理员",
    "word_whitelist": [],
    "spamming_ban_time": 0,
    "spamming_count": 5,
    "spamming_interval": 0.5,
    "random_ban_time": "",
    "admin_audit": False,
    "level_threshold": 50,
    "perms": {},
}

_DEFAULT_SECTIONS: dict[str, dict[str, Any]] = {
    "vote_ban": {
        "ttl": 120,
        "threshold": 3,
        "allow_self_vote": False,
        "mode": "两者都行",
        "agree_emoji": "76",
        "disagree_emoji": "77",
    },
    "safety": {
        "batch_interval": 0.4,
        "max_batch_kick": 50,
        "at_targets_on_clear": False,
        "undo_window": 900,
    },
    "audit": {"enable": True, "retain_days": 30},
    "album": {
        "level_threshold": 0,
        "show_title": True,
        "max_stitch_count": 20,
        "cloud_random": True,
    },
    "fonts": {"auto_download": False},
    "output": {"long_list_mode": "合并转发", "node_lines": 15},
    "voice": {"default_character": "", "chat_type": 1},
}


class FakeConfig:
    """StewardConfig 的最小替身：只实现被测代码真正读到的属性。

    用 `FakeConfig(safety={"undo_window": 1})` 这样的写法覆盖单个分区里的键。
    """

    def __init__(self, **overrides: Any) -> None:
        self.plugin_dir: Path = PLUGIN_DIR
        self.admins_id: list[str] = [str(item) for item in overrides.pop("admins_id", [])]
        self.perms: dict[str, Any] = dict(overrides.pop("perms", {}))
        self.level_threshold: int = int(overrides.pop("level_threshold", 50))
        self.admin_audit: bool = bool(overrides.pop("admin_audit", False))
        self.random_ban_time: str = str(overrides.pop("random_ban_time", "30~300"))
        self.llm_get_msg_count: int = int(overrides.pop("llm_get_msg_count", 10))
        self.enable_llm_tools: bool = bool(overrides.pop("enable_llm_tools", True))
        self.timezone: str = "Asia/Shanghai"
        self._sections: dict[str, dict[str, Any]] = {
            name: dict(values) for name, values in _DEFAULT_SECTIONS.items()
        }
        for name, values in overrides.items():
            if not isinstance(values, dict):
                raise TypeError("分区覆写必须是 dict：" + name)
            self._sections.setdefault(name, {}).update(values)

    def _section(self, name: str) -> Section:
        return Section(self._sections.get(name, {}), _DEFAULT_SECTIONS.get(name, {}))

    @property
    def vote_ban(self) -> Section:
        return self._section("vote_ban")

    @property
    def safety(self) -> Section:
        return self._section("safety")

    @property
    def audit(self) -> Section:
        return self._section("audit")

    @property
    def album(self) -> Section:
        return self._section("album")

    @property
    def fonts(self) -> Section:
        return self._section("fonts")

    @property
    def output(self) -> Section:
        return self._section("output")

    @property
    def voice(self) -> Section:
        return self._section("voice")

    @property
    def group_defaults(self) -> dict[str, Any]:
        return dict(GROUP_DEFAULTS)


@pytest.fixture
def config() -> FakeConfig:
    return FakeConfig()


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = Database(tmp_path / "steward-test.db")
    await db.connect()
    try:
        yield db
    finally:
        await db.close()


@pytest.fixture
async def store(database: Database) -> GroupStore:
    group_store = GroupStore(database, GROUP_DEFAULTS)
    await group_store.load()
    return group_store


@pytest.fixture
def plugin_dir() -> Path:
    """插件根目录，供仓库完整性测试读取资源文件。"""
    return PLUGIN_DIR


@pytest.fixture
def group_defaults() -> dict[str, Any]:
    """群级默认配置的独立副本，测试里可以随意改。"""
    return dict(GROUP_DEFAULTS)


@pytest.fixture
def make_config() -> Callable[..., FakeConfig]:
    """配置替身工厂，例如 make_config(safety={"undo_window": 1})。"""
    return FakeConfig

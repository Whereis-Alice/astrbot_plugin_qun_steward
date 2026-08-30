"""权限等级模型与权限矩阵解析。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from astrbot_plugin_qun_steward.core.permission import (
    PERM_OPTIONS,
    PermissionResolver,
    PermLevel,
)
from astrbot_plugin_qun_steward.core.store import GroupStore

GID = "10001"
ConfigFactory = Callable[..., Any]


class TestPermLevel:
    def test_ordering(self) -> None:
        assert (
            PermLevel.UNKNOWN
            < PermLevel.MEMBER
            < PermLevel.HIGH
            < PermLevel.ADMIN
            < PermLevel.OWNER
            < PermLevel.SUPERUSER
        )

    def test_labels(self) -> None:
        assert PermLevel.SUPERUSER.label == "超管"
        assert PermLevel.OWNER.label == "群主"
        assert PermLevel.ADMIN.label == "管理员"
        assert PermLevel.HIGH.label == "高等级成员"
        assert PermLevel.MEMBER.label == "成员"
        assert PermLevel.UNKNOWN.label == "未知"

    def test_str_is_label(self) -> None:
        # 便于直接拼进提示文案
        assert "需要" + str(PermLevel.OWNER) + "权限" == "需要群主权限"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("超管", PermLevel.SUPERUSER),
            ("群主", PermLevel.OWNER),
            ("管理员", PermLevel.ADMIN),
            ("高等级成员", PermLevel.HIGH),
            ("成员", PermLevel.MEMBER),
            ("未知", PermLevel.UNKNOWN),
            ("无权限", PermLevel.UNKNOWN),
            ("OWNER", PermLevel.OWNER),
            ("member", PermLevel.MEMBER),
            ("  群主  ", PermLevel.OWNER),
        ],
    )
    def test_from_label(self, raw: str, expected: PermLevel) -> None:
        assert PermLevel.from_label(raw) is expected

    def test_from_label_falls_back_to_admin(self) -> None:
        # 配置里写了不认识的值时，宁可收紧到管理员，也不要放开给所有人
        assert PermLevel.from_label("乱写的") is PermLevel.ADMIN
        assert PermLevel.from_label("") is PermLevel.ADMIN
        assert PermLevel.from_label(None) is PermLevel.ADMIN

    def test_from_label_custom_default(self) -> None:
        assert PermLevel.from_label("", PermLevel.MEMBER) is PermLevel.MEMBER

    def test_perm_options_match_labels(self) -> None:
        assert [
            PermLevel.SUPERUSER.label,
            PermLevel.OWNER.label,
            PermLevel.ADMIN.label,
            PermLevel.HIGH.label,
            PermLevel.MEMBER.label,
        ] == PERM_OPTIONS


class TestRequiredLevel:
    async def test_no_perm_key_means_everyone(
        self, store: GroupStore, make_config: ConfigFactory
    ) -> None:
        resolver = PermissionResolver(make_config(), store)
        assert resolver.required_level(GID, None) is PermLevel.MEMBER
        assert resolver.required_level(GID, "") is PermLevel.MEMBER

    async def test_global_perm_matrix(
        self, store: GroupStore, make_config: ConfigFactory
    ) -> None:
        resolver = PermissionResolver(make_config(perms={"set_group_kick": "群主"}), store)
        assert resolver.required_level(GID, "set_group_kick") is PermLevel.OWNER

    async def test_group_override_wins(
        self, store: GroupStore, make_config: ConfigFactory
    ) -> None:
        resolver = PermissionResolver(make_config(perms={"set_group_kick": "群主"}), store)
        await store.set(GID, "perms", {"set_group_kick": "成员"})
        assert resolver.required_level(GID, "set_group_kick") is PermLevel.MEMBER
        # 其它群仍然沿用全局矩阵
        assert resolver.required_level("20002", "set_group_kick") is PermLevel.OWNER

    async def test_unknown_key_defaults_to_admin(
        self, store: GroupStore, make_config: ConfigFactory
    ) -> None:
        resolver = PermissionResolver(make_config(), store)
        assert resolver.required_level(GID, "从未定义过的功能") is PermLevel.ADMIN


class TestSuperuserAndThreshold:
    async def test_is_superuser(self, store: GroupStore, make_config: ConfigFactory) -> None:
        resolver = PermissionResolver(make_config(admins_id=[123456]), store)
        assert resolver.is_superuser("123456") is True
        assert resolver.is_superuser(123456) is True
        assert resolver.is_superuser("999") is False

    async def test_level_threshold_prefers_group_value(
        self, store: GroupStore, make_config: ConfigFactory
    ) -> None:
        # 群配置默认值是 50，即使全局是 40 也应读到 50
        resolver = PermissionResolver(make_config(level_threshold=40), store)
        assert resolver.level_threshold(GID) == 50

    async def test_level_threshold_group_override(
        self, store: GroupStore, make_config: ConfigFactory
    ) -> None:
        resolver = PermissionResolver(make_config(), store)
        await store.set(GID, "level_threshold", 80)
        assert resolver.level_threshold(GID) == 80

    async def test_level_threshold_ignores_non_positive(
        self, store: GroupStore, make_config: ConfigFactory
    ) -> None:
        resolver = PermissionResolver(make_config(level_threshold=45), store)
        await store.set(GID, "level_threshold", 0)
        assert resolver.level_threshold(GID) == 45

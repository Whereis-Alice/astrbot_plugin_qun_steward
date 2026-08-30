"""群配置存储：稀疏覆写、中文导入导出、持久化。"""

from __future__ import annotations

from typing import Any

from astrbot_plugin_qun_steward.core.db import Database
from astrbot_plugin_qun_steward.core.store import FIELD_LABELS, LABEL_FIELDS, GroupStore

GID = "10001"


def test_labels_are_bijective() -> None:
    # 中文名重复会让「群务配置」的导入行为变得不可预测
    assert len(LABEL_FIELDS) == len(FIELD_LABELS)


class TestSparseOverride:
    async def test_unknown_group_follows_defaults(
        self, store: GroupStore, group_defaults: dict[str, Any]
    ) -> None:
        assert store.follows_default(GID)
        assert store.snapshot(GID) == group_defaults
        assert store.overrides(GID) == {}
        assert store.value(GID, "word_ban_time") == 360
        assert store.value(GID, "不存在的字段", "兜底") == "兜底"

    async def test_set_creates_override(self, store: GroupStore) -> None:
        await store.set(GID, "word_ban_time", 60)
        assert store.overrides(GID) == {"word_ban_time": 60}
        assert store.value(GID, "word_ban_time") == 60
        assert store.snapshot(GID)["word_ban_time"] == 60
        assert not store.follows_default(GID)
        assert store.overridden_group_ids() == [GID]

    async def test_value_equal_to_default_drops_override(self, store: GroupStore) -> None:
        await store.set(GID, "word_ban_time", 60)
        await store.set(GID, "word_ban_time", 360)
        # 与默认值相同就不该继续占一条覆写记录，否则改默认值影响不到这个群
        assert store.overrides(GID) == {}
        assert store.follows_default(GID)

    async def test_update_multiple_fields(self, store: GroupStore) -> None:
        await store.update(GID, {"join_switch": False, "join_min_level": 20})
        assert store.overrides(GID) == {"join_switch": False, "join_min_level": 20}

    async def test_update_with_no_changes_is_noop(self, store: GroupStore) -> None:
        await store.update(GID, {})
        assert store.follows_default(GID)

    async def test_replace_overrides_strips_defaults(self, store: GroupStore) -> None:
        await store.replace_overrides(
            GID, {"join_switch": True, "join_min_level": 20, "word_ban_time": 360}
        )
        assert store.overrides(GID) == {"join_min_level": 20}

    async def test_follow_default_single_group(self, store: GroupStore) -> None:
        await store.set(GID, "join_min_level", 20)
        await store.follow_default(GID)
        assert store.follows_default(GID)
        assert store.overridden_group_ids() == []

    async def test_follow_default_all_groups(self, store: GroupStore) -> None:
        await store.set("1", "join_min_level", 20)
        await store.set("2", "join_min_level", 30)
        await store.follow_default()
        assert store.overridden_group_ids() == []

    async def test_int_and_str_group_ids_are_the_same_group(self, store: GroupStore) -> None:
        await store.set(10001, "join_min_level", 20)
        assert store.value("10001", "join_min_level") == 20


class TestDefaultsRefresh:
    async def test_set_defaults_affects_non_overridden_groups(
        self, store: GroupStore, group_defaults: dict[str, Any]
    ) -> None:
        group_defaults["word_ban_time"] = 999
        store.set_defaults(group_defaults)
        assert store.value(GID, "word_ban_time") == 999
        assert store.defaults["word_ban_time"] == 999

    async def test_defaults_property_returns_a_copy(self, store: GroupStore) -> None:
        snapshot = store.defaults
        snapshot["word_ban_time"] = -1
        assert store.defaults["word_ban_time"] == 360


class TestPersistence:
    async def test_overrides_survive_reload(
        self, database: Database, store: GroupStore, group_defaults: dict[str, Any]
    ) -> None:
        await store.set(GID, "join_welcome", "欢迎新朋友")
        reopened = GroupStore(database, group_defaults)
        await reopened.load()
        assert reopened.value(GID, "join_welcome") == "欢迎新朋友"

    async def test_broken_json_row_is_skipped(
        self, database: Database, group_defaults: dict[str, Any]
    ) -> None:
        await database.execute(
            "INSERT INTO group_config (group_id, data, updated_at) VALUES (?, ?, ?)",
            ("bad", "这不是 JSON", 0.0),
        )
        reopened = GroupStore(database, group_defaults)
        await reopened.load()
        assert reopened.overridden_group_ids() == []


class TestTextImportExport:
    async def test_export_uses_chinese_labels(self, store: GroupStore) -> None:
        text = store.export_lines(GID)
        assert "进群审核: 开" in text
        assert "禁词禁言时长: 360" in text
        assert "自定义违禁词: " in text

    async def test_import_updates_typed_fields(self, store: GroupStore) -> None:
        updated, unknown = await store.import_lines(
            GID,
            "\n".join(
                [
                    "进群审核：关",
                    "进群等级门槛=20",
                    "禁词白名单: 张三、李四",
                    "刷屏判定间隔: 1.5",
                    "进群欢迎词: 你好呀",
                ]
            ),
        )
        assert unknown == []
        assert set(updated) == {
            "进群审核",
            "进群等级门槛",
            "禁词白名单",
            "刷屏判定间隔",
            "进群欢迎词",
        }
        assert store.value(GID, "join_switch") is False
        assert store.value(GID, "join_min_level") == 20
        assert store.value(GID, "word_whitelist") == ["张三", "李四"]
        assert store.value(GID, "spamming_interval") == 1.5
        assert store.value(GID, "join_welcome") == "你好呀"

    async def test_import_reports_unknown_lines(self, store: GroupStore) -> None:
        updated, unknown = await store.import_lines(GID, "没有分隔符\n不认识的字段: 1")
        assert updated == []
        assert unknown == ["没有分隔符", "不认识的字段: 1"]

    async def test_import_accepts_semicolon_separated_text(self, store: GroupStore) -> None:
        updated, unknown = await store.import_lines(GID, "进群审核：关；进群等级门槛：20")
        assert unknown == []
        assert len(updated) == 2

    async def test_import_keeps_reference_on_bad_number(self, store: GroupStore) -> None:
        await store.import_lines(GID, "进群等级门槛: 一大堆")
        assert store.value(GID, "join_min_level") == 8

    async def test_roundtrip_is_stable(self, store: GroupStore) -> None:
        await store.update(GID, {"join_switch": False, "custom_ban_words": ["a", "b"]})
        exported = store.export_lines(GID)
        await store.follow_default(GID)
        await store.import_lines(GID, exported)
        assert store.value(GID, "join_switch") is False
        assert store.value(GID, "custom_ban_words") == ["a", "b"]

"""操作审计：写入、过滤查询、统计、清理。"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from astrbot_plugin_qun_steward.core.audit import ACTION_LABELS, AuditLog, action_label
from astrbot_plugin_qun_steward.core.db import Database

ConfigFactory = Callable[..., Any]
GID = "10001"


def test_action_label_falls_back_to_raw() -> None:
    assert action_label("ban") == "禁言"
    assert action_label("没登记过的动作") == "没登记过的动作"


def test_action_labels_are_non_empty() -> None:
    assert all(key and value for key, value in ACTION_LABELS.items())


class TestRecordAndQuery:
    async def test_record_then_query(
        self, database: Database, make_config: ConfigFactory
    ) -> None:
        audit = AuditLog(database, make_config())
        await audit.record(
            group_id=GID,
            action="ban",
            operator_id="10000",
            operator_name="群主小明",
            target_id="20002",
            detail="60 秒",
        )
        rows = await audit.query()
        assert len(rows) == 1
        row = rows[0]
        assert row["action"] == "ban"
        assert row["action_label"] == "禁言"
        assert row["group_id"] == GID
        assert row["operator_name"] == "群主小明"
        assert row["target_id"] == "20002"
        assert row["detail"] == "60 秒"
        assert row["source"] == "command"
        assert row["success"] is True

    async def test_disabled_audit_writes_nothing(
        self, database: Database, make_config: ConfigFactory
    ) -> None:
        audit = AuditLog(database, make_config(audit={"enable": False}))
        assert audit.enabled is False
        await audit.record(group_id=GID, action="ban")
        assert await audit.count() == 0

    async def test_query_is_newest_first(
        self, database: Database, make_config: ConfigFactory
    ) -> None:
        audit = AuditLog(database, make_config())
        for index in range(3):
            await audit.record(group_id=GID, action="ban", detail="第" + str(index) + "条")
        rows = await audit.query()
        assert [row["detail"] for row in rows] == ["第2条", "第1条", "第0条"]

    async def test_filters(self, database: Database, make_config: ConfigFactory) -> None:
        audit = AuditLog(database, make_config())
        await audit.record(group_id="1", action="ban", operator_id="a", detail="禁言小明")
        await audit.record(group_id="2", action="kick", operator_id="b", detail="踢了小红")
        assert len(await audit.query(group_id="1")) == 1
        assert len(await audit.query(action="kick")) == 1
        assert len(await audit.query(operator_id="b")) == 1
        assert len(await audit.query(keyword="小红")) == 1
        assert len(await audit.query(keyword="不存在")) == 0

    async def test_failed_action_is_marked(
        self, database: Database, make_config: ConfigFactory
    ) -> None:
        audit = AuditLog(database, make_config())
        await audit.record(group_id=GID, action="kick", success=False)
        rows = await audit.query()
        assert rows[0]["success"] is False

    async def test_limit_and_offset(
        self, database: Database, make_config: ConfigFactory
    ) -> None:
        audit = AuditLog(database, make_config())
        for index in range(5):
            await audit.record(group_id=GID, action="ban", detail=str(index))
        page = await audit.query(limit=2, offset=2)
        assert [row["detail"] for row in page] == ["2", "1"]

    async def test_count_by_group(
        self, database: Database, make_config: ConfigFactory
    ) -> None:
        audit = AuditLog(database, make_config())
        await audit.record(group_id="1", action="ban")
        await audit.record(group_id="2", action="ban")
        assert await audit.count() == 2
        assert await audit.count(group_id="1") == 1


class TestStats:
    async def test_stats_groups_by_action(
        self, database: Database, make_config: ConfigFactory
    ) -> None:
        audit = AuditLog(database, make_config())
        await audit.record(group_id=GID, action="ban")
        await audit.record(group_id=GID, action="ban")
        await audit.record(group_id=GID, action="kick")
        stats = await audit.stats(7)
        assert stats["days"] == 7
        assert stats["total"] == 3
        assert stats["by_action"][0] == {"action": "ban", "label": "禁言", "count": 2}

    async def test_stats_ignores_old_rows(
        self, database: Database, make_config: ConfigFactory
    ) -> None:
        audit = AuditLog(database, make_config())
        await _insert_at(database, int(time.time()) - 30 * 86400)
        stats = await audit.stats(7)
        assert stats["total"] == 0


class TestPurge:
    async def test_purge_removes_expired_rows(
        self, database: Database, make_config: ConfigFactory
    ) -> None:
        audit = AuditLog(database, make_config(audit={"retain_days": 7}))
        await _insert_at(database, int(time.time()) - 30 * 86400)
        await audit.record(group_id=GID, action="ban")
        assert await audit.purge() == 1
        assert await audit.count() == 1

    async def test_retain_days_zero_keeps_everything(
        self, database: Database, make_config: ConfigFactory
    ) -> None:
        audit = AuditLog(database, make_config(audit={"retain_days": 0}))
        await _insert_at(database, int(time.time()) - 365 * 86400)
        assert await audit.purge() == 0
        assert await audit.count() == 1


async def _insert_at(database: Database, ts: int) -> None:
    """直接按指定时间戳插一条记录，用于构造历史数据。"""
    await database.execute(
        "INSERT INTO audit_log (ts, group_id, operator_id, action) VALUES (?, ?, ?, ?)",
        (ts, GID, "10000", "ban"),
    )

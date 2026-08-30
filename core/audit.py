"""操作审计日志。

所有会改变群状态的操作（禁言、踢人、拉黑、改配置……）都会落一条记录，
方便事后追责，也是 WebUI「操作日志」页的数据源。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger

from .config import LOG_TAG, StewardConfig
from .db import Database

#: 动作标识 -> 中文名，用于展示
ACTION_LABELS: dict[str, str] = {
    "ban": "禁言",
    "unban": "解除禁言",
    "whole_ban": "全员禁言",
    "kick": "移出群聊",
    "block": "移出并拉黑",
    "unblock": "移出黑名单",
    "set_card": "修改群名片",
    "set_title": "修改头衔",
    "set_admin": "设置管理员",
    "unset_admin": "取消管理员",
    "essence_add": "设为精华",
    "essence_del": "移除精华",
    "recall": "撤回消息",
    "notice": "发布群公告",
    "set_group_name": "修改群名",
    "set_portrait": "修改群头像",
    "config": "修改群配置",
    "config_reset": "重置群配置",
    "curfew_on": "开启宵禁",
    "curfew_off": "关闭宵禁",
    "join_approve": "同意进群",
    "join_reject": "拒绝进群",
    "clear_member": "清理群成员",
    "file_upload": "上传群文件",
    "file_delete": "删除群文件",
    "album_upload": "上传群相册",
    "word_ban": "违禁词处置",
    "spamming_ban": "刷屏处置",
    "vote_ban": "投票禁言",
    "undo": "撤销操作",
}


def action_label(action: str) -> str:
    return ACTION_LABELS.get(action, action)


class AuditLog:
    """审计日志读写。"""

    def __init__(self, db: Database, config: StewardConfig) -> None:
        self._db = db
        self._config = config

    @property
    def enabled(self) -> bool:
        return self._config.audit.bool("enable", True)

    async def record(
        self,
        *,
        group_id: Any,
        action: str,
        operator_id: Any = "",
        operator_name: str = "",
        target_id: Any = "",
        detail: str = "",
        source: str = "command",
        success: bool = True,
    ) -> None:
        """写入一条审计记录。失败只记日志，绝不影响主流程。"""
        if not self.enabled:
            return
        try:
            await self._db.execute(
                "INSERT INTO audit_log"
                " (ts, group_id, operator_id, operator_name, action, target_id, detail,"
                "  source, success)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    int(time.time()),
                    str(group_id or ""),
                    str(operator_id or ""),
                    operator_name or "",
                    action,
                    str(target_id or ""),
                    detail or "",
                    source,
                    1 if success else 0,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 写审计日志失败 action={action}: {exc}")

    async def query(
        self,
        *,
        group_id: Any = None,
        action: str | None = None,
        operator_id: Any = None,
        keyword: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """按条件倒序查询审计记录。"""
        clauses: list[str] = []
        params: list[Any] = []
        if group_id:
            clauses.append("group_id = ?")
            params.append(str(group_id))
        if action:
            clauses.append("action = ?")
            params.append(action)
        if operator_id:
            clauses.append("operator_id = ?")
            params.append(str(operator_id))
        if keyword:
            clauses.append("(detail LIKE ? OR operator_name LIKE ? OR target_id LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, like, like])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        rows = await self._db.fetch_all(
            "SELECT id, ts, group_id, operator_id, operator_name, action, target_id,"
            f" detail, source, success FROM audit_log{where}"
            " ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        return [
            {
                "id": row["id"],
                "ts": row["ts"],
                "group_id": row["group_id"],
                "operator_id": row["operator_id"],
                "operator_name": row["operator_name"],
                "action": row["action"],
                "action_label": action_label(row["action"]),
                "target_id": row["target_id"],
                "detail": row["detail"],
                "source": row["source"],
                "success": bool(row["success"]),
            }
            for row in rows
        ]

    async def count(self, *, group_id: Any = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM audit_log"
        params: tuple[Any, ...] = ()
        if group_id:
            sql += " WHERE group_id = ?"
            params = (str(group_id),)
        row = await self._db.fetch_one(sql, params)
        return int(row["n"]) if row else 0

    async def stats(self, days: int = 7) -> dict[str, Any]:
        """近 N 天的操作概览，供 WebUI 首页卡片使用。"""
        since = int(time.time()) - max(1, days) * 86400
        rows = await self._db.fetch_all(
            "SELECT action, COUNT(*) AS n FROM audit_log WHERE ts >= ?"
            " GROUP BY action ORDER BY n DESC",
            (since,),
        )
        by_action = [
            {"action": row["action"], "label": action_label(row["action"]), "count": int(row["n"])}
            for row in rows
        ]
        return {
            "days": days,
            "total": sum(item["count"] for item in by_action),
            "by_action": by_action,
        }

    async def purge(self) -> int:
        """按保留天数清理历史记录，返回删除条数。"""
        retain = self._config.audit.int("retain_days", 30)
        if retain <= 0:
            return 0
        cutoff = int(time.time()) - retain * 86400
        row = await self._db.fetch_one("SELECT COUNT(*) AS n FROM audit_log WHERE ts < ?", (cutoff,))
        removed = int(row["n"]) if row else 0
        if removed:
            await self._db.execute("DELETE FROM audit_log WHERE ts < ?", (cutoff,))
            logger.info(f"{LOG_TAG} 已清理 {removed} 条过期审计日志")
        return removed

"""共享的 SQLite 连接。

群配置、操作审计、进群待办都放在同一个库文件里，避免多份连接互相打架。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS group_config (
    group_id   TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    group_id      TEXT NOT NULL DEFAULT '',
    operator_id   TEXT NOT NULL DEFAULT '',
    operator_name TEXT NOT NULL DEFAULT '',
    action        TEXT NOT NULL,
    target_id     TEXT NOT NULL DEFAULT '',
    detail        TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT 'command',
    success       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log (ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_group ON audit_log (group_id, ts DESC);

CREATE TABLE IF NOT EXISTS join_request (
    flag       TEXT PRIMARY KEY,
    group_id   TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    nickname   TEXT NOT NULL DEFAULT '',
    comment    TEXT NOT NULL DEFAULT '',
    level      INTEGER NOT NULL DEFAULT -1,
    created_at REAL NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',
    handled_by TEXT NOT NULL DEFAULT '',
    seq        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_join_group ON join_request (group_id, status, created_at DESC);
"""


class Database:
    """极薄的 aiosqlite 封装：一次连接，全插件共用。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    async def connect(self) -> aiosqlite.Connection:
        if self._conn is not None:
            return self._conn
        async with self._lock:
            if self._conn is None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                conn = await aiosqlite.connect(self._path)
                conn.row_factory = aiosqlite.Row
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.executescript(_SCHEMA)
                await conn.commit()
                self._conn = conn
        return self._conn

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        conn = await self.connect()
        await conn.execute(sql, params)
        await conn.commit()

    async def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        conn = await self.connect()
        async with conn.execute(sql, params) as cursor:
            return list(await cursor.fetchall())

    async def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        conn = await self.connect()
        async with conn.execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

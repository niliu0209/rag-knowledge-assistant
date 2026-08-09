"""SQLite schema 迁移执行器（owner：data/ DataAccess）。

版本化 DDL，幂等可重复执行；已应用版本记录在 schema_migrations 表。
所有 DDL 与代码同仓单真源（architecture.md 数据节字段为准）。
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

# (version, [statements])——新增版本按序追加，已发布版本不得修改
MIGRATIONS: list[tuple[str, list[str]]] = [
    (
        "v001",
        [
            # documents：文档清单（F0-2），user_id 从第一天隔离设计
            """
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,              -- UUID
                user_id TEXT NOT NULL DEFAULT 'default',
                name TEXT NOT NULL,
                category TEXT NOT NULL,           -- 开发调试/业务报告/其他（服务端白名单校验）
                file_path TEXT NOT NULL,
                page_count INTEGER,
                char_count INTEGER,
                status TEXT NOT NULL DEFAULT 'uploading',  -- uploading/ready/failed
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """,
            # qa_records：问答记录（F0-5），检索质量评估证据
            """
            CREATE TABLE IF NOT EXISTS qa_records (
                id TEXT PRIMARY KEY,              -- UUID
                user_id TEXT NOT NULL DEFAULT 'default',
                question TEXT NOT NULL,
                retrieved_chunks TEXT NOT NULL,   -- JSON（检索片段，评估用）
                answer TEXT NOT NULL,
                provider TEXT NOT NULL,
                latency_ms INTEGER,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """,
        ],
    ),
    (
        "v002",
        [
            # provider_settings：提供商配置与 Key（F0-4，S1）
            # 单行配置按 user_id 主键（阶段 0 恒 'default'）；Key 明文存本地 SQLite
            # （architecture.md 权限节：单机自用文件权限保护；阶段 1 加密存储为已确认演进）
            """
            CREATE TABLE IF NOT EXISTS provider_settings (
                user_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL DEFAULT 'preset',      -- preset | byok
                provider TEXT NOT NULL DEFAULT 'siliconflow',
                model TEXT NOT NULL,
                embedding_model TEXT NOT NULL,
                api_key TEXT,
                base_url TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """,
        ],
    ),
]


def apply_migrations(conn: sqlite3.Connection) -> None:
    """按序应用未执行的迁移；任一语句失败则整体回滚（单事务）。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version TEXT PRIMARY KEY,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    applied = {
        row[0]
        for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }
    pending = [(v, stmts) for v, stmts in MIGRATIONS if v not in applied]
    if not pending:
        return

    conn.execute("BEGIN")
    try:
        for version, statements in pending:
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (?)", (version,)
            )
            logger.info("applied schema migration %s", version)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

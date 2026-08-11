"""SQLite schema 迁移执行器（owner：data/ DataAccess）。

版本化 DDL，幂等可重复执行；已应用版本记录在 schema_migrations 表。
所有 DDL 与代码同仓单真源（architecture.md 数据节字段为准）。
语句可以是 SQL 字符串或 `callable(conn, encryptor)`（数据转换，如 v003 Key 加密）。
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# enc$v1$ 前缀（与 app.core.crypto.ENC_PREFIX 同值；迁移与存储共用此标记）
ENC_PREFIX = "enc$v1$"


def _has_plaintext_key(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM provider_settings"
        " WHERE api_key IS NOT NULL AND api_key NOT LIKE ? LIMIT 1",
        (f"{ENC_PREFIX}%",),
    ).fetchone()
    return row is not None


def _encrypt_plaintext_keys(
    conn: sqlite3.Connection,
    encryptor: Callable[[str], str] | None,
) -> None:
    """v003：明文 api_key 加密写回（幂等：已带前缀的记录跳过）。"""
    rows = conn.execute(
        "SELECT user_id, api_key FROM provider_settings"
        " WHERE api_key IS NOT NULL AND api_key NOT LIKE ?",
        (f"{ENC_PREFIX}%",),
    ).fetchall()
    if not rows:
        return
    if encryptor is None:
        raise RuntimeError(
            "v003 迁移发现明文 api_key 但未提供加密器（主密钥未配置）——"
            "拒绝静默留明文，请配置 RAG_KEY_ENCRYPTION_KEY 后重试"
        )
    for user_id, api_key in rows:
        conn.execute(
            "UPDATE provider_settings SET api_key = ? WHERE user_id = ?",
            (encryptor(api_key), user_id),
        )
    logger.info("v003 已加密 %d 条既有 api_key 记录", len(rows))


# (version, [statements])——新增版本按序追加，已发布版本不得修改
# 语句可为 SQL 字符串或 callable(conn, encryptor) 数据转换（v003 Key 加密）
MIGRATIONS: list[tuple[str, list[Any]]] = [
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
    (
        "v003",
        [
            # S1-2 Key 加密存储：既有明文 api_key 迁移为密文（enc$v1$ 前缀）。
            # 转换前自动备份原库（backup_path）；存在明文但未提供加密器时
            # 显式报错，拒绝静默留明文（主密钥未配置时应用启动即暴露）。
            _encrypt_plaintext_keys,
        ],
    ),
]


def apply_migrations(
    conn: sqlite3.Connection,
    legacy_encryptor: Callable[[str], str] | None = None,
    backup_path: Path | None = None,
) -> None:
    """按序应用未执行的迁移；任一语句失败则整体回滚（单事务）。

    - legacy_encryptor：v003 明文 Key 加密器（encrypt: str -> str）。
    - backup_path：v003 转换前备份原库（回滚路径，备份保留迁移前明文）。
    """
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
            if version == "v003" and _has_plaintext_key(conn) and backup_path:
                _backup_before_v003(conn, backup_path)
            for statement in statements:
                if callable(statement):
                    statement(conn, legacy_encryptor)
                else:
                    conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (?)", (version,)
            )
            logger.info("applied schema migration %s", version)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _backup_before_v003(conn: sqlite3.Connection, backup_path: Path) -> None:
    """迁移前备份原库（SQLite online backup，含迁移前明文，回滚依据）。"""
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    dest = sqlite3.connect(backup_path)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    # 备份含迁移前明文 Key，权限收紧为仅属主可读写
    os.chmod(backup_path, 0o600)
    logger.warning(
        "v003 迁移前已备份原库到 %s（含迁移前明文，请按数据敏感级别保管）",
        backup_path,
    )

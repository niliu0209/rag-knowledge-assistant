"""provider_settings 表存取（owner：data/ DataAccess，纯数据不做业务判断）。

单行配置按 user_id 主键（阶段 0 恒 'default'）；表结构见 migrations v002。
Key 明文存 SQLite（architecture.md 权限节：单机自用文件权限保护；阶段 1 加密演进）。
"""

from __future__ import annotations

import sqlite3
from typing import Any


def upsert_provider_settings(
    conn: sqlite3.Connection,
    user_id: str,
    mode: str,
    provider: str,
    model: str,
    embedding_model: str,
    api_key: str | None,
    base_url: str | None,
) -> None:
    """写入或覆盖单用户提供商配置（单行 upsert）。"""
    conn.execute(
        """
        INSERT INTO provider_settings
            (user_id, mode, provider, model, embedding_model, api_key, base_url)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            mode = excluded.mode,
            provider = excluded.provider,
            model = excluded.model,
            embedding_model = excluded.embedding_model,
            api_key = excluded.api_key,
            base_url = excluded.base_url,
            updated_at = datetime('now')
        """,
        (user_id, mode, provider, model, embedding_model, api_key, base_url),
    )
    conn.commit()


def get_provider_settings(
    conn: sqlite3.Connection, user_id: str
) -> dict[str, Any] | None:
    """读取单用户配置；无记录返回 None。

    按列序索引取值，不依赖调用方 row_factory 设置。
    """
    row = conn.execute(
        "SELECT mode, provider, model, embedding_model, api_key, base_url"
        " FROM provider_settings WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "mode": row[0],
        "provider": row[1],
        "model": row[2],
        "embedding_model": row[3],
        "api_key": row[4],
        "base_url": row[5],
    }

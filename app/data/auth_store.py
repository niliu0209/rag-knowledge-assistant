"""users/sessions/invite_codes 表读写（owner：data/ DataAccess，不做业务判断）。

字段与迁移见 migrations.py v004（architecture.md 阶段 2 规划数据节为字段真源）。
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path


def new_id() -> str:
    return str(uuid.uuid4())


# ---------- users ----------

def insert_user(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    username: str,
    password_hash: str,
    role: str,
    invite_code: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO users (id, username, password_hash, role, invite_code) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, username, password_hash, role, invite_code),
    )


def get_user_by_username(
    conn: sqlite3.Connection, username: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()


def get_user_by_id(conn: sqlite3.Connection, user_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def list_users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM users ORDER BY created_at ASC, id"
    ).fetchall()


def count_users(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def update_user_status(
    conn: sqlite3.Connection, user_id: str, status: str
) -> None:
    conn.execute("UPDATE users SET status = ? WHERE id = ?", (status, user_id))


def update_password(
    conn: sqlite3.Connection, user_id: str, password_hash: str
) -> None:
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id)
    )


# ---------- sessions ----------

def create_session(
    conn: sqlite3.Connection, *, session_id: str, user_id: str, expires_at: str
) -> None:
    conn.execute(
        "INSERT INTO sessions (id, user_id, expires_at) VALUES (?, ?, ?)",
        (session_id, user_id, expires_at),
    )


def get_session(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    """查有效会话（未撤销未过期）；过期/撤销由业务层判定（auth_store 不做判断）。"""
    return conn.execute(
        "SELECT * FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()


def revoke_session(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute(
        "UPDATE sessions SET revoked_at = datetime('now') WHERE id = ?",
        (session_id,),
    )


def revoke_sessions_by_user(conn: sqlite3.Connection, user_id: str) -> int:
    """停用用户时撤销其全部会话（即时失效）；返回撤销条数。"""
    cur = conn.execute(
        "UPDATE sessions SET revoked_at = datetime('now')"
        " WHERE user_id = ? AND revoked_at IS NULL",
        (user_id,),
    )
    return cur.rowcount


def delete_expired_sessions(conn: sqlite3.Connection) -> None:
    """惰性清理：登录时清掉已过期/已撤销的旧会话（防 sessions 表无限膨胀）。"""
    conn.execute("DELETE FROM sessions WHERE expires_at < datetime('now')")
    conn.execute("DELETE FROM sessions WHERE revoked_at IS NOT NULL")


# ---------- invite_codes ----------

def insert_invite_code(
    conn: sqlite3.Connection, *, code: str, created_by: str, expires_at: str | None
) -> None:
    conn.execute(
        "INSERT INTO invite_codes (code, created_by, expires_at) VALUES (?, ?, ?)",
        (code, created_by, expires_at),
    )


def get_invite_code(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM invite_codes WHERE code = ?", (code,)
    ).fetchone()


def mark_invite_used(
    conn: sqlite3.Connection, code: str, user_id: str
) -> None:
    conn.execute(
        "UPDATE invite_codes SET used_by = ?, used_at = datetime('now') WHERE code = ?",
        (user_id, code),
    )


def revoke_invite_code(conn: sqlite3.Connection, code: str) -> None:
    conn.execute(
        "UPDATE invite_codes SET revoked_at = datetime('now') WHERE code = ?",
        (code,),
    )


def list_invite_codes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    # invite_codes 建表（v004）无 created_at 列；按主键 code 排序即可
    return conn.execute("SELECT * FROM invite_codes ORDER BY code").fetchall()


def claim_default_data(
    conn: sqlite3.Connection, new_user_id: str, data_dir: Path | None = None
) -> dict[str, int]:
    """首启 admin 认领阶段 0/1 存量数据（user_id='default' → admin）。

    返回 {documents, qa_records, provider_settings} 各迁移条数（无 default 数据时全 0）。
    owner：本函数只做数据搬运，不判断是否首启（由 AuthService 决定调用时机）。
    有存量文档时同时迁移 Chroma 切片 user_id（data_dir 传入才执行）——
    只迁 SQLite 会让文档列表可见但检索不到（切片仍挂在 'default'）。
    """
    counts: dict[str, int] = {}
    for table in ("documents", "qa_records", "provider_settings"):
        cur = conn.execute(
            f"UPDATE {table} SET user_id = ? WHERE user_id = 'default'",
            (new_user_id,),
        )
        counts[table] = cur.rowcount
    if counts["documents"] > 0 and data_dir is not None:
        _reassign_chroma_user_id(data_dir, new_user_id)
    return counts


def _reassign_chroma_user_id(data_dir: Path, new_user_id: str) -> None:
    """把 Chroma 集合中 user_id='default' 的切片改挂到新 user_id。

    先读原 metadata 再合并写回（Chroma update 整体替换 metadatas，
    直接覆盖会丢 doc_id/category 等字段，破坏检索与删除）。
    """
    from app.data import chroma_store  # 延迟导入：本模块不应成为 data 层根依赖

    collection = chroma_store.get_collection(data_dir)
    got = collection.get(where={"user_id": "default"}, include=["metadatas"])
    ids: list[str] = got.get("ids") or []
    if not ids:
        return
    metadatas = got.get("metadatas") or []
    reassigned = [dict(meta, user_id=new_user_id) for meta in metadatas]
    collection.update(ids=ids, metadatas=reassigned)

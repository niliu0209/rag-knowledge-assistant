"""documents 表读写（owner：data/ DataAccess，不做业务判断）。

字段与迁移见 migrations.py v001（architecture.md 数据节为字段真源）。
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any


def new_document_id() -> str:
    return str(uuid.uuid4())


def insert_document(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    doc_id: str,
    name: str,
    category: str,
    file_path: str,
    status: str,
) -> None:
    conn.execute(
        "INSERT INTO documents (id, user_id, name, category, file_path, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (doc_id, user_id, name, category, file_path, status),
    )


def get_active_document_by_name(
    conn: sqlite3.Connection, user_id: str, name: str
) -> sqlite3.Row | None:
    """查非 failed 记录（409 语义：不静默覆盖已入库文档；failed 痕迹不阻塞重试）。"""
    return conn.execute(
        "SELECT * FROM documents WHERE user_id = ? AND name = ? AND status != 'failed'",
        (user_id, name),
    ).fetchone()


def get_failed_documents_by_name(
    conn: sqlite3.Connection, user_id: str, name: str
) -> list[sqlite3.Row]:
    """查同名的 failed 记录（重试时替换，列表不留同名残留）。"""
    return conn.execute(
        "SELECT * FROM documents WHERE user_id = ? AND name = ? AND status = 'failed'",
        (user_id, name),
    ).fetchall()


def delete_document(conn: sqlite3.Connection, doc_id: str) -> None:
    conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))


def update_document_meta(
    conn: sqlite3.Connection,
    doc_id: str,
    *,
    page_count: int | None,
    char_count: int,
    status: str,
) -> None:
    conn.execute(
        "UPDATE documents SET page_count = ?, char_count = ?, status = ? WHERE id = ?",
        (page_count, char_count, status, doc_id),
    )


def update_document_status(
    conn: sqlite3.Connection, doc_id: str, status: str
) -> None:
    conn.execute(
        "UPDATE documents SET status = ? WHERE id = ?", (status, doc_id)
    )


def get_document(conn: sqlite3.Connection, doc_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()

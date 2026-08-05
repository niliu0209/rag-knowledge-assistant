"""T2：schema 迁移幂等可重复执行（architecture.md 数据节：documents/qa_records 带 user_id）。"""

import sqlite3

from app.data.migrations import apply_migrations


def test_migration_creates_schema(data_dir):
    db_path = data_dir / "rag.db"
    conn = sqlite3.connect(db_path)
    try:
        apply_migrations(conn)

        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"documents", "qa_records", "schema_migrations"} <= tables

        doc_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(documents)").fetchall()
        }
        assert {
            "id", "user_id", "name", "category", "file_path",
            "page_count", "char_count", "status", "created_at",
        } <= doc_cols

        qa_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(qa_records)").fetchall()
        }
        assert {
            "id", "user_id", "question", "retrieved_chunks", "answer",
            "provider", "latency_ms", "created_at",
        } <= qa_cols
    finally:
        conn.close()


def test_migration_is_idempotent(data_dir):
    db_path = data_dir / "rag.db"
    conn = sqlite3.connect(db_path)
    try:
        apply_migrations(conn)
        applied_first = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]

        apply_migrations(conn)  # 第二次执行
        applied_second = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]

        assert applied_first == applied_second
        assert applied_first >= 1
    finally:
        conn.close()

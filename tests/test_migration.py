"""T2：schema 迁移幂等可重复执行（architecture.md 数据节：documents/qa_records 带 user_id）。"""

import sqlite3
from functools import partial
from unittest.mock import patch

import pytest

from app.core.crypto import encrypt_text, get_fernet
from app.data.migrations import MIGRATIONS, apply_migrations
from app.data.provider_store import upsert_provider_settings


def _apply_legacy_schema(conn):
    """模拟 v002 时代库：只应用 v001/v002，v003 尚未应用。"""
    with patch("app.data.migrations.MIGRATIONS", MIGRATIONS[:2]):
        apply_migrations(conn)


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


def test_v003_encrypts_existing_plaintext_keys(data_dir):
    """v003：既有明文 Key 迁移为密文（enc$v1$ 前缀），库中不再有明文。"""
    db_path = data_dir / "rag.db"
    conn = sqlite3.connect(db_path)
    try:
        _apply_legacy_schema(conn)  # v002 时代库
        upsert_provider_settings(
            conn, "default", "preset", "siliconflow", "m", "e",
            api_key="sk-legacy-plain-123456", base_url=None,
        )
        fernet = get_fernet(data_dir, None)
        apply_migrations(conn, legacy_encryptor=partial(encrypt_text, fernet))

        row = conn.execute(
            "SELECT api_key FROM provider_settings WHERE user_id = 'default'"
        ).fetchone()
        assert row[0].startswith("enc$v1$")
        assert "sk-legacy-plain-123456" not in row[0]
        # 已加密的记录不二次套前缀（幂等：再跑一次值不变）
        apply_migrations(conn, legacy_encryptor=partial(encrypt_text, fernet))
        row_again = conn.execute(
            "SELECT api_key FROM provider_settings WHERE user_id = 'default'"
        ).fetchone()
        assert row_again[0] == row[0]
    finally:
        conn.close()


def test_v003_requires_encryptor_when_plaintext_exists(data_dir):
    """存在明文 Key 但未提供加密器时必须报错，防静默留明文。"""
    db_path = data_dir / "rag.db"
    conn = sqlite3.connect(db_path)
    try:
        _apply_legacy_schema(conn)  # v002 时代库
        upsert_provider_settings(
            conn, "default", "preset", "siliconflow", "m", "e",
            api_key="sk-plain-123456", base_url=None,
        )
        with pytest.raises(RuntimeError):
            apply_migrations(conn, legacy_encryptor=None)
        row = conn.execute(
            "SELECT api_key FROM provider_settings WHERE user_id = 'default'"
        ).fetchone()
        assert row[0] == "sk-plain-123456"  # 明文未被改动
    finally:
        conn.close()


def test_v003_backs_up_db_before_encrypt(data_dir):
    """v003 转换前备份原库（回滚路径：备份含迁移前明文）。"""
    db_path = data_dir / "rag.db"
    conn = sqlite3.connect(db_path)
    backup_path = data_dir / "rag.db.pre-v003-20260811.bak"
    try:
        _apply_legacy_schema(conn)  # v002 时代库
        upsert_provider_settings(
            conn, "default", "preset", "siliconflow", "m", "e",
            api_key="sk-plain-123456", base_url=None,
        )
        apply_migrations(
            conn,
            legacy_encryptor=partial(encrypt_text, get_fernet(data_dir, None)),
            backup_path=backup_path,
        )
        assert backup_path.exists()
        bak = sqlite3.connect(backup_path)
        try:
            row = bak.execute(
                "SELECT api_key FROM provider_settings WHERE user_id = 'default'"
            ).fetchone()
            assert row[0] == "sk-plain-123456"  # 备份保留迁移前明文（回滚依据）
        finally:
            bak.close()
    finally:
        conn.close()


def test_v003_no_backup_without_plaintext(data_dir):
    """无明文 Key 时不生成多余备份（启动自检场景不残留备份文件）。"""
    db_path = data_dir / "rag.db"
    conn = sqlite3.connect(db_path)
    backup_path = data_dir / "rag.db.pre-v003-20260811.bak"
    try:
        _apply_legacy_schema(conn)  # v002 时代库，空表无明文
        apply_migrations(conn, backup_path=backup_path)
        assert not backup_path.exists()
    finally:
        conn.close()

"""T2：provider_settings 存取（owner：data/ DataAccess，纯数据不做业务判断）。"""

from app.data.provider_store import get_provider_settings, upsert_provider_settings
from app.data.migrations import apply_migrations


def _conn(data_dir):
    conn = data_dir / "rag.db"
    import sqlite3

    c = sqlite3.connect(conn)
    apply_migrations(c)
    return c


def test_no_config_returns_none(data_dir):
    conn = _conn(data_dir)
    try:
        assert get_provider_settings(conn, "default") is None
    finally:
        conn.close()


def test_upsert_then_read_roundtrip(data_dir):
    conn = _conn(data_dir)
    try:
        upsert_provider_settings(
            conn,
            "default",
            mode="preset",
            provider="siliconflow",
            model="Qwen/Qwen2.5-7B-Instruct",
            embedding_model="BAAI/bge-m3",
            api_key="sk-test1234567890",
            base_url="https://api.siliconflow.cn/v1",
        )
        row = get_provider_settings(conn, "default")
        assert row is not None
        assert row["mode"] == "preset"
        assert row["provider"] == "siliconflow"
        assert row["model"] == "Qwen/Qwen2.5-7B-Instruct"
        assert row["embedding_model"] == "BAAI/bge-m3"
        assert row["api_key"] == "sk-test1234567890"
        assert row["base_url"] == "https://api.siliconflow.cn/v1"
    finally:
        conn.close()


def test_upsert_overwrites_same_user(data_dir):
    conn = _conn(data_dir)
    try:
        upsert_provider_settings(conn, "default", "preset", "siliconflow", "m1", "e1", "k1", None)
        upsert_provider_settings(conn, "default", "byok", "custom", "m2", "e2", "k2", "http://x/v1")
        row = get_provider_settings(conn, "default")
        assert row["mode"] == "byok"
        assert row["model"] == "m2"
        assert row["api_key"] == "k2"
    finally:
        conn.close()

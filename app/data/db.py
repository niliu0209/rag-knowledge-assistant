"""SQLite 连接与就绪检查（owner：data/ DataAccess，不做业务判断）。"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def get_connection(data_dir: Path) -> sqlite3.Connection:
    """打开应用数据库连接；数据目录不存在时创建。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(data_dir / "rag.db")
    conn.row_factory = sqlite3.Row
    return conn


def check_ready(data_dir: Path) -> None:
    """就绪检查：数据目录可写、数据库可打开（异常向上抛，由调用方映射为 503）。"""
    probe = data_dir / ".probe"
    probe.touch()
    probe.unlink()
    with get_connection(data_dir) as conn:
        conn.execute("SELECT 1")

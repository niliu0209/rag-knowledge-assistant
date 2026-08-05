"""FastAPI 应用入口（阶段 0）。

启动时初始化数据目录并应用 schema 迁移；create_app 可注入数据目录（测试隔离）。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from app import __version__
from app.api.routes.health import create_health_router
from app.core.config import get_settings
from app.data.db import get_connection
from app.data.migrations import apply_migrations


def create_app(data_dir: Path | None = None) -> FastAPI:
    data_dir = data_dir or get_settings().data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    with get_connection(data_dir) as conn:
        apply_migrations(conn)

    app = FastAPI(title="rag-knowledge-assistant", version=__version__)
    app.include_router(create_health_router(data_dir))
    return app


# uvicorn app.main:app 入口
app = create_app()

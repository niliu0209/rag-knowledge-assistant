"""FastAPI 应用入口（阶段 0）。

启动时初始化数据目录并应用 schema 迁移；create_app 可注入数据目录与
provider transport（测试隔离与外部边界 fake）。
"""

from __future__ import annotations

from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse

from app import __version__
from app.api.routes.health import create_health_router
from app.api.routes.providers import create_providers_router
from app.core.config import get_settings
from app.data.db import get_connection
from app.data.migrations import apply_migrations


def create_app(
    data_dir: Path | None = None,
    provider_transport: httpx.BaseTransport | None = None,
) -> FastAPI:
    data_dir = data_dir or get_settings().data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    with get_connection(data_dir) as conn:
        apply_migrations(conn)

    app = FastAPI(title="rag-knowledge-assistant", version=__version__)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request, exc: HTTPException):
        # 统一错误体 {error: {code, message}}（architecture.md API 合同）
        # HTTPException.detail 即错误体本身，去掉 FastAPI 的 {"detail": ...} 包装
        return JSONResponse(status_code=exc.status_code, content=exc.detail)

    app.include_router(create_health_router(data_dir))
    app.include_router(
        create_providers_router(data_dir, provider_transport=provider_transport)
    )
    return app


# uvicorn app.main:app 入口
app = create_app()

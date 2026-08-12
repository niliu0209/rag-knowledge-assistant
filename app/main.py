"""FastAPI 应用入口（阶段 0）。

启动时初始化数据目录并应用 schema 迁移；create_app 可注入数据目录与
provider transport（测试隔离与外部边界 fake）。

启动方式：uvicorn --factory app.main:create_app（工厂模式，避免模块级
副作用——import 即迁移会污染测试隔离与误生成密钥文件）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import partial
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse

from app import __version__
from app.api.routes.admin import create_admin_router
from app.api.routes.auth import create_auth_router
from app.api.routes.documents import create_documents_router
from app.api.routes.health import create_health_router
from app.api.routes.providers import create_providers_router
from app.api.routes.qa import create_qa_router
from app.core.config import get_settings
from app.core.crypto import encrypt_text, get_fernet
from app.data.db import get_connection
from app.data.migrations import apply_migrations
from app.services.auth import AuthService


def create_app(
    data_dir: Path | None = None,
    provider_transport: httpx.BaseTransport | None = None,
) -> FastAPI:
    settings = get_settings()
    data_dir = data_dir or settings.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    with get_connection(data_dir) as conn:
        apply_migrations(
            conn,
            # S1-2：v003 明文 Key 迁移加密（主密钥 env 注入优先，否则持久化密钥文件）
            legacy_encryptor=partial(
                encrypt_text, get_fernet(data_dir, settings.rag_key_encryption_key)
            ),
            # 回滚路径：v003 转换前备份原库（含迁移前明文，按敏感数据保管）
            backup_path=data_dir
            / f"rag.db.pre-v003-{datetime.now(timezone.utc):%Y%m%d%H%M%S}.bak",
        )

    app = FastAPI(title="rag-knowledge-assistant", version=__version__)

    # 认证服务实例（api 依赖经 app.state 取用；测试可整体替换）
    app.state.data_dir = data_dir
    app.state.auth_service = AuthService(data_dir)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request, exc: HTTPException):
        # 统一错误体 {error: {code, message}}（architecture.md API 合同）
        # HTTPException.detail 即错误体本身，去掉 FastAPI 的 {"detail": ...} 包装
        return JSONResponse(status_code=exc.status_code, content=exc.detail)

    app.include_router(create_health_router(data_dir))
    app.include_router(create_auth_router())
    app.include_router(create_admin_router(data_dir))
    app.include_router(
        create_providers_router(data_dir, provider_transport=provider_transport)
    )
    app.include_router(
        create_documents_router(data_dir, provider_transport=provider_transport)
    )
    app.include_router(
        create_qa_router(data_dir, provider_transport=provider_transport)
    )
    return app

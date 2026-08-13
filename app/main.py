"""FastAPI 应用入口（阶段 0）。

启动时初始化数据目录并应用 schema 迁移；create_app 可注入数据目录与
provider transport（测试隔离与外部边界 fake）。

启动方式：uvicorn --factory app.main:create_app（工厂模式，避免模块级
副作用——import 即迁移会污染测试隔离与误生成密钥文件）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from functools import partial
from logging.handlers import RotatingFileHandler
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


# S2-3 日志落盘防重：同一 log_dir 只挂一次 FileHandler（create_app 在测试中被
# 多次调用，重复挂载会导致日志重复写入）
_configured_log_dirs: set[Path] = set()


def _setup_file_logging(log_dir: Path) -> None:
    """访问日志 + 应用/错误日志落盘（S2-3；RotatingFileHandler 防单文件膨胀）。

    访问日志走 uvicorn.access logger；应用/错误日志挂 root logger（保留 stderr
    输出——docker logs 仍可查，双通道）。日志内容不包含密钥（S2-2 日志脱敏）。
    """
    key = log_dir.resolve()
    if key in _configured_log_dirs:
        return
    log_dir.mkdir(parents=True, exist_ok=True)
    _configured_log_dirs.add(key)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    access_logger = logging.getLogger("uvicorn.access")
    access_handler = RotatingFileHandler(
        log_dir / "access.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    access_handler.setFormatter(formatter)
    access_logger.addHandler(access_handler)

    app_handler = RotatingFileHandler(
        log_dir / "app.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    app_handler.setFormatter(formatter)
    logging.getLogger().addHandler(app_handler)


def create_app(
    data_dir: Path | None = None,
    provider_transport: httpx.BaseTransport | None = None,
) -> FastAPI:
    settings = get_settings()
    data_dir = data_dir or settings.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    # S2-3 日志落盘：env LOG_DIR 优先，否则 data_dir/logs（测试跟随临时目录）
    _setup_file_logging(settings.log_dir or (data_dir / "logs"))
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

    @app.middleware("http")
    async def limit_body_size(request, call_next):
        # S2-3 请求体限制：防大 body 攻击（上传 20MiB 已有业务校验，这里兜底
        # 全局上限）。request.body() 有 starlette 缓存，之后路由处理不受影响。
        max_body = settings.max_body_bytes
        body = await request.body()
        if len(body) > max_body:
            return JSONResponse(
                status_code=413,
                content={
                    "error": {
                        "code": "PAYLOAD_TOO_LARGE",
                        "message": f"请求体超过限制（最大 {max_body // (1024 * 1024)}MiB）",
                    }
                },
            )
        return await call_next(request)

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

"""健康检查（architecture.md API 合同：GET /api/health）。

仅做就绪探测，不拥有业务规则；依赖异常映射为统一错误体 503。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app import __version__
from app.data.db import check_ready

logger = logging.getLogger(__name__)


def create_health_router(data_dir: Path) -> APIRouter:
    router = APIRouter()

    @router.get("/api/health")
    def health() -> JSONResponse:
        try:
            check_ready(data_dir)
        except Exception as exc:  # noqa: BLE001——就绪探测覆盖一切依赖故障
            logger.warning("health check failed: %s", exc)
            return JSONResponse(
                status_code=503,
                content={"error": {"code": "unavailable", "message": "依赖未就绪"}},
            )
        return JSONResponse(content={"status": "ok", "version": __version__})

    return router

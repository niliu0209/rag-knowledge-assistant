"""提供商配置 API（architecture.md API 节：GET /api/providers、PUT /api/provider、
POST /api/provider/validate）。

薄层：参数校验与错误码映射（400 invalid_config / 422 invalid_key / 500），
不拥有业务规则；掩码由 ProviderService 保证，Key 不进响应与日志。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.services.provider import (
    PRESETS,
    InvalidConfigError,
    InvalidKeyError,
    ProviderError,
    ProviderService,
)

logger = logging.getLogger(__name__)


class ProviderPutRequest(BaseModel):
    mode: str
    provider: str
    api_key: str | None = None
    model: str | None = None
    embedding_model: str | None = None
    # BYOK 扩展（API 合同未列字段；provider=custom 时必填，其余忽略）
    base_url: str | None = None


def create_providers_router(
    data_dir: Path,
    service: ProviderService | None = None,
    provider_transport=None,
) -> APIRouter:
    router = APIRouter()
    if service is None:
        service = ProviderService(data_dir, transport=provider_transport)

    @router.get("/api/providers")
    def list_providers(user: dict = Depends(get_current_user)):
        return {"presets": PRESETS, "current": service.get_config(user["id"])}

    @router.put("/api/provider")
    def put_provider(req: ProviderPutRequest, user: dict = Depends(get_current_user)):
        current = service.get_full_config(user["id"])
        model = req.model or current["model"]
        embedding_model = req.embedding_model or current["embedding_model"]
        # 未传 Key 时保留已存 Key（避免覆盖清空）
        api_key = req.api_key if req.api_key is not None else current["api_key"]
        try:
            service.validate_config(
                req.mode, req.provider, model, embedding_model, api_key, req.base_url
            )
            ok, message = service.validate_connectivity(
                user["id"],
                mode=req.mode,
                provider=req.provider,
                model=model,
                embedding_model=embedding_model,
                api_key=api_key,
                base_url=req.base_url,
            )
        except InvalidConfigError as exc:
            raise HTTPException(
                status_code=400,
                detail={"error": {"code": "invalid_config", "message": str(exc)}},
            ) from exc
        except InvalidKeyError as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": {"code": "invalid_key", "message": str(exc)}},
            ) from exc
        except ProviderError as exc:
            logger.warning("provider 校验失败: %s", exc)
            raise HTTPException(
                status_code=500,
                detail={"error": {"code": "provider_unavailable", "message": str(exc)}},
            ) from exc

        if not ok:
            raise HTTPException(
                status_code=422,
                detail={"error": {"code": "invalid_key", "message": message}},
            )

        service.save_config(
            user["id"],
            mode=req.mode,
            provider=req.provider,
            model=model,
            embedding_model=embedding_model,
            api_key=api_key,
            base_url=req.base_url,
        )
        return {"ok": True}

    @router.post("/api/provider/validate")
    def validate_provider(
        req: ProviderPutRequest, user: dict = Depends(get_current_user)
    ):
        current = service.get_config(user["id"])
        model = req.model or current["model"]
        embedding_model = req.embedding_model or current["embedding_model"]
        try:
            ok, message = service.validate_connectivity(
                user["id"],
                mode=req.mode,
                provider=req.provider,
                model=model,
                embedding_model=embedding_model,
                api_key=req.api_key,
                base_url=req.base_url,
            )
        except InvalidConfigError as exc:
            raise HTTPException(
                status_code=400,
                detail={"error": {"code": "invalid_config", "message": str(exc)}},
            ) from exc
        return {"ok": ok, "message": message}

    return router

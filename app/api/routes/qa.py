"""问答 API（architecture.md API 节：POST /api/qa，F0-3）。

薄层：读 JSON 请求体、调用 QaService、映射错误码为统一错误体
{error: {code, message}}；业务规则 owner 是 services/qa.py，此处不拥有。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app.services.provider import ProviderService
from app.services.qa import (
    EmptyQuestionError,
    EmbeddingFailedError,
    LlmFailedError,
    ProviderNotConfiguredError,
    QaError,
    QaService,
)

logger = logging.getLogger(__name__)

# 错误码 → HTTP 状态映射（architecture.md API 节：422 空问题；503 未配置/LLM 失败）
_STATUS_MAP = {
    EmptyQuestionError: 422,
    ProviderNotConfiguredError: 503,
    LlmFailedError: 503,
    EmbeddingFailedError: 502,
}


def create_qa_router(
    data_dir: Path,
    service: QaService | None = None,
    provider_transport=None,
) -> APIRouter:
    router = APIRouter()
    if service is None:
        provider = ProviderService(data_dir, transport=provider_transport)
        service = QaService(data_dir, provider)

    @router.post("/api/qa")
    async def ask_question(request: Request):
        # 手动解析 JSON：保证非法请求体也返回统一错误体（pydantic 默认 422 格式不统一）
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001——JSON 解析失败返回统一错误体
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {"code": "invalid_json", "message": "请求体不是合法 JSON"}
                },
            ) from exc
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "code": "invalid_json",
                        "message": "请求体必须是 JSON 对象",
                    }
                },
            )
        try:
            return service.ask(body.get("question"))
        except QaError as exc:
            raise HTTPException(
                status_code=_STATUS_MAP.get(type(exc), 500),
                detail={"error": {"code": _code_of(exc), "message": str(exc)}},
            ) from exc
        except Exception as exc:  # noqa: BLE001——未预期故障统一 500
            logger.exception("问答未预期失败")
            raise HTTPException(
                status_code=500,
                detail={"error": {"code": "internal_error", "message": "问答失败，请重试"}},
            ) from exc

    return router


def _code_of(exc: QaError) -> str:
    return {
        EmptyQuestionError: "empty_question",
        ProviderNotConfiguredError: "provider_not_configured",
        LlmFailedError: "llm_failed",
        EmbeddingFailedError: "embedding_failed",
    }.get(type(exc), "qa_failed")

"""问答 API（architecture.md API 节：POST /api/qa，F0-3）。

薄层：读 JSON 请求体、调用 QaService、映射错误码为统一错误体
{error: {code, message}}；业务规则 owner 是 services/qa.py，此处不拥有。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import get_current_user
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

# S1-3 多轮上下文：历史消息上限与单条长度上限（服务端归一化，防 prompt 膨胀）
HISTORY_MAX_MESSAGES = 10
HISTORY_MAX_MESSAGE_CHARS = 2000
_HISTORY_ROLES = {"user", "assistant"}


class HistoryTooLongError(ValueError):
    """单条历史消息超长（api 映射 422 history_too_long）。"""


def _parse_history(value) -> list[dict]:
    """解析并归一化可选 history 字段；非法 → ValueError（api 映射 422）。

    合同（S1-3 实施决策）：[{role: user|assistant, content}]；超条数截断取
    最近 HISTORY_MAX_MESSAGES 条；单条超长/非法结构 → 422（明确拒绝，
    不做静默截断——截断只用于条数，防内容被悄悄丢失）。
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("history 必须是数组")
    normalized: list[dict] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError("history 元素必须是对象")
        role = entry.get("role")
        content = entry.get("content")
        if role not in _HISTORY_ROLES:
            raise ValueError("history role 仅支持 user | assistant")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("history content 必须是非空字符串")
        if len(content) > HISTORY_MAX_MESSAGE_CHARS:
            raise HistoryTooLongError(
                f"单条历史消息超过 {HISTORY_MAX_MESSAGE_CHARS} 字符上限"
            )
        normalized.append({"role": role, "content": content.strip()})
    return normalized[-HISTORY_MAX_MESSAGES:]


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
    async def ask_question(
        request: Request, user: dict = Depends(get_current_user)
    ):
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
            history = _parse_history(body.get("history"))
        except HistoryTooLongError as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": {"code": "history_too_long", "message": str(exc)}},
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": {"code": "invalid_history", "message": str(exc)}},
            ) from exc
        try:
            return service.ask(
                body.get("question"), user_id=user["id"], history=history
            )
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

"""文档上传 API（architecture.md API 节：POST /api/documents，F0-1）。

薄层：读 multipart、调用 DocumentIngestService、映射错误码为统一错误体
{error: {code, message}}；校验规则 owner 是 services/ingest.py，此处不拥有业务规则。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.services.ingest import (
    DocumentIngestService,
    DuplicateDocumentError,
    EmbeddingFailedError,
    FileTooLargeError,
    IngestError,
    InternalIngestError,
    InvalidCategoryError,
    InvalidFormatError,
)
from app.services.provider import EmbeddingMismatchError, ProviderService
from rag.parse import NoTextError

logger = logging.getLogger(__name__)

# 错误码 → HTTP 状态映射（architecture.md API 节）
_STATUS_MAP = {
    InvalidFormatError: 400,
    FileTooLargeError: 400,
    InvalidCategoryError: 400,
    DuplicateDocumentError: 409,
    EmbeddingMismatchError: 409,
    NoTextError: 422,
    EmbeddingFailedError: 502,
    InternalIngestError: 500,
}


def create_documents_router(
    data_dir: Path,
    service: DocumentIngestService | None = None,
    provider_transport=None,
) -> APIRouter:
    router = APIRouter()
    if service is None:
        provider = ProviderService(data_dir, transport=provider_transport)
        service = DocumentIngestService(data_dir, provider)

    @router.post("/api/documents")
    async def upload_document(
        file: UploadFile = File(...),
        category: str = Form(...),
    ):
        content = await file.read()
        try:
            return service.ingest(file.filename or "unnamed", content, category)
        except (IngestError, NoTextError) as exc:
            raise HTTPException(
                status_code=_STATUS_MAP.get(type(exc), 422),
                detail={"error": {"code": _code_of(exc), "message": str(exc)}},
            ) from exc
        except EmbeddingMismatchError as exc:
            raise HTTPException(
                status_code=409,
                detail={"error": {"code": "embedding_mismatch", "message": str(exc)}},
            ) from exc

    return router


def _code_of(exc: IngestError) -> str:
    return {
        InvalidFormatError: "invalid_format",
        FileTooLargeError: "file_too_large",
        InvalidCategoryError: "invalid_category",
        DuplicateDocumentError: "duplicate_document",
        EmbeddingFailedError: "embedding_failed",
        InternalIngestError: "internal_error",
        NoTextError: "no_text",
    }.get(type(exc), "ingest_failed")

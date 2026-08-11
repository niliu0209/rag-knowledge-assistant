"""文档上传 API（architecture.md API 节：POST /api/documents，F0-1）。

薄层：读 multipart、调用 DocumentIngestService、映射错误码为统一错误体
{error: {code, message}}；校验规则 owner 是 services/ingest.py，此处不拥有业务规则。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from app.services.ingest import (
    DocumentDeleteError,
    DocumentIngestService,
    DocumentNotFoundError,
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
    DocumentNotFoundError: 404,
    DocumentDeleteError: 500,
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

    @router.get("/api/documents")
    def list_documents():
        return service.list_documents()

    @router.delete("/api/documents/{doc_id}")
    def delete_document(doc_id: str):
        try:
            service.delete_document(doc_id)
        except IngestError as exc:
            raise HTTPException(
                status_code=_STATUS_MAP.get(type(exc), 500),
                detail={"error": {"code": _code_of(exc), "message": str(exc)}},
            ) from exc
        return {"ok": True}

    # S1-4 批量删除：一次删除多份文档（全有或全无，见 service.delete_documents）
    # 校验顺序：结构（非数组/空/元素类型）→ 去重 → 上限（去重后 ≤100）→ 存在性（404 不执行）
    BATCH_DELETE_MAX = 100

    @router.post("/api/documents/batch-delete")
    async def batch_delete(request: Request):
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001——JSON 解析失败返回统一错误体
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {"code": "invalid_json", "message": "请求体不是合法 JSON"}
                },
            ) from exc
        doc_ids = body.get("doc_ids") if isinstance(body, dict) else None
        if not isinstance(doc_ids, list) or not doc_ids:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": {
                        "code": "invalid_batch",
                        "message": "doc_ids 必须是包含 1 个以上 id 的数组",
                    }
                },
            )
        seen: list[str] = []
        for item in doc_ids:
            if not isinstance(item, str) or not item.strip():
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": {
                            "code": "invalid_batch",
                            "message": "doc_ids 元素必须是非空字符串",
                        }
                    },
                )
            if item not in seen:
                seen.append(item)
        if len(seen) > BATCH_DELETE_MAX:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": {
                        "code": "batch_too_large",
                        "message": f"一次批量删除最多 {BATCH_DELETE_MAX} 份文档",
                    }
                },
            )
        try:
            deleted = service.delete_documents(seen)
        except IngestError as exc:
            raise HTTPException(
                status_code=_STATUS_MAP.get(type(exc), 500),
                detail={"error": {"code": _code_of(exc), "message": str(exc)}},
            ) from exc
        return {"deleted": deleted}

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
        DocumentNotFoundError: "document_not_found",
        DocumentDeleteError: "document_delete_failed",
    }.get(type(exc), "ingest_failed")

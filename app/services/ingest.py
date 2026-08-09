"""DocumentIngestService：上传入库管线编排（owner：services/ingest.py）。

业务规则唯一 owner（architecture.md 链路一，9 步全实现）：
  1. 校验（格式/分类白名单、≤20MB，服务端重新验证）        → 400
  2. 同名检查                                              → 409
  3. 存文件 /data/uploads/<doc_id>.<ext>
  4. LlamaIndex 解析（rag/parse）                          → 422（扫描件/损坏，清理文件）
  5. 切片（rag/splitter，默认配置）
  6. embedding 一致性检查（ProviderService，S1 提供）       → 409
  7. 向量化（OpenAI 兼容 embeddings，429 退避）            → 502（provider 不可用）
  8. Chroma 写入全部切片（metadata 合同）
  9. SQLite 插入 documents 记录（ready）
  任一步失败 → 补偿：删已写向量 + 清理文件 + 记录 failed 状态。

api 路由为薄层，只做错误码映射，不拥有规则。
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.data import chroma_store, document_store
from app.data.db import get_connection
from app.services.provider import (
    EmbeddingMismatchError,
    ProviderError,
    ProviderService,
)
from rag.parse import ALLOWED_EXTENSIONS, NoTextError, parse_file
from rag.splitter import split_pages

logger = logging.getLogger(__name__)

# 分类白名单（function-list F0-1；仅元数据标签，不做差异化管线行为）
ALLOWED_CATEGORIES = ("开发调试", "业务报告", "其他")

# 单文件大小上限（F0-1 边界，architecture.md 上传节）
MAX_FILE_SIZE = 20 * 1024 * 1024

USER_ID = "default"  # 阶段 0 恒为 default（architecture.md 权限节）


class IngestError(Exception):
    """入库管线错误基类（api 层映射统一错误体）。"""


class InvalidFormatError(IngestError):
    """非 PDF/Word（api 映射 400 invalid_format）。"""


class FileTooLargeError(IngestError):
    """超过 20MB（api 映射 400 file_too_large）。"""


class InvalidCategoryError(IngestError):
    """分类不在白名单（api 映射 400 invalid_category）。"""


class DuplicateDocumentError(IngestError):
    """同名已存在（api 映射 409 duplicate_document，不静默覆盖）。"""


class EmbeddingFailedError(IngestError):
    """向量化失败，provider 不可用（api 映射 502 embedding_failed）。"""


class InternalIngestError(IngestError):
    """未预期内部故障（存储等，api 映射 500 internal_error）。"""


class DocumentIngestService:
    def __init__(self, data_dir: Path, provider: ProviderService) -> None:
        self.data_dir = data_dir
        self.provider = provider

    def ingest(
        self,
        filename: str,
        content: bytes,
        category: str,
        user_id: str = USER_ID,
    ) -> dict:
        """执行完整入库管线；成功返回入库结果，失败抛 IngestError 子类并补偿清理。"""
        ext = self._validate(filename, content, category)

        # 同名检查（不静默覆盖已入库文档；failed 痕迹不阻塞重试，允许替换）
        with get_connection(self.data_dir) as conn:
            if document_store.get_active_document_by_name(conn, user_id, filename):
                raise DuplicateDocumentError(
                    f"文档「{filename}」已存在，请勿重复上传"
                )
            old_failed = document_store.get_failed_documents_by_name(conn, user_id, filename)
            for old in old_failed:
                document_store.delete_document(conn, old["id"])
        for old in old_failed:  # 旧 failed 记录不应有向量；保险清理（尽力而为）
            try:
                chroma_store.delete_by_doc_id(
                    chroma_store.get_collection(self.data_dir), old["id"]
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("清理旧 failed 向量失败（doc_id=%s）: %s", old["id"], exc)

        doc_id = document_store.new_document_id()
        upload_dir = self.data_dir / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        file_path = upload_dir / f"{doc_id}.{ext}"

        # 事务边界：documents 记录先以 uploading 落库，任一失败更新 failed
        with get_connection(self.data_dir) as conn:
            document_store.insert_document(
                conn,
                user_id=user_id,
                doc_id=doc_id,
                name=filename,
                category=category,
                file_path=str(file_path),
                status="uploading",
            )
        file_path.write_bytes(content)

        try:
            pages = parse_file(file_path, ext)
            char_count = sum(len(p.text) for p in pages)
            page_count = max(
                (p.page for p in pages if p.page is not None), default=None
            )
            chunks = split_pages(pages)

            collection = chroma_store.get_collection(self.data_dir)
            embedding_model = self.provider.get_config(user_id)["embedding_model"]
            self.provider.check_embedding_consistency(collection, embedding_model)

            try:
                embeddings = self.provider.embed(
                    user_id, [c.text for c in chunks]
                )
            except ProviderError as exc:
                raise EmbeddingFailedError(
                    f"向量化失败（提供商不可用或额度受限）：{exc}"
                ) from exc
            if len(embeddings) != len(chunks):
                raise EmbeddingFailedError("提供商返回向量数量与切片数不一致")

            chroma_store.add_chunks(
                collection,
                ids=[f"{doc_id}:{c.chunk_index}" for c in chunks],
                texts=[c.text for c in chunks],
                metadatas=[
                    # Chroma metadata 不接受 None 值：docx 无页码时不带 page 键
                    {
                        k: v
                        for k, v in {
                            "user_id": user_id,
                            "doc_id": doc_id,
                            "category": category,
                            "page": c.page,
                            "chunk_index": c.chunk_index,
                            "embedding_model": embedding_model,
                        }.items()
                        if v is not None
                    }
                    for c in chunks
                ],
                embeddings=embeddings,
            )

            with get_connection(self.data_dir) as conn:
                document_store.update_document_meta(
                    conn,
                    doc_id,
                    page_count=page_count,
                    char_count=char_count,
                    status="ready",
                )
            logger.info(
                "文档入库成功: %s（%d 切片，%d 字符，%s）", filename, len(chunks), char_count, category
            )
            return {
                "id": doc_id,
                "name": filename,
                "category": category,
                "page_count": page_count,
                "char_count": char_count,
                "status": "ready",
            }
        except (IngestError, EmbeddingMismatchError, NoTextError):
            self._compensate(doc_id, file_path)
            raise
        except Exception as exc:  # noqa: BLE001——未预期异常（存储故障等）同样补偿与 failed 记录
            logger.exception("入库管线未预期失败: %s", filename)
            self._compensate(doc_id, file_path)
            raise InternalIngestError("入库失败，请重试或检查服务状态") from exc

    # ---------- 校验 ----------

    def _validate(self, filename: str, content: bytes, category: str) -> str:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in ALLOWED_EXTENSIONS:
            raise InvalidFormatError("仅支持 PDF（.pdf）或 Word（.docx）文档")
        if len(content) > MAX_FILE_SIZE:
            raise FileTooLargeError("文件超过 20MB 上限，请拆分后上传")
        if category not in ALLOWED_CATEGORIES:
            raise InvalidCategoryError("分类仅支持：开发调试 / 业务报告 / 其他")
        return ext

    # ---------- 补偿回滚 ----------

    def _compensate(self, doc_id: str, file_path: Path) -> None:
        """任一步失败：删已写向量 + 清理文件 + 记录 failed 状态（架构链路一第 9 步）。"""
        try:
            collection = chroma_store.get_collection(self.data_dir)
            chroma_store.delete_by_doc_id(collection, doc_id)
        except Exception as exc:  # noqa: BLE001——补偿尽力而为，异常不掩盖原始错误
            logger.warning("补偿删除向量失败（doc_id=%s）: %s", doc_id, exc)
        try:
            file_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            with get_connection(self.data_dir) as conn:
                document_store.update_document_status(conn, doc_id, "failed")
        except Exception as exc:  # noqa: BLE001
            logger.warning("记录失败状态失败（doc_id=%s）: %s", doc_id, exc)

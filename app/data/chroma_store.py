"""Chroma 集合管理（owner：data/，单集合 knowledge_blocks）。

切片 metadata 合同（architecture.md 数据节）：user_id/doc_id/category/
page/chunk_index/embedding_model。持久化于 <data_dir>/chroma。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import chromadb

COLLECTION_NAME = "knowledge_blocks"


def get_collection(data_dir: Path) -> chromadb.Collection:
    """打开（不存在则创建）knowledge_blocks 集合；存储异常由调用方映射。"""
    client = chromadb.PersistentClient(
        path=str(data_dir / "chroma"),
        settings=chromadb.Settings(anonymized_telemetry=False),
    )
    return client.get_or_create_collection(COLLECTION_NAME)


def add_chunks(
    collection: chromadb.Collection,
    ids: list[str],
    texts: list[str],
    metadatas: list[dict[str, Any]],
    embeddings: list[list[float]],
) -> None:
    """写入全部切片（单次调用；失败由 ingest 补偿删除本 doc_id）。"""
    collection.add(ids=ids, documents=texts, metadatas=metadatas, embeddings=embeddings)


def delete_by_doc_id(collection: chromadb.Collection, doc_id: str) -> None:
    """按 doc_id metadata 删除该文档全部切片（补偿回滚/F0-2 删除复用）。"""
    collection.delete(where={"doc_id": doc_id})

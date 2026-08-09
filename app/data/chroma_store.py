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


def query_chunks(
    collection: chromadb.Collection,
    embedding: list[float],
    user_id: str,
    top_k: int = 5,
) -> list[dict]:
    """向量检索（user_id 强制过滤——权限节：所有查询强制 user_id）。

    top_k 参数取值由 QaService 决定（检索参数 owner 在 services/qa.py）。
    返回行：[{doc_id, chunk_index, page, snippet, distance}]；page 可能缺失（docx）。
    空集合返回空列表（调用方按"无结果诚实回答"处理）；存储异常上抛由调用方映射。
    """
    if collection.count() == 0:
        return []
    result = collection.query(
        query_embeddings=[embedding],
        n_results=top_k,
        where={"user_id": user_id},
        include=["documents", "metadatas", "distances"],
    )
    documents = (result.get("documents") or [[]])[0]
    metadatas = (result.get("metadatas") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]
    rows: list[dict] = []
    for doc, meta, distance in zip(documents, metadatas, distances):
        rows.append(
            {
                "doc_id": meta.get("doc_id"),
                "chunk_index": meta.get("chunk_index"),
                "page": meta.get("page"),
                "snippet": doc,
                "distance": distance,
            }
        )
    return rows

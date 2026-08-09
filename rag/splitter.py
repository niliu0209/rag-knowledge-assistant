"""切片（owner：rag/；LlamaIndex SentenceSplitter 默认配置）。

默认 chunk_size=512 / overlap=50：中文场景检索粒度合理值，S4 检索质量实测后
按证据调优（stage0 真源：差异化解析/切片仅实测证据驱动才启用）。
每页文本独立切片，chunk 保留 page 归属（引用需页码）；chunk_index 文档内全局递增。
"""

from __future__ import annotations

from dataclasses import dataclass

from llama_index.core.node_parser import SentenceSplitter

from rag.parse import Page

DEFAULT_CHUNK_SIZE = 512
DEFAULT_CHUNK_OVERLAP = 50


@dataclass
class Chunk:
    """切片结果：正文 + 页码 + 文档内序号（Chroma metadata 合同字段）。"""

    text: str
    page: int | None
    chunk_index: int


def split_pages(pages: list[Page]) -> list[Chunk]:
    """每页切片（SentenceSplitter 默认配置），返回全局序号切片列表。"""
    splitter = SentenceSplitter(
        chunk_size=DEFAULT_CHUNK_SIZE, chunk_overlap=DEFAULT_CHUNK_OVERLAP
    )
    chunks: list[Chunk] = []
    index = 0
    for page in pages:
        for node in splitter.split_text(page.text):
            chunks.append(Chunk(text=node, page=page.page, chunk_index=index))
            index += 1
    return chunks

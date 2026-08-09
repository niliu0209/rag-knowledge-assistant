"""LlamaIndex 解析适配（owner：rag/；S2 实现 PDF/Word 提取）。

- PDFReader 每页一个 Document（metadata.page_label）；DocxReader 单 Document。
- 空文本（扫描件/图片型）→ NoTextError，由 ingest 映射 422"未提取到文本"。
- 损坏/加密 → 解析异常统一转 NoTextError 同一提示（architecture.md 链路一第 3 步）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from llama_index.readers.file import DocxReader, PDFReader

logger = logging.getLogger(__name__)

# 允许的扩展名白名单（服务端校验，前端不可信）
ALLOWED_EXTENSIONS: dict[str, str] = {"pdf": "pdf", "docx": "docx"}


class NoTextError(Exception):
    """解析后无文本（扫描件/图片型/损坏/加密），ingest 映射 422。"""


@dataclass
class Page:
    """解析结果：一页文本（docx 无分页概念，page=None）。"""

    text: str
    page: int | None


def parse_pdf(path: Path) -> list[Page]:
    """提取 PDF 每页文本；全空文本抛 NoTextError；损坏/加密同提示。"""
    try:
        docs = PDFReader().load_data(path)
    except Exception as exc:  # noqa: BLE001——pypdf/tenacity 对损坏文件的异常形态不定
        logger.warning("PDF 解析失败（损坏/加密）: %s %s", path.name, exc)
        raise NoTextError("文件损坏或加密，无法提取文本") from exc
    pages = []
    for doc in docs:
        text = (doc.text or "").strip()
        if not text:
            continue
        page = doc.metadata.get("page_label")
        try:
            page = int(page)
        except (TypeError, ValueError):
            page = None
        pages.append(Page(text=text, page=page))
    if not pages:
        raise NoTextError("未提取到文本（文档可能为扫描件或无文字内容）")
    return pages


def parse_docx(path: Path) -> list[Page]:
    """提取 Word 文本（单页无页码）；空文本/损坏抛 NoTextError。"""
    try:
        docs = DocxReader().load_data(path)
    except Exception as exc:  # noqa: BLE001——docx2txt 对损坏文件异常形态不定
        logger.warning("Word 解析失败（损坏/加密）: %s %s", path.name, exc)
        raise NoTextError("文件损坏或加密，无法提取文本") from exc
    text = "".join((d.text or "") for d in docs).strip()
    if not text:
        raise NoTextError("未提取到文本（文档可能为扫描件或无文字内容）")
    return [Page(text=text, page=None)]


def parse_file(path: Path, ext: str) -> list[Page]:
    """按扩展名分发解析；未知扩展名视为格式不支持（api 层已先拦截）。"""
    if ext == "pdf":
        return parse_pdf(path)
    if ext == "docx":
        return parse_docx(path)
    raise ValueError(f"不支持的扩展名: {ext}")

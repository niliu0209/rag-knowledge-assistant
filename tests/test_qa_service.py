"""T2：S4 问答服务（F0-3，architecture.md 链路二）。

外部边界（embedding/chat API）用 httpx MockTransport（行为保持 fake）；
确定性业务规则用真实 owner 边界断言（SQLite qa_records / Chroma 实际查询）。
"""

from __future__ import annotations

import json
import sqlite3

import httpx
import pytest

from app.data import chroma_store, document_store
from app.data.db import get_connection
from app.services.provider import ProviderService
from app.services.qa import (
    RELEVANCE_DISTANCE_THRESHOLD,
    EmptyQuestionError,
    EmbeddingFailedError,
    LlmFailedError,
    ProviderNotConfiguredError,
    QaService,
)

DOC_TEXT = "行政部本周完成办公用品采购，共支出 3000 元"
DOC_NAME = "周工作小结.docx"


def _embed_ok(request: httpx.Request) -> httpx.Response:
    """固定向量 [1,0,0,0]：只与预置 chunk0（相同向量）最近。"""
    body = json.loads(request.content)
    n = len(body["input"])
    return httpx.Response(
        200, json={"data": [{"embedding": [1.0, 0.0, 0.0, 0.0]} for _ in range(n)]}
    )


def _transport(chat_handler) -> httpx.MockTransport:
    def dispatch(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/embeddings"):
            return _embed_ok(request)
        return chat_handler(request)

    return httpx.MockTransport(dispatch)


def _chat_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"content": "根据片段，答案是 3000 元。"}}]}
    )


def _chat_fail(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, json={"error": {"message": "provider down"}})


def _make_service(data_dir, chat_handler=_chat_ok):
    provider = ProviderService(
        data_dir, transport=_transport(chat_handler), retry_base_delay=0
    )
    return QaService(data_dir, provider)


def _configure_provider(data_dir):
    """保存 provider 配置（含 Key）：模拟用户在配置页完成设置（S1 路径）。"""
    from app.data.migrations import apply_migrations
    from app.services.provider import DEFAULT_PRESET

    with get_connection(data_dir) as conn:
        apply_migrations(conn)
    provider = ProviderService(
        data_dir, transport=_transport(_chat_ok), retry_base_delay=0
    )
    provider.save_config(
        "default",
        mode="preset",
        provider=DEFAULT_PRESET["provider"],
        model=DEFAULT_PRESET["model"],
        embedding_model=DEFAULT_PRESET["embedding_model"],
        api_key="sk-test",
        base_url=None,
    )


def _seed(
    data_dir,
    *,
    chunks: list[tuple[str, list[float], int | None]] | None = None,
    configure: bool = True,
):
    """预置 1 份 ready 文档与切片（真实 SQLite/Chroma 写入）。

    直接构造数据层（绕过 create_app）→ 先幂等应用迁移保证表存在；
    configure=False 时不写 provider 配置（测"有文档但未配置 Key"场景）。
    """
    from app.data.migrations import apply_migrations

    chunks = chunks or [(DOC_TEXT, [1.0, 0.0, 0.0, 0.0], 1)]
    if configure:
        _configure_provider(data_dir)
    doc_id = document_store.new_document_id()
    with get_connection(data_dir) as conn:
        apply_migrations(conn)
        document_store.insert_document(
            conn,
            user_id="default",
            doc_id=doc_id,
            name=DOC_NAME,
            category="业务报告",
            file_path=str(data_dir / "uploads" / f"{doc_id}.docx"),
            status="ready",
        )
    collection = chroma_store.get_collection(data_dir)
    chroma_store.add_chunks(
        collection,
        ids=[f"{doc_id}:{i}" for i in range(len(chunks))],
        texts=[c[0] for c in chunks],
        metadatas=[
            # Chroma metadata 不接受 None 值：page=None（docx）时省略键（S2 已确立模式）
            {
                k: v
                for k, v in {
                    "user_id": "default",
                    "doc_id": doc_id,
                    "category": "业务报告",
                    "page": c[2],
                    "chunk_index": i,
                    "embedding_model": "BAAI/bge-m3",
                }.items()
                if v is not None
            }
            for i, c in enumerate(chunks)
        ],
        embeddings=[c[1] for c in chunks],
    )
    return doc_id


def _qa_records(data_dir) -> list[sqlite3.Row]:
    conn = sqlite3.connect(data_dir / "rag.db")
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM qa_records").fetchall()
    finally:
        conn.close()


# ---------- 正常路径 ----------

def test_ask_returns_answer_with_citations(data_dir):
    _seed(data_dir)
    result = _make_service(data_dir).ask("办公用品采购花了多少钱？")

    assert result["answer"] == "根据片段，答案是 3000 元。"
    assert result["citations"], "引用不能为空"
    first = result["citations"][0]
    # 引用合同：document_id/document_name/snippet/page/chunk_index 逐条对应原文
    assert first["document_id"]
    assert first["document_name"] == DOC_NAME
    assert first["snippet"] == DOC_TEXT
    assert first["page"] == 1
    assert first["chunk_index"] == 0
    assert result["provider"]


def test_ask_filters_low_relevance_chunk(data_dir):
    """S1-1：相似度阈值过滤——低相关片段（distance > 阈值）不再进引用。"""
    doc_id = _seed(
        data_dir,
        chunks=[
            (DOC_TEXT, [1.0, 0.0, 0.0, 0.0], 1),
            ("其他页内容，与问题无关", [0.0, 1.0, 0.0, 0.0], 2),
        ],
    )
    result = _make_service(data_dir).ask("办公用品采购花了多少钱？")

    # l2 距离：chunk0 命中（0.0 ≤ 阈值）保留，chunk1 低相关（≈1.414 > 阈值）过滤
    assert [c["chunk_index"] for c in result["citations"]] == [0]
    assert all(c["document_id"] == doc_id for c in result["citations"])


def test_ask_docx_chunk_without_page_omits_page_key(data_dir):
    _seed(data_dir, chunks=[("无页码的 Word 内容", [1.0, 0.0, 0.0, 0.0], None)])
    result = _make_service(data_dir).ask("问题")

    citation = result["citations"][0]
    assert "page" not in citation  # docx 无页码：引用省略 page 键（S2 已确立模式）


# ---------- 无结果诚实回答 ----------

def test_ask_no_results_honest_without_llm_and_writes_record(data_dir):
    calls = {"chat": 0}

    def chat_counter(request: httpx.Request) -> httpx.Response:
        calls["chat"] += 1
        return _chat_ok(request)

    _configure_provider(data_dir)  # 有 Key 但知识库为空 → 检索无结果
    result = _make_service(data_dir, chat_handler=chat_counter).ask("库里没有的问题")

    assert result["answer"] == "知识库中没有相关内容"
    assert result["citations"] == []
    assert calls["chat"] == 0, "无结果时不得调用 LLM"
    # 记录仍写入（评估证据链路）
    records = _qa_records(data_dir)
    assert len(records) == 1
    assert records[0]["question"] == "库里没有的问题"
    assert records[0]["answer"] == "知识库中没有相关内容"


def test_ask_all_irrelevant_filtered_honest_without_llm(data_dir):
    """S1-1：检索结果全部低于阈值 → 诚实回答且不调 LLM（检索层判定，非 LLM 判断）。"""
    calls = {"chat": 0}

    def chat_counter(request: httpx.Request) -> httpx.Response:
        calls["chat"] += 1
        return _chat_ok(request)

    _seed(
        data_dir,
        chunks=[
            ("片段一", [0.0, 1.0, 0.0, 0.0], 1),
            ("片段二", [0.0, 0.0, 1.0, 0.0], 2),
        ],
    )  # query 向量 [1,0,0,0] 与两者 l2 距离均 > 阈值
    result = _make_service(data_dir, chat_handler=chat_counter).ask("与知识库无关的问题")

    assert result["answer"] == "知识库中没有相关内容"
    assert result["citations"] == []
    assert calls["chat"] == 0, "全部片段被过滤时不得调用 LLM"
    records = _qa_records(data_dir)
    assert len(records) == 1
    assert records[0]["answer"] == "知识库中没有相关内容"


# ---------- 异常路径 ----------

def test_ask_empty_question_raises(data_dir):
    svc = _make_service(data_dir)
    with pytest.raises(EmptyQuestionError):
        svc.ask("")
    with pytest.raises(EmptyQuestionError):
        svc.ask("   ")


def test_ask_provider_not_configured_raises(data_dir):
    # 无 provider 配置记录且 api_key 为空 → 503 provider_not_configured
    from app.data.migrations import apply_migrations

    with get_connection(data_dir) as conn:
        apply_migrations(conn)  # 表存在但无配置记录
    with pytest.raises(ProviderNotConfiguredError):
        _make_service(data_dir).ask("办公用品采购花了多少钱？")


def test_ask_llm_failure_retries_then_raises(data_dir):
    _seed(data_dir)
    with pytest.raises(LlmFailedError):
        _make_service(data_dir, chat_handler=_chat_fail).ask("办公用品采购花了多少钱？")


def test_ask_embed_failure_raises(data_dir):
    _seed(data_dir)

    def embed_fail(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "provider down"}})

    def dispatch(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/embeddings"):
            return embed_fail(request)
        return _chat_ok(request)

    provider = ProviderService(
        data_dir, transport=httpx.MockTransport(dispatch), retry_base_delay=0
    )
    with pytest.raises(EmbeddingFailedError):
        QaService(data_dir, provider).ask("办公用品采购花了多少钱？")


# ---------- prompt 构造 ----------

def test_prompt_contains_numbered_snippets_and_honest_instruction(data_dir):
    captured: dict = {}

    def chat_capture(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured["prompt"] = body["messages"][0]["content"]
        return _chat_ok(request)

    _seed(data_dir)
    _make_service(data_dir, chat_handler=chat_capture).ask("办公用品采购花了多少钱？")

    prompt = captured["prompt"]
    assert "[1]" in prompt and DOC_TEXT in prompt, "片段必须编号嵌入 prompt"
    assert DOC_NAME in prompt, "prompt 必须标注来源文档"
    assert "编造" in prompt, "prompt 必须包含诚实回答（不得编造）指令"
    assert "规格" in prompt and "价格" in prompt, (
        "prompt 必须包含区分价格与规格指令（S1-1 A4 波动修复）"
    )


# ---------- qa_records（F0-5） ----------

def test_qa_record_written_with_all_fields(data_dir):
    doc_id = _seed(data_dir)
    result = _make_service(data_dir).ask("办公用品采购花了多少钱？")

    records = _qa_records(data_dir)
    assert len(records) == 1
    row = records[0]
    assert row["user_id"] == "default"
    assert row["question"] == "办公用品采购花了多少钱？"
    assert row["answer"] == result["answer"]
    assert row["provider"]  # 所用提供商（provider · model）
    assert row["latency_ms"] is not None and row["latency_ms"] >= 0
    assert row["created_at"]

    chunks = json.loads(row["retrieved_chunks"])
    assert len(chunks) == 1
    assert chunks[0]["doc_id"] == doc_id
    assert chunks[0]["snippet"] == DOC_TEXT
    assert chunks[0]["page"] == 1
    assert chunks[0]["chunk_index"] == 0
    assert chunks[0]["document_name"] == DOC_NAME
    assert chunks[0]["relevant"] is True, "命中片段必须标记 relevant（S1-1 记录快照）"


def test_qa_record_snapshot_marks_distance_and_relevance(data_dir):
    """S1-1：记录快照含全部检索结果——distance + relevant 标记（阈值效果评估）。"""
    doc_id = _seed(
        data_dir,
        chunks=[
            (DOC_TEXT, [1.0, 0.0, 0.0, 0.0], 1),
            ("低相关片段", [0.0, 1.0, 0.0, 0.0], 2),
        ],
    )
    _make_service(data_dir).ask("办公用品采购花了多少钱？")

    chunks = json.loads(_qa_records(data_dir)[0]["retrieved_chunks"])
    assert len(chunks) == 2, "记录快照必须包含全部检索结果（含被过滤片段）"
    by_index = {c["chunk_index"]: c for c in chunks}
    assert by_index[0]["relevant"] is True
    assert by_index[0]["distance"] == 0.0
    assert by_index[1]["relevant"] is False
    assert by_index[1]["distance"] > RELEVANCE_DISTANCE_THRESHOLD
    assert by_index[1]["document_name"] == DOC_NAME, "被过滤片段仍记录来源（评估可读）"
    assert by_index[1]["doc_id"] == doc_id


def test_qa_record_write_failure_does_not_block_answer(data_dir, caplog):
    _seed(data_dir)
    import app.data.document_store as ds

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ds, "insert_qa_record", boom)
    try:
        with caplog.at_level("ERROR", logger="app.services.qa"):
            result = _make_service(data_dir).ask("办公用品采购花了多少钱？")
    finally:
        monkeypatch.undo()

    assert result["answer"] == "根据片段，答案是 3000 元。", "记录失败不得阻塞回答"
    assert any("qa_records" in r.message or "问答记录" in r.message for r in caplog.records), (
        "记录失败必须有告警日志（评估证据链路需可见）"
    )


# ---------- S1-3 多轮上下文 ----------

def test_build_prompt_includes_history_as_context_only():
    """S1-3：历史注入 prompt（片段后、问题前），并明确仅作上下文参考。"""
    snippets = [{"document_name": "周工作小结.docx", "snippet": "片段A", "page": 1}]
    prompt = QaService._build_prompt(
        "它花了多少钱？",
        snippets,
        history=[
            {"role": "user", "content": "办公用品采购花了多少钱？"},
            {"role": "assistant", "content": "答案是 3000 元。"},
        ],
    )
    assert "办公用品采购花了多少钱？" in prompt
    assert "答案是 3000 元。" in prompt
    assert "它花了多少钱？" in prompt
    # 历史定位：片段之后、当前问题之前
    assert prompt.index("片段A") < prompt.index("办公用品采购花了多少钱？")
    assert prompt.index("办公用品采购花了多少钱？") < prompt.index("它花了多少钱？")
    # 诚实回答规则不变：历史仅作上下文参考，不作为事实来源
    assert "不作为事实来源" in prompt


def test_ask_with_history_injects_history_into_prompt(data_dir):
    """S1-3：全链路——带 history 提问，chat prompt 含历史；检索不受历史影响。"""
    _seed(data_dir)
    captured = {}

    def chat_capture(request: httpx.Request) -> httpx.Response:
        captured["prompt"] = json.loads(request.content)["messages"][0]["content"]
        return _chat_ok(request)

    service = _make_service(data_dir, chat_handler=chat_capture)
    result = service.ask(
        "它花了多少钱？",
        history=[
            {"role": "user", "content": "办公用品采购花了多少钱？"},
            {"role": "assistant", "content": "根据片段，答案是 3000 元。"},
        ],
    )
    assert "办公用品采购花了多少钱？" in captured["prompt"]
    assert "根据片段，答案是 3000 元。" in captured["prompt"]
    assert result["answer"] == "根据片段，答案是 3000 元。"


def test_ask_without_history_prompt_unchanged(data_dir):
    """S1-3：无 history 向后兼容——prompt 不含历史段落，单轮语义不变。"""
    _seed(data_dir)
    captured = {}

    def chat_capture(request: httpx.Request) -> httpx.Response:
        captured["prompt"] = json.loads(request.content)["messages"][0]["content"]
        return _chat_ok(request)

    service = _make_service(data_dir, chat_handler=chat_capture)
    service.ask("办公用品采购花了多少钱？")
    assert "历史对话" not in captured["prompt"]

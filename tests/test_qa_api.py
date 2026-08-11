"""T2：POST /api/qa API 合同（architecture.md API 节 + 链路二）。

路由薄层：422 空问题、503 未配置/LLM 失败、502 embedding 失败；
错误体统一 {error: {code, message}}。外部边界 httpx MockTransport（行为保持 fake）。
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.data import chroma_store, document_store
from app.data.db import get_connection
from app.main import create_app
from tests.test_qa_service import (
    DOC_NAME,
    DOC_TEXT,
    _configure_provider,
    _embed_ok,
    _seed,
)


def _chat_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"content": "答案是 3000 元。"}}]}
    )


def _chat_fail(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, json={"error": {"message": "provider down"}})


def _transport(chat_handler) -> httpx.MockTransport:
    def dispatch(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/embeddings"):
            return _embed_ok(request)
        return chat_handler(request)

    return httpx.MockTransport(dispatch)


def _client(data_dir, chat_handler=_chat_ok):
    app = create_app(
        data_dir=data_dir, provider_transport=_transport(chat_handler)
    )
    return TestClient(app)


def test_qa_ok_200(data_dir):
    _seed(data_dir)
    resp = _client(data_dir).post("/api/qa", json={"question": "办公用品采购花了多少钱？"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer"] == "答案是 3000 元。"
    assert body["citations"] and body["citations"][0]["document_name"] == DOC_NAME
    assert body["provider"]


def test_qa_empty_question_422(data_dir):
    _seed(data_dir)
    resp = _client(data_dir).post("/api/qa", json={"question": "   "})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "empty_question"


def test_qa_missing_question_422(data_dir):
    _seed(data_dir)
    resp = _client(data_dir).post("/api/qa", json={})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "empty_question"


def test_qa_invalid_json_400(data_dir):
    resp = _client(data_dir).post("/api/qa", content="not-json", headers={"content-type": "application/json"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_json"


def test_qa_no_results_honest_200_without_llm(data_dir):
    calls = {"chat": 0}

    def chat_counter(request: httpx.Request) -> httpx.Response:
        calls["chat"] += 1
        return _chat_ok(request)

    _configure_provider(data_dir)  # 有 Key 但知识库为空 → 检索无结果诚实回答
    resp = _client(data_dir, chat_handler=chat_counter).post(
        "/api/qa", json={"question": "空库提问"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "知识库中没有相关内容"
    assert body["citations"] == []
    assert calls["chat"] == 0


def test_qa_not_configured_503(data_dir):
    _seed(data_dir, configure=False)  # 有文档但无 provider 配置（api_key 为空）
    resp = _client(data_dir).post("/api/qa", json={"question": "问题"})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "provider_not_configured"


def test_qa_llm_failure_503(data_dir):
    _seed(data_dir)
    resp = _client(data_dir, chat_handler=_chat_fail).post(
        "/api/qa", json={"question": "办公用品采购花了多少钱？"}
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "llm_failed"


# ---------- S1-3 多轮上下文 API 合同 ----------

def test_qa_with_history_200(data_dir):
    _seed(data_dir)
    resp = _client(data_dir).post(
        "/api/qa",
        json={
            "question": "它花了多少钱？",
            "history": [
                {"role": "user", "content": "办公用品采购花了多少钱？"},
                {"role": "assistant", "content": "答案是 3000 元。"},
            ],
        },
    )
    assert resp.status_code == 200, resp.text


def test_qa_history_invalid_role_422(data_dir):
    _seed(data_dir)
    resp = _client(data_dir).post(
        "/api/qa",
        json={"question": "问题", "history": [{"role": "system", "content": "越权角色"}]},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_history"


def test_qa_history_entry_too_long_422(data_dir):
    _seed(data_dir)
    resp = _client(data_dir).post(
        "/api/qa",
        json={"question": "问题", "history": [{"role": "user", "content": "x" * 2001}]},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "history_too_long"


def test_qa_history_truncated_to_recent_10(data_dir):
    """超 10 条截断取最近 10 条（服务端归一化，防 prompt 膨胀）。"""
    _seed(data_dir)
    captured = {}

    def chat_capture(request: httpx.Request) -> httpx.Response:
        captured["prompt"] = json.loads(request.content)["messages"][0]["content"]
        return _chat_ok(request)

    history = [{"role": "user", "content": f"历史问题 {i}"} for i in range(1, 16)]
    resp = _client(data_dir, chat_handler=chat_capture).post(
        "/api/qa", json={"question": "当前问题", "history": history}
    )
    assert resp.status_code == 200, resp.text
    assert "历史问题 15" in captured["prompt"]  # 最近保留
    assert "历史问题 6" in captured["prompt"]
    assert "历史问题 5" not in captured["prompt"]  # 前 5 条丢弃

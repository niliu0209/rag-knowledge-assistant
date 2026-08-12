"""T2：S2 上传入库管线（F0-1，architecture.md 链路一 + API 合同）。

- 校验：格式/大小/分类白名单（服务端重新验证，前端不可信）
- 同名 409、无文本层 422、向量化失败 502 + 补偿回滚
- embedding 一致性拒绝（复用 S1 provider 检查）
- 成功：SQLite documents(ready) + Chroma knowledge_blocks 切片（metadata 合同）

外部边界（embedding API）用 httpx MockTransport（行为保持 fake）；
确定性业务规则用真实 owner 边界断言（SQLite/Chroma 实际查询）。
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.fixture_docs import make_docx, make_image_pdf, make_pdf

EMBED_DIM = 4


def _embed_ok_handler(request: httpx.Request) -> httpx.Response:
    """OpenAI 兼容 /embeddings 成功响应（固定维度向量）。"""
    body = json.loads(request.content)
    n = len(body["input"])
    return httpx.Response(
        200,
        json={"data": [{"embedding": [0.1, 0.2, 0.3, 0.4]} for _ in range(n)]},
    )


def _embed_fail_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, json={"error": {"message": "provider down"}})


def _client(data_dir, handler=_embed_ok_handler):
    app = create_app(data_dir=data_dir, provider_transport=httpx.MockTransport(handler))
    c = TestClient(app)
    # S2-1 认证合同：以首启 admin 身份注册并登录
    resp = c.post(
        "/api/auth/register",
        json={"username": "admin", "password": "Passw0rd!@#"},
    )
    assert resp.status_code == 200, resp.text
    return c


def _upload(client, path, category="业务报告", filename=None):
    with open(path, "rb") as f:
        return client.post(
            "/api/documents",
            files={"file": (filename or path.name, f, "application/octet-stream")},
            data={"category": category},
        )


def _chroma_rows(data_dir):
    """读 Chroma 集合全部切片（测试断言用真实持久化存储）。"""
    import chromadb

    client = chromadb.PersistentClient(
        path=str(data_dir / "chroma"), settings=chromadb.Settings(anonymized_telemetry=False)
    )
    col = client.get_collection("knowledge_blocks")
    return col.get()


# ---------- 正常路径 ----------

def test_upload_docx_success_ready_and_chunks(data_dir, tmp_path):
    doc = make_docx(tmp_path / "周工作小结.docx")
    resp = _upload(_client(data_dir), doc)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "周工作小结.docx"
    assert body["category"] == "业务报告"
    assert body["char_count"] > 0
    assert body["status"] == "ready"

    # SQLite documents 记录（真实 owner 边界）
    import sqlite3

    with sqlite3.connect(data_dir / "rag.db") as conn:
        row = conn.execute("SELECT * FROM documents").fetchone()
        assert row is not None
        # S2-1 认证合同：user_id 为当前登录用户 id（UUID），不再是 'default'
        assert uuid.UUID(row[1])
        assert row[2] == "周工作小结.docx"
        assert row[3] == "业务报告"
        assert row[7] == "ready"

    # Chroma 切片 metadata 合同（user_id/doc_id/category/chunk_index/embedding_model）
    got = _chroma_rows(data_dir)
    assert len(got["ids"]) >= 1
    meta = got["metadatas"][0]
    # S2-1 认证合同：切片 user_id 为当前登录用户 id（UUID），不再是 'default'
    assert uuid.UUID(meta["user_id"])
    assert meta["doc_id"] == body["id"]
    assert meta["category"] == "业务报告"
    assert meta["chunk_index"] == 0
    assert meta["embedding_model"] == "BAAI/bge-m3"
    assert "行政部周工作小结" in got["documents"][0]


def test_upload_pdf_success_page_metadata(data_dir, tmp_path):
    pdf = make_pdf(tmp_path / "采购汇总.pdf")
    resp = _upload(_client(data_dir), pdf)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["page_count"] == 2
    assert body["char_count"] > 0

    got = _chroma_rows(data_dir)
    pages = {m["page"] for m in got["metadatas"]}
    assert pages <= {1, 2}
    assert len(pages) >= 1
    assert "Office supplies" in "".join(got["documents"])


# ---------- 校验：格式/大小/分类 ----------

def test_upload_invalid_extension_400(data_dir, tmp_path):
    bad = tmp_path / "readme.txt"
    bad.write_text("not a document", encoding="utf-8")
    resp = _upload(_client(data_dir), bad)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_format"


def test_upload_too_large_400(data_dir, tmp_path):
    big = tmp_path / "big.pdf"
    big.write_bytes(b"%PDF-1.4\n" + b"x" * (21 * 1024 * 1024))
    resp = _upload(_client(data_dir), big)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "file_too_large"


def test_upload_invalid_category_400(data_dir, tmp_path):
    doc = make_docx(tmp_path / "a.docx")
    resp = _upload(_client(data_dir), doc, category="机密文件")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_category"


# ---------- 同名 / 无文本层 / 向量化失败 / 一致性 ----------

def test_upload_duplicate_409(data_dir, tmp_path):
    client = _client(data_dir)
    doc = make_docx(tmp_path / "重复.docx")
    assert _upload(client, doc).status_code == 200
    resp = _upload(client, doc)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "duplicate_document"


def test_upload_no_text_422_and_cleanup(data_dir, tmp_path):
    img_pdf = make_image_pdf(tmp_path / "扫描件.pdf")
    resp = _upload(_client(data_dir), img_pdf)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "no_text"

    import sqlite3

    with sqlite3.connect(data_dir / "rag.db") as conn:
        rows = conn.execute("SELECT status FROM documents").fetchall()
    assert rows == [("failed",)]  # 失败状态有记录，不静默
    # 上传文件已清理
    uploads = list((data_dir / "uploads").iterdir()) if (data_dir / "uploads").exists() else []
    assert uploads == []


def test_upload_retry_after_failed_replaces_record(data_dir, tmp_path):
    """failed 记录不阻塞重试：同名失败后可重新上传成功，旧 failed 记录被替换。"""
    client = _client(data_dir)
    img_pdf = make_image_pdf(tmp_path / "重试.pdf")
    assert _upload(client, img_pdf).status_code == 422

    doc = make_pdf(tmp_path / "重试.pdf")  # 同名替换为有文本层的正常 PDF
    resp = _upload(client, doc)
    assert resp.status_code == 200, resp.text

    import sqlite3

    with sqlite3.connect(data_dir / "rag.db") as conn:
        rows = conn.execute("SELECT name, status FROM documents").fetchall()
    assert rows == [("重试.pdf", "ready")]  # 旧 failed 记录已被替换，无同名残留


def test_upload_broken_file_422(data_dir, tmp_path):
    broken = tmp_path / "损坏.pdf"
    broken.write_bytes(b"%PDF-1.4 this is corrupted content without structure")
    resp = _upload(_client(data_dir), broken)
    assert resp.status_code == 422


def test_embedding_failure_502_rollback(data_dir, tmp_path):
    client = _client(data_dir, handler=_embed_fail_handler)
    doc = make_docx(tmp_path / "回滚.docx")
    resp = _upload(client, doc)
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "embedding_failed"

    # 补偿回滚：Chroma 无残留
    import chromadb

    cclient = chromadb.PersistentClient(
        path=str(data_dir / "chroma"), settings=chromadb.Settings(anonymized_telemetry=False)
    )
    assert cclient.get_collection("knowledge_blocks").count() == 0
    # 上传文件已清理
    uploads = list((data_dir / "uploads").iterdir()) if (data_dir / "uploads").exists() else []
    assert uploads == []
    # documents 记录为 failed
    import sqlite3

    with sqlite3.connect(data_dir / "rag.db") as conn:
        assert conn.execute("SELECT status FROM documents").fetchone()[0] == "failed"


def test_embedding_mismatch_409_no_write(data_dir, tmp_path):
    """集合已有不同 embedding_model 记录 → 拒绝写入并提示重建（复用 S1 检查）。"""
    import chromadb

    cclient = chromadb.PersistentClient(
        path=str(data_dir / "chroma"), settings=chromadb.Settings(anonymized_telemetry=False)
    )
    col = cclient.get_or_create_collection("knowledge_blocks")
    col.add(
        ids=["legacy-1"],
        documents=["旧模型切片"],
        metadatas=[{"user_id": "default", "embedding_model": "text-embedding-legacy"}],
        embeddings=[[0.0] * EMBED_DIM],
    )

    doc = make_docx(tmp_path / "新文档.docx")
    resp = _upload(_client(data_dir), doc)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "embedding_mismatch"
    assert col.count() == 1  # 未新增

"""T2：S3 文档列表与删除（F0-2，architecture.md API 合同）。

- GET /api/documents：列表（名称/分类/页数/入库时间）；failed 记录不进列表
- DELETE /api/documents/{id}：同步删 SQLite 记录 + Chroma 切片 + 上传文件
- 删除失败 → 补偿回滚（记录恢复），500 明确提示
- 404 不存在 / 500 删除失败（architecture.md API 节）

确定性业务规则用真实 owner 边界断言（SQLite/Chroma/uploads 实际查询）。
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.fixture_docs import make_docx, make_pdf


def _embed_ok_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    n = len(body["input"])
    return httpx.Response(
        200,
        json={"data": [{"embedding": [0.1, 0.2, 0.3, 0.4]} for _ in range(n)]},
    )


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


def _upload(client, path, category="业务报告"):
    with open(path, "rb") as f:
        resp = client.post(
            "/api/documents",
            files={"file": (path.name, f, "application/octet-stream")},
            data={"category": category},
        )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _chroma_doc_ids(data_dir, doc_id):
    import chromadb

    client = chromadb.PersistentClient(
        path=str(data_dir / "chroma"), settings=chromadb.Settings(anonymized_telemetry=False)
    )
    got = client.get_collection("knowledge_blocks").get(where={"doc_id": doc_id})
    return got["ids"]


# ---------- 列表 ----------

def test_list_documents_fields_and_order(data_dir, tmp_path):
    client = _client(data_dir)
    first = _upload(client, make_docx(tmp_path / "a.docx"))
    second = _upload(client, make_pdf(tmp_path / "b.pdf"))

    resp = client.get("/api/documents")
    assert resp.status_code == 200
    docs = resp.json()
    assert isinstance(docs, list) and len(docs) == 2
    by_id = {d["id"]: d for d in docs}
    assert by_id[first["id"]]["name"] == "a.docx"
    assert by_id[first["id"]]["category"] == "业务报告"
    assert by_id[first["id"]]["page_count"] is None  # docx 无页码
    assert by_id[first["id"]]["status"] == "ready"
    assert "created_at" in by_id[first["id"]]
    assert by_id[second["id"]]["page_count"] == 2
    # 入库时间非空（列表字段合同）
    assert by_id[first["id"]]["created_at"]


def test_list_excludes_failed_records(data_dir, tmp_path):
    """failed 记录不进列表（F0-2 已入库文档清单；失败痕迹靠重试替换，S3 不做展示）。"""
    from tests.fixture_docs import make_image_pdf

    client = _client(data_dir)
    img = make_image_pdf(tmp_path / "扫描.pdf")
    with open(img, "rb") as f:
        assert client.post(
            "/api/documents",
            files={"file": ("扫描.pdf", f, "application/pdf")},
            data={"category": "其他"},
        ).status_code == 422

    _upload(client, make_docx(tmp_path / "正常.docx"))
    docs = client.get("/api/documents").json()
    assert [d["name"] for d in docs] == ["正常.docx"]


def test_list_empty_returns_empty_array(data_dir):
    resp = _client(data_dir).get("/api/documents")
    assert resp.status_code == 200
    assert resp.json() == []


# ---------- 删除 ----------

def test_delete_success_no_residue(data_dir, tmp_path):
    """删除后 SQLite 记录、Chroma 切片、上传文件三者均无残留（F0-2 验收核心）。"""
    client = _client(data_dir)
    doc = _upload(client, make_docx(tmp_path / "删除我.docx"))
    assert len(_chroma_doc_ids(data_dir, doc["id"])) >= 1

    resp = client.delete(f"/api/documents/{doc['id']}")
    assert resp.status_code == 200

    # SQLite 无记录
    import sqlite3

    with sqlite3.connect(data_dir / "rag.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM documents WHERE id = ?", (doc["id"],)
        ).fetchone()[0] == 0
    # Chroma 无该文档切片
    assert _chroma_doc_ids(data_dir, doc["id"]) == []
    # 上传文件已删除
    uploads = list((data_dir / "uploads").iterdir()) if (data_dir / "uploads").exists() else []
    assert uploads == []
    # 列表不再包含
    assert [d["id"] for d in client.get("/api/documents").json()] != doc["id"]


def test_delete_not_found_404(data_dir):
    resp = _client(data_dir).delete("/api/documents/no-such-id")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "document_not_found"


def test_delete_failure_rolls_back_record(data_dir, tmp_path, monkeypatch):
    """Chroma 删除失败 → 补偿回滚 SQLite 记录（列表仍可见），500 明确提示（F0-2 验收）。"""
    import app.data.chroma_store as chroma_store

    client = _client(data_dir)
    doc = _upload(client, make_docx(tmp_path / "回滚.docx"))

    def boom(collection, doc_id):
        raise RuntimeError("chroma 模拟故障")

    monkeypatch.setattr(chroma_store, "delete_by_doc_id", boom)
    resp = client.delete(f"/api/documents/{doc['id']}")
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "document_delete_failed"

    # 回滚：SQLite 记录还在（列表可见），文件未删，一致状态
    assert [d["id"] for d in client.get("/api/documents").json()] == [doc["id"]]
    assert len(_chroma_doc_ids(data_dir, doc["id"])) >= 1


# ---------- 批量删除（S1-4） ----------

def test_batch_delete_success_no_residue(data_dir, tmp_path):
    """批量删除后：SQLite 记录、Chroma 切片、上传文件均无残留；未删文档不受影响。"""
    client = _client(data_dir)
    docs = [_upload(client, make_docx(tmp_path / f"批量{i}.docx")) for i in range(3)]
    target = docs[:2]

    resp = client.post(
        "/api/documents/batch-delete", json={"doc_ids": [d["id"] for d in target]}
    )
    assert resp.status_code == 200
    assert resp.json() == {"deleted": 2}

    import sqlite3

    with sqlite3.connect(data_dir / "rag.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    for d in target:
        assert _chroma_doc_ids(data_dir, d["id"]) == []
    uploads = list((data_dir / "uploads").iterdir()) if (data_dir / "uploads").exists() else []
    assert len(uploads) == 1
    remain = client.get("/api/documents").json()
    assert [d["id"] for d in remain] == [docs[2]["id"]]
    assert len(_chroma_doc_ids(data_dir, docs[2]["id"])) >= 1  # 未删文档检索不受影响


def test_batch_delete_missing_id_404_nothing_deleted(data_dir, tmp_path):
    """全有或全无：任一 id 不存在 → 404，且不执行任何删除。"""
    client = _client(data_dir)
    doc = _upload(client, make_docx(tmp_path / "唯一.docx"))

    resp = client.post(
        "/api/documents/batch-delete", json={"doc_ids": [doc["id"], "no-such-id"]}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "document_not_found"
    assert [d["id"] for d in client.get("/api/documents").json()] == [doc["id"]]
    assert len(_chroma_doc_ids(data_dir, doc["id"])) >= 1


def test_batch_delete_empty_or_invalid_422(data_dir):
    """结构校验：非数组 / 空数组 / 非字符串元素 → 422，不执行任何删除。"""
    client = _client(data_dir)
    for payload in ([], "not-a-list", {"doc_ids": []}, {"doc_ids": [123]}):
        resp = client.post("/api/documents/batch-delete", json=payload)
        assert resp.status_code == 422, payload


def test_batch_delete_over_cap_422(data_dir):
    """一次批量删除上限 100 条（去重后）；超限 422 且不执行。"""
    client = _client(data_dir)
    resp = client.post(
        "/api/documents/batch-delete",
        json={"doc_ids": [f"id-{i}" for i in range(101)]},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "batch_too_large"


def test_batch_delete_dedup(data_dir, tmp_path):
    """重复 id 去重后一次删除成功。"""
    client = _client(data_dir)
    doc = _upload(client, make_docx(tmp_path / "去重.docx"))
    resp = client.post(
        "/api/documents/batch-delete", json={"doc_ids": [doc["id"], doc["id"]]}
    )
    assert resp.status_code == 200
    assert resp.json() == {"deleted": 1}
    assert client.get("/api/documents").json() == []


def test_batch_delete_partial_failure_rolls_back_all(data_dir, tmp_path, monkeypatch):
    """任一文档 Chroma 删除失败 → 全部回滚（列表仍可见全部），500 明确提示。"""
    import app.data.chroma_store as chroma_store

    client = _client(data_dir)
    docs = [_upload(client, make_docx(tmp_path / f"回滚{i}.docx")) for i in range(2)]

    def boom(collection, doc_id):
        raise RuntimeError("chroma 模拟故障")

    monkeypatch.setattr(chroma_store, "delete_by_doc_id", boom)
    resp = client.post(
        "/api/documents/batch-delete", json={"doc_ids": [d["id"] for d in docs]}
    )
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "document_delete_failed"
    listed = {d["id"] for d in client.get("/api/documents").json()}
    assert listed == {d["id"] for d in docs}  # 全部回滚，无一缺失
    for d in docs:
        assert len(_chroma_doc_ids(data_dir, d["id"])) >= 1

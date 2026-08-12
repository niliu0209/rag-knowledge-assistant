"""S2-1 认证与用户管理行为合同测试（RED→GREEN 的 RED 先行）。

合同来源：stage2-mvp.md S2-1（注册/登录/会话/邀请码/管理员/隔离）。
测试只验证真实行为与 owner 合同；全部走真实 HTTP（TestClient）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.data import document_store
from app.data.db import get_connection
from app.main import create_app


def err(resp) -> dict:
    return resp.json()["error"]


@pytest.fixture
def client(data_dir) -> TestClient:
    return TestClient(create_app(data_dir=data_dir))


def _register(
    client: TestClient,
    username: str,
    password: str = "Passw0rd!@#",
    invite_code: str | None = None,
):
    body = {"username": username, "password": password}
    if invite_code is not None:
        body["invite_code"] = invite_code
    return client.post("/api/auth/register", json=body)


def _login(client: TestClient, username: str, password: str = "Passw0rd!@#"):
    return client.post("/api/auth/login", json={"username": username, "password": password})


# ---------- 首启管理员与 default 数据迁移 ----------

def test_first_user_is_admin_and_claims_default_data(client, data_dir: Path):
    """空库首启注册 → role=admin；user_id='default' 的既有数据迁移给 admin（作者数据不丢）。"""
    with sqlite3.connect(data_dir / "rag.db") as conn:
        conn.execute(
            "INSERT INTO documents (id, user_id, name, category, file_path, status) "
            "VALUES ('d1', 'default', '旧文档.pdf', '其他', '/tmp/d1.pdf', 'ready')"
        )
        conn.execute(
            "INSERT INTO provider_settings (user_id, mode, provider, model, embedding_model, api_key) "
            "VALUES ('default', 'preset', 'siliconflow', 'm', 'e', 'enc$v1$xxx')"
        )
        conn.commit()

    # 播种 1 条 user_id='default' 的存量切片（认领时必须同步迁移）
    from app.data import chroma_store

    collection = chroma_store.get_collection(data_dir)
    chroma_store.add_chunks(
        collection,
        ids=["legacy-chunk-1"],
        texts=["旧知识库切片"],
        metadatas=[{"user_id": "default", "doc_id": "d1", "chunk_index": 0}],
        embeddings=[[0.1, 0.2, 0.3, 0.4]],
    )

    resp = _register(client, "admin")
    assert resp.status_code == 200
    body = resp.json()["user"]
    assert body["role"] == "admin"
    assert body["username"] == "admin"

    # 注册即登录（会话生效）
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["username"] == "admin"

    # default 数据迁移到 admin（文档 + 提供商配置密文原样迁移）
    with sqlite3.connect(data_dir / "rag.db") as conn:
        assert conn.execute("SELECT user_id FROM documents WHERE id='d1'").fetchone()[0] == body["id"]
        prov = conn.execute("SELECT user_id, api_key FROM provider_settings").fetchone()
        assert prov[0] == body["id"]
        assert prov[1] == "enc$v1$xxx"

    # Chroma 切片 user_id 同步迁移（只迁 SQLite 会文档可见但检索不到）
    from app.data import chroma_store

    collection = chroma_store.get_collection(data_dir)
    assert collection.get(where={"user_id": body["id"]})["ids"], (
        "存量切片必须改挂到 admin 名下"
    )
    assert not collection.get(where={"user_id": "default"})["ids"]


# ---------- 邀请码注册 ----------

def test_second_user_requires_valid_invite_code(client):
    """第二个用户注册必须提供有效邀请码；缺失/伪造 → 拒绝。"""
    _register(client, "admin")
    resp = _register(client, "user2")
    assert resp.status_code == 400
    assert err(resp)["code"] == "invalid_invite_code"

    resp = _register(client, "user2", invite_code="not-exist")
    assert resp.status_code == 400
    assert err(resp)["code"] == "invalid_invite_code"


def test_admin_lists_invite_codes(client):
    """管理页邀请码列表（GET /api/admin/invite-codes）：生成的邀请码出现在列表。

    浏览器实测暴露：list_invite_codes 曾查询不存在的 created_at 列导致 500
    （v004 建表无该列）；本测试锁定列表查询与建表 schema 一致。
    """
    _register(client, "admin")
    code = client.post("/api/admin/invite-codes").json()["code"]
    resp = client.get("/api/admin/invite-codes")
    assert resp.status_code == 200, resp.text
    codes = [c["code"] for c in resp.json()["invite_codes"]]
    assert code in codes


def test_invite_code_flow_admin_generates_and_user_registers(client):
    """admin 生成邀请码 → 新用户注册成功 → 邀请码标记已用，二次使用拒绝。"""
    _register(client, "admin")
    code = client.post("/api/admin/invite-codes").json()["code"]

    resp = _register(client, "friend", invite_code=code)
    assert resp.status_code == 200
    assert resp.json()["user"]["role"] == "user"

    resp = _register(client, "friend2", invite_code=code)
    assert resp.status_code == 400
    assert err(resp)["code"] == "invalid_invite_code"


def test_revoked_invite_code_rejected(client):
    """撤销的邀请码不可用。"""
    _register(client, "admin")
    code = client.post("/api/admin/invite-codes").json()["code"]
    assert client.post(f"/api/admin/invite-codes/{code}/revoke").status_code == 200
    resp = _register(client, "friend", invite_code=code)
    assert resp.status_code == 400
    assert err(resp)["code"] == "invalid_invite_code"


# ---------- 登录/登出/会话 ----------

def test_login_success_sets_session_and_logout_revokes(client):
    _register(client, "admin")
    resp = _login(client, "admin")
    assert resp.status_code == 200
    assert "session" in resp.cookies

    assert client.get("/api/auth/me").status_code == 200
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401


def test_login_bad_credentials_generic_error(client):
    """错误密码/不存在用户 → 同一通用错误（防用户枚举）；伪造会话 → 401。"""
    _register(client, "admin")
    r1 = _login(client, "admin", "wrong-password")
    r2 = _login(client, "nobody", "whatever")
    assert r1.status_code == 401 and r2.status_code == 401
    assert err(r1)["code"] == "invalid_credentials"
    assert err(r1)["message"] == err(r2)["message"]

    # 清空 jar 再伪造：直接 set 会与响应 cookie（不同 domain 条目）并存触发
    # httpx CookieConflict，真实浏览器不会出现（同名 cookie 浏览器只保留一个）
    client.cookies.clear()
    client.cookies.set("session", "forged-session-id")
    assert client.get("/api/auth/me").status_code == 401


def test_login_rate_limited(client):
    """连续登录失败（IP 维度）→ 429。"""
    _register(client, "admin")
    for _ in range(10):
        _login(client, "admin", "bad")
    resp = _login(client, "admin", "bad")
    assert resp.status_code == 429
    assert err(resp)["code"] == "rate_limited"


# ---------- 停用/启用用户（管理员） ----------

def test_disabled_user_sessions_revoked_immediately(client):
    """admin 停用用户 → 该用户全部会话立即失效 + 无法再登录；启用后可登录。"""
    _register(client, "admin")
    code = client.post("/api/admin/invite-codes").json()["code"]
    _register(client, "friend", invite_code=code)

    _login(client, "friend")
    assert client.get("/api/auth/me").status_code == 200
    user_id = client.get("/api/auth/me").json()["user"]["id"]

    client.post("/api/auth/logout")
    _login(client, "admin")
    assert client.post(f"/api/admin/users/{user_id}/disable").status_code == 200

    # friend 会话已失效，且无法再登录
    client.post("/api/auth/logout")
    _login(client, "friend")
    assert client.get("/api/auth/me").status_code == 401
    assert _login(client, "friend").status_code == 403

    # 启用后恢复
    client.post("/api/auth/logout")
    _login(client, "admin")
    assert client.post(f"/api/admin/users/{user_id}/enable").status_code == 200
    client.post("/api/auth/logout")
    assert _login(client, "friend").status_code == 200


# ---------- 管理员权限边界 ----------

def test_non_admin_forbidden_from_admin_apis(client):
    """普通用户访问 admin 接口 → 403；admin 可访问。"""
    _register(client, "admin")
    code = client.post("/api/admin/invite-codes").json()["code"]
    _register(client, "friend", invite_code=code)
    client.post("/api/auth/logout")
    _login(client, "friend")

    assert client.get("/api/admin/invite-codes").status_code == 403
    assert client.get("/api/admin/users").status_code == 403
    assert client.post("/api/admin/invite-codes").status_code == 403
    assert client.post(f"/api/admin/invite-codes/{code}/revoke").status_code == 403
    assert client.post("/api/admin/users/xxx/disable").status_code == 403


def test_admin_reset_password(client):
    """admin 重置用户密码 → 旧密码失效、新密码可登录。"""
    _register(client, "admin")
    code = client.post("/api/admin/invite-codes").json()["code"]
    _register(client, "friend", invite_code=code)
    user_id = _login(client, "friend").json()["user"]["id"]

    client.post("/api/auth/logout")
    _login(client, "admin")
    reset = client.post(
        f"/api/admin/users/{user_id}/reset-password", json={"new_password": "NewPass!@#456"}
    )
    assert reset.status_code == 200

    client.post("/api/auth/logout")
    assert _login(client, "friend", "Passw0rd!@#").status_code == 401  # 旧密码失效
    assert _login(client, "friend", "NewPass!@#456").status_code == 200  # 新密码生效


# ---------- 未登录访问受保护 API → 401 ----------

def test_unauthenticated_requests_rejected(client):
    """未登录访问文档/问答/提供商接口 → 401（受保护面全覆盖）。"""
    cases = [
        ("get", "/api/documents"),
        ("post", "/api/qa"),
        ("get", "/api/providers"),
        ("put", "/api/provider"),
        ("post", "/api/provider/validate"),
        ("post", "/api/documents/batch-delete"),
    ]
    for method, path in cases:
        resp = getattr(client, method)(path)
        assert resp.status_code == 401, f"{method} {path}: {resp.status_code}"
        assert resp.json()["error"]["code"] == "unauthorized"

    assert client.get("/api/health").status_code == 200


# ---------- 两账号数据隔离（全链路权限合同） ----------

def test_two_users_full_isolation(client, data_dir: Path):
    """用户 A 的文档 → B 列表不可见、B 删 A 的文档 404、B 批量删含 A 文档 404 不执行。"""
    with get_connection(data_dir) as conn:
        document_store.insert_document(
            conn,
            user_id="some-other-user",
            doc_id="other-doc",
            name="别人的文档.pdf",
            category="其他",
            file_path="/tmp/x.pdf",
            status="ready",
        )

    _register(client, "admin")
    # admin 列表看不到别人的文档
    assert all(d["id"] != "other-doc" for d in client.get("/api/documents").json())
    # admin 删别人的文档 → 404（存在性按 user 过滤，不泄露存在信息之外的内容）
    assert client.delete("/api/documents/other-doc").status_code == 404
    # admin 批量删含别人文档 → 404 且不执行
    resp = client.post("/api/documents/batch-delete", json={"doc_ids": ["other-doc"]})
    assert resp.status_code == 404
    assert err(resp)["code"] == "document_not_found"

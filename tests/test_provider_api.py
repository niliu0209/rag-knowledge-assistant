"""T2：provider API 合同（architecture.md API 节：GET/PUT/validate）。

路由薄层：格式错误 400、Key 无效 422、成功 200；Key 仅回显掩码。
外部边界用 httpx MockTransport（行为保持 fake）。
"""

import httpx
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.provider import DEFAULT_PRESET


def _client(data_dir, transport=None, register: bool = True):
    app = create_app(data_dir=data_dir, provider_transport=transport)
    c = TestClient(app)
    if register:
        # S2-1 认证合同：以首启 admin 身份注册并登录（register=False 供复用会话场景）
        resp = c.post(
            "/api/auth/register",
            json={"username": "admin", "password": "Passw0rd!@#"},
        )
        assert resp.status_code == 200, resp.text
    return c


def test_get_providers_default(data_dir):
    client = _client(data_dir)
    resp = client.get("/api/providers")
    assert resp.status_code == 200
    body = resp.json()
    assert "presets" in body and len(body["presets"]) >= 1
    assert body["presets"][0]["id"] == "siliconflow-free"
    current = body["current"]
    assert current["mode"] == "preset"
    assert current["model"] == DEFAULT_PRESET["model"]
    assert current["embedding_model"] == DEFAULT_PRESET["embedding_model"]
    assert "key_masked" in current


def test_put_invalid_mode_400(data_dir):
    client = _client(data_dir)
    resp = client.put("/api/provider", json={"mode": "bad", "provider": "siliconflow"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_config"


def test_put_byok_custom_without_base_url_400(data_dir):
    client = _client(data_dir)
    resp = client.put(
        "/api/provider",
        json={"mode": "byok", "provider": "custom", "model": "m", "embedding_model": "e", "api_key": "sk-x"},
    )
    assert resp.status_code == 400


def test_put_invalid_key_422(data_dir):
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "invalid key"}})

    client = _client(data_dir, transport=httpx.MockTransport(handler))
    resp = client.put(
        "/api/provider",
        json={
            "mode": "preset",
            "provider": "siliconflow",
            "model": "Qwen/Qwen2.5-7B-Instruct",
            "embedding_model": "BAAI/bge-m3",
            "api_key": "sk-invalid",
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_key"


def test_put_success_then_masked_get(data_dir):
    def handler(request):
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}}
        )

    client = _client(data_dir, transport=httpx.MockTransport(handler))
    resp = client.put(
        "/api/provider",
        json={
            "mode": "preset",
            "provider": "siliconflow",
            "model": "Qwen/Qwen2.5-7B-Instruct",
            "embedding_model": "BAAI/bge-m3",
            "api_key": "sk-abcdef1234567890",
        },
    )
    assert resp.status_code == 200

    got = client.get("/api/providers").json()
    masked = got["current"]["key_masked"]
    assert masked == "sk-****7890"
    assert "sk-abcdef1234567890" not in str(got)


def test_validate_endpoint(data_dir):
    ok_client = _client(
        data_dir,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}}
            )
        ),
    )
    resp = ok_client.post("/api/provider/validate", json={
        "mode": "preset", "provider": "siliconflow",
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "embedding_model": "BAAI/bge-m3", "api_key": "sk-ok",
    })
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # 第二个客户端复用 admin 会话（邀请制下不可二次注册；会话 cookie 跨实例复制）
    bad_client = _client(
        data_dir,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(401, json={"error": {"message": "bad"}})
        ),
        register=False,
    )
    bad_client.cookies.clear()
    bad_client.cookies.set("session", ok_client.cookies.get("session"))
    resp = bad_client.post("/api/provider/validate", json={
        "mode": "preset", "provider": "siliconflow",
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "embedding_model": "BAAI/bge-m3", "api_key": "sk-bad",
    })
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert "Key" in resp.json()["message"]

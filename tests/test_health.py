"""T2：GET /api/health 合同（architecture.md API 合同：200 {status: ok}）。"""

from fastapi.testclient import TestClient

from app.main import create_app


def test_health_returns_ok(data_dir):
    client = TestClient(create_app(data_dir=data_dir))
    resp = client.get("/api/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert isinstance(body.get("version"), str)


def test_health_fails_when_data_dir_unwritable(data_dir, monkeypatch):
    """数据目录不可写（模拟依赖未就绪）时 health 返回 503 而非假成功。"""

    def _blocked(*args, **kwargs):
        raise PermissionError("data dir not writable")

    import app.api.routes.health as health_module

    monkeypatch.setattr(health_module, "check_ready", _blocked)
    client = TestClient(create_app(data_dir=data_dir))
    resp = client.get("/api/health")

    assert resp.status_code == 503

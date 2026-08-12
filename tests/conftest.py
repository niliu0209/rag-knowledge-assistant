"""测试公共配置：每个测试使用独立的临时数据目录，不触碰本地数据。"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _clear_settings_cache(monkeypatch):
    """每个测试前清 Settings 缓存并屏蔽 .env 注入（S2-2 起本地 .env 可能含
    真实 RAG_SHARED_PRESET_KEY / RAG_KEY_ENCRYPTION_KEY——测试环境必须与
    本地配置隔离，否则「无共享 Key」假设的测试被真实注入破坏）。

    需要共享 Key 的测试自行 monkeypatch.setenv + cache_clear（如
    test_provider_service._inject_shared_key）。"""
    from app.core.config import get_settings

    # pydantic-settings 优先级：env 变量 > .env 文件；置空 env 覆盖 dotenv 注入
    monkeypatch.setenv("RAG_SHARED_PRESET_KEY", "")
    monkeypatch.setenv("RAG_KEY_ENCRYPTION_KEY", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """隔离的数据目录：环境变量与测试数据均指向临时目录。"""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("API_PORT", "0")
    return tmp_path


@pytest.fixture
def client(data_dir):
    """已认证客户端（S2-1 合同升级）：以首启 admin 身份注册并登录。

    存量测试（S0/S1）语义不变——业务 API 全部需要认证，默认用
    admin 会话；需要未认证/普通用户场景的测试局部覆盖此 fixture
    （如 tests/test_auth.py 自带 fixture）。
    """
    from fastapi.testclient import TestClient

    from app.main import create_app

    c = TestClient(create_app(data_dir=data_dir))
    resp = c.post(
        "/api/auth/register",
        json={"username": "admin", "password": "Passw0rd!@#"},
    )
    assert resp.status_code == 200, resp.text
    return c

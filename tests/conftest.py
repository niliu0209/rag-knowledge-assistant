"""测试公共配置：每个测试使用独立的临时数据目录，不触碰本地数据。"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """每个测试前清 Settings 缓存：防止首个无 fixture 测试缓存真实 env，
    import app.main 曾因此把迁移/密钥副作用打进真实 data 目录（S1-2 修复）。"""
    from app.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """隔离的数据目录：环境变量与测试数据均指向临时目录。"""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("API_PORT", "0")
    return tmp_path

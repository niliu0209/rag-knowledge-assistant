"""测试公共配置：每个测试使用独立的临时数据目录，不触碰本地数据。"""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """隔离的数据目录：环境变量与测试数据均指向临时目录。"""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("API_PORT", "0")
    return tmp_path

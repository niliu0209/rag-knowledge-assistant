"""S2-3 公开部署：请求体限制 / 页脚备案号 / 隐私说明 / 日志落盘。

覆盖发布准备安全面：大 body 拒绝（413）、备案号渲染规则、隐私页关键条款
（上线必需）、日志文件落盘。UI 可达性/HTTPS 由容器 + 浏览器实测（S2-3
验收锚点），单测锁定服务端与纯函数行为。
"""

from __future__ import annotations

import logging
from unittest import mock

from fastapi.testclient import TestClient


def test_body_limit_rejects_oversize_payload(data_dir, monkeypatch):
    """超限请求体必须 413（S2-3 防大 body 攻击，全局中间件兜底）。"""
    monkeypatch.setenv("MAX_BODY_BYTES", str(1024))
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    c = TestClient(create_app(data_dir=data_dir))
    resp = c.post(
        "/api/auth/login",
        json={"username": "x", "password": "y" * 2000},
    )
    assert resp.status_code == 413, resp.text
    assert resp.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_body_limit_allows_normal_payload(client):
    """正常大小请求不受限制（默认 26MiB 上限，注册/登录正常）。"""
    resp = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "Passw0rd!@#"},
    )
    assert resp.status_code == 200, resp.text


def test_log_files_created_on_app_start(data_dir):
    """create_app 启动即创建访问日志与应用日志文件（S2-3 日志落盘）。"""
    from app.main import create_app

    TestClient(create_app(data_dir=data_dir))
    assert (data_dir / "logs" / "access.log").exists()
    assert (data_dir / "logs" / "app.log").exists()


def test_footer_hidden_without_icp_number():
    """未配置备案号（本地/未备案）不渲染页脚。"""
    from ui.footer import render_footer

    with mock.patch("ui.footer.st.caption") as caption:
        render_footer("", "")
    caption.assert_not_called()


def test_footer_shows_icp_with_link():
    """配置 ICP 备案号后展示并链接工信部。"""
    from ui.footer import render_footer

    with mock.patch("ui.footer.st.caption") as caption:
        render_footer("浙ICP备2026999999号-1", "")
    caption.assert_called_once()
    text = caption.call_args.args[0]
    assert "ICP备案号 浙ICP备2026999999号-1" in text
    assert "https://beian.miit.gov.cn/" in text


def test_footer_shows_police_number_after_public_security():
    """公安联网备案通过后补公安备案号（与 ICP 并列展示）。"""
    from ui.footer import render_footer

    with mock.patch("ui.footer.st.caption") as caption:
        render_footer("浙ICP备2026999999号-1", "浙公网安备33010002000000号")
    text = caption.call_args.args[0]
    assert "公安备案号 浙公网安备33010002000000号" in text
    assert "https://www.beian.gov.cn/" in text


def test_privacy_page_covers_required_terms():
    """隐私说明必须覆盖上线合同关键条款（内容变更先改此处）。"""
    from ui.privacy_page import PRIVACY_SECTIONS

    body = "\n".join(t for _, t in PRIVACY_SECTIONS)
    assert "发送给" in body  # 数据流向
    assert "SiliconFlow" in body  # 免费预设提供商点名
    assert "加密" in body  # Key 加密存储
    assert "邀请" in body  # 邀请制范围
    assert "隔离" in body  # 数据隔离
    assert "删除" in body  # 用户权利


def test_privacy_section_titles_cover_contract():
    """隐私页各节标题覆盖：范围/数据流向/Key/预设/托管。"""
    from ui.privacy_page import PRIVACY_SECTIONS

    titles = [t for t, _ in PRIVACY_SECTIONS]
    assert any("邀请" in t for t in titles)
    assert any("流向" in t for t in titles)
    assert any("Key" in t for t in titles)
    assert any("预设" in t for t in titles)
    assert any("托管" in t for t in titles)


# ---------- S2-3 修复：Secure cookie 显式管理（cookie_secure=true 时内部
# 明文链路 httpx jar 不发送 Secure cookie——登录后会话丢失） ----------

def _fake_session_state() -> dict:
    return {}


def test_store_session_cookie_extracts_and_rotates_client():
    """登录响应 Set-Cookie 提取会话值；旧 client（无 Cookie 头）被销毁。"""
    import httpx

    import ui.http as http
    old_client = httpx.Client(base_url="http://api:8000")
    ss = {"api_client_http://api:8000": old_client}
    with mock.patch.object(http.st, "session_state", ss):
        resp = httpx.Response(
            200,
            headers={
                "set-cookie": "session=abc123; Path=/; HttpOnly; Secure; SameSite=Lax"
            },
        )
        http.store_session_cookie("http://api:8000", resp)
    assert ss["session_cookie_http://api:8000"] == "abc123"
    assert "api_client_http://api:8000" not in ss  # 旧 client 已销毁


def test_get_client_carries_explicit_cookie_header():
    """重建后的 client 显式携带 Cookie 头（绕过 jar 的 Secure 明文拒绝）。"""
    import ui.http as http
    ss = {"session_cookie_http://api:8000": "abc123"}
    with mock.patch.object(http.st, "session_state", ss):
        client = http.get_client("http://api:8000")
    try:
        assert client.headers["Cookie"] == "session=abc123"
    finally:
        client.close()


def test_get_client_without_cookie_has_no_cookie_header():
    """未登录（无会话 cookie）时 client 不带 Cookie 头。"""
    import ui.http as http
    ss = {}
    with mock.patch.object(http.st, "session_state", ss):
        client = http.get_client("http://api:8000")
    try:
        assert "Cookie" not in client.headers
    finally:
        client.close()


def test_clear_auth_removes_session_cookie():
    """登出/401 清理时同时清除会话 cookie（防残留旧会话）。"""
    import ui.http as http
    ss = {"user": {"username": "admin"}, "session_cookie_http://api:8000": "abc123"}
    with mock.patch.object(http.st, "session_state", ss):
        http.clear_auth()
    assert "user" not in ss
    assert not [k for k in ss if k.startswith("session_cookie_")]

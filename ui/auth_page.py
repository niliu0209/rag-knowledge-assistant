"""登录/注册页（S2-1 认证门）。

纯客户端：只调 /api/auth/* 并保存登录态；错误体 message 直接展示。
首启用户注册即管理员（服务端合同）；非首启注册需要邀请码。
"""

from __future__ import annotations

import httpx
import streamlit as st

from ui.http import clear_auth, get_client


def render_auth_page(api_url: str) -> None:
    st.title("知识库问答助手")

    # 会话失效提示（401 后 rerun 至此）
    if st.session_state.pop("auth_expired", None):
        st.warning("登录已失效，请重新登录")

    tab_login, tab_register = st.tabs(["登录", "注册"])

    with tab_login:
        with st.form("login_form"):
            username = st.text_input("用户名")
            password = st.text_input("密码", type="password")
            submitted = st.form_submit_button("登录")
        if submitted:
            _login(api_url, username, password)

    with tab_register:
        st.caption("首个注册用户成为管理员（自用部署：作者先注册再邀请他人）")
        with st.form("register_form"):
            new_username = st.text_input("用户名", key="reg_username")
            new_password = st.text_input("密码（至少 8 位）", type="password", key="reg_password")
            invite_code = st.text_input("邀请码（可选）", key="reg_invite")
            submitted = st.form_submit_button("注册")
        if submitted:
            _register(api_url, new_username, new_password, invite_code)


def _login(api_url: str, username: str, password: str) -> None:
    if not username or not password:
        st.error("请输入用户名和密码")
        return
    client = get_client(api_url)
    try:
        resp = client.post("/api/auth/login", json={"username": username, "password": password})
    except httpx.HTTPError as exc:
        st.error(f"无法连接后端服务：{exc}")
        return
    if resp.status_code == 200:
        _store_user(resp.json()["user"])
        st.rerun()
    else:
        st.error(resp.json()["error"]["message"])


def _register(api_url: str, username: str, password: str, invite_code: str) -> None:
    if not username or not password:
        st.error("请输入用户名和密码")
        return
    client = get_client(api_url)
    body = {"username": username, "password": password}
    if invite_code and invite_code.strip():
        body["invite_code"] = invite_code.strip()
    try:
        resp = client.post("/api/auth/register", json=body)
    except httpx.HTTPError as exc:
        st.error(f"无法连接后端服务：{exc}")
        return
    if resp.status_code == 200:
        _store_user(resp.json()["user"])
        st.rerun()
    else:
        st.error(resp.json()["error"]["message"])


def _store_user(user: dict) -> None:
    st.session_state["user"] = user

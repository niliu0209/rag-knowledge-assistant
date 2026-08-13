"""Streamlit 入口（纯客户端：只调 API，不直连数据库、不持有密钥）。

S2-1 认证门：未登录只显示登录/注册；登录后侧边栏展示用户与登出；
每个 rerun 做会话探活（/api/auth/me）——用户被停用/会话过期即时回登录页。
管理员额外可见「管理」页（邀请码/用户管理）。
"""

from __future__ import annotations

import httpx
import streamlit as st

from app.core.config import get_settings
from ui.admin_page import render as render_admin_page
from ui.auth_page import render_auth_page
from ui.documents_page import render as render_documents_page
from ui.footer import render_footer
from ui.http import clear_auth, get_client
from ui.privacy_page import render_privacy_page
from ui.provider_page import render as render_provider_page
from ui.qa_page import render as render_qa_page
from ui.theme import inject_theme
from ui.upload_page import render as render_upload_page

st.set_page_config(page_title="知识库问答助手", page_icon="📚", layout="wide")
inject_theme()

settings = get_settings()
api_url = settings.api_url

# ---------- 认证门 ----------
user = st.session_state.get("user")
if user is None:
    render_auth_page(api_url)
    st.stop()

# 会话探活：停用/过期即时失效（S2-1 合同；5xx 服务暂不可达不清登录态）
client = get_client(api_url)
try:
    resp = client.get("/api/auth/me", timeout=5.0)
    if resp.status_code == 401:
        clear_auth()
        st.session_state["auth_expired"] = True
        st.rerun()
    elif resp.status_code == 200:
        user = resp.json()["user"]
        st.session_state["user"] = user
except httpx.HTTPError:
    pass  # 服务不可达：保留登录态，页面内已有错误提示

# ---------- 导航 ----------
options = ["首页", "文档上传", "文档列表", "问答", "提供商配置", "隐私说明"]
if user["role"] == "admin":
    options.append("管理")

with st.sidebar:
    st.caption(f"👤 {user['username']}" + ("（管理员）" if user["role"] == "admin" else ""))
    page = st.radio("导航", options=options, label_visibility="collapsed")
    if st.button("退出登录", use_container_width=True):
        try:
            client.post("/api/auth/logout", timeout=5.0)
        except httpx.HTTPError:
            pass
        clear_auth()
        st.rerun()

# S2-3 页脚备案号（env 配置后展示，所有页面可见；未配置不显示）
render_footer(settings.icp_number, settings.police_number)

if page == "文档上传":
    render_upload_page(api_url)
    st.stop()

if page == "文档列表":
    render_documents_page(api_url)
    st.stop()

if page == "问答":
    render_qa_page(api_url)
    st.stop()

if page == "提供商配置":
    render_provider_page(api_url)
    st.stop()

if page == "隐私说明":
    render_privacy_page()
    st.stop()

if page == "管理":
    render_admin_page(api_url)
    st.stop()

st.title("本地知识库问答助手")

with st.spinner("检查服务状态…"):
    try:
        resp = client.get("/api/health", timeout=5.0)
        resp.raise_for_status()
        body = resp.json()
        st.success(f"服务正常（版本 {body.get('version', '?')}）")
    except httpx.HTTPError as exc:
        st.error(f"无法连接后端服务：{exc}")
        st.caption("请确认 api 服务已启动（docker compose up）")

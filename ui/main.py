"""Streamlit 入口（纯客户端：只调 API，不直连数据库、不持有密钥）。

阶段 0 页面导航：首页（服务状态）→ S1 起提供配置页；S2-S5 逐步扩展
上传/列表/问答页。
"""

from __future__ import annotations

import httpx
import streamlit as st

from app.core.config import get_settings
from ui.documents_page import render as render_documents_page
from ui.provider_page import render as render_provider_page
from ui.theme import inject_theme
from ui.upload_page import render as render_upload_page

st.set_page_config(page_title="知识库问答助手", page_icon="📚", layout="wide")
inject_theme()

settings = get_settings()

page = st.sidebar.radio(
    "导航",
    options=["首页", "文档上传", "文档列表", "提供商配置"],
    label_visibility="collapsed",
)

if page == "文档上传":
    render_upload_page(settings.api_url)
    st.stop()

if page == "文档列表":
    render_documents_page(settings.api_url)
    st.stop()

if page == "提供商配置":
    render_provider_page(settings.api_url)
    st.stop()

st.title("本地知识库问答助手")

with st.spinner("检查服务状态…"):
    try:
        resp = httpx.get(f"{settings.api_url}/api/health", timeout=5.0)
        resp.raise_for_status()
        body = resp.json()
        st.success(f"服务正常（版本 {body.get('version', '?')}）")
    except httpx.HTTPError as exc:
        st.error(f"无法连接后端服务：{exc}")
        st.caption("请确认 api 服务已启动（docker compose up）")

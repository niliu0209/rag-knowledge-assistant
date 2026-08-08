"""Streamlit 入口（纯客户端：只调 API，不直连数据库、不持有密钥）。

阶段 0 骨架页：显示服务健康状态。S1-S5 逐步扩展配置/上传/列表/问答页。
"""

from __future__ import annotations

import httpx
import streamlit as st

from app.core.config import get_settings
from ui.theme import inject_theme

st.set_page_config(page_title="知识库问答助手", page_icon="📚", layout="wide")
inject_theme()

st.title("本地知识库问答助手")

settings = get_settings()

with st.spinner("检查服务状态…"):
    try:
        resp = httpx.get(f"{settings.api_url}/api/health", timeout=5.0)
        resp.raise_for_status()
        body = resp.json()
        st.success(f"服务正常（版本 {body.get('version', '?')}）")
    except httpx.HTTPError as exc:
        st.error(f"无法连接后端服务：{exc}")
        st.caption("请确认 api 服务已启动（docker compose up）")

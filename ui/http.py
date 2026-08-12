"""UI 层共享 HTTP client（S2-1 认证）。

Streamlit rerun 模型下，各页面裸建 httpx 调用无法携带会话 cookie；
这里按 api_url 缓存一个 httpx.Client（自带 cookie jar），登录后
会话在 jar 中保持，页面只负责展示与映射，不持有任何密钥。

401 统一处理：会话过期/用户被停用时，页面请求返回 401 →
handle_unauthorized 清空登录状态并提示重登（服务不可达 5xx 不清）。
"""

from __future__ import annotations

import httpx
import streamlit as st


def get_client(api_url: str) -> httpx.Client:
    key = f"api_client_{api_url}"
    client = st.session_state.get(key)
    if client is None:
        client = httpx.Client(base_url=api_url, timeout=30.0)
        st.session_state[key] = client
    return client


def clear_auth() -> None:
    """登出/会话失效：清登录态与共享 client（含失效 cookie）。"""
    for key in [k for k in st.session_state.keys() if k.startswith("api_client_")]:
        client = st.session_state.pop(key)
        if client is not None:
            client.close()
    st.session_state.pop("user", None)


def handle_unauthorized(api_url: str) -> bool:
    """401 处理：清登录态并提示重登；返回是否已清理（调用方应停渲染）。"""
    clear_auth()
    st.session_state["auth_expired"] = True
    st.rerun()
    return True

"""UI 层共享 HTTP client（S2-1 认证；S2-3 Secure cookie 适配）。

Streamlit rerun 模型下，各页面裸建 httpx 调用无法携带会话 cookie；
这里按 api_url 缓存一个 httpx.Client，登录后会话在共享状态中保持，
页面只负责展示与映射，不持有任何密钥。

会话 cookie 显式管理（S2-3 修复）：ui→api 走内部明文 HTTP，httpx 的
cookie jar 遵循 RFC 6265 不发送 Secure cookie（cookie_secure=true 时
会话在浏览器侧可接受、但 jar 拒绝明文携带）——登录响应中提取
Set-Cookie 会话值存入 session_state，客户端显式带 Cookie 头（绕过
jar 的 Secure 检查；Secure 语义仍由浏览器→Caddy 的 HTTPS 链路保证）。

401 统一处理：会话过期/用户被停用时，页面请求返回 401 →
handle_unauthorized 清空登录状态并提示重登（服务不可达 5xx 不清）。
"""

from __future__ import annotations

import httpx
import streamlit as st

# 与 app/api/routes/auth.py 的会话 cookie 名保持一致（S2-1 合同）
_SESSION_COOKIE = "session"


def _cookie_state_key(api_url: str) -> str:
    return f"session_cookie_{api_url}"


def get_client(api_url: str) -> httpx.Client:
    key = f"api_client_{api_url}"
    client = st.session_state.get(key)
    if client is None:
        headers = {}
        session_value = st.session_state.get(_cookie_state_key(api_url))
        if session_value:
            headers["Cookie"] = f"{_SESSION_COOKIE}={session_value}"
        client = httpx.Client(base_url=api_url, timeout=30.0, headers=headers)
        st.session_state[key] = client
    return client


def store_session_cookie(api_url: str, resp: httpx.Response) -> None:
    """登录/注册成功后：从 Set-Cookie 提取会话值存入共享状态，并销毁旧
    client（其创建时未带 Cookie 头，rerun 后按新会话重建）。"""
    for name, value in resp.headers.multi_items():
        if name.lower() == "set-cookie":
            cookie_name, _, rest = value.partition("=")
            if cookie_name.strip() == _SESSION_COOKIE:
                st.session_state[_cookie_state_key(api_url)] = rest.split(";")[0].strip()
                old = st.session_state.pop(f"api_client_{api_url}", None)
                if old is not None:
                    old.close()
                return


def clear_auth() -> None:
    """登出/会话失效：清登录态、共享 client 与会话 cookie。"""
    for key in [k for k in st.session_state.keys() if k.startswith("api_client_")]:
        client = st.session_state.pop(key)
        if client is not None:
            client.close()
    for key in [k for k in st.session_state.keys() if k.startswith("session_cookie_")]:
        st.session_state.pop(key)
    st.session_state.pop("user", None)


def handle_unauthorized(api_url: str) -> bool:
    """401 处理：清登录态并提示重登；返回是否已清理（调用方应停渲染）。"""
    clear_auth()
    st.session_state["auth_expired"] = True
    st.rerun()
    return True

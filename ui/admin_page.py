"""管理员页（S2-1）：邀请码管理 + 用户管理（仅 role=admin 可见）。

纯客户端：只调 /api/admin/*；管理员 API 由服务端 require_admin 保护，
本页只是菜单入口（main.py 按 role 控制可见性），不持有任何业务规则。
"""

from __future__ import annotations

import httpx
import streamlit as st

from ui.http import get_client, handle_unauthorized


def render(api_url: str) -> None:
    st.subheader("邀请码管理")
    st.caption("邀请制注册：新用户必须持有效邀请码注册（首启管理员除外）")

    client = get_client(api_url)

    col_gen, _ = st.columns([1, 3])
    with col_gen:
        if st.button("生成邀请码", use_container_width=True):
            try:
                resp = client.post("/api/admin/invite-codes")
            except httpx.HTTPError as exc:
                st.error(f"无法连接后端服务：{exc}")
                return
            if resp.status_code == 401:
                handle_unauthorized(api_url)
                return
            if resp.status_code == 200:
                st.code(resp.json()["code"], language=None)
            else:
                st.error(resp.json()["error"]["message"])

    try:
        resp = client.get("/api/admin/invite-codes")
    except httpx.HTTPError as exc:
        st.error(f"无法加载邀请码：{exc}")
        return
    if resp.status_code == 401:
        handle_unauthorized(api_url)
        return
    codes = resp.json()["invite_codes"]
    if not codes:
        st.info("还没有邀请码。点击上方按钮生成后分发给邀请用户。")
    else:
        rows = []
        for c in codes:
            if c["used_by"]:
                status = "已使用"
            elif c["revoked_at"]:
                status = "已撤销"
            else:
                status = "可用"
            rows.append(
                {
                    "邀请码": c["code"],
                    "状态": status,
                    "操作": c["code"] if c["revoked_at"] is None and c["used_by"] is None else "",
                }
            )
        # 撤销按钮逐行渲染（列表行操作）
        revoke_placeholder = st.empty()
        st.table([{k: v for k, v in r.items() if k != "操作"} for r in rows])
        revoke_target = st.selectbox(
            "选择要撤销的邀请码", [r["邀请码"] for r in rows if r["状态"] == "可用"]
        ) if any(r["状态"] == "可用" for r in rows) else None
        if revoke_target and st.button("撤销所选邀请码"):
            try:
                resp = client.post(f"/api/admin/invite-codes/{revoke_target}/revoke")
            except httpx.HTTPError as exc:
                st.error(f"无法连接后端服务：{exc}")
                return
            if resp.status_code == 401:
                handle_unauthorized(api_url)
                return
            if resp.status_code == 200:
                st.success(f"已撤销 {revoke_target}")
                st.rerun()
            else:
                st.error(resp.json()["error"]["message"])

    st.divider()
    st.subheader("用户管理")

    try:
        resp = client.get("/api/admin/users")
    except httpx.HTTPError as exc:
        st.error(f"无法加载用户列表：{exc}")
        return
    if resp.status_code == 401:
        handle_unauthorized(api_url)
        return
    users = resp.json()["users"]
    rows = [
        {
            "用户名": u["username"],
            "角色": "管理员" if u["role"] == "admin" else "用户",
            "状态": "正常" if u["status"] == "active" else "已停用",
        }
        for u in users
    ]
    st.table(rows)

    target = st.selectbox("选择用户", [u["username"] for u in users])
    user_row = next(u for u in users if u["username"] == target)
    col_disable, col_enable, col_reset = st.columns(3)
    with col_disable:
        if st.button("停用", disabled=user_row["status"] != "active", use_container_width=True):
            _admin_post(api_url, f"/api/admin/users/{user_row['id']}/disable")
    with col_enable:
        if st.button("启用", disabled=user_row["status"] != "disabled", use_container_width=True):
            _admin_post(api_url, f"/api/admin/users/{user_row['id']}/enable")
    with col_reset:
        if st.button("重置密码", use_container_width=True):
            st.session_state["reset_target"] = user_row
    if st.session_state.get("reset_target") == user_row:
        with st.form("reset_password_form"):
            new_password = st.text_input("新密码（至少 8 位）", type="password")
            if st.form_submit_button("确认重置"):
                _admin_post(
                    api_url,
                    f"/api/admin/users/{user_row['id']}/reset-password",
                    json={"new_password": new_password},
                )
                st.session_state.pop("reset_target", None)


def _admin_post(api_url: str, path: str, json: dict | None = None) -> None:
    client = get_client(api_url)
    try:
        resp = client.post(path, json=json)
    except httpx.HTTPError as exc:
        st.error(f"无法连接后端服务：{exc}")
        return
    if resp.status_code == 401:
        handle_unauthorized(api_url)
        return
    if resp.status_code == 200:
        st.success("操作成功")
        st.rerun()
    else:
        st.error(resp.json()["error"]["message"])

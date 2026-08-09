"""文档列表页（F0-2，S3）。

纯客户端：列表/删除均调 API；删除有二次确认；空列表引导上传。
"""

from __future__ import annotations

import httpx
import streamlit as st


def render(api_url: str) -> None:
    st.subheader("文档列表")

    # 删除成功的提示跨 rerun 保留（session_state；下次交互前持续可见）
    msg = st.session_state.pop("list_success", None)
    if msg:
        st.success(msg)

    try:
        resp = httpx.get(f"{api_url}/api/documents", timeout=10.0)
        resp.raise_for_status()
        docs = resp.json()
    except httpx.HTTPError as exc:
        st.error(f"无法加载文档列表：{exc}")
        return

    if not docs:
        st.info("知识库还没有文档。请到「文档上传」页上传 PDF 或 Word 开始使用。")
        return

    st.caption(f"共 {len(docs)} 份文档")

    rows = []
    for d in docs:
        page_txt = f"{d['page_count']} 页" if d.get("page_count") else "Word（无页码）"
        rows.append(
            {
                "名称": d["name"],
                "分类": d["category"],
                "页数": page_txt,
                "字符数": d["char_count"],
                "入库时间": (d["created_at"] or "")[:19],
            }
        )
    st.dataframe(rows, use_container_width=True, hide_index=True)

    st.divider()
    st.markdown("### 删除文档")
    options = {d["name"]: d["id"] for d in docs}
    target = st.selectbox("选择要删除的文档", options=list(options.keys()))
    confirm = st.checkbox("我确认删除该文档及其全部切片与向量（不可恢复）")
    if st.button("删除", type="primary", disabled=not confirm):
        try:
            resp = httpx.delete(
                f"{api_url}/api/documents/{options[target]}", timeout=30.0
            )
            body = resp.json()
        except httpx.HTTPError as exc:
            st.error(f"删除请求失败：{exc}")
            return
        if resp.status_code == 200:
            st.session_state["list_success"] = f"已删除「{target}」"
            st.rerun()
        else:
            st.error(body.get("error", {}).get("message", "删除失败，请稍后重试"))

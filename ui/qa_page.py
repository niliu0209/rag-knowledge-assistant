"""问答页（F0-3，S4）。

纯客户端：提问调 POST /api/qa；回答 + 引用来源（文档名+原文片段+页码）展示。
阶段 0 不做多轮对话历史 UI（function-list 不做什么）；记录在后台（F0-5）。
"""

from __future__ import annotations

import httpx
import streamlit as st


def render(api_url: str) -> None:
    st.subheader("问答")

    # 知识库为空时引导（不阻塞提问，用户可直接看到错误信息）
    try:
        docs = httpx.get(f"{api_url}/api/documents", timeout=10.0).json()
    except httpx.HTTPError:
        docs = []
    if not docs:
        st.info("知识库还没有文档。请先到「文档上传」页上传 PDF 或 Word，再来提问。")

    with st.form("qa_form"):
        question = st.text_area(
            "你的问题",
            placeholder="例如：行政部本周完成了哪些工作？",
            height=100,
        )
        submitted = st.form_submit_button("提问", type="primary")
    if not submitted:
        return
    if not question.strip():
        st.warning("请输入问题")
        return

    with st.spinner("检索知识库并生成回答…"):
        try:
            resp = httpx.post(
                f"{api_url}/api/qa", json={"question": question}, timeout=120.0
            )
            body = resp.json()
        except httpx.HTTPError as exc:
            st.error(f"提问失败：{exc}")
            return

    if resp.status_code != 200:
        st.error(body.get("error", {}).get("message", "提问失败，请稍后重试"))
        return

    st.markdown(body["answer"])
    citations = body.get("citations") or []
    if citations:
        st.divider()
        st.caption(f"引用来源（{len(citations)} 条，可展开核对原文）")
        for i, c in enumerate(citations, start=1):
            page_txt = f" · 第 {c['page']} 页" if c.get("page") else ""
            with st.expander(f"[{i}] {c['document_name']}{page_txt}"):
                st.write(c["snippet"])
    else:
        st.caption("本次未检索到相关内容，未调用大模型。")
    st.caption(f"提供商：{body.get('provider', '')}")

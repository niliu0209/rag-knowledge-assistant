"""问答页（F0-3，S4；S1-3 会话式多轮）。

纯客户端：会话消息存 session_state.qa_messages（[{role, content}]）；
提问携带最近对话作 history（前端携带历史，无服务端会话状态）；
回答 + 引用来源（文档名+原文片段+页码）展示；新建会话清空消息。
会话隔离：不同会话 = 不同消息列表（单用户场景隔离在 UI 会话级）。
"""

from __future__ import annotations

import httpx
import streamlit as st

# 携带历史上限：与服务端归一化一致（最近 10 条，服务端兜底截断）
HISTORY_MAX_MESSAGES = 10


def render(api_url: str) -> None:
    st.subheader("问答")

    if "qa_messages" not in st.session_state:
        st.session_state.qa_messages = []

    # 工具栏：新建会话（清空当前会话消息）
    left, _ = st.columns([1, 8])
    if left.button("新建会话", use_container_width=True):
        st.session_state.qa_messages = []
        st.rerun()

    # 知识库为空时引导（不阻塞提问，用户可直接看到错误信息）
    try:
        docs = httpx.get(f"{api_url}/api/documents", timeout=10.0).json()
    except httpx.HTTPError:
        docs = []
    if not docs:
        st.info("知识库还没有文档。请先到「文档上传」页上传 PDF 或 Word，再来提问。")

    # 会话消息展示（含上一轮回答与引用）
    for msg in st.session_state.qa_messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    question = st.chat_input("输入问题…（支持连续提问，可引用上文）")
    if not question or not question.strip():
        return
    question = question.strip()

    # 当前问题入会话；history = 当前问题之前最近 N 条（刚 append 的是当前问题）
    st.session_state.qa_messages.append({"role": "user", "content": question})
    history = [
        {"role": m["role"], "content": m["content"]}
        for m in st.session_state.qa_messages[-HISTORY_MAX_MESSAGES - 1 : -1]
    ]

    with st.chat_message("user"):
        st.markdown(question)

    with st.spinner("检索知识库并生成回答…"):
        try:
            resp = httpx.post(
                f"{api_url}/api/qa",
                json={"question": question, "history": history},
                timeout=120.0,
            )
            body = resp.json()
        except httpx.HTTPError as exc:
            st.error(f"提问失败：{exc}")
            return

    if resp.status_code != 200:
        st.error(body.get("error", {}).get("message", "提问失败，请稍后重试"))
        return

    answer = body["answer"]
    citations = body.get("citations") or []
    with st.chat_message("assistant"):
        st.markdown(answer)
        if citations:
            st.caption(f"引用来源（{len(citations)} 条，可展开核对原文）")
            for i, c in enumerate(citations, start=1):
                page_txt = f" · 第 {c['page']} 页" if c.get("page") else ""
                with st.expander(f"[{i}] {c['document_name']}{page_txt}"):
                    st.write(c["snippet"])
        else:
            st.caption("本次未检索到相关内容，未调用大模型。")
        st.caption(f"提供商：{body.get('provider', '')}")

    st.session_state.qa_messages.append({"role": "assistant", "content": answer})

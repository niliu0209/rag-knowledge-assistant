"""文档上传页（F0-1，S2 最小版）。

纯客户端：分类/大小/格式校验由服务端重新验证（POST /api/documents）；
成功提示含文档名、页数/字符数（F0-1 验收）。
"""

from __future__ import annotations

import httpx
import streamlit as st

CATEGORIES = ("开发调试", "业务报告", "其他")


def render(api_url: str) -> None:
    st.subheader("文档上传")

    with st.form("upload"):
        file = st.file_uploader(
            "选择文档（PDF / Word）",
            type=["pdf", "docx"],
            help="支持中文 PDF 与 Word，单文件不超过 20MB；扫描件（无文字）会被拒绝。",
        )
        category = st.selectbox("文档分类", options=CATEGORIES)
        submitted = st.form_submit_button("上传并入库", type="primary")

    if not submitted or file is None:
        return

    try:
        resp = httpx.post(
            f"{api_url}/api/documents",
            files={"file": (file.name, file.getvalue(), "application/octet-stream")},
            data={"category": category},
            timeout=300.0,  # 解析+向量化，首次上传可能较慢
        )
        body = resp.json()
    except httpx.HTTPError as exc:
        st.error(f"请求失败：{exc}")
        return

    if resp.status_code == 200:
        pages = body.get("page_count")
        page_txt = f"{pages} 页" if pages else "无页码（Word）"
        st.success(
            f"入库成功：{body['name']}（{body['category']}，"
            f"{page_txt}，{body['char_count']} 字符）"
        )
    else:
        st.error(body.get("error", {}).get("message", "上传失败，请稍后重试"))

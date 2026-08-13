"""页脚备案号（S2-3 公开部署合规展示）。

ICP 备案号 + 工信部链接（beian.miit.gov.cn）；公安联网备案通过后补公安备案号。
配置经 env 注入（RAG_ICP_NUMBER / RAG_POLICE_NUMBER，ui 进程 settings 读取）；
未配置（本地开发/未备案）不显示。纯客户端展示，号码不入库。
"""

from __future__ import annotations

import streamlit as st


def render_footer(icp_number: str, police_number: str) -> None:
    links: list[str] = []
    if icp_number:
        links.append(f"[ICP备案号 {icp_number}](https://beian.miit.gov.cn/)")
    if police_number:
        links.append(f"[公安备案号 {police_number}](https://www.beian.gov.cn/)")
    if links:
        st.caption(" ｜ ".join(links))

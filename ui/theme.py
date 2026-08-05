"""样式 token 唯一注入点（design 真源：dev-docs/design/README.md）。

所有界面样式值必须取自本文件；禁止在组件中散落硬编码色值/字号/间距。
改动样式 = 先更新 dev-docs/design/README.md token 表，再同步本文件。
"""

from __future__ import annotations

import streamlit as st

TOKENS = {
    # 颜色
    "color_primary": "#2563EB",
    "color_bg": "#FFFFFF",
    "color_bg_muted": "#F4F5F7",
    "color_text": "#1F2937",
    "color_text_muted": "#6B7280",
    "color_border": "#E5E7EB",
    "color_error": "#DC2626",
    "color_success": "#16A34A",
    # 间距（4px 基数）
    "space_xs": "4px",
    "space_sm": "8px",
    "space_md": "16px",
    "space_lg": "24px",
    # 字体
    "font_family": "'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', system-ui, sans-serif",
    "font_size_base": "15px",
    "font_size_title": "24px",
    # 圆角
    "radius_sm": "6px",
    "radius_md": "10px",
    # 断点（三档，桌面优先）
    "breakpoint_desktop": "1024px",
    "breakpoint_tablet": "768px",
    "breakpoint_mobile": "600px",
}


def inject_theme() -> None:
    """注入全局 CSS（仅入口页调用一次）。"""
    t = TOKENS
    st.markdown(
        f"""
        <style>
        html, body, .stApp {{
            font-family: {t["font_family"]};
            color: {t["color_text"]};
        }}
        .stApp {{
            background: {t["color_bg"]};
            font-size: {t["font_size_base"]};
        }}
        /* 手机断点：保证"能读能问" */
        @media (max-width: 600px) {{
            .stApp {{ font-size: 14px; }}
            .block-container {{ padding: 1rem; }}
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )

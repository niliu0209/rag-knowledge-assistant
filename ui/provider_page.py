"""提供商配置页（F0-4，S1 最小版；S2-2 预设模式 Key 可选）。

纯客户端：所有校验依赖服务端重新验证（PUT /api/provider / validate）；
Key 输入用 password 控件不回显明文，服务端返回的 key_masked 仅展示掩码。

S2-2：preset 模式不要求 Key——未自备 Key 时服务端回落平台共享额度
（RAG_SHARED_PRESET_KEY env，不入库/日志/界面）；本页按 key_source 展示状态。
"""

from __future__ import annotations

import httpx
import streamlit as st

from ui.http import get_client, handle_unauthorized


def render(api_url: str) -> None:
    st.subheader("提供商配置")

    client = get_client(api_url)
    try:
        resp = client.get("/api/providers", timeout=5.0)
        data = resp.json()
    except httpx.HTTPError as exc:
        st.error(f"无法连接后端服务：{exc}")
        return
    if resp.status_code == 401:
        handle_unauthorized(api_url)
        return
    presets = data["presets"]
    current = data["current"]

    with st.form("provider_config"):
        mode = st.radio(
            "模式",
            options=["preset", "byok"],
            format_func=lambda m: "免费预设" if m == "preset" else "自带 Key（BYOK）",
            index=0 if current["mode"] == "preset" else 1,
            horizontal=True,
        )

        if mode == "preset":
            preset_ids = [p["id"] for p in presets]
            preset_labels = {p["id"]: f'{p["name"]}（{p["model"]}）' for p in presets}
            provider = st.selectbox(
                "免费预设",
                options=preset_ids,
                format_func=lambda pid: preset_labels[pid],
                index=preset_ids.index("siliconflow-free"),
            )
            chosen = next(p for p in presets if p["id"] == provider)
            model = chosen["model"]
            embedding_model = chosen["embedding_model"]
            base_url = None
            st.caption("免费模型政策多变：若遇限流（429）会自动退避重试，持续失败请切换预设或配置 Key。")
            # S2-2 共享额度状态（基于当前生效配置，随页面加载展示）
            if current["key_source"] == "shared":
                st.success("平台免费预设（无需 Key）：当前使用平台共享额度")
            elif current["key_source"] == "own":
                st.caption(
                    f"已使用你自己的 Key（{current['key_masked']}），优先于平台共享额度"
                )
            else:
                st.warning(
                    "免费预设暂不可用：平台共享 Key 未配置（RAG_SHARED_PRESET_KEY），"
                    "请联系管理员，或切换 BYOK 配置自己的 Key"
                )
        else:
            provider = st.selectbox(
                "提供商",
                options=["siliconflow", "deepseek", "openai", "custom"],
                format_func=lambda p: {
                    "siliconflow": "SiliconFlow", "deepseek": "DeepSeek",
                    "openai": "OpenAI", "custom": "自定义（OpenAI 兼容）",
                }[p],
            )
            model = st.text_input("LLM 模型名", value=current["model"])
            embedding_model = st.text_input("embedding 模型名", value=current["embedding_model"])
            base_url = None
            if provider == "custom":
                base_url = st.text_input(
                    "API 地址（base_url，如 https://xxx/v1）", placeholder="https://..."
                )
            api_key = st.text_input(
                "API Key",
                type="password",
                placeholder=(
                    f"已配置：{current['key_masked']}（留空保持不变）"
                    if current["key_masked"]
                    else "输入 Key（BYOK 必须提供自己的 Key）"
                ),
            )
            st.caption("Key 仅保存在本机数据库（加密存储），不会写入日志或明文回显。")

        col_save, col_validate = st.columns(2)
        save_clicked = col_save.form_submit_button("保存并校验连通", type="primary")
        validate_clicked = col_validate.form_submit_button("仅校验连通")

    # S2-2：preset 且已存自己的 Key → 一键清除改用共享额度（显式空串清空，
    # 服务端归一化为 NULL；表单外按钮，payload 用当前已保存配置）。
    if mode == "preset" and current["key_source"] == "own":
        if st.button("改用平台共享额度（清除我的 Key）", use_container_width=True):
            _call_api(
                api_url,
                "PUT",
                "/api/provider",
                {
                    "mode": "preset",
                    "provider": current["provider"],
                    "model": current["model"],
                    "embedding_model": current["embedding_model"],
                    "api_key": "",
                    "base_url": None,
                },
                on_success="已清除我的 Key，改用平台共享额度",
            )
            st.rerun()

    # preset 不发 Key（保留已存或回落共享，清空走上面的显式空串路径）
    payload = {
        "mode": mode,
        "provider": provider,
        "model": model,
        "embedding_model": embedding_model,
        "api_key": None if mode == "preset" else (api_key or None),
        "base_url": base_url,
    }
    if save_clicked:
        _call_api(api_url, "PUT", "/api/provider", payload, on_success="配置已保存并生效")
    elif validate_clicked:
        _call_api(api_url, "POST", "/api/provider/validate", payload)


def _call_api(
    api_url: str, method: str, path: str, payload: dict, on_success: str = ""
) -> None:
    try:
        resp = get_client(api_url).request(method, path, json=payload, timeout=30.0)
        body = resp.json()
    except httpx.HTTPError as exc:
        st.error(f"请求失败：{exc}")
        return
    if resp.status_code == 401:
        handle_unauthorized(api_url)
        return
    if resp.status_code in (200, 201):
        if path.endswith("/validate"):
            if body.get("ok"):
                st.success("连接成功，配置有效")
            else:
                st.error(body.get("message", "连接失败"))
        else:
            st.success(on_success or "操作成功")
    else:
        st.error(body.get("error", {}).get("message", "操作失败，请稍后重试"))

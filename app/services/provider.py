"""ProviderService：提供商清单与预设、Key 生命周期与掩码、embedding 一致性、
超时/429 退避重试（owner 合同见 architecture.md 模块表；不拥有文档业务规则）。

外部 API 全部走 OpenAI 兼容协议（/chat/completions、/embeddings）；真实调用只
在验收阶段执行，自动化测试注入 httpx transport（行为保持 fake，不消耗额度）。
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx

from app.data.provider_store import (
    get_provider_settings,
    upsert_provider_settings,
)

logger = logging.getLogger(__name__)

# 免费预设清单：2026-08-08 真实调用验证（chat/completions 与 embeddings 均可用，
# bge-m3 维度 1024；v1/models 无免费标志字段，免费判定靠本清单 + 试调用）。
# 免费政策多变（RPM/TPM 限流、模型可能下架）→ 运行时 429 退避 + 失败提示切换。
DEFAULT_PRESET: dict[str, str] = {
    "id": "siliconflow-free",
    "name": "SiliconFlow 免费预设",
    "provider": "siliconflow",
    "base_url": "https://api.siliconflow.cn/v1",
    "model": "Qwen/Qwen2.5-7B-Instruct",
    "embedding_model": "BAAI/bge-m3",
}

PRESETS: list[dict[str, str]] = [
    DEFAULT_PRESET,
    {
        "id": "siliconflow-glm",
        "name": "SiliconFlow GLM-4.5-Air（免费候选）",
        "provider": "siliconflow",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "zai-org/GLM-4.5-Air",
        "embedding_model": "BAAI/bge-m3",
    },
]

# BYOK 内置提供商（任意 OpenAI 兼容服务可用 provider=custom + base_url）
BYOK_PROVIDERS: dict[str, str] = {
    "siliconflow": "https://api.siliconflow.cn/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "openai": "https://api.openai.com/v1",
}

# 429/5xx 重试上限（指数退避 base*2^attempt）
MAX_RETRIES = 3


class ProviderError(Exception):
    """提供商层错误基类（api 层映射为统一错误体）。"""


class InvalidConfigError(ProviderError):
    """配置非法（api 映射 400 invalid_config）。"""


class InvalidKeyError(ProviderError):
    """Key 无效/未授权（api 映射 422 invalid_key）。"""


class EmbeddingMismatchError(ProviderError):
    """索引已有 embedding_model 与当前配置不一致（防维度不匹配，架构最小可逆边界）。"""


def mask_api_key(key: str | None) -> str:
    """掩码规则：保留前 3 字符 + **** + 尾 4 字符；过短或空则全掩。"""
    if not key:
        return ""
    if len(key) <= 10:
        return "****"
    return f"{key[:3]}****{key[-4:]}"


# 参数未传哨兵（区分"未传回落存储"与"显式传 None"，如 byok/custom 校验 base_url 缺失）
_UNSET = object()


class ProviderService:
    def __init__(
        self,
        data_dir: Path,
        transport: httpx.BaseTransport | None = None,
        retry_base_delay: float = 1.0,
        request_timeout: float = 60.0,
    ) -> None:
        self.data_dir = data_dir
        self._transport = transport
        self.retry_base_delay = retry_base_delay
        self.request_timeout = request_timeout

    # ---------- 配置读写 ----------

    def get_config(self, user_id: str) -> dict[str, Any]:
        """当前生效配置（含掩码 Key）；无存储记录时返回默认预设。"""
        with sqlite3.connect(self.data_dir / "rag.db") as conn:
            row = get_provider_settings(conn, user_id)
        if row is None:
            return {
                "mode": "preset",
                "provider": DEFAULT_PRESET["provider"],
                "model": DEFAULT_PRESET["model"],
                "embedding_model": DEFAULT_PRESET["embedding_model"],
                "key_masked": "",
            }
        return {
            "mode": row["mode"],
            "provider": row["provider"],
            "model": row["model"],
            "embedding_model": row["embedding_model"],
            "key_masked": mask_api_key(row["api_key"]),
        }

    def save_config(
        self,
        user_id: str,
        mode: str,
        provider: str,
        model: str,
        embedding_model: str,
        api_key: str | None,
        base_url: str | None,
    ) -> None:
        """保存配置；base_url 按 mode/provider 解析为完整地址后落库。"""
        resolved = self._resolve_base_url(mode, provider, base_url)
        with sqlite3.connect(self.data_dir / "rag.db") as conn:
            upsert_provider_settings(
                conn,
                user_id,
                mode=mode,
                provider=provider,
                model=model,
                embedding_model=embedding_model,
                api_key=api_key,
                base_url=resolved,
            )

    def get_full_config(self, user_id: str) -> dict[str, Any]:
        """读取存储完整配置（含 api_key/base_url）；无记录返回默认预设。

        仅供服务端内部使用（保存/调用前取值），不得直接进响应或日志。
        """
        with sqlite3.connect(self.data_dir / "rag.db") as conn:
            row = get_provider_settings(conn, user_id)
        if row is None:
            return {
                "mode": "preset",
                "provider": DEFAULT_PRESET["provider"],
                "model": DEFAULT_PRESET["model"],
                "embedding_model": DEFAULT_PRESET["embedding_model"],
                "api_key": None,
                "base_url": DEFAULT_PRESET["base_url"],
            }
        return row

    # ---------- 配置校验 ----------

    def _resolve_base_url(self, mode: str, provider: str, base_url: str | None) -> str:
        """按 mode/provider 解析请求地址；非法配置抛 InvalidConfigError。"""
        if mode == "preset":
            preset = next((p for p in PRESETS if p["id"] == provider), None)
            if preset is None and provider != DEFAULT_PRESET["provider"]:
                raise InvalidConfigError("未知的免费预设，请选择预设清单中的提供商")
            return DEFAULT_PRESET["base_url"]
        if mode == "byok":
            if provider == "custom":
                if not base_url:
                    raise InvalidConfigError("自定义提供商（byok/custom）必须提供 base_url")
                return base_url.rstrip("/")
            preset_base = BYOK_PROVIDERS.get(provider)
            if preset_base is None:
                raise InvalidConfigError(
                    "未知提供商；支持 siliconflow/deepseek/openai，或 custom + base_url"
                )
            return preset_base
        raise InvalidConfigError("mode 仅支持 preset | byok")

    def validate_config(
        self,
        mode: str,
        provider: str,
        model: str | None,
        embedding_model: str | None,
        api_key: str | None,
        base_url: str | None,
    ) -> None:
        """格式校验（api 映射 400）：mode/provider/base_url/model/embedding_model。"""
        self._resolve_base_url(mode, provider, base_url)
        if not model or not model.strip():
            raise InvalidConfigError("model 不能为空")
        if not embedding_model or not embedding_model.strip():
            raise InvalidConfigError("embedding_model 不能为空")
        if not api_key or not api_key.strip():
            raise InvalidConfigError("api_key 不能为空（免费预设同样需要 Key 认证）")

    # ---------- 外部调用（OpenAI 兼容，429/5xx 指数退避） ----------

    def _client(self, api_key: str) -> httpx.Client:
        return httpx.Client(
            transport=self._transport,
            timeout=self.request_timeout,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def _post_with_retry(
        self, url: str, payload: dict[str, Any], api_key: str
    ) -> dict[str, Any]:
        """POST JSON；429/5xx 指数退避重试（MAX_RETRIES 次），读超时重试一次。"""
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = self._client(api_key).post(url, json=payload)
                if resp.status_code in (429,) or resp.status_code >= 500:
                    delay = self.retry_base_delay * (2**attempt)
                    logger.warning("provider %s 限流/服务错误 %s，%ss 后重试",
                                   url, resp.status_code, delay)
                    time.sleep(delay)
                    continue
                if resp.status_code in (401, 403):
                    raise InvalidKeyError("API Key 无效或已过期，请检查后重试")
                if resp.status_code >= 400:
                    raise ProviderError(f"提供商返回错误 {resp.status_code}")
                return resp.json()
            except (httpx.ReadTimeout, httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # 读超时/连接失败：重试一次（F0-3 约定超时重试），仍失败给友好错误
                last_exc = exc
                if attempt == 0:
                    logger.warning("provider 超时/连接失败，重试一次: %s", exc)
                    continue
                break
        if isinstance(last_exc, (httpx.ReadTimeout, httpx.ConnectError, httpx.ConnectTimeout)):
            raise ProviderError("提供商连接超时，请检查网络后重试")
        raise ProviderError("提供商持续限流或服务不可用，请稍后重试或切换提供商")

    def _chat_with_cfg(self, cfg: dict[str, Any], prompt: str, max_tokens: int) -> str:
        """用给定配置发一次 chat 请求（不写库；校验候选配置时复用）。"""
        url = f"{cfg['base_url']}/chat/completions"
        payload = {
            "model": cfg["model"],
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        }
        data = self._post_with_retry(url, payload, cfg.get("api_key") or "")
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("提供商返回格式异常") from exc

    def chat(self, user_id: str, prompt: str, max_tokens: int = 1024) -> str:
        """LLM 生成（OpenAI 兼容 chat/completions，按存储配置）。"""
        return self._chat_with_cfg(self.get_full_config(user_id), prompt, max_tokens)

    def embed(self, user_id: str, texts: list[str]) -> list[list[float]]:
        """文本向量化（OpenAI 兼容 embeddings）。"""
        cfg = self.get_full_config(user_id)
        url = f"{cfg['base_url']}/embeddings"
        payload = {"model": cfg["embedding_model"], "input": texts}
        data = self._post_with_retry(url, payload, cfg.get("api_key") or "")
        try:
            return [item["embedding"] for item in data["data"]]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("提供商返回格式异常") from exc

    # ---------- 连通校验 ----------

    def validate_connectivity(
        self,
        user_id: str,
        mode: object = _UNSET,
        provider: object = _UNSET,
        model: object = _UNSET,
        embedding_model: object = _UNSET,
        api_key: object = _UNSET,
        base_url: object = _UNSET,
    ) -> tuple[bool, str]:
        """发一次最小 chat 请求校验连通；返回 (ok, message)，不写库。

        参数可覆盖当前配置（PUT/validate 场景传完整候选配置）；未传的参数回落存储。
        显式传 None 会覆盖为 None（如 custom 校验必须带 base_url 才能通过格式校验）。
        """
        cfg = self.get_full_config(user_id)
        mode = cfg["mode"] if mode is _UNSET else mode
        provider = cfg["provider"] if provider is _UNSET else provider
        model = cfg["model"] if model is _UNSET else model
        embedding_model = (
            cfg["embedding_model"] if embedding_model is _UNSET else embedding_model
        )
        api_key = cfg["api_key"] if api_key is _UNSET else api_key
        base_url = cfg["base_url"] if base_url is _UNSET else base_url

        # 候选配置临时生效（仅本次请求），校验失败不落库
        candidate = {
            "mode": mode,
            "provider": provider,
            "model": model,
            "embedding_model": embedding_model,
            "api_key": api_key,
            "base_url": self._resolve_base_url(mode, provider, base_url),
        }
        try:
            self.validate_config(mode, provider, model, embedding_model, api_key, base_url)
        except InvalidConfigError:
            raise
        try:
            self._chat_with_cfg(candidate, "连通性校验", max_tokens=5)
            return True, "连接成功"
        except InvalidKeyError as exc:
            return False, str(exc)
        except ProviderError as exc:
            return False, str(exc)

    # ---------- embedding 一致性检查（S1 提供检查；S2 入库时调用） ----------

    def check_embedding_consistency(
        self, collection: Any, embedding_model: str
    ) -> None:
        """集合已有记录的 embedding_model 与当前配置不一致时拒绝写入（架构最小可逆边界）。

        空集合返回（允许首次写入）；S2 入库管线在向量化前调用本检查。
        """
        try:
            if collection.count() == 0:
                return
            peek = collection.peek(limit=1)
            existing = (peek.get("metadatas") or [{}])[0].get("embedding_model")
        except Exception as exc:  # noqa: BLE001——chroma 存储异常统一按不可用处理
            raise ProviderError(f"向量库不可用：{exc}") from exc
        if existing and existing != embedding_model:
            raise EmbeddingMismatchError(
                f"索引已有 embedding 模型 {existing}，当前配置为 {embedding_model}；"
                "维度可能不匹配，需重建索引后重新入库"
            )

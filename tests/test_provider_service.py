"""T2：ProviderService 业务（掩码/预设/退避/一致性/连通校验）。

外部 API 边界用 httpx MockTransport（行为保持 fake），不消耗真实额度。
"""

import sqlite3

import httpx
import pytest

from app.data.migrations import apply_migrations
from app.data.provider_store import upsert_provider_settings
from app.services.provider import (
    DEFAULT_PRESET,
    EmbeddingMismatchError,
    InvalidConfigError,
    InvalidKeyError,
    ProviderService,
    mask_api_key,
)


def _service(data_dir, transport=None, retry_base_delay=0.001):
    conn = sqlite3.connect(data_dir / "rag.db")
    apply_migrations(conn)
    conn.close()
    return ProviderService(
        data_dir=data_dir,
        transport=transport,
        retry_base_delay=retry_base_delay,
        request_timeout=5.0,
    )


def test_mask_api_key_rule():
    assert mask_api_key("sk-1234567890abcd") == "sk-****abcd"
    assert mask_api_key("abc") == "****"
    assert mask_api_key(None) == ""


def test_default_config_without_stored(data_dir):
    svc = _service(data_dir)
    cfg = svc.get_config("default")
    assert cfg["mode"] == "preset"
    assert cfg["provider"] == "siliconflow"
    assert cfg["model"] == DEFAULT_PRESET["model"]
    assert cfg["embedding_model"] == DEFAULT_PRESET["embedding_model"]
    assert cfg["key_masked"] == ""


def test_save_then_masked_read(data_dir):
    svc = _service(data_dir)
    svc.save_config("default", mode="preset", provider="siliconflow",
                    model="Qwen/Qwen2.5-7B-Instruct",
                    embedding_model="BAAI/bge-m3",
                    api_key="sk-abcdef1234567890", base_url=None)
    cfg = svc.get_config("default")
    assert cfg["key_masked"] == "sk-****7890"


def test_api_key_encrypted_at_rest(data_dir):
    """S1-2：库中 api_key 字段无明文，密文带 enc$v1$ 前缀。"""
    svc = _service(data_dir)
    svc.save_config("default", mode="preset", provider="siliconflow",
                    model="m", embedding_model="e",
                    api_key="sk-secret-1234567890", base_url=None)
    conn = sqlite3.connect(data_dir / "rag.db")
    try:
        row = conn.execute(
            "SELECT api_key FROM provider_settings WHERE user_id = 'default'"
        ).fetchone()
    finally:
        conn.close()
    assert row[0].startswith("enc$v1$")
    assert "sk-secret-1234567890" not in row[0]


def test_saved_key_decrypts_in_full_config(data_dir):
    """S1-2：解密仅服务端内部读取路径（get_full_config），明文不落库。"""
    svc = _service(data_dir)
    svc.save_config("default", mode="preset", provider="siliconflow",
                    model="m", embedding_model="e",
                    api_key="sk-secret-1234567890", base_url=None)
    cfg = svc.get_full_config("default")
    assert cfg["api_key"] == "sk-secret-1234567890"


def test_resave_overwrites_and_still_decrypts(data_dir):
    """S1-2：覆盖保存（PUT 保留 Key 场景）幂等——旧密文被替换，新 Key 可解。"""
    svc = _service(data_dir)
    svc.save_config("default", mode="preset", provider="siliconflow",
                    model="m1", embedding_model="e1",
                    api_key="key-one-1234567890", base_url=None)
    svc.save_config("default", mode="preset", provider="siliconflow",
                    model="m2", embedding_model="e2",
                    api_key="key-two-1234567890", base_url=None)
    cfg = svc.get_full_config("default")
    assert cfg["api_key"] == "key-two-1234567890"
    assert cfg["model"] == "m2"


def test_none_key_stored_as_null(data_dir):
    """S1-2：无 Key（预设回落）不加密、不产生密文噪音。"""
    svc = _service(data_dir)
    svc.save_config("default", mode="preset", provider="siliconflow",
                    model="m", embedding_model="e",
                    api_key=None, base_url=None)
    cfg = svc.get_full_config("default")
    assert cfg["api_key"] is None


def test_chat_success_with_retry_on_429(data_dir):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "你好"}}], "usage": {}},
        )

    transport = httpx.MockTransport(handler)
    svc = _service(data_dir, transport=transport)
    answer = svc.chat("default", "你好", max_tokens=10)
    assert answer == "你好"
    assert len(calls) == 3  # 429 两次后成功


def test_chat_raises_after_persistent_429(data_dir):
    def handler(request):
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    svc = _service(data_dir, transport=httpx.MockTransport(handler))
    with pytest.raises(Exception, match="限流|超时|失败"):
        svc.chat("default", "你好")


def test_chat_invalid_key_raises_422_mapping(data_dir):
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "invalid key"}})

    svc = _service(data_dir, transport=httpx.MockTransport(handler))
    with pytest.raises(InvalidKeyError):
        svc.chat("default", "你好")


def test_embed_success(data_dir):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "data": [{"embedding": [0.1, 0.2, 0.3], "index": 0}],
                "usage": {},
            },
        )

    svc = _service(data_dir, transport=httpx.MockTransport(handler))
    vec = svc.embed("default", ["测试"])
    assert len(vec) == 1
    assert vec[0] == [0.1, 0.2, 0.3]


def test_validate_ok_and_bad_key(data_dir):
    ok_svc = _service(
        data_dir,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}}
            )
        ),
    )
    assert ok_svc.validate_connectivity(
        "default", mode="preset", provider="siliconflow",
        model="Qwen/Qwen2.5-7B-Instruct", embedding_model="BAAI/bge-m3",
        api_key="sk-ok", base_url=None,
    ) == (True, "连接成功")

    bad_svc = _service(
        data_dir,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(401, json={"error": {"message": "bad key"}})
        ),
    )
    ok, message = bad_svc.validate_connectivity(
        "default", mode="preset", provider="siliconflow",
        model="Qwen/Qwen2.5-7B-Instruct", embedding_model="BAAI/bge-m3",
        api_key="sk-bad", base_url=None,
    )
    assert ok is False
    assert "Key" in message


def test_validate_custom_requires_base_url(data_dir):
    svc = _service(data_dir)
    with pytest.raises(InvalidConfigError):
        svc.validate_connectivity("default", mode="byok", provider="custom",
                                  model="m", embedding_model="e", api_key="sk-x",
                                  base_url=None)


def test_embedding_consistency_empty_collection_ok(data_dir):
    import chromadb

    chroma_dir = data_dir / "chroma"
    client = chromadb.PersistentClient(path=str(chroma_dir))
    collection = client.get_or_create_collection("knowledge_blocks")
    svc = _service(data_dir)
    # 空集合：无既有记录，允许写入
    svc.check_embedding_consistency(collection, "BAAI/bge-m3")


def test_embedding_consistency_mismatch_rejected(data_dir):
    import chromadb

    chroma_dir = data_dir / "chroma"
    client = chromadb.PersistentClient(path=str(chroma_dir))
    collection = client.get_or_create_collection("knowledge_blocks")
    collection.add(
        ids=["c1"],
        documents=["已有片段"],
        embeddings=[[0.1] * 8],
        metadatas=[{"embedding_model": "other/embed-v1", "user_id": "default"}],
    )
    svc = _service(data_dir)
    with pytest.raises(EmbeddingMismatchError):
        svc.check_embedding_consistency(collection, "BAAI/bge-m3")


def test_embedding_consistency_same_model_ok(data_dir):
    import chromadb

    chroma_dir = data_dir / "chroma"
    client = chromadb.PersistentClient(path=str(chroma_dir))
    collection = client.get_or_create_collection("knowledge_blocks")
    collection.add(
        ids=["c1"],
        documents=["已有片段"],
        embeddings=[[0.1] * 8],
        metadatas=[{"embedding_model": "BAAI/bge-m3", "user_id": "default"}],
    )
    svc = _service(data_dir)
    svc.check_embedding_consistency(collection, "BAAI/bge-m3")


# ---------- S2-2 平台共享预设 Key（回落 RAG_SHARED_PRESET_KEY） ----------

def _inject_shared_key(monkeypatch, key="sk-sharedtestkey"):
    monkeypatch.setenv("RAG_SHARED_PRESET_KEY", key)
    from app.core.config import get_settings

    get_settings.cache_clear()


def test_preset_without_key_falls_back_to_shared(data_dir, monkeypatch):
    """preset 无已存 Key → 回落平台共享 Key（服务端调用取值 get_full_config）。"""
    _inject_shared_key(monkeypatch)
    svc = _service(data_dir)
    cfg = svc.get_full_config("default")
    assert cfg["api_key"] == "sk-sharedtestkey"
    assert cfg["key_source"] == "shared"
    shown = svc.get_config("default")
    assert shown["key_source"] == "shared"
    # S2-2：共享 Key 不入界面——掩码也不回显（区别于自有 Key 的掩码展示）
    assert shown["key_masked"] == ""


def test_validate_connectivity_preset_without_any_key_reports_shared_missing(
    data_dir,
):
    """preset 无已存 Key 且平台共享未配置 → 不发请求，直接提示共享 Key 缺失。

    S2-2：避免把「共享未配置」误报为「API Key 无效」（误导用户输自己的 Key）。
    """
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, json={})

    svc = _service(data_dir, transport=httpx.MockTransport(handler))
    ok, message = svc.validate_connectivity("default")
    assert ok is False
    assert "RAG_SHARED_PRESET_KEY" in message
    assert calls == []  # 未发任何请求（共享未配置时无 Key 可试）


def test_preset_with_own_key_no_fallback(data_dir, monkeypatch):
    """preset 已存自己的 Key → 用自己的（不回落共享）。"""
    _inject_shared_key(monkeypatch)
    svc = _service(data_dir)
    svc.save_config(
        "default",
        mode="preset",
        provider="siliconflow-free",
        model="Qwen/Qwen3-14B",
        embedding_model="BAAI/bge-m3",
        api_key="sk-ownkey-1234",
        base_url=None,
    )
    cfg = svc.get_full_config("default")
    assert cfg["api_key"] == "sk-ownkey-1234"
    assert cfg["key_source"] == "own"


def test_validate_config_preset_allows_empty_key(data_dir):
    """preset 无 Key 允许（回落共享）；格式校验通过。"""
    svc = _service(data_dir)
    svc.validate_config("preset", "siliconflow-free", "M", "E", None, None)


def test_validate_config_byok_still_requires_key(data_dir):
    """BYOK 仍必须自备 Key（格式校验拒绝空）。"""
    svc = _service(data_dir)
    with pytest.raises(InvalidConfigError):
        svc.validate_config("byok", "siliconflow", "M", "E", None, None)


def test_chat_uses_shared_key(data_dir, monkeypatch):
    """真实行为：preset 无 Key 用户 chat 请求携带共享 Key（Authorization 头断言）。"""
    _inject_shared_key(monkeypatch)
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    svc = _service(data_dir, transport=httpx.MockTransport(handler))
    assert svc.chat("default", "hi") == "ok"
    assert seen["auth"] == "Bearer sk-sharedtestkey"


def test_chat_no_key_no_shared_401(data_dir):
    """无已存 Key 且平台共享未配置 → 真实 401 → InvalidKeyError（提示配置）。"""

    def handler(request):
        return httpx.Response(401, json={})

    svc = _service(data_dir, transport=httpx.MockTransport(handler))
    with pytest.raises(InvalidKeyError):
        svc.chat("default", "hi")

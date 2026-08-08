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

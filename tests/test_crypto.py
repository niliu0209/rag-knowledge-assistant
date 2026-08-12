"""T2：主密钥管理与 Fernet 加解密（owner：core/ 基础设施，S1-2 新增）。

密钥规则：环境变量注入优先（不入 Git/日志/界面）；未注入时自动生成
持久化到 data_dir/secrets/rag_key.bin（0600，幂等读取）。
"""

import pytest
from cryptography.fernet import Fernet

from app.core.crypto import (
    decrypt_text,
    encrypt_text,
    generate_key,
    get_fernet,
)


def test_generate_key_is_fernet_key():
    key = generate_key()
    assert isinstance(key, str)
    # 能构造 Fernet 即合法（32 字节 url-safe base64 密钥）
    Fernet(key.encode())


def test_key_file_auto_generated_idempotent(data_dir):
    f1 = get_fernet(data_dir, None)
    f2 = get_fernet(data_dir, None)
    # 同一密钥：f1 加密的密文 f2 必须能解（幂等读取，非两次随机 IV 相等）
    assert decrypt_text(f2, encrypt_text(f1, "sk-same-key")) == "sk-same-key"
    key_file = data_dir / "secrets" / "rag_key.bin"
    assert key_file.exists()
    # 权限 0600：仅属主可读写（密钥文件保护）
    assert (key_file.stat().st_mode & 0o777) == 0o600


def test_env_key_preferred_over_file(data_dir):
    key = generate_key()
    f = get_fernet(data_dir, key)
    assert decrypt_text(f, encrypt_text(f, "sk-x")) == "sk-x"
    # 已注入 env 时不再生成密钥文件
    assert not (data_dir / "secrets").exists()


def test_rag_env_name_reaches_settings(data_dir, monkeypatch):
    """S2-2 遗留修复锁定：env 名 RAG_KEY_ENCRYPTION_KEY 必须映射进 Settings。

    S1-2 时字段名 key_encryption_key 与 env 名不匹配（pydantic-settings 按字段
    名匹配），env 注入路径实际未生效（靠参数路径兜底生成密钥文件）；字段改名
    rag_key_encryption_key 后此处锁定 env → Settings 链路。
    """
    key = generate_key()
    monkeypatch.setenv("RAG_KEY_ENCRYPTION_KEY", key)
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        assert get_settings().rag_key_encryption_key == key
    finally:
        get_settings.cache_clear()


def test_invalid_env_key_raises(data_dir):
    with pytest.raises(RuntimeError):
        get_fernet(data_dir, "not-a-valid-fernet-key")


def test_encrypt_decrypt_roundtrip(data_dir):
    f = get_fernet(data_dir, None)
    token = encrypt_text(f, "sk-abcdef1234567890")
    assert token.startswith("enc$v1$")
    assert "sk-abcdef1234567890" not in token
    assert decrypt_text(f, token) == "sk-abcdef1234567890"


def test_none_and_legacy_plaintext_passthrough(data_dir):
    f = get_fernet(data_dir, None)
    assert encrypt_text(f, None) is None
    assert encrypt_text(f, "") == ""
    assert decrypt_text(f, None) is None
    # 无前缀（历史明文/回滚场景）透传不崩溃，由迁移负责转换
    assert decrypt_text(f, "sk-legacy-plain") == "sk-legacy-plain"

"""主密钥管理与 Key 加解密（owner：core/ 基础设施，S1-2）。

- 主密钥经环境变量 `RAG_KEY_ENCRYPTION_KEY` 注入（优先，不入 Git/日志/界面）；
  未注入时自动生成 Fernet 密钥持久化到 `data_dir/secrets/rag_key.bin`
  （0600 权限，.gitignore 已覆盖数据目录），保证开箱即用且重启不换钥。
- 存储格式：密文带 `enc$v1$` 前缀，用于识别已加密记录与迁移幂等判断；
  无前缀值按历史明文/回滚场景透传，不崩溃。
- 加解密只在服务端内部路径使用，密文绝不进响应/日志/错误体。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

# 密文存储前缀（v1 格式标记；迁移幂等与识别均依赖此前缀）
ENC_PREFIX = "enc$v1$"

_KEY_FILE_REL = Path("secrets") / "rag_key.bin"


def generate_key() -> str:
    """生成一条可用作 RAG_KEY_ENCRYPTION_KEY 的 Fernet 密钥（供运维配置）。"""
    return Fernet.generate_key().decode()


def _load_or_create_key_file(data_dir: Path) -> bytes:
    """读取或生成持久化密钥文件（0600；并发安全由单进程应用保证）。"""
    key_file = data_dir / _KEY_FILE_REL
    if key_file.exists():
        return key_file.read_bytes().strip()
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    key_file.write_bytes(key + b"\n")
    os.chmod(key_file, 0o600)
    logger.info("生成主密钥文件 %s（0600，不入 Git）", key_file)
    return key


def get_fernet(data_dir: Path, key_encryption_key: str | None) -> Fernet:
    """解析主密钥并构造 Fernet；env 注入优先，否则回落持久化密钥文件。

    无效注入值立即抛 RuntimeError（应用启动即暴露，防数据解不开才被发现）。
    """
    raw = (key_encryption_key or "").strip()
    key = raw.encode() if raw else _load_or_create_key_file(data_dir)
    try:
        return Fernet(key)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            "RAG_KEY_ENCRYPTION_KEY 无效：Fernet 密钥必须为 32 字节 url-safe "
            "base64（可用 app.core.crypto.generate_key() 生成）"
        ) from exc


def encrypt_text(fernet: Fernet, plain: str | None) -> str | None:
    """加密为带前缀密文；None/空串透传（无 Key 配置不产生噪音）。"""
    if not plain:
        return plain
    return ENC_PREFIX + fernet.encrypt(plain.encode()).decode()


def decrypt_text(fernet: Fernet, token: str | None) -> str | None:
    """解密服务端读取路径；None/空串透传，无前缀值按历史明文透传。

    密文损坏或主密钥不匹配时抛 InvalidToken——由调用方（ProviderService）
    映射为友好错误，不在此层吞异常。
    """
    if not token:
        return token
    if not token.startswith(ENC_PREFIX):
        return token
    return fernet.decrypt(token[len(ENC_PREFIX) :].encode()).decode()

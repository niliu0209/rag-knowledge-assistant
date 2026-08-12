"""运行配置：环境变量 + .env（不入 Git），pydantic-settings 加载。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    data_dir: Path = Path("./data")
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    ui_port: int = 8501
    api_url: str = "http://localhost:8000"
    log_level: str = "INFO"
    # S1-2 Key 加密存储主密钥（Fernet key，env 注入优先；未注入自动生成持久化）
    key_encryption_key: str | None = None
    # S2-1 会话 cookie Secure 标志：本机 HTTP False；S2-3 公开部署 HTTPS 时置 True
    cookie_secure: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()

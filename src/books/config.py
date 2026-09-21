"""Single source of configuration truth.

Every module reads settings through :func:`get_settings`; nothing reads
``os.environ`` directly. That keeps the faces (API, CLI, MCP, workers)
configured identically from one ``.env``.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BOOKS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Core service ---
    env: Literal["local", "staging", "production"] = "local"
    log_level: str = "INFO"
    api_token: str = "change-me"
    api_base_url: str = "http://localhost:8000"
    api_timeout_seconds: float = 30.0

    # --- Storage ---
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/books"
    db_echo: bool = False
    db_pool_size: int = 5

    # --- Async work ---
    redis_url: str = "redis://localhost:6379/0"
    sync_all_items_minutes: int = 60

    # --- Secrets at rest ---
    encryption_key: str | None = None

    # --- Providers ---
    default_provider: str = "plaid"
    plaid_env: Literal["sandbox", "production"] = "sandbox"
    plaid_client_id: str | None = None
    plaid_secret: str | None = None
    plaid_webhook_url: str | None = None
    plaid_redirect_uri: str | None = None

    # --- Categorization agent ---
    anthropic_api_key: str | None = None
    categorizer_model: str = "claude-sonnet-5"
    categorizer_actor: str = "agent:categorizer-v1"
    confidence_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    similar_transaction_limit: int = Field(default=15, ge=0, le=100)

    @property
    def is_local(self) -> bool:
        return self.env == "local"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

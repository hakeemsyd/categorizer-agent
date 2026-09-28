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
    default_provider: str = "teller"

    # Teller (https://teller.io/docs). application_id is public — it's the
    # value Teller Connect embeds client-side, not a secret. The certificate
    # is what actually authenticates API calls (mutual TLS) and is required
    # outside sandbox; sandbox accepts requests with no certificate at all.
    teller_application_id: str | None = None
    teller_environment: Literal["sandbox", "development", "production"] = "sandbox"
    teller_base_url: str = "https://api.teller.io"
    teller_cert_path: str | None = None
    teller_key_path: str | None = None
    # HMAC secret from the Teller Dashboard, for verifying webhook signatures.
    teller_signing_secret: str | None = None

    # --- Categorization agent ---
    anthropic_api_key: str | None = None
    # Only needed if anthropic_api_key is an organization-level key rather
    # than one scoped to a workspace — Anthropic then requires the caller to
    # say which workspace to use on every request. A workspace-scoped key
    # needs none of this; find one under console.anthropic.com's workspace
    # settings, or the workspace's own "ID" field to fill this in instead.
    anthropic_workspace_id: str | None = None
    categorizer_model: str = "claude-sonnet-5"
    categorizer_actor: str = "agent:categorizer-v1"
    confidence_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    similar_transaction_limit: int = Field(default=15, ge=0, le=100)
    #: How many "a human overruled the agent here" examples to put in the
    #: prompt. Few but high-signal — they outrank everything else.
    correction_example_limit: int = Field(default=5, ge=0, le=50)
    #: Representative transactions shown per merchant when categorizing a
    #: whole vendor group at once (`books tx bootstrap`).
    vendor_sample_size: int = Field(default=3, ge=1, le=20)

    @property
    def is_local(self) -> bool:
        return self.env == "local"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

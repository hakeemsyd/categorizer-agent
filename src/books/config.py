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
    sync_all_connections_minutes: int = 60

    # --- Secrets at rest ---
    encryption_key: str | None = None

    # --- Provider (Fintable: https://fintable.io/docs) ---
    default_provider: str = "fintable"

    #: Public OAuth client id. Fintable is a public client using PKCE — there
    #: is no client secret, and the docs are explicit that none is ever
    #: accepted, so do not go looking for one.
    fintable_client_id: str | None = None
    fintable_base_url: str = "https://fintable.io"
    #: Must match a redirect URI registered on the OAuth app. For a loopback
    #: address Fintable accepts any runtime port (RFC 8252), so registering
    #: http://127.0.0.1/oauth/callback covers the port below — but the *path*
    #: is still matched, and "localhost" is rejected outright in favour of the
    #: literal IP. The CLI serves this address itself during `books link`.
    fintable_redirect_uri: str = "http://127.0.0.1:8420/oauth/callback"
    fintable_scopes: str = "read"
    #: Cursors are bound to the workspace that issued them, so every paged
    #: read must name the same one. Left unset, the token's default workspace
    #: is resolved once from /api/v2/me.
    fintable_workspace_id: str | None = None

    # --- Categorization agent ---
    #: Which model provider the agent calls. Qwen by default; Anthropic is
    #: kept switchable because the agent depends entirely on structured
    #: output, and being able to A/B the same prompt across two models is how
    #: you tell "the prompt is wrong" from "this model cannot hold the schema".
    llm_provider: Literal["qwen", "anthropic"] = "qwen"

    #: Qwen via Alibaba's DashScope, which speaks the OpenAI wire format.
    qwen_api_key: str | None = None
    #: Singapore by default; use https://dashscope.aliyuncs.com/compatible-mode/v1
    #: for Beijing. The key and the endpoint are region-scoped together — a
    #: Singapore key against the Beijing URL fails authentication, which reads
    #: like a bad key rather than the wrong region.
    qwen_base_url: str = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    #: Which wire format that URL speaks. Qwen is served behind both: Alibaba's
    #: DashScope exposes the OpenAI format, while some Qwen MaaS endpoints
    #: (".../apps/anthropic") expose Anthropic's Messages API instead. Getting
    #: this wrong is a bare 404 — the client posts to /chat/completions when
    #: the server only answers /v1/messages — so it is explicit rather than
    #: guessed from the URL. Change it whenever you change qwen_base_url.
    qwen_api_style: Literal["openai", "anthropic"] = "openai"

    anthropic_api_key: str | None = None
    # Only needed if anthropic_api_key is an organization-level key rather
    # than one scoped to a workspace — Anthropic then requires the caller to
    # say which workspace to use on every request. A workspace-scoped key
    # needs none of this; find one under console.anthropic.com's workspace
    # settings, or the workspace's own "ID" field to fill this in instead.
    anthropic_workspace_id: str | None = None

    #: The model to call, read against whichever provider is selected. Change
    #: both together: "claude-sonnet-5" means nothing to DashScope.
    categorizer_model: str = "qwen-plus"
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

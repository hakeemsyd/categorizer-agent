"""Which model gets called, and with what.

This is config-driven wiring, so the failure mode is silence: the wrong
provider, or the right one pointed at the wrong region, looks exactly like
working code until a request goes out. These assert the construction rather
than mocking it away.
"""

from __future__ import annotations

import pytest

from books.agent.categorizer import build_chat_model
from books.config import Settings, get_settings
from books.core.errors import ConfigurationError


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch, tmp_path):
    """Read configuration from this test only.

    Settings loads a relative ``.env``, so without chdir these assertions run
    against whatever the developer happens to have configured — they passed or
    failed depending on the machine, which is worse than not having them.
    """
    monkeypatch.chdir(tmp_path)
    for name in (
        "BOOKS_LLM_PROVIDER",
        "BOOKS_QWEN_API_KEY",
        "BOOKS_QWEN_BASE_URL",
        "BOOKS_ANTHROPIC_API_KEY",
        "BOOKS_ANTHROPIC_WORKSPACE_ID",
        "BOOKS_CATEGORIZER_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    build_chat_model.cache_clear()
    yield
    get_settings.cache_clear()
    build_chat_model.cache_clear()


def _configure(monkeypatch, **env: str) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_qwen_is_the_default_provider() -> None:
    assert Settings().llm_provider == "qwen"


def test_the_default_model_belongs_to_the_default_provider() -> None:
    """A mismatched pair is the easy mistake: 'claude-sonnet-5' on DashScope."""
    settings = Settings()
    assert settings.categorizer_model.startswith("qwen")


def test_qwen_is_built_against_the_configured_dashscope_endpoint(monkeypatch) -> None:
    _configure(
        monkeypatch,
        BOOKS_QWEN_API_KEY="sk-test",
        BOOKS_CATEGORIZER_MODEL="qwen-plus",
    )
    model = build_chat_model()

    assert model.model_name == "qwen-plus"
    assert "dashscope" in str(model.openai_api_base)
    # Every task picks one item from a fixed list; sampling variety is a
    # liability there, not a feature.
    assert model.temperature == 0


def test_a_beijing_base_url_is_honoured(monkeypatch) -> None:
    """The key and the URL are region-scoped together, so the URL must stick."""
    beijing = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    _configure(monkeypatch, BOOKS_QWEN_API_KEY="sk-test", BOOKS_QWEN_BASE_URL=beijing)
    assert str(build_chat_model().openai_api_base) == beijing


def test_anthropic_is_still_reachable_by_configuration(monkeypatch) -> None:
    """Kept switchable so a structured-output failure can be A/B'd."""
    _configure(
        monkeypatch,
        BOOKS_LLM_PROVIDER="anthropic",
        BOOKS_ANTHROPIC_API_KEY="sk-ant-test",
        BOOKS_CATEGORIZER_MODEL="claude-sonnet-5",
    )
    model = build_chat_model()
    assert model.model == "claude-sonnet-5"
    assert type(model).__name__ == "ChatAnthropic"


def test_a_missing_qwen_key_names_the_variable_and_the_region_trap(monkeypatch) -> None:
    monkeypatch.delenv("BOOKS_QWEN_API_KEY", raising=False)
    with pytest.raises(ConfigurationError) as exc:
        build_chat_model()
    assert "BOOKS_QWEN_API_KEY" in str(exc.value)
    assert "region" in str(exc.value)


def test_a_missing_anthropic_key_says_which_provider_asked_for_it(monkeypatch) -> None:
    _configure(monkeypatch, BOOKS_LLM_PROVIDER="anthropic")
    monkeypatch.delenv("BOOKS_ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ConfigurationError, match="BOOKS_LLM_PROVIDER=anthropic"):
        build_chat_model()

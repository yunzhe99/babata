import asyncio

import pytest
from pydantic import ValidationError

from babata.config import Settings
from babata.main import ChatRequest
from babata.providers import model_provider, model_settings
from babata.sessions import session_key


def settings(**overrides):
    return Settings(
        _env_file=None, llm_api_key="test-only", postgres_password="a@b:c/d$e", **overrides
    )


def test_password_is_encoded_and_secrets_are_hidden():
    config = settings()
    assert config.database_url.password == "a@b:c/d$e"
    assert "a@b:c/d$e" not in repr(config)
    assert "test-only" not in repr(config)
    assert "a@b:c/d$e" not in str(config.database_url)


def test_empty_secrets_are_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_api_key="", postgres_password="")


def test_compatible_provider_requires_endpoint():
    with pytest.raises(ValidationError, match="LLM_BASE_URL"):
        settings(llm_provider="openai_compatible")


def test_provider_options_from_environment(monkeypatch):
    monkeypatch.setenv("LLM_EXTRA_BODY", '{"thinking":{"type":"disabled"}}')
    config = settings(llm_provider="openai_compatible", llm_base_url="https://example.test")
    options = model_settings(config)
    assert options.extra_body == {"thinking": {"type": "disabled"}}
    assert options.max_tokens == 2048
    assert options.store is None
    assert model_settings(settings()).store is False


@pytest.mark.parametrize("style", ["responses", "chat_completions"])
def test_model_configuration(style):
    from agents import OpenAIChatCompletionsModel, OpenAIResponsesModel

    async def check():
        async with model_provider(settings(llm_api_style=style, llm_model="custom-model")) as model:
            expected = OpenAIResponsesModel if style == "responses" else OpenAIChatCompletionsModel
            assert isinstance(model, expected)
            assert model.model == "custom-model"

    asyncio.run(check())


def test_session_namespaces_do_not_collide():
    assert session_key("alice", "daily") != session_key("bob", "daily")
    assert session_key("a:b", "c") != session_key("a", "b:c")
    assert session_key("用户", "日常") == session_key("用户", "日常")


@pytest.mark.parametrize("message", ["", "   ", "x" * 32001, 123])
def test_invalid_message(message):
    with pytest.raises(ValidationError):
        ChatRequest(user_id="alice", session_id="daily", message=message)


def test_deepseek_switch_includes_native_memory_and_real_context_capacity():
    from babata.providers import codex_provider_config

    config = settings(agent_runtime="codex", llm_provider="deepseek")
    assert config.llm_model == "deepseek-flash"
    assert config.tokyo_memory_extract_model == "deepseek-flash"
    assert config.tokyo_memory_consolidation_model == "deepseek-flash"
    opts = codex_provider_config(config)
    assert opts["model_context_window"] == 1048576
    assert opts["model_auto_compact_token_limit"] == 900000
    assert opts["model_providers.deepseek.env_key"] == "BABATA_MODEL_API_KEY"
    assert "test-only" not in str(opts)


@pytest.mark.parametrize(
    "overrides",
    [
        {"tokyo_memory_extract_model": "gpt-5.6-luna"},
        {"tokyo_memory_consolidation_model": "gpt-5.6-terra"},
        {"llm_base_url": "https://unrelated.invalid"},
        {"llm_api_style": "chat_completions"},
        {"tokyo_model_context_window": 10000, "tokyo_auto_compact_token_limit": 10000},
    ],
)
def test_deepseek_rejects_hidden_gpt_calls_and_invalid_context(overrides):
    with pytest.raises(ValidationError):
        settings(agent_runtime="codex", llm_provider="deepseek", **overrides)

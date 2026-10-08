"""Provider-specific setup stays here; the agent only receives an SDK Model."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from agents import Model, ModelSettings, OpenAIChatCompletionsModel, OpenAIResponsesModel
from openai import AsyncOpenAI

from babata.config import Settings


def codex_provider_config(settings: Settings) -> dict:
    """Public app-server overrides; credentials travel only through its environment."""
    result = {}
    if settings.llm_provider == "deepseek":
        result.update(
            {
                "model_provider": "deepseek",
                "model": settings.llm_model,
                "model_catalog_json": str(Path(__file__).with_name("deepseek_models.json")),
                "model_providers.deepseek.name": "DeepSeek",
                "model_providers.deepseek.base_url": settings.llm_base_url,
                "model_providers.deepseek.wire_api": "responses",
                "model_providers.deepseek.env_key": "BABATA_MODEL_API_KEY",
                "model_providers.deepseek.requires_openai_auth": False,
            }
        )
    if settings.tokyo_model_context_window:
        result["model_context_window"] = settings.tokyo_model_context_window
    if settings.tokyo_auto_compact_token_limit:
        result["model_auto_compact_token_limit"] = settings.tokyo_auto_compact_token_limit
    return result


def model_settings(settings: Settings) -> ModelSettings:
    return ModelSettings(
        store=False if settings.llm_provider == "openai" else None,
        max_tokens=settings.llm_max_tokens,
        extra_body=settings.llm_extra_body or None,
    )


@asynccontextmanager
async def model_provider(settings: Settings) -> AsyncIterator[Model]:
    # Both supported providers use the OpenAI wire protocol. A future provider
    # with a different protocol can return another SDK Model from this module.
    async with AsyncOpenAI(
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_base_url or "https://api.openai.com/v1",
        timeout=settings.llm_timeout_seconds,
        max_retries=0,
    ) as client:
        model_type = (
            OpenAIResponsesModel
            if settings.llm_api_style == "responses"
            else OpenAIChatCompletionsModel
        )
        yield model_type(model=settings.llm_model, openai_client=client)

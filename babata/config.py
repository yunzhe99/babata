from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    llm_provider: Literal["openai", "openai_compatible", "deepseek"] = "openai"
    llm_api_style: Literal["responses", "chat_completions"] = "responses"
    llm_model: str = Field(default="gpt-5.6-luna", min_length=1)
    llm_api_key: SecretStr
    llm_base_url: str | None = None
    llm_timeout_seconds: float = Field(default=60, gt=0, le=300)
    llm_max_tokens: int = Field(default=2048, ge=1, le=65536)
    llm_extra_body: dict[str, Any] = Field(default_factory=dict)
    auto_memory_enabled: bool = True
    memory_timeout_seconds: float = Field(default=20, ge=1, le=120)
    codex_bridge_enabled: bool = False
    codex_bridge_token: SecretStr = SecretStr("")
    codex_user_id: str = "user"
    agent_runtime: Literal["agents_sdk", "codex"] = "agents_sdk"
    tokyo_codex_binary: str = "/usr/local/bin/codex"
    tokyo_state_dir: str = "/var/lib/babata-tokyo"
    tokyo_native_memories: bool = True
    tokyo_memory_extract_model: str = Field(default="gpt-5.6-luna", min_length=1)
    tokyo_memory_consolidation_model: str = Field(default="gpt-5.6-terra", min_length=1)
    # Zero keeps the selected model's default. DeepSeek defaults below are explicit
    # so an unknown-model fallback cannot silently reduce its 1M context window.
    tokyo_model_context_window: int = Field(default=0, ge=0, le=1048576)
    tokyo_auto_compact_token_limit: int = Field(default=0, ge=0, le=1048576)

    postgres_host: str = "db"
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_db: str = "babata"
    postgres_user: str = "babata"
    postgres_password: SecretStr

    @field_validator("llm_api_key", "postgres_password")
    @classmethod
    def require_secret(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("must be set in .env")
        return value

    @field_validator("llm_base_url", mode="before")
    @classmethod
    def empty_url_is_none(cls, value: str | None) -> str | None:
        return value or None

    @model_validator(mode="after")
    def validate_provider(self) -> "Settings":
        if self.agent_runtime == "codex" and self.llm_provider == "openai_compatible":
            raise ValueError("Tokyo Codex supports the OpenAI or DeepSeek provider")
        if self.llm_provider == "deepseek":
            if self.llm_api_style != "responses":
                raise ValueError("DeepSeek integration requires the Responses API")
            if not self.llm_base_url:
                self.llm_base_url = "https://api.deepseek.com"
            if self.llm_base_url.rstrip("/") not in (
                "https://api.deepseek.com",
                "https://api.deepseek.com/v1",
            ):
                raise ValueError("DeepSeek requires its official HTTPS endpoint")
            for name in (
                "llm_model",
                "tokyo_memory_extract_model",
                "tokyo_memory_consolidation_model",
            ):
                if name not in self.model_fields_set:
                    setattr(self, name, "deepseek-flash")
                if getattr(self, name) != "deepseek-flash":
                    raise ValueError("DeepSeek chat and native memory must use deepseek-flash")
            if not self.tokyo_model_context_window:
                self.tokyo_model_context_window = 1048576
            if not self.tokyo_auto_compact_token_limit:
                self.tokyo_auto_compact_token_limit = 900000
        if self.tokyo_auto_compact_token_limit and (
            not self.tokyo_model_context_window
            or self.tokyo_auto_compact_token_limit >= self.tokyo_model_context_window
        ):
            raise ValueError(
                "Tokyo auto compaction must leave room within the model context window"
            )
        if self.llm_provider == "openai_compatible" and not self.llm_base_url:
            raise ValueError("LLM_BASE_URL is required for openai_compatible")
        if self.codex_bridge_enabled and len(self.codex_bridge_token.get_secret_value()) < 32:
            raise ValueError("CODEX_BRIDGE_TOKEN must contain at least 32 characters")
        return self

    @property
    def database_url(self) -> URL:
        # URL.create safely handles passwords containing @, :, /, etc.
        return URL.create(
            "postgresql+asyncpg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    openai_api_key: SecretStr | None = None
    # Unset means api.openai.com; set it to route through an OpenAI-compatible gateway.
    openai_base_url: str | None = None
    model: str = "gpt-5-mini"
    # Only sent to reasoning models (gpt-5.x, o-series). Leave unset for gpt-4.x.
    reasoning_effort: ReasoningEffort | None = "minimal"
    max_tokens: int | None = None
    request_timeout: float = 120.0

    database_url: str = "postgresql://chatbot:chatbot@localhost:5433/chatbot"

    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_host: str = "http://localhost:3000"

    log_level: str = "INFO"

    @property
    def tracing_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()

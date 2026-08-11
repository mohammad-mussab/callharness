from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="CALLHARNESS_", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./callharness.db"
    data_dir: str = "./data"

    # Optional static API key. When set, ingestion/mutation endpoints require
    # "Authorization: Bearer <key>" or "x-api-key: <key>".
    api_key: str | None = None

    # LLM provider for post-call analysis: "openai", "anthropic", or "none".
    # "openai" also covers any OpenAI-compatible endpoint (Ollama, vLLM, ...)
    # via llm_base_url.
    llm_provider: str = "auto"
    llm_model: str | None = None
    llm_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("CALLHARNESS_OPENAI_API_KEY", "OPENAI_API_KEY"),
    )
    anthropic_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("CALLHARNESS_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
    )

    analysis_enabled: bool = True
    analysis_poll_seconds: float = 3.0
    analysis_concurrency: int = 2

    # SMTP settings for the email alert channel (CALLHARNESS_SMTP_*).
    # Leave smtp_host unset to disable email delivery.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_starttls: bool = True

    @property
    def resolved_provider(self) -> str:
        if self.llm_provider != "auto":
            return self.llm_provider
        if self.anthropic_api_key:
            return "anthropic"
        if self.openai_api_key:
            return "openai"
        return "none"

    @property
    def resolved_model(self) -> str:
        if self.llm_model:
            return self.llm_model
        if self.resolved_provider == "anthropic":
            return "claude-haiku-4-5-20251001"
        return "gpt-4o-mini"

    @property
    def recordings_dir(self) -> Path:
        p = Path(self.data_dir) / "recordings"
        p.mkdir(parents=True, exist_ok=True)
        return p


settings = Settings()

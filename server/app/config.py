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

    # Days to keep call recordings before deleting them from disk. Audio is by far the
    # largest thing this stores — a 50s stereo 16kHz WAV is ~3MB, so 1,000 calls/day is
    # ~1TB/year without a limit, roughly 300x everything else combined. Deleting the
    # file clears Call.recording_path; the call, transcript and analysis are kept
    # forever. Set to 0 to keep recordings indefinitely (watch the disk).
    recording_retention_days: int = 30
    # How often the cleanup pass runs. Hourly is plenty for a daily-granularity policy.
    recording_cleanup_interval_seconds: float = 3600.0

    # Azure Blob Storage holding the agents' raw per-call logs (CALLHARNESS_AZURE_*).
    # Leave the connection string unset to disable log linking entirely — every entry
    # point in azure_logs.py degrades to a no-op, so this is also the switch for any
    # install that isn't the Italian healthcare deployment.
    # The unprefixed name is accepted too, because that is what the agent VMs already
    # export for their own uploader.
    azure_storage_connection_string: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "CALLHARNESS_AZURE_STORAGE_CONNECTION_STRING", "AZURE_STORAGE_CONNECTION_STRING"
        ),
    )
    azure_log_container: str = "call-data"
    # agent_id -> blob prefix inside the container. Lombardia was the first deployment
    # and writes to the container root, so its prefix is deliberately empty; an agent
    # that isn't listed falls back to "<agent_id lowercased>/".
    azure_log_prefixes: dict[str, str] = {
        "Lazio": "lazio/",
        "Piemonte": "piemonte/",
        "Trentino": "trentino/",
        "Lombardia": "",
    }
    # How often the worker looks for calls whose log blob hasn't been located yet.
    azure_log_sync_interval_seconds: float = 300.0
    # How far back that pass looks. Past this window the blob is never going to appear
    # — the agent uploads once with no retry and deletes un-uploaded leftovers after
    # 7 days — so retrying forever would just burn list calls for nothing.
    # scripts/sync_azure_logs.py --recheck ignores this for a manual sweep.
    azure_log_lookback_days: int = 2

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

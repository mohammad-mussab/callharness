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
    # Sampling temperature for analysis. Classification wants determinism: measured over
    # 3 runs of 6 real calls, gpt-4.1 returned identical buckets at both 0.1 and 0.5, but
    # only 3/6 at the provider default of 1.0 — and an unstable bucket moves the charts
    # while nothing has changed. Ignored by models that reject it (the gpt-5 family
    # accepts only the default); see llm.py, which strips it on a 400.
    llm_temperature: float = 0.5
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
        # Bucket classification (buckets.py) needs two things a cheap model cannot give:
        # multi-hop reasoning over the tool-call sequence, and a stable answer. Measured
        # over 3 runs of 6 real Lazio calls:
        #
        #   gpt-4o-mini @0.1   stable, but returned `record_missing` for everything —
        #                      consistently wrong is not useful
        #   gpt-5-mini         50% stable. It rejects any temperature but the default,
        #                      so it necessarily runs at 1.0 and the same call lands in
        #                      a different bucket on re-analysis
        #   gpt-4.1 @1.0       50% stable
        #   gpt-4.1 @0.1/@0.5  100% stable, identical answers at both temperatures, and
        #                      the answers match what the raw Azure logs show happened
        #
        # Cost on the full production prompt: 2,519 in / 240 out = $0.0070 per call, so
        # ~$209/month at 1,000 calls/day and $4.63 to re-analyse the current 665. That is
        # cheaper than majority-voting a weaker model, which is the usual fix for an
        # unstable classifier and would still not reach 100%.
        if self.llm_model:
            return self.llm_model
        if self.resolved_provider == "anthropic":
            return "claude-haiku-4-5-20251001"
        return "gpt-4.1"

    @property
    def recordings_dir(self) -> Path:
        p = Path(self.data_dir) / "recordings"
        p.mkdir(parents=True, exist_ok=True)
        return p


settings = Settings()

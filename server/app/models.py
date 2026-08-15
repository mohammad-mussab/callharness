import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Call(Base):
    __tablename__ = "calls"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    external_id: Mapped[str | None] = mapped_column(String(255), index=True)
    agent_id: Mapped[str] = mapped_column(String(255), default="default", index=True)
    direction: Mapped[str] = mapped_column(String(16), default="inbound")
    from_number: Mapped[str | None] = mapped_column(String(64))
    to_number: Mapped[str | None] = mapped_column(String(64))

    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    end_reason: Mapped[str | None] = mapped_column(String(64), index=True)
    transferred: Mapped[bool] = mapped_column(Boolean, default=False)

    recording_path: Mapped[str | None] = mapped_column(String(512))
    recording_url: Mapped[str | None] = mapped_column(String(1024))
    meta: Mapped[dict | None] = mapped_column(JSON, default=None)

    # Where this call's raw agent log lives in Azure Blob Storage, e.g.
    # "lazio/call-logs/2026-08-14/20260814_054036_f8c9881e_393473397746.log".
    # Only the pointer is stored — the bytes stay in Azure and are streamed on demand,
    # because unlike recordings (capped by recording_retention_days) logs are worth
    # keeping forever, and 12.5GB of them already exist there. See azure_logs.py.
    log_blob: Mapped[str | None] = mapped_column(String(512))
    # When the log was last looked for. Stamped on a miss too, so calls whose agent
    # never managed to upload a log stop being re-scanned on every pass.
    log_checked_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Conversation-quality metrics computed at ingest (no LLM needed)
    quality: Mapped[dict | None] = mapped_column(JSON, default=None)
    interruption_count: Mapped[int] = mapped_column(Integer, default=0)

    # Primary language spoken on the call, detected during analysis ("italian", ...)
    language: Mapped[str | None] = mapped_column(String(32))

    # pending | processing | completed | failed | skipped
    analysis_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    analysis_error: Mapped[str | None] = mapped_column(Text)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime)
    llm_model: Mapped[str | None] = mapped_column(String(128))

    summary: Mapped[str | None] = mapped_column(Text)
    sentiment_label: Mapped[str | None] = mapped_column(String(16), index=True)
    sentiment_score: Mapped[float | None] = mapped_column(Float)
    success: Mapped[bool | None] = mapped_column(Boolean, index=True)
    success_score: Mapped[float | None] = mapped_column(Float)
    success_rationale: Mapped[str | None] = mapped_column(Text)
    structured_data: Mapped[dict | None] = mapped_column(JSON, default=None)

    # Classification of *why* this call transferred / didn't complete, against the
    # taxonomies on AnalysisConfig (seeded from taxonomy.py) — null unless applicable.
    transfer_reason: Mapped[str | None] = mapped_column(String(32), index=True)
    non_completion_reason: Mapped[str | None] = mapped_column(String(32), index=True)
    # Where those two labels came from: "agent" (supplied at ingest by an agent that
    # classifies itself) or "llm" (CallHarness's own analysis pass). Agent-supplied wins
    # and is never overwritten by analysis — see analysis/engine.py apply_result().
    reason_source: Mapped[str | None] = mapped_column(String(16))

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    turns: Mapped[list["Turn"]] = relationship(
        back_populates="call", cascade="all, delete-orphan", order_by="Turn.idx"
    )
    evaluation_results: Mapped[list["EvaluationResult"]] = relationship(
        back_populates="call", cascade="all, delete-orphan"
    )


class Turn(Base):
    __tablename__ = "turns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"), index=True)
    idx: Mapped[int] = mapped_column(Integer, default=0)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    text: Mapped[str] = mapped_column(Text)
    translated_text: Mapped[str | None] = mapped_column(Text)  # cached on-demand translation
    start_time: Mapped[float | None] = mapped_column(Float)  # seconds from call start
    end_time: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[float | None] = mapped_column(Float)  # response latency (assistant turns)
    stt_ms: Mapped[float | None] = mapped_column(Float)  # STT time-to-final-transcript
    llm_ttft_ms: Mapped[float | None] = mapped_column(Float)  # LLM time-to-first-token
    tts_ttfb_ms: Mapped[float | None] = mapped_column(Float)  # TTS time-to-first-byte
    interrupted: Mapped[bool] = mapped_column(Boolean, default=False)
    # [{"name": str, "arguments": Any, "result": Any, "success": bool}, ...] — function/tool
    # calls the agent made while producing this turn (populated by CallHarnessFrameObserver)
    tool_calls: Mapped[list | None] = mapped_column(JSON, default=None)

    call: Mapped[Call] = relationship(back_populates="turns")


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Per-call triggers: negative_sentiment_call | failed_call | keyword_match | high_latency_call
    # Windowed triggers: success_rate_window | sentiment_window
    trigger: Mapped[str] = mapped_column(String(64))
    threshold: Mapped[float | None] = mapped_column(Float)
    keyword: Mapped[str | None] = mapped_column(String(255))
    window_minutes: Mapped[int] = mapped_column(Integer, default=60)
    min_calls: Mapped[int] = mapped_column(Integer, default=5)
    channel: Mapped[str] = mapped_column(String(16), default="webhook")  # webhook | slack
    target_url: Mapped[str] = mapped_column(String(1024))
    cooldown_minutes: Mapped[int] = mapped_column(Integer, default=15)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AlertEvent(Base):
    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[int | None] = mapped_column(Integer, index=True)
    rule_name: Mapped[str] = mapped_column(String(255))
    call_id: Mapped[str | None] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    fired_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    delivery_error: Mapped[str | None] = mapped_column(Text)


class Evaluator(Base):
    __tablename__ = "evaluators"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255))
    prompt: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class EvaluationResult(Base):
    __tablename__ = "evaluation_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    call_id: Mapped[str] = mapped_column(
        ForeignKey("calls.id", ondelete="CASCADE"), index=True
    )
    evaluator_id: Mapped[int] = mapped_column(Integer, index=True)
    evaluator_name: Mapped[str] = mapped_column(String(255))
    passed: Mapped[bool | None] = mapped_column(Boolean)  # None = evaluator errored
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    call: Mapped[Call] = relationship(back_populates="evaluation_results")


class AnalysisConfig(Base):
    __tablename__ = "analysis_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    summary_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    summary_prompt: Mapped[str | None] = mapped_column(Text)
    sentiment_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    success_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    success_prompt: Mapped[str | None] = mapped_column(Text)
    success_rubric: Mapped[str] = mapped_column(String(32), default="pass_fail")  # pass_fail | numeric_scale
    # Language the LLM writes summaries/rationales/extracted text in, regardless of
    # the language spoken on the call ("english", "italian", ... or "auto" = same as call)
    output_language: Mapped[str] = mapped_column(String(32), default="english")
    extraction_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # list of {"name": str, "type": "text|boolean|number|enum", "description": str, "choices": [str]}
    extraction_fields: Mapped[list | None] = mapped_column(JSON, default=list)
    # Classify why calls transfer / don't complete. Both taxonomies are lists of
    # {"key": str, "description": str}, editable in Settings and seeded from
    # taxonomy.py on first run; an empty list falls back to those defaults.
    classification_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    transfer_reasons: Mapped[list | None] = mapped_column(JSON, default=list)
    non_completion_reasons: Mapped[list | None] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

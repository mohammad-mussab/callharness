from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class TurnIn(BaseModel):
    role: Literal["user", "assistant"]
    text: str
    start_time: float | None = None
    end_time: float | None = None
    latency_ms: float | None = None
    stt_ms: float | None = None
    llm_ttft_ms: float | None = None
    tts_ttfb_ms: float | None = None
    interrupted: bool = False


class CallCreate(BaseModel):
    external_id: str | None = None
    agent_id: str = "default"
    direction: Literal["inbound", "outbound", "web"] = "inbound"
    from_number: str | None = None
    to_number: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_seconds: float | None = None
    end_reason: str | None = None
    transferred: bool = False
    recording_url: str | None = None
    metadata: dict[str, Any] | None = None
    turns: list[TurnIn] = Field(default_factory=list)


class TurnOut(BaseModel):
    idx: int
    role: str
    text: str
    translated_text: str | None = None
    start_time: float | None
    end_time: float | None
    latency_ms: float | None
    stt_ms: float | None
    llm_ttft_ms: float | None
    tts_ttfb_ms: float | None
    interrupted: bool

    model_config = {"from_attributes": True}


class CallOut(BaseModel):
    id: str
    external_id: str | None
    agent_id: str
    direction: str
    from_number: str | None
    to_number: str | None
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: float | None
    end_reason: str | None
    transferred: bool
    recording_url: str | None
    has_recording: bool = False
    metadata: dict[str, Any] | None = Field(default=None, alias="meta")
    analysis_status: str
    analysis_error: str | None
    summary: str | None
    sentiment_label: str | None
    sentiment_score: float | None
    success: bool | None
    success_score: float | None
    success_rationale: str | None
    structured_data: dict[str, Any] | None
    quality: dict[str, Any] | None
    interruption_count: int
    language: str | None = None
    llm_model: str | None
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class EvaluationResultOut(BaseModel):
    evaluator_id: int
    evaluator_name: str
    passed: bool | None
    reason: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class CallDetailOut(CallOut):
    turns: list[TurnOut] = Field(default_factory=list)
    evaluations: list[EvaluationResultOut] = Field(default_factory=list)


class CallListOut(BaseModel):
    items: list[CallOut]
    total: int
    limit: int
    offset: int


class ExtractionField(BaseModel):
    name: str
    type: Literal["text", "boolean", "number", "enum"] = "text"
    description: str = ""
    choices: list[str] | None = None


class AnalysisConfigIn(BaseModel):
    summary_enabled: bool = True
    summary_prompt: str | None = None
    sentiment_enabled: bool = True
    success_enabled: bool = True
    success_prompt: str | None = None
    success_rubric: Literal["pass_fail", "numeric_scale"] = "pass_fail"
    output_language: str = "english"
    extraction_enabled: bool = True
    extraction_fields: list[ExtractionField] = Field(default_factory=list)


class AnalysisConfigOut(AnalysisConfigIn):
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class OverviewOut(BaseModel):
    total_calls: int
    analyzed_calls: int
    success_rate: float | None
    transfer_rate: float | None
    avg_duration_seconds: float | None
    avg_sentiment_score: float | None
    sentiment_distribution: dict[str, int]
    end_reason_breakdown: list[dict[str, Any]]
    agents: list[str]
    # Per-agent (per-region) comparison: {agent_id, calls, success_rate, avg_sentiment}
    agent_stats: list[dict[str, Any]] = Field(default_factory=list)


class TimeseriesPoint(BaseModel):
    date: str
    calls: int
    success_rate: float | None
    avg_sentiment: float | None
    avg_duration_seconds: float | None


class LatencyOut(BaseModel):
    turn_count: int
    e2e: dict[str, float | None]
    components: dict[str, dict[str, float | None]]
    daily: list[dict[str, Any]]
    quality: dict[str, float | None]


AlertTrigger = Literal[
    "negative_sentiment_call",
    "failed_call",
    "keyword_match",
    "high_latency_call",
    "success_rate_window",
    "sentiment_window",
]


class AlertRuleIn(BaseModel):
    name: str
    enabled: bool = True
    trigger: AlertTrigger
    threshold: float | None = None
    keyword: str | None = None
    window_minutes: int = Field(default=60, ge=5, le=1440)
    min_calls: int = Field(default=5, ge=1)
    channel: Literal["webhook", "slack", "email"] = "webhook"
    # Webhook/Slack: the URL to POST to. Email: recipient address(es), comma-separated.
    target_url: str
    cooldown_minutes: int = Field(default=15, ge=0)


class AlertRuleOut(AlertRuleIn):
    id: int
    last_fired_at: datetime | None

    model_config = {"from_attributes": True}


class AlertEventOut(BaseModel):
    id: int
    rule_id: int | None
    rule_name: str
    call_id: str | None
    message: str
    fired_at: datetime
    delivered: bool
    delivery_error: str | None

    model_config = {"from_attributes": True}


class EvaluatorIn(BaseModel):
    name: str
    prompt: str
    enabled: bool = True


class EvaluatorOut(EvaluatorIn):
    id: int
    created_at: datetime

    model_config = {"from_attributes": True}


class EvaluatorStatsOut(BaseModel):
    id: int
    name: str
    enabled: bool
    total: int
    passed: int
    pass_rate: float | None


class HealthOut(BaseModel):
    status: str
    version: str
    llm_provider: str
    analysis_enabled: bool

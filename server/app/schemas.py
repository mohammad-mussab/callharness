from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class TurnIn(BaseModel):
    role: Literal["user", "assistant"]
    text: str
    start_time: float | None = None
    end_time: float | None = None
    latency_ms: float | None = None
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
    start_time: float | None
    end_time: float | None
    latency_ms: float | None
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
    llm_model: str | None
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class CallDetailOut(CallOut):
    turns: list[TurnOut] = Field(default_factory=list)


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


class TimeseriesPoint(BaseModel):
    date: str
    calls: int
    success_rate: float | None
    avg_sentiment: float | None
    avg_duration_seconds: float | None


class HealthOut(BaseModel):
    status: str
    version: str
    llm_provider: str
    analysis_enabled: bool

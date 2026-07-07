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

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    turns: Mapped[list["Turn"]] = relationship(
        back_populates="call", cascade="all, delete-orphan", order_by="Turn.idx"
    )


class Turn(Base):
    __tablename__ = "turns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"), index=True)
    idx: Mapped[int] = mapped_column(Integer, default=0)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    text: Mapped[str] = mapped_column(Text)
    start_time: Mapped[float | None] = mapped_column(Float)  # seconds from call start
    end_time: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[float | None] = mapped_column(Float)  # response latency (assistant turns)
    interrupted: Mapped[bool] = mapped_column(Boolean, default=False)

    call: Mapped[Call] = relationship(back_populates="turns")


class AnalysisConfig(Base):
    __tablename__ = "analysis_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    summary_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    summary_prompt: Mapped[str | None] = mapped_column(Text)
    sentiment_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    success_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    success_prompt: Mapped[str | None] = mapped_column(Text)
    success_rubric: Mapped[str] = mapped_column(String(32), default="pass_fail")  # pass_fail | numeric_scale
    extraction_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # list of {"name": str, "type": "text|boolean|number|enum", "description": str, "choices": [str]}
    extraction_fields: Mapped[list | None] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

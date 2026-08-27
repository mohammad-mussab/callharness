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

    # Auto-requeue bookkeeping (analysis/failure_kind.py). analysis_failure_kind is
    # retryable | blocked | terminal; analysis_attempts counts analyses that have
    # already failed, so the backoff can grow and the worker can stop after N;
    # analysis_next_retry_at is when this call becomes claimable again (NULL = now).
    analysis_failure_kind: Mapped[str | None] = mapped_column(String(16))
    analysis_attempts: Mapped[int] = mapped_column(Integer, default=0)
    analysis_next_retry_at: Mapped[datetime | None] = mapped_column(DateTime)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime)
    llm_model: Mapped[str | None] = mapped_column(String(128))

    summary: Mapped[str | None] = mapped_column(Text)
    sentiment_label: Mapped[str | None] = mapped_column(String(16), index=True)
    sentiment_score: Mapped[float | None] = mapped_column(Float)
    success: Mapped[bool | None] = mapped_column(Boolean, index=True)
    success_score: Mapped[float | None] = mapped_column(Float)
    success_rationale: Mapped[str | None] = mapped_column(Text)
    structured_data: Mapped[dict | None] = mapped_column(JSON, default=None)

    # What actually happened on this call, against the taxonomy on AnalysisConfig
    # (seeded from buckets.py). Exactly one per analysed call, whatever its outcome —
    # this is the axis that replaced transfer_reason/non_completion_reason, which only
    # ever applied to transferred / non-completed calls respectively.
    bucket: Mapped[str | None] = mapped_column(String(32), index=True)
    # One sentence describing what happened on *this specific* call. The bucket is a
    # fixed key so it can be charted and filtered; everything unique about the call
    # lives here instead of fragmenting the taxonomy.
    issue_note: Mapped[str | None] = mapped_column(Text)
    # The question that hit nothing, in the words the tool was actually queried with.
    # Set only on bucket == "record_missing". This is the line that goes in the Missing
    # Information report and the string the verification sweep re-runs against the API.
    unanswered_query: Mapped[str | None] = mapped_column(Text)

    # Which missing record this call's unanswered_query belongs to, decided by the
    # grouping pass in knowledge_gaps.py — NOT by the per-call analysis LLM. Several
    # calls asking the same thing in different words share one gap_group_id, and
    # gap_group_question holds the canonical phrasing that goes in the customer's
    # report. Only the row that seeded a group needs the question, but every member
    # carries it so a group survives its seed call being re-analysed away.
    #
    # gap_group_id == GAP_NEEDS_REVIEW is the reserved group for questions nobody can
    # act on (mis-heard speech, a subject with no attribute, internal search strings);
    # those are kept off the customer report entirely — see routes/analytics.py.
    #
    # NULL means "not grouped yet", which is what the grouping endpoint selects on, so
    # it must be cleared whenever unanswered_query changes underneath it. engine.py's
    # apply_result() does that on every re-analysis; without it a stored group would
    # point at wording that no longer exists.
    gap_group_id: Mapped[str | None] = mapped_column(String(64), index=True)
    gap_group_question: Mapped[str | None] = mapped_column(Text)

    # SUPERSEDED by `bucket`. Kept with their existing values rather than dropped:
    # DROP COLUMN destroys history with no undo, and disputes.py still reads them.
    # Nothing writes them any more — the freeze is achieved by turning
    # AnalysisConfig.classification_enabled off, which makes engine.apply_result() skip
    # the assignment entirely. Removing the fields from the prompt instead would null
    # every stored value on re-analysis; see the comment there.
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
    # Every attempt at proving this call's missing record really is missing. Usually
    # empty: verification runs per GROUP, and only the member whose transcript was read
    # carries the row. Kept on Call anyway so the call detail page can show the evidence
    # without knowing anything about groups.
    gap_verifications: Mapped[list["GapVerification"]] = relationship(
        back_populates="call",
        cascade="all, delete-orphan",
        order_by="GapVerification.created_at.desc()",
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
    # Sort every analysed call into exactly one bucket. List of
    # {"key": str, "description": str}, editable in Settings and seeded from buckets.py
    # on first run; an empty list falls back to those defaults. The description is what
    # the LLM reads to decide membership, so it does the real work.
    bucketing_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    buckets: Mapped[list | None] = mapped_column(JSON, default=list)

    # SUPERSEDED by `buckets`. Turning this off is what freezes Call.transfer_reason /
    # Call.non_completion_reason at their existing values — see models.Call and
    # analysis/engine.py apply_result().
    classification_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    transfer_reasons: Mapped[list | None] = mapped_column(JSON, default=list)
    non_completion_reasons: Mapped[list | None] = mapped_column(JSON, default=list)

    # The lookup APIs to re-ask a "missing record" question against, so a gap is proved
    # rather than assumed. List of
    #   {"key", "label", "url", "method", "headers", "body_template", "result_path",
    #    "enabled", "agent_ids"}
    # where body_template contains {{query}}. See gap_verification.py for the contract.
    #
    # `agent_ids` is which regions the source serves; empty means every region. A call is
    # never probed by a source that does not list its agent_id, because these backends
    # dispatch on a region-specific tool name and answer an unrecognised one with 200 OK
    # and a polite sentence — read as data, that sentence becomes "this record is missing
    # from your database" for every single gap.
    #
    # Defaults to EMPTY, unlike buckets/taxonomies: those have sensible universal
    # defaults, a lookup endpoint does not. An empty list makes the whole verification
    # feature inert, which is the right behaviour for every install that has not pointed
    # it at its own agent's knowledge sources.
    lookup_probes: Mapped[list | None] = mapped_column(JSON, default=list)

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class GapGroup(Base):
    """One missing record, and how far it has got towards being fixed.

    WHY THE STATE LIVES HERE AND NOT ON THE CALL

    A "missing record" is not a call — it is a question several calls asked in different
    words, merged by the grouping pass (gap_grouping.py). Verifying it means asking the
    lookup API once, about the canonical wording, and that one answer is about the record
    rather than about any particular call. Storing the verdict on each member call instead
    would let a call claim "verified missing" for a question that was only ever probed in
    somebody else's phrasing.

    The row is created LAZILY, on first verification — the grouping pass writes only
    `Call.gap_group_id` / `Call.gap_group_question` and knows nothing about this table.
    That is what keeps grouping and verification independent of each other.

    `id` matches `Call.gap_group_id` but is deliberately NOT a foreign key in either
    direction: `gap_group_id` also holds the reserved GAP_NEEDS_REVIEW value, membership
    is cleared by re-analysis without warning, and a group row must be able to outlive
    the loss of a member.
    """

    __tablename__ = "gap_groups"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Which region's probes to use. Taken from the members, all of which share it — a
    # Lazio question must never be sent to a Piemonte endpoint.
    agent_id: Mapped[str] = mapped_column(String(255), index=True)
    # The canonical question as it stood when this was last verified. Duplicated from
    # Call.gap_group_question on purpose: it is the exact string the verdict is about, so
    # a later re-grouping that rewords the headline cannot silently re-point the evidence.
    question: Mapped[str | None] = mapped_column(Text)

    # not_verified (NULL) | confirmed_missing | found_in_source | bad_question |
    # verify_error | sent | added | added_confirmed. See gap_verification.py for the order
    # these happen in and for the one-way rule that protects `sent`.
    status: Mapped[str | None] = mapped_column(String(32), index=True)
    status_at: Mapped[datetime | None] = mapped_column(DateTime)
    # One line saying why it landed in that state — shown on the row so a verdict never
    # has to be taken on trust.
    status_note: Mapped[str | None] = mapped_column(Text)
    # Stamped when the record goes out to the customer, shared by everything sent
    # together. This is what stops the same missing record being reported again tomorrow.
    sent_batch: Mapped[str | None] = mapped_column(String(64), index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    verifications: Mapped[list["GapVerification"]] = relationship(
        back_populates="group", order_by="GapVerification.created_at.desc()"
    )


class GapVerification(Base):
    """One attempt at proving a "missing record" really is missing.

    Kept as a history rather than a single column on GapGroup because a re-check after the
    customer says they have added the record must not erase the evidence that it was
    absent before — that pair of rows IS the proof the fix landed. It is also the only
    place the actual API responses are stored, and a verdict nobody can inspect is a
    verdict nobody should act on.
    """

    __tablename__ = "gap_verifications"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    # The record this is about. SET NULL rather than CASCADE: ungrouping a wrong merge
    # destroys the record but not what was learned, and the evidence stays reachable from
    # the call below.
    group_id: Mapped[str | None] = mapped_column(
        ForeignKey("gap_groups.id", ondelete="SET NULL"), index=True
    )
    # The member call whose transcript was read for context — which day the caller meant,
    # and whether the question was heard correctly. Named because the evidence has to say
    # which call it was reasoning about.
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    # confirmed_missing | found_in_source | bad_question | verify_error
    verdict: Mapped[str] = mapped_column(String(32), index=True)

    # The canonical question as the report showed it, and the question actually sent after
    # resolving dates and (where the wording was garbled) correcting it. Keeping both is
    # what lets you see that a verdict was reached about a different sentence.
    question_original: Mapped[str | None] = mapped_column(Text)
    question_resolved: Mapped[str | None] = mapped_column(Text)

    # The calendar date the caller meant, worked out from the member call's own start
    # time, and the date we ended up asking about. They differ when the caller's day had
    # already passed by the time we checked — an empty answer about a day that is over is
    # not evidence of a missing record, so the substitution has to be visible.
    date_meant: Mapped[str | None] = mapped_column(String(10))
    date_probed: Mapped[str | None] = mapped_column(String(10))

    # Why the question was judged usable or garbled, and why the replies were read the way
    # they were.
    question_note: Mapped[str | None] = mapped_column(Text)

    # [{"probe_key", "probe_label", "variant", "variant_kind", "url", "http_status",
    #   "ms", "response", "verdict": "ok"|"empty"|"error"}, ...]
    probes: Mapped[list | None] = mapped_column(JSON, default=list)

    llm_model: Mapped[str | None] = mapped_column(String(128))

    call: Mapped[Call] = relationship(back_populates="gap_verifications")
    group: Mapped[GapGroup | None] = relationship(back_populates="verifications")


class TestScenario(Base):
    """One rehearsed phone call: who to ring, which keys to press, what to say.

    Deliberately not an `Evaluator`, though the judging looks similar. An evaluator is a
    question asked of *every* call that already happened; a scenario is an instruction to
    *make* a call happen and then judge that one. Nothing else in the schema owns the
    "cause a call" half.
    """

    __tablename__ = "test_scenarios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255))

    # Which region this dials. Doubles as the key used to find the call row the agent
    # ingests afterwards, so it must match the agent's own `agent_id` exactly
    # ("Lazio", "Lombardia", "Trentino") — not a display name.
    agent_id: Mapped[str] = mapped_column(String(255), index=True)
    to_number: Mapped[str] = mapped_column(String(64))

    # Keypad presses needed to get past the call-centre menu, in order, e.g. "2,2".
    # They are sent BEFORE the audio stream opens and cannot be sent after: Twilio
    # passes keypad presses inward only, never outward from a media server. So the
    # pause is a blind guess at how long the menu talks, and is a setting because the
    # first real call is what tells you whether it is right.
    dtmf_digits: Mapped[str | None] = mapped_column(String(64))
    dtmf_pause_seconds: Mapped[float] = mapped_column(Float, default=4.0)

    # The caller's character and goal, handed to the Realtime model as its system
    # instructions. Written in the language the agent speaks.
    persona: Mapped[str] = mapped_column(Text)

    # What the run has to show for itself. Judged together in one pass — the run passes
    # only if every criterion holds — because criteria interact ("asked about X" and
    # "was given opening hours" are one story, not two independent facts).
    criteria: Mapped[list | None] = mapped_column(JSON, default=list)

    # Per-scenario override of settings.testcall_max_duration_seconds. A scenario with
    # three questions needs longer than a "does it answer at all" ping.
    max_duration_seconds: Mapped[int | None] = mapped_column(Integer)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TestRun(Base):
    """One execution of a scenario: what was dialled, what was said, what it proved."""

    __tablename__ = "test_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)

    # SET NULL, and the name/number copied alongside: deleting a scenario must not
    # delete the evidence that a call was made and what it found.
    scenario_id: Mapped[int | None] = mapped_column(
        ForeignKey("test_scenarios.id", ondelete="SET NULL"), index=True
    )
    scenario_name: Mapped[str] = mapped_column(String(255))
    agent_id: Mapped[str] = mapped_column(String(255), index=True)
    to_number: Mapped[str] = mapped_column(String(64))

    # queued -> dialing -> talking -> completed | failed
    # "completed" means the call happened and was judged; a scenario the agent failed is
    # still a completed run with verdict "fail". "failed" is reserved for the call never
    # happening, which is a different problem with a different owner.
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)

    # Twilio's identifier, kept so a call can be traced (or force-ended) from their side.
    provider_call_sid: Mapped[str | None] = mapped_column(String(64))
    # Shared secret in the stream URL. Twilio is not the only thing that can reach a
    # public websocket, and without this anyone could open one and be handed a live
    # OpenAI session on our key.
    stream_token: Mapped[str | None] = mapped_column(String(64))

    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    duration_seconds: Mapped[float | None] = mapped_column(Float)

    # What our own caller heard and said: [{"speaker": "agent"|"tester", "text": ...}].
    # Note "agent" here is the production assistant — from the Realtime model's point of
    # view that is the *user*, and mislabelling it makes every transcript unreadable.
    caller_transcript: Mapped[list | None] = mapped_column(JSON, default=list)

    # The row the production agent's own SDK posted for this call, once matched. That
    # row is the better evidence — it carries the tool calls, the latency and the raw
    # agent log, none of which our side can see.
    call_id: Mapped[str | None] = mapped_column(
        ForeignKey("calls.id", ondelete="SET NULL"), index=True
    )
    # When that row is due to be deleted. The verdict and both transcripts live here on
    # the run and survive it, so nothing about the test is lost — only the synthetic
    # call is kept out of the customer's reports.
    call_expires_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    call_deleted: Mapped[bool] = mapped_column(Boolean, default=False)

    # pass | fail | error. "error" is not a failing agent — it is a test that could not
    # reach a verdict (nobody answered, the stream never opened, the judge threw). Filing
    # those as failures would make a broken harness look like a broken agent.
    verdict: Mapped[str | None] = mapped_column(String(16), index=True)
    verdict_reason: Mapped[str | None] = mapped_column(Text)
    criteria_results: Mapped[list | None] = mapped_column(JSON, default=list)

    # Set when the caller heard the agent announce a transfer and hung up on purpose.
    # Reaching a human is a real outcome of a real call, but for a synthetic one it means
    # occupying an operator, so the run ends there and says so.
    ended_on_transfer: Mapped[bool] = mapped_column(Boolean, default=False)

    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

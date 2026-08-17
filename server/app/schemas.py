import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field, field_validator

from .outcome import compute_outcome
from .taxonomy import normalize_key


class ToolCall(BaseModel):
    name: str
    arguments: Any = None
    result: Any = None
    success: bool | None = None


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
    tool_calls: list[ToolCall] | None = None


class CallCreate(BaseModel):
    external_id: str | None = None
    agent_id: str = "default"
    direction: Literal["inbound", "outbound", "web"] = "inbound"
    from_number: str | None = None
    to_number: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_seconds: float | None = None
    transferred: bool = False
    recording_url: str | None = None
    metadata: dict[str, Any] | None = None
    turns: list[TurnIn] = Field(default_factory=list)
    # Optional: agents that already classify their own calls can send the reason
    # directly instead of paying for CallHarness to infer it. Whatever is sent here is
    # authoritative — analysis will not overwrite it. Ideally these match a key from
    # the configured taxonomy (Settings → Call classification) so the breakdown
    # charts stay meaningful; unknown values are stored as-is, not rejected.
    transfer_reason: str | None = Field(default=None, max_length=32)
    non_completion_reason: str | None = Field(default=None, max_length=32)


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
    tool_calls: list[ToolCall] | None = None

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
    transferred: bool
    recording_url: str | None
    has_recording: bool = False
    # Whether this call's raw agent log has been located in Azure. Like has_recording
    # it is derived in _to_out(), not a column — the blob name itself is internal.
    has_log: bool = False
    # Reads the ORM's `meta` column but is published as `metadata`, matching the name
    # CallCreate accepts. FastAPI serializes with by_alias=True, so without the explicit
    # serialization_alias this went out as `meta` and the dashboard's `call.metadata`
    # was permanently undefined.
    metadata: dict[str, Any] | None = Field(
        default=None, alias="meta", serialization_alias="metadata"
    )
    analysis_status: str
    analysis_error: str | None
    summary: str | None
    sentiment_label: str | None
    sentiment_score: float | None
    success: bool | None
    success_score: float | None
    success_rationale: str | None
    structured_data: dict[str, Any] | None
    # What happened on this call — one key from the configured taxonomy (buckets.py).
    bucket: str | None = None
    issue_note: str | None = None
    unanswered_query: str | None = None
    # Which missing record this call was merged into by the grouping pass, and the
    # canonical wording of that record. Null until it has been grouped.
    gap_group_id: str | None = None
    gap_group_question: str | None = None
    # Superseded by `bucket`, kept so historical values stay visible and queryable.
    transfer_reason: str | None = None
    non_completion_reason: str | None = None
    reason_source: str | None = None  # "agent" | "llm" | null
    quality: dict[str, Any] | None
    interruption_count: int
    language: str | None = None
    llm_model: str | None
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}

    @computed_field
    @property
    def outcome(self) -> str:
        """One of "transferred" | "completed" | "non_completed" — see outcome.py
        for the precedence. Derived, not stored."""
        return compute_outcome(self.success, self.transferred)


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
    # How far this call's missing record has got towards being fixed. Read from the
    # GapGroup it belongs to, not from the call — verification is about the record, and
    # several calls share one. Filled in by the route; null when ungrouped or unverified.
    gap_status: str | None = None
    gap_status_note: str | None = None


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


class ReasonCategory(BaseModel):
    """One bucket in a transfer / non-completion taxonomy.

    `key` is what gets persisted on the call and used as a chart slice and filter
    value, so it's normalized and length-capped to match the column. `description`
    is what the LLM reads to decide whether a call belongs in this bucket — it does
    the real work, so vague descriptions produce vague classification.
    """

    key: str = Field(min_length=1, max_length=32)
    description: str = ""

    @field_validator("key")
    @classmethod
    def _normalize(cls, v: str) -> str:
        key = normalize_key(v)
        if not key:
            raise ValueError("key must contain at least one non-space character")
        return key


class LookupProbe(BaseModel):
    """One knowledge source a "missing record" question can be re-asked against.

    Generic on purpose: a URL, a request body with {{query}} in it, and where to find the
    answer in the reply. That covers the VAPI-envelope endpoints the Italian healthcare
    agents use without naming them, and covers a plain REST lookup too — so nothing
    customer-specific has to live in the code.
    """

    key: str = Field(min_length=1, max_length=32)
    label: str = ""
    url: str = Field(min_length=1, max_length=1024)
    method: Literal["POST", "GET", "PUT"] = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    # Must contain {{query}}. The substitution is JSON-escaped, so an Italian question
    # with an apostrophe cannot break the body.
    body_template: str = Field(min_length=1)
    # Dotted path into the response, e.g. "results.0.result". Empty means the whole body.
    result_path: str = ""
    enabled: bool = True

    # WHICH REGIONS THIS SOURCE SERVES. Empty means every region, so a single-region
    # install needs no extra setup.
    #
    # This is not a convenience filter. These backends dispatch on a region-specific tool
    # name inside the request body and answer an unrecognised one with 200 OK and the
    # plain sentence "Tool non supportato: <name>". Sending a Lazio question to Piemonte's
    # /query_new returns exactly that — and read as data it becomes "this record is
    # missing from your database", for every single gap, with an evidence trail that looks
    # correct. So a call is never probed by a source that does not list its agent_id.
    agent_ids: list[str] = Field(default_factory=list)

    @field_validator("key")
    @classmethod
    def _normalize_key(cls, v: str) -> str:
        key = normalize_key(v)
        if not key:
            raise ValueError("key must contain at least one non-space character")
        return key

    @field_validator("body_template")
    @classmethod
    def _must_carry_query(cls, v: str) -> str:
        # Both caught on save rather than at probe time. A template without the
        # placeholder sends the same fixed request for every question, so every answer
        # would be recorded against the wrong gap; one that is not valid JSON fails on
        # every record in a sweep. Either way the failure is in the config, and the person
        # who can fix it is the one pressing Save.
        if "{{query}}" not in v:
            raise ValueError("body_template must contain {{query}}")
        try:
            json.loads(v)
        except ValueError as exc:
            raise ValueError(f"body_template is not valid JSON: {exc}") from exc
        return v


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
    # The single call-classification taxonomy. ReasonCategory is reused verbatim: same
    # {key, description} shape, same normalization and 32-char cap as the columns.
    bucketing_enabled: bool = True
    buckets: list[ReasonCategory] = Field(default_factory=list)
    # Superseded by `buckets`. Left on the schema so an install that still has it on
    # keeps round-tripping its saved taxonomy instead of silently resetting it.
    classification_enabled: bool = True
    transfer_reasons: list[ReasonCategory] = Field(default_factory=list)
    non_completion_reasons: list[ReasonCategory] = Field(default_factory=list)
    # Where to re-ask a missing-record question. Unlike the taxonomies above, an empty
    # list means exactly that — there is no universal default for somebody else's
    # knowledge base, so an unconfigured install simply cannot verify.
    lookup_probes: list[LookupProbe] = Field(default_factory=list)

    @field_validator("lookup_probes", "buckets", "extraction_fields", "transfer_reasons",
                     "non_completion_reasons", mode="before")
    @classmethod
    def _null_is_empty(cls, v):
        """A JSON column added by ALTER TABLE holds NULL, not [].

        `default=list` on the model only applies to rows SQLAlchemy inserts, so on every
        install that already existed the new column comes back None and a `list[...]`
        field rejects it — which took out GET /config/analysis, and with it the whole
        Settings page, the moment this deployed. The taxonomies escaped that only because
        `get_or_create_config()` materializes their defaults on first access; lookup_probes
        has no defaults to materialize, and must not grow any. They are all listed here so
        the next JSON column added cannot reintroduce this.
        """
        return [] if v is None else v


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
    outcome_distribution: dict[str, int] = Field(default_factory=dict)
    # What happened, across every analysed call — [{reason: key, count: n}, ...].
    bucket_breakdown: list[dict[str, Any]] = Field(default_factory=list)
    # answered / every bucketed call.
    raw_answer_rate: float | None = None
    # answered / (bucketed calls − needs_human − out_of_scope − no_caller_audio):
    # the share of calls we could actually have done something about.
    addressable_answer_rate: float | None = None
    transfer_reason_breakdown: list[dict[str, Any]] = Field(default_factory=list)
    non_completion_reason_breakdown: list[dict[str, Any]] = Field(default_factory=list)
    agents: list[str]
    # Per-agent (per-region) comparison: {agent_id, calls, success_rate, avg_sentiment}
    agent_stats: list[dict[str, Any]] = Field(default_factory=list)


class BucketsOut(BaseModel):
    """Distribution of what happened, plus the two answer rates. See buckets.py."""

    total_calls: int
    # Calls carrying a bucket. Lower than total_calls while a backfill is in flight,
    # and the denominator both rates are computed against.
    bucketed_calls: int
    distribution: list[dict[str, Any]] = Field(default_factory=list)
    raw_answer_rate: float | None = None
    addressable_answer_rate: float | None = None
    agent_stats: list[dict[str, Any]] = Field(default_factory=list)


class DisputedCallOut(BaseModel):
    """One call where the agent's verdict and CallHarness's disagree."""

    id: str
    started_at: datetime
    agent_id: str
    duration_seconds: float | None
    kind: str  # "outcome" | "reason"
    overcount: bool  # agent said completed, CallHarness didn't — the costly direction
    agent_esito: str | None
    agent_motivazione: str | None
    callharness_outcome: str
    callharness_reason: str | None
    summary: str | None
    success_rationale: str | None
    # Evidence the agent's judge never saw. A dispute backed by a failed tool call is
    # far more likely to be CallHarness being right than the agent.
    failed_tool_calls: list[str] = Field(default_factory=list)


class DisputesOut(BaseModel):
    comparable: int  # calls carrying an agent verdict AND a finished CallHarness analysis
    agreed: int
    disputed_outcome: int
    disputed_reason: int
    overcounted: int
    agreement_rate: float | None
    # Confusion matrix between the two judges: {agent, callharness, count}
    matrix: list[dict[str, Any]] = Field(default_factory=list)
    items: list[DisputedCallOut] = Field(default_factory=list)


class GapExampleOut(BaseModel):
    """One call that hit this gap — the customer looks the id up in their own system."""

    call_id: str
    external_id: str | None
    started_at: datetime
    agent_id: str
    question: str
    outcome: str


class KnowledgeGapOut(BaseModel):
    """A question the agent couldn't answer because the data wasn't there.

    Ungrouped, this is one call. Once the grouping pass has run, several calls asking
    the same thing share one of these, and `question` is the canonical phrasing it wrote.
    """

    question: str  # the clearest phrasing seen
    # Which lookup came back empty. Informational only — grouping deliberately ignores
    # it, since the attribution is a best guess (knowledge_gaps._tool_that_was_asked).
    tool: str
    count: int  # how many calls asked it
    transferred: int  # how many of those ended up with a human
    variants: list[str] = Field(default_factory=list)
    examples: list[GapExampleOut] = Field(default_factory=list)
    # Null until the grouping pass has placed these calls. Present means the row can be
    # ungrouped again; GAP_NEEDS_REVIEW means nobody can act on it.
    group_id: str | None = None
    grouped: bool = False
    needs_review: bool = False

    # Whether anyone has re-asked the lookup API about this record, and what came back —
    # read from the GapGroup row (gap_verification.py). `status` is null until somebody
    # verifies it; a row can only be reported to the customer once it says
    # "confirmed_missing" and `sent_batch` is still null.
    status: str | None = None
    status_at: datetime | None = None
    status_note: str | None = None
    sent_batch: str | None = None
    # The region whose lookup sources this record would be checked against, and whether
    # any are configured for it. Surfaced so the page can explain a disabled Verify button
    # rather than failing when it is pressed.
    agent_id: str | None = None
    probes_configured: int = 0


class KnowledgeGapsOut(BaseModel):
    window_days: int
    calls_scanned: int
    calls_with_gaps: int
    total_gaps: int
    # Share of scanned calls that hit at least one missing record. This is the headline:
    # it says how much of the transfer rate is a content problem, not an agent problem.
    gap_call_rate: float | None
    # Records the customer can act on. Excludes the needs-review set entirely.
    groups: list[KnowledgeGapOut] = Field(default_factory=list)
    # Questions nobody can add a record for: mis-heard speech, a subject with no
    # attribute, an internal search string. Kept off the report and out of its counts,
    # but shown on the dashboard so somebody can listen to the calls.
    needs_review: list[KnowledgeGapOut] = Field(default_factory=list)
    # How many gaps in this window have never been through the grouping pass. The button
    # is only worth pressing when this is above zero.
    ungrouped_count: int = 0


class GapGroupingOut(BaseModel):
    """Result of one grouping pass."""

    considered: int  # gaps sent to the model
    grouped: int  # gaps this pass placed alongside another gap from the same pass
    # Gaps slotted into a record an earlier pass created. Reported separately because a
    # pass doing only this has `grouped == 0` and `new_groups == 0`, which read as "did
    # nothing" when it had in fact merged a call into an existing record.
    joined_existing: int = 0
    needs_review: int
    new_groups: int
    remaining: int  # left ungrouped because the batch was capped; press again
    # Anything that degraded: an id the model dropped, a group it invented. Surfaced so
    # a partial run cannot be mistaken for a clean one.
    warnings: list[str] = Field(default_factory=list)


class GapUngroupOut(BaseModel):
    group_id: str
    calls_released: int


# ---------------------------------------------------------------------------
# Missing-record verification (gap_verification.py)
# ---------------------------------------------------------------------------


class ProbeAttemptOut(BaseModel):
    """One request to one source with one wording of the question.

    Every field is shown on the page. A verdict about somebody's data that cannot be
    inspected is a verdict nobody should act on, and "which exact sentence did you send,
    and what came back" is the whole of the inspection.
    """

    probe_key: str
    probe_label: str
    variant: str
    variant_kind: str  # canonical | paraphrase | corrected | dated | test
    url: str | None = None
    http_status: int | None = None
    ms: int | None = None
    response: str | None = None
    verdict: str  # ok | empty | error


class GapVerificationOut(BaseModel):
    id: str
    created_at: datetime
    verdict: str
    group_id: str | None = None
    call_id: str
    question_original: str | None
    question_resolved: str | None
    # Differ when the caller's day had already passed and a same-weekday substitute was
    # used instead — an empty answer about a day that is over proves nothing.
    date_meant: str | None
    date_probed: str | None
    question_note: str | None
    llm_model: str | None
    probes: list[ProbeAttemptOut] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class GapStatusIn(BaseModel):
    """A decision a person made, rather than one a probe produced."""

    status: str
    note: str | None = None


class GapGroupIdsIn(BaseModel):
    group_ids: list[str] = Field(default_factory=list)


class GapGroupStatusOut(BaseModel):
    group_id: str
    status: str
    status_at: datetime | None = None
    status_note: str | None = None
    sent_batch: str | None = None


class GapVerifyIn(BaseModel):
    """What to include in a batch run. Explicit ids win over the filters."""

    group_ids: list[str] = Field(default_factory=list)
    agent_id: str | None = None
    days: int = 30
    # Which statuses to (re-)check. Defaults to the ones nobody has looked at plus the
    # ones that failed for a reason that may have gone away.
    statuses: list[str] = Field(default_factory=list)
    limit: int = Field(default=50, ge=1, le=500)


class GapVerifyRunOut(BaseModel):
    """Progress of the single in-flight batch. One run at a time, by design: the limit
    exists to cap load on somebody else's production service, not to simplify the code."""

    running: bool
    total: int = 0
    done: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    current_group_id: str | None = None
    verdicts: dict[str, int] = Field(default_factory=dict)
    error: str | None = None


class GapVerifyPlanOut(BaseModel):
    """What a run would cost, before anything is spent.

    Every probe lands on the customer's live service — the same instance answering phone
    calls — and on our own LLM key. So the page asks first, and this is what it asks with.
    """

    groups: int
    requests: int  # upper bound: variants x sources, summed over the groups
    sources: list[str] = Field(default_factory=list)
    # Records that cannot be checked because no source is configured for their region.
    unroutable: dict[str, int] = Field(default_factory=dict)


class ProbeTestIn(BaseModel):
    probe: LookupProbe
    query: str = Field(min_length=1, max_length=500)


class ProbeTestOut(BaseModel):
    attempt: ProbeAttemptOut


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

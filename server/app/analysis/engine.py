"""Post-call analysis engine.

Runs a single LLM pass over the call transcript and produces:
summary, sentiment, success evaluation, and user-defined structured data.
"""

import json
from typing import Any

from ..buckets import (
    FALLBACK_BUCKET,
    NO_CALLER_AUDIO,
    buckets_or_default,
    has_caller_audio,
    ordered_for_prompt,
)
from ..models import AnalysisConfig, Call
from ..taxonomy import (
    DEFAULT_NON_COMPLETION_REASONS,
    DEFAULT_TRANSFER_REASONS,
    FALLBACK_KEY,
    categories_or_default,
    normalize_key,
)
from .llm import chat_json

SYSTEM_PROMPT = """You are a call quality analyst for AI voice agents. You are given the \
transcript of a phone call between a user and an AI assistant. Lines starting with \
"[tool call: ...]" are ground-truth system events (function calls the assistant actually \
made, with their real results) — not spoken dialogue. Treat them as fact, and use them \
instead of guessing when they explain why something happened (e.g. a failed tool call \
explains a dead end better than inferring one from tone). Analyze the call and respond with \
a single JSON object exactly matching the requested structure. Be factual: only state \
things supported by the transcript. Respond with JSON only."""


def classification_enabled(config: AnalysisConfig) -> bool:
    """Whether the LLM should classify transfer / non-completion reasons.

    Reads as enabled when the column is NULL — rows migrated from before this
    setting existed always classified, so "unset" must not silently turn it off.
    """
    return config.classification_enabled is not False


def bucketing_enabled(config: AnalysisConfig) -> bool:
    """Whether the LLM should sort this call into a bucket.

    NULL reads as enabled, for the same reason classification_enabled does: rows that
    predate the column must not silently lose the feature.
    """
    return config.bucketing_enabled is not False


def bucket_categories(config: AnalysisConfig) -> list[dict[str, str]]:
    return buckets_or_default(config.buckets)


def transfer_categories(config: AnalysisConfig) -> list[dict[str, str]]:
    return categories_or_default(config.transfer_reasons, DEFAULT_TRANSFER_REASONS)


def non_completion_categories(config: AnalysisConfig) -> list[dict[str, str]]:
    return categories_or_default(
        config.non_completion_reasons, DEFAULT_NON_COMPLETION_REASONS
    )


def _choice_list(categories: list[dict[str, str]]) -> str:
    return "; ".join(f'"{c["key"]}" ({c["description"]})' for c in categories)


def _resolve_key(value: Any, categories: list[dict[str, str]]) -> str:
    """Map an LLM answer onto a configured key, falling back to the catch-all."""
    key = normalize_key(value)
    valid = {c["key"] for c in categories}
    if key in valid:
        return key
    return FALLBACK_KEY if FALLBACK_KEY in valid else next(iter(valid))


# How much of a tool call reaches the judge. The result limit was 200 characters, which
# cut 43% of real production results off mid-sentence — and it cut the load-bearing part
# every time, because a long result is long precisely when it carries the full weekly
# hours and the "CHIUSURE PROSSIMI GIORNI" (upcoming closures) block. The judge then read
# the assistant quoting closure dates with no tool evidence behind them and scored a
# correct answer as an invented one.
#
# 2000 clears the largest result seen in production (1,085 chars) with room to spare.
# There is deliberately NO cap on the per-call total: a call with many tool calls is
# usually a lookup loop or a caller with several questions — exactly the calls whose
# evidence must not be trimmed. The realistic worst case is ~8.6k chars (~2.9k tokens),
# which is not a prompt size worth defending against.
_ARGS_CHAR_LIMIT = 500
_RESULT_CHAR_LIMIT = 2000


def _format_tool_call(tc: dict) -> str:
    name = tc.get("name", "unknown_tool")
    # ensure_ascii=False keeps accented text as itself. Escaping rewrites every accented
    # letter as a six-character Unicode escape, spending the limit above on encoding
    # rather than content (Italian weekday lists are the worst case) and costing ~7%
    # more tokens without adding meaning: the model reads both forms identically.
    args = json.dumps(tc.get("arguments"), default=str, ensure_ascii=False)[:_ARGS_CHAR_LIMIT]
    result = json.dumps(tc.get("result"), default=str, ensure_ascii=False)[:_RESULT_CHAR_LIMIT]
    success = tc.get("success")
    status = " [FAILED]" if success is False else ""
    return f"  [tool call: {name}({args}) -> {result}{status}]"


def build_transcript(call: Call) -> str:
    """Turns in order, with each turn's tool calls rendered BEFORE its text.

    The ordering is not cosmetic. A tool call is stashed against the *next* assistant
    turn (see the SDK's record_tool_call) because the lookup happens while that turn is
    still being produced — the tool always runs first, then the assistant speaks. This
    used to render the tool call after the turn text, which inverted that on every call
    and made the tool result look like the last thing the caller received. It is why a
    call whose assistant turn was cut to the single word "Il" scored `answered`: the
    transcript ended with a complete answer sitting under a one-word turn, so the judge
    read the answer as delivered. Rendering the tool call first puts the truncated
    speech last, where it actually belongs.
    """
    lines = []
    for t in call.turns:
        speaker = "User" if t.role == "user" else "Assistant"
        for tc in t.tool_calls or []:
            lines.append(_format_tool_call(tc))
        lines.append(f"{speaker}: {t.text}")
    return "\n".join(lines)


def build_user_prompt(call: Call, config: AnalysisConfig) -> str:
    sections: list[str] = [
        '- "language" (string): the primary language spoken on the call, as a '
        'lowercase English word (e.g. "italian", "english", "spanish").'
    ]
    schema: dict[str, Any] = {}

    if config.summary_enabled:
        instruction = config.summary_prompt or (
            "Write a 2-3 sentence summary of the call: who called about what, "
            "what the assistant did, and how it ended."
        )
        sections.append(f'- "summary" (string): {instruction}')
        schema["summary"] = "string"

    if config.sentiment_enabled:
        sections.append(
            '- "sentiment" (object): the caller\'s overall sentiment. '
            '{"label": one of "positive"|"neutral"|"negative", '
            '"score": number from -1.0 (very negative) to 1.0 (very positive)}'
        )
        schema["sentiment"] = {"label": "string", "score": "number"}

    if config.success_enabled:
        # The default deliberately turns on what the caller RECEIVED, not on what the
        # assistant produced. The older wording tested only for errors, dead-ends and
        # frustration — a caller who hung up in the middle of a correct answer is none
        # of those three, so every such call was scored a success, with the judge
        # writing "the assistant began to provide it... no unresolved issues".
        #
        # This leans on a guarantee the SDK now provides: an assistant turn is
        # truncated to the words whose playback timestamp precedes the hangup, so a
        # turn that breaks off mid-sentence is evidence the caller stopped hearing
        # there. Without that guarantee the transcript showed the full sentence and
        # this instruction would have nothing to key on.
        criteria = config.success_prompt or (
            "The call is successful only if the caller actually received what they "
            "asked for. Judge what reached the caller, not what the assistant intended "
            "to say: the transcript contains only what the caller actually heard, so a "
            "final assistant turn that breaks off mid-sentence means the caller hung up "
            "while it was still speaking and never heard the rest. Treat that as a "
            "failure when the answer itself was still being delivered, but not when the "
            "substantive answer had already been given and only a closing pleasantry "
            "was cut off. Unresolved errors, dead-ends, or the caller giving up "
            "frustrated are failures too."
        )
        if config.success_rubric == "numeric_scale":
            sections.append(
                f'- "success" (object): evaluate against this criteria: {criteria} '
                '{"passed": boolean, "score": integer 1-10, "rationale": short string}'
            )
        else:
            sections.append(
                f'- "success" (object): evaluate against this criteria: {criteria} '
                '{"passed": boolean, "rationale": short string}'
            )
        schema["success"] = {"passed": "boolean", "rationale": "string"}

    # Only ever ask about the one dimension that can apply. `transferred` is a
    # deterministic flag set by the agent, so a transferred call can never have a
    # non-completion reason and vice versa — asking for both wastes tokens and
    # invites the LLM to fill in a field that will be discarded. An agent that
    # classified the call itself (reason_source="agent") is authoritative, so we
    # don't ask at all.
    if classification_enabled(config) and call.reason_source != "agent":
        if call.transferred:
            sections.append(
                '- "transfer_reason" (string): this call was transferred to a human. '
                "Classify why, using exactly one of these labels: "
                f"{_choice_list(transfer_categories(config))}. Prefer the tool call log "
                'over guessing from tone — e.g. use "technical_error" if a tool call '
                "failed shortly before the transfer."
            )
            schema["transfer_reason"] = "string"
        else:
            sections.append(
                '- "non_completion_reason" (string or null): if the call ended WITHOUT '
                "the caller's need being resolved, classify why using exactly one of "
                f"these labels: {_choice_list(non_completion_categories(config))}. "
                "If the caller's need WAS resolved, use null."
            )
            schema["non_completion_reason"] = "string|null"

    if bucketing_enabled(config):
        ordered = ordered_for_prompt(bucket_categories(config))
        choices = "\n".join(f'    {i}. "{c["key"]}" — {c["description"]}'
                            for i, c in enumerate(ordered, 1))
        sections.append(
            '- "bucket" (string): what actually happened on this call. Choose EXACTLY '
            "one key from the list below, copied verbatim. Never invent a key.\n"
            f"{choices}\n"
            "  The list is ordered by severity. A call can fit several — take the "
            "FIRST one in this order that applies, not the one that feels most typical.\n"
            "  HOW TO DECIDE WHEN A TOOL ASKED A QUESTION BACK: do not judge by wording. "
            "When a tool result is a question or a request for more input rather than "
            "data, read what happens after it. If the caller supplies the detail and a "
            'later tool call containing that detail returns real data → "answered". If a '
            "later tool call does contain the caller's answer and the tool still returns "
            'a question or a generic non-answer → "tool_kept_asking". If the caller never '
            'supplies it → "caller_abandoned".\n'
            "  Ground every choice in the [tool call: ...] lines. What the assistant said "
            "is not evidence that a tool returned it."
        )
        sections.append(
            '- "issue_note" (string): ONE sentence describing what specifically happened '
            "on this call — the detail the bucket key cannot carry. Name the actual "
            "subject (which branch, which exam, which question), not a restatement of "
            "the bucket."
        )
        sections.append(
            '- "unanswered_query" (string or null): ONLY when bucket is '
            '"record_missing" — the question that came back with nothing, worded as it '
            "was sent to the tool (use the tool call's query argument where there is "
            "one). This line is sent to the customer as a record they should add, so it "
            "must be a coherent question a person could actually act on. If the query "
            "was built from mis-heard or fragmentary speech — a branch name that is not "
            "a real branch, a garbled service name — use null and say so in issue_note "
            'instead (and prefer the "not_understood" bucket). Use null for every other '
            "bucket."
        )
        schema["bucket"] = "string"
        schema["issue_note"] = "string"
        schema["unanswered_query"] = "string|null"

    fields = config.extraction_fields or []
    if config.extraction_enabled and fields:
        field_specs = []
        for f in fields:
            spec = f'"{f["name"]}" ({f.get("type", "text")})'
            if f.get("description"):
                spec += f': {f["description"]}'
            if f.get("type") == "enum" and f.get("choices"):
                spec += f' — one of: {", ".join(f["choices"])}'
            field_specs.append(spec)
        sections.append(
            '- "structured_data" (object): extract these fields from the call; '
            "use null when the information is not present. Fields: "
            + "; ".join(field_specs)
        )
        schema["structured_data"] = "object"

    transcript = build_transcript(call)
    meta = f"\nCall metadata: direction={call.direction}, agent={call.agent_id}"
    if call.end_reason:
        meta += f", end_reason={call.end_reason}"
    if call.duration_seconds:
        meta += f", duration={round(call.duration_seconds)}s"

    output_language = (config.output_language or "english").strip().lower()
    if output_language and output_language != "auto":
        language_note = (
            f"\nIMPORTANT: Write every free-text value (summary, rationale, extracted "
            f"text fields) in {output_language.capitalize()}, even if the call is in "
            f"another language. Keep enum/boolean/number values unchanged."
        )
    else:
        language_note = "\nWrite free-text values in the same language spoken on the call."

    return (
        "Analyze this call and return a JSON object with these keys:\n"
        + "\n".join(sections)
        + language_note
        + f"\n{meta}\n\nTranscript:\n{transcript}"
    )


def _resolve_bucket(value: Any, categories: list[dict[str, str]]) -> str:
    """Map the LLM's answer onto a configured bucket key.

    Anything unrecognised becomes `other` rather than a new chart slice — the model is
    never allowed to invent a key at runtime. Whatever made the call unusual belongs in
    `issue_note`, which is reviewed on the Other page and promoted by hand.
    """
    if not value:
        return FALLBACK_BUCKET
    key = normalize_key(value)
    valid = {c["key"] for c in categories}
    if key in valid:
        return key
    return FALLBACK_BUCKET if FALLBACK_BUCKET in valid else next(iter(valid))


def _coerce_number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def apply_result(call: Call, config: AnalysisConfig, result: dict) -> None:
    language = result.get("language")
    if language and isinstance(language, str):
        call.language = language.strip().lower()[:32]

    if config.summary_enabled:
        summary = result.get("summary")
        call.summary = str(summary) if summary else None

    if config.sentiment_enabled:
        sentiment = result.get("sentiment") or {}
        label = str(sentiment.get("label", "")).lower()
        call.sentiment_label = label if label in ("positive", "neutral", "negative") else None
        score = _coerce_number(sentiment.get("score"))
        call.sentiment_score = max(-1.0, min(1.0, score)) if score is not None else None

    if config.success_enabled:
        success = result.get("success") or {}
        passed = success.get("passed")
        call.success = bool(passed) if passed is not None else None
        call.success_score = _coerce_number(success.get("score"))
        rationale = success.get("rationale")
        call.success_rationale = str(rationale) if rationale else None

    if bucketing_enabled(config):
        categories = bucket_categories(config)
        bucket = result.get("bucket")
        call.bucket = _resolve_bucket(bucket, categories)
        note = result.get("issue_note")
        call.issue_note = str(note).strip()[:2000] if note else None
        # Only meaningful for record_missing, and only trustworthy there — asked for as
        # null everywhere else, but a model that fills it in anyway shouldn't be able to
        # put a phantom line in the customer's Missing Information report.
        query = result.get("unanswered_query")
        call.unanswered_query = (
            str(query).strip()[:500]
            if query and call.bucket == "record_missing"
            else None
        )

    # SUPERSEDED by the bucket block above; retained only so installs that still have
    # classification_enabled on keep working. Note the `else` branches: they null the
    # stored value whenever the LLM's reply lacks the field, and cannot tell "the model
    # judged it inapplicable" from "we never asked". That is exactly why the old
    # taxonomy is retired by turning classification_enabled OFF rather than by dropping
    # the fields from the prompt — the latter would wipe every stored reason on the
    # first re-analysis.
    if call.reason_source != "agent" and classification_enabled(config):
        transfer_reason = result.get("transfer_reason")
        if call.transferred and transfer_reason:
            call.transfer_reason = _resolve_key(transfer_reason, transfer_categories(config))
        else:
            call.transfer_reason = None

        non_completion_reason = result.get("non_completion_reason")
        if not call.transferred and call.success is not True and non_completion_reason:
            call.non_completion_reason = _resolve_key(
                non_completion_reason, non_completion_categories(config)
            )
        else:
            call.non_completion_reason = None

        call.reason_source = (
            "llm" if (call.transfer_reason or call.non_completion_reason) else None
        )

    if config.extraction_enabled and (config.extraction_fields or []):
        data = result.get("structured_data")
        if isinstance(data, dict):
            call.structured_data = data
        elif isinstance(data, str):
            try:
                call.structured_data = json.loads(data)
            except json.JSONDecodeError:
                call.structured_data = None


async def analyze_call(call: Call, config: AnalysisConfig) -> None:
    """Run analysis and write results onto the call object (not committed here).

    A call the caller never spoke on short-circuits here without an LLM request. It
    used to raise, which marked the call `failed` and left it looking like an outage;
    it is in fact a routine and frequent outcome (a silent line, or audio that never
    reached STT), and the one bucket that can be decided from the data alone.
    """
    if not has_caller_audio(call):
        if bucketing_enabled(config):
            call.bucket = NO_CALLER_AUDIO
            call.issue_note = "The caller never speaks in this call."
            call.unanswered_query = None
        return
    user_prompt = build_user_prompt(call, config)
    result = await chat_json(SYSTEM_PROMPT, user_prompt)
    apply_result(call, config, result)

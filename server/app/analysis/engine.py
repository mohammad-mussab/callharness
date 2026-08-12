"""Post-call analysis engine.

Runs a single LLM pass over the call transcript and produces:
summary, sentiment, success evaluation, and user-defined structured data.
"""

import json
from typing import Any

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


def _format_tool_call(tc: dict) -> str:
    name = tc.get("name", "unknown_tool")
    args = json.dumps(tc.get("arguments"), default=str)[:200]
    result = json.dumps(tc.get("result"), default=str)[:200]
    success = tc.get("success")
    status = " [FAILED]" if success is False else ""
    return f"  [tool call: {name}({args}) -> {result}{status}]"


def build_transcript(call: Call) -> str:
    lines = []
    for t in call.turns:
        speaker = "User" if t.role == "user" else "Assistant"
        lines.append(f"{speaker}: {t.text}")
        for tc in t.tool_calls or []:
            lines.append(_format_tool_call(tc))
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

    # The LLM classifies *why*, never *whether*: a reason is only persisted when the
    # deterministic fields (`transferred`, and `success` from the block above) agree
    # it applies. An agent that classified the call itself is left untouched, so
    # re-analysis can't overwrite ground truth with a guess.
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
    """Run analysis and write results onto the call object (not committed here)."""
    if not call.turns:
        raise ValueError("Call has no transcript turns to analyze")
    user_prompt = build_user_prompt(call, config)
    result = await chat_json(SYSTEM_PROMPT, user_prompt)
    apply_result(call, config, result)

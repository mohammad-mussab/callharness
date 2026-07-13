"""Post-call analysis engine.

Runs a single LLM pass over the call transcript and produces:
summary, sentiment, success evaluation, and user-defined structured data.
"""

import json
from typing import Any

from ..models import AnalysisConfig, Call
from .llm import chat_json

SYSTEM_PROMPT = """You are a call quality analyst for AI voice agents. You are given the \
transcript of a phone call between a user and an AI assistant. Analyze it and respond with \
a single JSON object exactly matching the requested structure. Be factual: only state \
things supported by the transcript. Respond with JSON only."""


def build_transcript(call: Call) -> str:
    lines = []
    for t in call.turns:
        speaker = "User" if t.role == "user" else "Assistant"
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
        criteria = config.success_prompt or (
            "The call is successful if the caller's need was handled without "
            "unresolved errors, dead-ends, or the caller giving up frustrated."
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

"""Build CallHarness turns from data your agent already has.

WHO THIS IS FOR
`CallRecorder` is the easy path: it collects turns as the call happens and uploads
them for you. But a mature agent usually already has its own call-data pipeline —
its own transcript list, its own record of tool calls, its own database write — and
adopting a second collector would mean maintaining two sources of truth.

For those, this module does the one genuinely fiddly part: merging a transcript, a
list of tool calls, and a stream of latency samples into the turn format the
ingestion API expects, attaching each event to the turn it actually belongs to.

    from callharness_sdk.turns import LatencyCollector, assemble_turns

    latency = LatencyCollector()          # feed from CallHarnessMetricsObserver
    ...
    turns = assemble_turns(
        transcript=my_transcript,          # [{role, content, timestamp}, ...]
        tool_calls=my_function_calls,      # [{function_name, parameters, result, timestamp}]
        latency=latency,
        started_at=call_started_at,
    )
    client.ingest_call(agent_id="my-agent", turns=turns, ...)

WHY EVENTS ARE MATCHED BY TIMESTAMP
A tool call and a latency measurement both happen *while* an assistant turn is being
produced — before its text exists. They belong to the reply that follows them, not the
one before. Rather than requiring you to call a hook at exactly the right moment, both
are timestamped when recorded and matched to the next assistant turn here.
"""

import json
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

__all__ = ["LatencyCollector", "assemble_turns"]

_COMPONENTS = ("stt", "llm", "tts")
_COMPONENT_FIELD = {"stt": "stt_ms", "llm": "llm_ttft_ms", "tts": "tts_ttfb_ms"}

# Tool results can be large (slot lists, search results). Cap them so one call can't
# post a multi-megabyte body; a judge needs the shape and the outcome, not every row.
DEFAULT_MAX_RESULT_CHARS = 2000


def _now() -> datetime:
    return datetime.now(timezone.utc)


class LatencyCollector:
    """Accumulates per-component latency samples, timestamped as they arrive.

    Satisfies the interface `CallHarnessMetricsObserver` expects, so you can hand it
    straight to the observer without adopting `CallRecorder`:

        latency = LatencyCollector()
        task = PipelineTask(pipeline,
                            params=PipelineParams(enable_metrics=True),
                            observers=[CallHarnessMetricsObserver(latency)])

    `enable_metrics=True` is required — without it Pipecat emits no MetricsFrames and
    every sample list stays empty.
    """

    def __init__(self) -> None:
        self._samples: list[dict[str, Any]] = []

    def record_component_latency(self, component: str, ms: float) -> None:
        """Record one STT/LLM/TTS time-to-first-byte measurement, in milliseconds."""
        if component not in _COMPONENTS:
            return
        try:
            value = float(ms)
        except (TypeError, ValueError):
            return
        if value < 0:
            return
        self._samples.append(
            {"component": component, "ms": round(value, 1), "timestamp": _now()}
        )

    @property
    def samples(self) -> list[dict[str, Any]]:
        return list(self._samples)

    def clear(self) -> None:
        self._samples.clear()

    def __len__(self) -> int:
        return len(self._samples)


def _parse_ts(value: Any) -> datetime | None:
    """Accept a datetime or an ISO string; return tz-aware UTC, or None."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _first(mapping: Mapping[str, Any], *names: str) -> Any:
    """Return the first present key — lets callers use their own field names."""
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return None


def _truncate(value: Any, limit: int) -> Any:
    """Shrink oversized values without destroying small structured ones."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        try:
            dumped = json.dumps(value, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            dumped = str(value)
        if len(dumped) <= limit:
            return value
        return dumped[:limit] + "…[truncated]"
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= limit else text[:limit] + "…[truncated]"


def tool_call_succeeded(result: Any) -> bool | None:
    """True / False / None for a tool result, never guessing True.

    An unknown marked as success would let a judge conclude a lookup worked when it may
    not have — which is the exact failure this whole path exists to remove. Unknown
    stays None.
    """
    if isinstance(result, Mapping):
        if result.get("error"):
            return False
        success = result.get("success")
        if isinstance(success, bool):
            return success
    if isinstance(result, BaseException):
        return False
    return None


def _normalize_tool_call(raw: Mapping[str, Any], limit: int) -> dict[str, Any]:
    result = _first(raw, "result", "response", "output")
    return {
        "name": _first(raw, "name", "function_name", "tool_name") or "unknown_tool",
        "arguments": _truncate(_first(raw, "arguments", "parameters", "args"), limit),
        "result": _truncate(result, limit),
        "success": raw["success"] if isinstance(raw.get("success"), bool)
        else tool_call_succeeded(result),
    }


def assemble_turns(
    transcript: Sequence[Mapping[str, Any]],
    tool_calls: Iterable[Mapping[str, Any]] = (),
    latency: "LatencyCollector | Iterable[Mapping[str, Any]]" = (),
    started_at: datetime | str | None = None,
    max_result_chars: int = DEFAULT_MAX_RESULT_CHARS,
) -> list[dict[str, Any]]:
    """Merge transcript, tool calls and latency into ingestion-ready turns.

    transcript: ordered entries with a role (``user``/``assistant``), the text under
        ``content`` or ``text``, and ideally a ``timestamp``.
    tool_calls: entries with ``name``/``function_name``, ``arguments``/``parameters``,
        ``result``, and a ``timestamp``.
    latency: a ``LatencyCollector``, or any iterable of
        ``{component, ms, timestamp}``.
    started_at: call start, used to express each turn's ``start_time`` in seconds from
        the beginning of the call. Omit it and ``start_time`` is left off.

    Entries without a timestamp still work: they simply attach to the next assistant
    turn in document order rather than by clock time.
    """
    call_start = _parse_ts(started_at)
    samples = latency.samples if isinstance(latency, LatencyCollector) else list(latency)

    def _sortable(items: Iterable[Mapping[str, Any]]) -> list[tuple[datetime | None, Mapping]]:
        stamped = [(_parse_ts(i.get("timestamp")), i) for i in items]
        # Undated events keep their original order and are treated as "as early as
        # possible", so they still land on the next assistant turn.
        return sorted(stamped, key=lambda p: (p[0] is not None, p[0] or datetime.min.replace(tzinfo=timezone.utc)))

    pending_tools = _sortable(tool_calls)
    pending_latency = _sortable(samples)
    tool_idx = 0
    lat_idx = 0

    carried_tools: list[dict[str, Any]] = []
    carried_latency: dict[str, float] = {}
    turns: list[dict[str, Any]] = []

    for entry in transcript:
        role = entry.get("role")
        raw_text = _first(entry, "content", "text")
        text = str(raw_text).strip() if raw_text is not None else ""
        if role not in ("user", "assistant") or not text:
            continue

        entry_ts = _parse_ts(entry.get("timestamp"))

        while tool_idx < len(pending_tools):
            ts, raw = pending_tools[tool_idx]
            if entry_ts is not None and ts is not None and ts > entry_ts:
                break
            carried_tools.append(_normalize_tool_call(raw, max_result_chars))
            tool_idx += 1

        while lat_idx < len(pending_latency):
            ts, sample = pending_latency[lat_idx]
            if entry_ts is not None and ts is not None and ts > entry_ts:
                break
            component = sample.get("component")
            if component in _COMPONENTS:
                carried_latency[component] = sample.get("ms")
            lat_idx += 1

        turn: dict[str, Any] = {"role": role, "text": text}
        if entry_ts is not None and call_start is not None:
            turn["start_time"] = round(max(0.0, (entry_ts - call_start).total_seconds()), 2)

        if role == "assistant":
            if carried_tools:
                turn["tool_calls"] = carried_tools
                carried_tools = []
            if carried_latency:
                for component, field in _COMPONENT_FIELD.items():
                    if carried_latency.get(component) is not None:
                        turn[field] = carried_latency[component]
                measured = [
                    turn.get(f) for f in _COMPONENT_FIELD.values() if turn.get(f) is not None
                ]
                if measured:
                    # Approximate voice-to-voice latency as the sum of the components,
                    # matching CallRecorder so both paths report the same number.
                    turn["latency_ms"] = round(sum(measured), 1)
                carried_latency = {}

        turns.append(turn)

    # Events after the final spoken turn — typically the transfer tool. Attach to the
    # last turn so they still reach the analysis rather than being silently dropped.
    leftover = carried_tools + [
        _normalize_tool_call(raw, max_result_chars) for _, raw in pending_tools[tool_idx:]
    ]
    if leftover and turns:
        turns[-1].setdefault("tool_calls", []).extend(leftover)

    return turns

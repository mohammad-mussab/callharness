"""Default classification taxonomies for *why* a call was transferred or left incomplete.

These are only the seed values. The live taxonomies are stored on the
`AnalysisConfig` singleton (``transfer_reasons`` / ``non_completion_reasons``) and
are editable from the dashboard's Settings page — no code change or redeploy needed
to add a category. `analysis/engine.py` reads them from config and falls back here
when the config row has an empty list.

Keep each taxonomy small and closed, with an "other" catch-all. Review the "other"
bucket in the breakdown charts periodically and promote a recurring pattern to a
named category, rather than letting the LLM invent one-off labels that fragment
the chart.
"""

# Keys are persisted on Call.transfer_reason / Call.non_completion_reason
# (VARCHAR(32)) and used as chart/filter values, so keep them short and stable —
# renaming a key orphans every call already classified under the old one.
DEFAULT_TRANSFER_REASONS: list[dict[str, str]] = [
    {
        "key": "knowledge_gap",
        "description": "the agent lacked information needed to help (no answer in its knowledge base/tools)",
    },
    {
        "key": "caller_requested_human",
        "description": "the caller explicitly asked for a human, unprompted by an agent failure",
    },
    {
        "key": "agent_confusion_loop",
        "description": "the agent repeatedly misunderstood or failed to make progress on a clear request",
    },
    {
        "key": "technical_error",
        "description": "a tool call, STT, LLM, or TTS failure (see tool call log) forced the transfer",
    },
    {
        "key": "policy_escalation",
        "description": "the situation required a human by policy regardless of agent capability (e.g. billing dispute, complaint)",
    },
    {
        "key": "other",
        "description": "none of the above, or the transcript doesn't make the reason clear",
    },
]

DEFAULT_NON_COMPLETION_REASONS: list[dict[str, str]] = [
    {
        "key": "caller_hangup_frustrated",
        "description": "the caller hung up showing frustration/anger before being helped",
    },
    {
        "key": "caller_hangup_silent",
        "description": "the caller hung up or stopped responding with no sign of frustration",
    },
    {
        "key": "silence_timeout",
        "description": "the call ended after a period of silence from the caller",
    },
    {
        "key": "technical_disconnect",
        "description": "a tool call, STT, LLM, or TTS failure (see tool call log) ended the call",
    },
    {
        "key": "agent_error",
        "description": "the agent gave an incorrect answer, looped, or otherwise mishandled the call without an underlying technical failure",
    },
    {
        "key": "other",
        "description": "none of the above, or the transcript doesn't make the reason clear",
    },
]

# Every taxonomy keeps a catch-all so an LLM answer outside the configured set has
# somewhere to land instead of being dropped or inventing a new chart slice.
FALLBACK_KEY = "other"

_KEY_MAX_LENGTH = 32  # must match the Call.transfer_reason / non_completion_reason columns


def normalize_key(value: str) -> str:
    """Coerce a free-form label into a taxonomy key: lowercase, underscores, trimmed."""
    return "_".join(str(value).strip().lower().split())[:_KEY_MAX_LENGTH]


def categories_or_default(
    configured: list | None, defaults: list[dict[str, str]]
) -> list[dict[str, str]]:
    """Return the usable category list, dropping malformed entries.

    An empty/absent config list means "never configured" and falls back to the
    defaults. A list that only contains junk also falls back, so a bad save can't
    silently disable classification.
    """
    cleaned = [
        {"key": normalize_key(c["key"]), "description": str(c.get("description") or "")}
        for c in (configured or [])
        if isinstance(c, dict) and str(c.get("key") or "").strip()
    ]
    return cleaned or defaults

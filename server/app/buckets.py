"""The single call-classification taxonomy: one bucket per analysed call.

WHY THIS REPLACED transfer_reason / non_completion_reason
Those two taxonomies were conditional on how the call ended. `transfer_reason` only
ever applied to transferred calls, `non_completion_reason` only to calls that were
neither transferred nor successful — so a *successful* call got no classification at
all. Three consequences, all measured on live Lazio data:

  - The same root cause landed in two different keys. A missing record filed as
    ``knowledge_gap`` if the call transferred and ``agent_error`` if it didn't, so
    one problem showed up as two chart slices that could never be added together.
  - 22% of calls (the completed ones) carried no diagnostic information whatsoever.
    A caller who asked three questions and got one was a "success" with nothing
    recorded about the two that failed.
  - A knowledge gap where the caller hung up before the transfer fired was invisible:
    not transferred, so no transfer_reason; often "successful", so no
    non_completion_reason either.

`bucket` applies to every analysed call regardless of outcome. It answers "what
actually happened", which is a different axis from `outcome` (transferred / completed /
non_completed, see outcome.py) — that one answers "how did it end" and is unchanged.

RELATIONSHIP TO THE OLD COLUMNS
`Call.transfer_reason` and `Call.non_completion_reason` are kept in the database with
their existing values, frozen. Nothing writes them any more. They are preserved rather
than dropped because DROP COLUMN destroys history with no undo, and because disputes.py
still reads them. The freeze is achieved by turning `AnalysisConfig.classification_enabled`
off — see the note in analysis/engine.py apply_result(), where doing it any other way
silently nulls every stored value on re-analysis.
"""

from __future__ import annotations

from typing import Any

from .taxonomy import normalize_key  # noqa: F401  (re-exported for callers)

# Set in code, never by the LLM: if the caller never appears in the transcript there is
# nothing for a judge to read, so asking one is both unreliable and a wasted API call.
NO_CALLER_AUDIO = "no_caller_audio"

# The catch-all. An answer outside the configured set lands here rather than becoming a
# new chart slice — uniqueness belongs in `issue_note`, not in the key.
FALLBACK_BUCKET = "other"

# The one bucket that counts as a win, and the ones excluded from the addressable rate
# because no amount of data or agent work would have changed them.
ANSWERED_BUCKET = "answered"
NOT_ADDRESSABLE = (NO_CALLER_AUDIO, "needs_human", "out_of_scope")


# Descriptions are not documentation — they are the prompt. The LLM decides membership
# by reading these, so vagueness here produces vague classification. Each one names the
# observable evidence rather than a feeling about the call.
DEFAULT_BUCKETS: list[dict[str, str]] = [
    {
        "key": "answered",
        "description": (
            "the caller got what they asked for. This INCLUDES the normal multi-turn "
            "round trip: a tool returned a question or asked for more detail, the "
            "assistant relayed it, the caller supplied the detail, and a later tool call "
            "containing that detail returned real data. That is correct behaviour, not a "
            "fault"
        ),
    },
    {
        "key": "partial_answered",
        "description": (
            "the caller asked about several things and received a real answer to some "
            "but not all of them, with nothing identifiable having gone wrong on the "
            "rest. If the unanswered part has a diagnosable cause, use that bucket "
            "instead"
        ),
    },
    {
        "key": "agent_invented_answer",
        "description": (
            "the assistant stated a specific fact — opening hours, a closure, a price, "
            "an address, availability — that does not appear in any preceding tool "
            "result, or stated it with no tool call at all. Compare what the assistant "
            "said against the tool results above it before choosing this"
        ),
    },
    {
        "key": "tool_kept_asking",
        "description": (
            "the tool asked for a detail, a LATER tool call's query ALREADY CONTAINED "
            "that detail, and the tool still responded by asking for the same thing "
            "again or by returning an aggregate instead of the specific answer. Read the "
            "query arguments, not the caller's words — the detail is usually supplied by "
            "the assistant rewriting the query. If the tool asks 'which branch?' and the "
            "next query names the branch and it asks 'which branch?' again, the loop "
            "never closed and this is the bucket, even if the tool answered a DIFFERENT "
            "question earlier in the same call. This is NOT record_missing: the data "
            "demonstrably exists — the tool can see it and says so in aggregate — the "
            "lookup just will not narrow to what was asked. Our bug, not the customer's "
            "missing data"
        ),
    },
    {
        "key": "caller_abandoned",
        "description": (
            "the assistant asked a reasonable clarifying question and the caller never "
            "answered it — they hung up or went silent instead of supplying the detail"
        ),
    },
    {
        "key": "record_missing",
        "description": (
            "a tool ran correctly and honestly returned nothing: no results, empty "
            "payload, or a message meaning it has no answer for this. The lookup worked; "
            "the record is absent from the customer's data. THIS IS STILL THE ANSWER when "
            "the assistant then handed the caller to a human or ended the call because it "
            "had nothing — an empty lookup is WHY the call failed, and being transferred "
            "is merely HOW it ended. Do NOT use this when a later tool call went on to "
            "answer the same question"
        ),
    },
    {
        "key": "lookup_error",
        "description": (
            "a tool failed technically — timeout, 5xx, connection refused, exception, "
            "traceback. Our infrastructure broke, as opposed to the data being absent"
        ),
    },
    {
        "key": "needs_human",
        "description": (
            "the request needed a person NO MATTER WHAT DATA EXISTED: making or changing "
            "a booking, NHS/prescription flows, the caller explicitly asking for an "
            "operator, or the agent correctly reporting no operator is available. The "
            "test is whether a human would still have been required if every record were "
            "present. Do NOT use this merely because the call was transferred — if the "
            'handoff happened because a lookup came back empty, that is "record_missing"; '
            'if a tool failed technically, that is "lookup_error"'
        ),
    },
    {
        "key": "out_of_scope",
        "description": (
            "the caller wanted something the agent may never do no matter what data "
            "existed: patient records, referrals, report status, issuing prescriptions, "
            "clinical advice"
        ),
    },
    {
        "key": NO_CALLER_AUDIO,
        "description": (
            "the caller never appears in the transcript at all. Normally decided before "
            "the judge runs; listed so the taxonomy is complete"
        ),
    },
    {
        "key": FALLBACK_BUCKET,
        "description": (
            "genuinely none of the above. Expected and fine — describe what happened in "
            "issue_note so recurring cases can be promoted to their own bucket"
        ),
    },
]


# Most severe first. A call can satisfy several buckets at once and only stores one, so
# this decides which wins. Without a fixed order the model picks arbitrarily and the
# same call classifies differently on different runs, which moves the charts while
# nothing has actually changed.
#
# Two placements that are deliberate and easy to get wrong:
#   - caller_abandoned outranks needs_human. A booking call where the caller vanished
#     during a clarifying question never got far enough to prove a human was required,
#     and needs_human is excluded from the addressable rate — so filing it there would
#     quietly inflate the headline number.
#   - partial_answered sits below every fault bucket. If part of a multi-question call
#     hit a missing record, "missing record" is the actionable label; "partly answered"
#     tells the customer nothing they can act on.
BUCKET_PRECEDENCE: tuple[str, ...] = (
    "agent_invented_answer",
    "tool_kept_asking",
    "record_missing",
    "lookup_error",
    "caller_abandoned",
    "needs_human",
    "out_of_scope",
    "partial_answered",
    ANSWERED_BUCKET,
    FALLBACK_BUCKET,
)


def buckets_or_default(configured: list | None) -> list[dict[str, str]]:
    """The usable bucket list, dropping malformed entries.

    Mirrors taxonomy.categories_or_default: an empty/absent list means "never
    configured" and falls back to the defaults, and a list containing only junk also
    falls back, so a bad save cannot silently empty the taxonomy.
    """
    cleaned = [
        {"key": normalize_key(c["key"]), "description": str(c.get("description") or "")}
        for c in (configured or [])
        if isinstance(c, dict) and str(c.get("key") or "").strip()
    ]
    return cleaned or DEFAULT_BUCKETS


def ordered_for_prompt(categories: list[dict[str, str]]) -> list[dict[str, str]]:
    """Configured buckets sorted by BUCKET_PRECEDENCE, most severe first.

    The prompt presents them in this order and tells the judge the first match wins, so
    the ordering has to survive whatever order they happen to sit in on the config row.
    Keys not in BUCKET_PRECEDENCE — ones the user added in Settings — sort to the end
    just above `other`, since we have no basis for ranking them higher than a bucket
    whose severity we defined.
    """
    rank = {key: i for i, key in enumerate(BUCKET_PRECEDENCE)}
    unknown = len(BUCKET_PRECEDENCE) - 1  # just before `other`
    return sorted(categories, key=lambda c: rank.get(c["key"], unknown))


def has_caller_audio(call: Any) -> bool:
    """True when the caller actually says something in this call.

    Requires `call.turns` loaded. A call with assistant turns but no user text is the
    common shape here — the agent greeted a line that was silent, or the caller's audio
    never reached STT — and it is indistinguishable from a caller who hung up instantly.
    Either way there is nothing to judge.
    """
    return any(
        t.role == "user" and t.text and t.text.strip() for t in (getattr(call, "turns", None) or [])
    )

"""Compare the agent's own verdict against OpenCall's.

WHY THIS EXISTS
Mature agents often classify every call, but their judge sees only
dialogue text. OpenCall re-analyses the same call with tool calls inlined, so it can
tell a failed lookup from a caller who hung up. When the two disagree one of them is
wrong, and that list is far more useful than either verdict alone: it's a ranked queue
of calls worth listening to, and it measures whether the reported numbers can be
trusted at all.

HOW A VERDICT ARRIVES
The agent sends its own classification in `Call.meta` (not in `transfer_reason` — that
field is OpenCall's own answer, and letting the agent write it would make OpenCall echo
the verdict it's supposed to be checking). Keys are `agent_esito` and
`agent_motivazione`, matching the esito_chiamata / motivazione columns those agents
already keep.

Calls with no `agent_esito` are simply not comparable and are excluded rather than
counted as agreement.
"""

from typing import Any

from .taxonomy import normalize_key

# esito_chiamata is a three-value taxonomy that OpenCall's own outcome buckets were
# deliberately modelled on, so the mapping is one-to-one. Keys are normalized before
# lookup, so "NON COMPLETATA", "non completata" and "Non Completata" all land together.
ESITO_TO_OUTCOME: dict[str, str] = {
    "completata": "completed",
    "trasferita": "transferred",
    "non_completata": "non_completed",
}

# What kind of disagreement this is.
AGREED = "agreed"
OUTCOME_DISPUTE = "outcome"  # different bucket entirely — the serious one
REASON_DISPUTE = "reason"  # same bucket, different reason for it


def agent_outcome(meta: dict[str, Any] | None) -> str | None:
    """Map the agent's esito onto an OpenCall outcome, or None if it didn't send one."""
    if not isinstance(meta, dict):
        return None
    esito = meta.get("agent_esito")
    if not esito or not isinstance(esito, str):
        return None
    return ESITO_TO_OUTCOME.get(normalize_key(esito))


def agent_reason_key(meta: dict[str, Any] | None) -> str | None:
    """The agent's motivazione as a taxonomy key, comparable to OpenCall's own."""
    if not isinstance(meta, dict):
        return None
    motivazione = meta.get("agent_motivazione")
    if not motivazione or not isinstance(motivazione, str):
        return None
    return normalize_key(motivazione)


def classify(
    *,
    meta: dict[str, Any] | None,
    opencall_outcome: str,
    opencall_reason: str | None,
) -> str | None:
    """Return AGREED / OUTCOME_DISPUTE / REASON_DISPUTE, or None if not comparable.

    A reason dispute is only reported when *both* sides actually named a reason —
    OpenCall leaving a reason null (e.g. a completed call, which has no transfer or
    non-completion reason) is not a disagreement about anything.
    """
    theirs = agent_outcome(meta)
    if theirs is None:
        return None
    if theirs != opencall_outcome:
        return OUTCOME_DISPUTE

    their_reason = agent_reason_key(meta)
    if their_reason and opencall_reason and their_reason != opencall_reason:
        return REASON_DISPUTE
    return AGREED


def is_overcount(meta: dict[str, Any] | None, opencall_outcome: str) -> bool:
    """The agent claimed success and OpenCall didn't.

    Called out separately because it's the asymmetric failure that matters: a call
    wrongly marked COMPLETATA inflates the headline success rate and is never
    reviewed, whereas the reverse is merely pessimistic.
    """
    return agent_outcome(meta) == "completed" and opencall_outcome != "completed"

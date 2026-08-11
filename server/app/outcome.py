"""Single-glance call outcome, derived from fields already on the Call row.

Three buckets, deliberately mirroring the taxonomy production voice agents
already use (e.g. the Italian healthcare agent's `esito_chiamata`:
COMPLETATA / TRASFERITA / NON COMPLETATA). "Success" is not a separate
outcome — a call that succeeded *is* a completed call; splitting them just
created two labels for one state.

Not a stored column: `success`, `transferred`, and `end_reason` are each set
independently (by the LLM judge, the SDK's transfer detection, and the SDK's
deterministic end-of-call signal respectively) and each answer a different
question. This collapses them into one field you can scan a list by.
"""

OUTCOMES = ("transferred", "completed", "non_completed")


def compute_outcome(success: bool | None, transferred: bool, end_reason: str | None) -> str:
    if transferred:
        return "transferred"
    if success is True:
        return "completed"
    if success is False:
        return "non_completed"
    # success is None: not judged (analysis pending/skipped, or no LLM configured).
    # Fall back to the SDK's own deterministic end-of-call signal.
    if end_reason == "completed":
        return "completed"
    return "non_completed"

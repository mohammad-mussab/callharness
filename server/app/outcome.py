"""Single-glance call outcome, derived from fields already on the Call row.

Three buckets, deliberately mirroring the taxonomy production voice agents
already use (e.g. the Italian healthcare agent's `esito_chiamata`:
COMPLETATA / TRASFERITA / NON COMPLETATA). "Success" is not a separate
outcome — a call that succeeded *is* a completed call; splitting them just
created two labels for one state.

Not a stored column: `success` and `transferred` are set independently (by the
LLM judge and the SDK's transfer detection) and answer different questions.
This collapses them into one field you can scan a list by.

This used to take a third argument, `end_reason`, as a fallback for unjudged
calls. That column has been removed: measured across 674 live Lazio calls it
only ever held `None` (397) or `"transferred"` (277), agreed with the
`transferred` flag on every single row, and never once carried the
`"completed"` value the fallback was checking for. It duplicated `transferred`
and decided nothing.
"""

OUTCOMES = ("transferred", "completed", "non_completed")


def compute_outcome(success: bool | None, transferred: bool) -> str:
    if transferred:
        return "transferred"
    if success is True:
        return "completed"
    # success is False, or None because the call was never judged (analysis
    # pending/skipped, or no LLM configured). Neither is a completed call.
    return "non_completed"

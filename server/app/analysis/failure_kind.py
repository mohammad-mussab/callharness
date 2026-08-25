"""Classify why an analysis failed, so the worker knows whether to retry it.

`analysis_status = "failed"` used to be terminal. The worker only ever claims
`pending` (worker.py `_claim_pending`), so nothing ever picked a failed call back
up and recovery meant someone running scripts/reanalyze.py by hand. In the
Aug-2026 audit that cost 158 calls: the OpenAI balance ran out mid-run, every
in-flight call was marked `failed`, and they simply vanished from every statistic
with no alert. Nobody noticed until the numbers were queried directly.

Blind auto-retry is not the fix either — retrying a genuine outage forever just
burns the loop. So failures are sorted into three kinds and each gets the
treatment it deserves:

  RETRYABLE  transient and worth another go on a timer: 429, 5xx, timeouts,
             connection resets, malformed JSON from the model. Bounded attempts
             with exponential backoff, then parked.

  BLOCKED    the account cannot serve requests until a human acts — no credit,
             bad key, org suspended. Waiting does NOT help, so these are NOT
             retried on the normal timer. They are counted and surfaced so the
             billing problem gets fixed, and requeued once when service returns.

  TERMINAL   this call will never analyse: a 400 the payload itself caused, a
             context-length overflow, an unparseable transcript. Retrying is
             pure waste. Parked immediately for a human to look at.

Classification is by substring against the stored error text because that is all
the failure path preserves (`analysis_error = str(exc)[:2000]`, worker.py). Kept
deliberately conservative: anything unrecognised is RETRYABLE, since a bounded
retry of a terminal error costs a few wasted calls, while mis-parking a transient
one silently loses data — the exact failure this module exists to prevent.
"""

from __future__ import annotations

RETRYABLE = "retryable"
BLOCKED = "blocked"
TERMINAL = "terminal"

# Checked FIRST and in this order: an insufficient_quota arrives as HTTP 429, the
# same status as an ordinary rate limit, so matching "429" before the quota
# markers would file a dead account as a transient blip and retry it forever.
_BLOCKED_MARKERS = (
    "insufficient_quota",
    "insufficient quota",
    "exceeded your current quota",
    "credit balance",
    "billing_hard_limit_reached",
    "billing hard limit",
    "account_deactivated",
    "account is not active",
    "invalid_api_key",
    "incorrect api key",
    "authentication_error",
    "permission_denied",
    "401",
    "403",
)

# A request that cannot succeed as written. Retrying replays the same payload and
# gets the same answer.
_TERMINAL_MARKERS = (
    "context_length_exceeded",
    "maximum context length",
    "string_above_max_length",
    "model_not_found",
    "does not exist or you do not have access",
    "unsupported_value",
    "invalid_request_error",
)


def classify_failure(error_text: str | None) -> str:
    """Sort a stored analysis_error into RETRYABLE / BLOCKED / TERMINAL.

    Args:
        error_text: the `analysis_error` string, or None.

    Returns:
        One of the three module-level constants. Unknown text -> RETRYABLE, so a
        failure mode nobody anticipated still gets its bounded retries instead of
        being silently dropped.
    """
    if not error_text:
        return RETRYABLE

    text = error_text.lower()

    for marker in _BLOCKED_MARKERS:
        if marker in text:
            return BLOCKED

    for marker in _TERMINAL_MARKERS:
        if marker in text:
            return TERMINAL

    return RETRYABLE


def retry_delay_seconds(attempts: int, base: float = 60.0, cap: float = 3600.0) -> float:
    """Exponential backoff for the Nth retry, capped.

    `attempts` is how many have already been made, so the first retry waits
    `base`. Capped at an hour: past that the failure is not transient and the
    attempt limit will park it anyway.
    """
    if attempts < 0:
        attempts = 0
    return min(base * (2 ** attempts), cap)

"""Auto-requeue of failed analyses — the lifecycle, against a real database.

Covers the behaviour that was missing when the Aug-2026 credit outage stranded
158 calls: a `failed` analysis was terminal, because the worker only ever claimed
`pending`. These tests drive the real worker functions against a temporary SQLite
database, so they exercise the actual claim SQL rather than a mock of it.

Run:  pytest server/tests/test_auto_requeue.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest

SERVER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_ROOT))

# Point the app at a throwaway database BEFORE app.config is imported, so no test
# can ever touch a real one.
_TMPDB = Path(tempfile.mkdtemp(prefix="callharness-test-")) / "test.db"
os.environ["CALLHARNESS_DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMPDB}"
os.environ["CALLHARNESS_LLM_PROVIDER"] = "openai"
os.environ["CALLHARNESS_OPENAI_API_KEY"] = "test-key-not-used"

from app.analysis import worker  # noqa: E402
from app.analysis.failure_kind import (  # noqa: E402
    BLOCKED,
    RETRYABLE,
    TERMINAL,
    classify_failure,
)
from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Call, utcnow  # noqa: E402



_DB_READY = False


@pytest.fixture(autouse=True)
async def _db():
    """Create the schema once. Function-scoped because pytest-asyncio gives each
    test its own event loop, and a module-scoped async fixture cannot be shared
    across loops."""
    global _DB_READY
    if not _DB_READY:
        await init_db()
        _DB_READY = True
    yield


async def _make_call(call_id: str, **kw) -> None:
    async with SessionLocal() as s:
        s.add(Call(id=call_id, agent_id="test", **kw))
        await s.commit()


async def _get(call_id: str) -> Call:
    async with SessionLocal() as s:
        return await s.get(Call, call_id)


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------

def test_quota_429_is_blocked_not_retryable():
    """The bug that cost 158 calls: insufficient_quota arrives as a 429, the same
    status as an ordinary rate limit. Matching on the status alone would retry a
    dead account forever."""
    quota = (
        "LLM request failed (429): {'error': {'message': 'You exceeded your "
        "current quota', 'type': 'insufficient_quota'}}"
    )
    ratelimit = "LLM request failed (429): Rate limit reached for gpt-4.1 on tokens per min"
    assert classify_failure(quota) == BLOCKED
    assert classify_failure(ratelimit) == RETRYABLE


def test_terminal_and_unknown():
    assert classify_failure("400 context_length_exceeded: maximum context length") == TERMINAL
    # Unknown text must be retryable: a wasted retry is cheap, a silently dropped
    # call is the failure this whole feature exists to prevent.
    assert classify_failure("something nobody predicted") == RETRYABLE
    assert classify_failure(None) == RETRYABLE


# --------------------------------------------------------------------------
# claiming
# --------------------------------------------------------------------------

async def test_pending_is_claimed():
    await _make_call("c-pending", analysis_status="pending")
    claimed = await worker._claim_pending(10)
    assert "c-pending" in claimed
    assert (await _get("c-pending")).analysis_status == "processing"


async def test_failed_with_due_retry_is_claimed():
    await _make_call(
        "c-due",
        analysis_status="failed",
        analysis_failure_kind=RETRYABLE,
        analysis_attempts=1,
        analysis_next_retry_at=utcnow() - timedelta(seconds=1),
    )
    assert "c-due" in await worker._claim_pending(10)


async def test_failed_with_future_retry_is_not_claimed():
    await _make_call(
        "c-future",
        analysis_status="failed",
        analysis_failure_kind=RETRYABLE,
        analysis_attempts=1,
        analysis_next_retry_at=utcnow() + timedelta(hours=1),
    )
    assert "c-future" not in await worker._claim_pending(10)


async def test_parked_call_is_never_claimed():
    """Terminal / out-of-attempts rows have next_retry_at NULL *and* a kind set.
    They must stay put, or a permanently-broken call loops forever."""
    await _make_call(
        "c-parked",
        analysis_status="failed",
        analysis_failure_kind=TERMINAL,
        analysis_attempts=1,
        analysis_next_retry_at=None,
    )
    assert "c-parked" not in await worker._claim_pending(10)


async def test_pre_existing_failure_is_recovered():
    """The 158 stranded calls: failed before this feature existed, so no kind and
    no schedule. Deploying must pick them up on the first poll."""
    await _make_call(
        "c-legacy",
        analysis_status="failed",
        analysis_error="LLM request failed (429): insufficient_quota",
        analysis_failure_kind=None,
        analysis_next_retry_at=None,
    )
    assert "c-legacy" in await worker._claim_pending(10)


# --------------------------------------------------------------------------
# the failure path itself
# --------------------------------------------------------------------------

async def _fail_with(call_id: str, message: str, monkeypatch) -> Call:
    """Drive the real _process_one with analyze_call raising `message`."""
    async def boom(call, config):
        raise RuntimeError(message)

    monkeypatch.setattr(worker, "analyze_call", boom)
    await _make_call(call_id, analysis_status="processing")
    await worker._process_one(call_id)
    return await _get(call_id)


async def test_retryable_failure_schedules_a_retry(monkeypatch):
    call = await _fail_with("c-r1", "LLM request failed (500): server error", monkeypatch)
    assert call.analysis_status == "failed"
    assert call.analysis_failure_kind == RETRYABLE
    assert call.analysis_attempts == 1
    assert call.analysis_next_retry_at is not None


async def test_blocked_failure_does_not_consume_attempts(monkeypatch):
    """A long outage must not burn the attempt budget on calls that never got a
    real chance — otherwise topping up the balance recovers nothing."""
    call = await _fail_with(
        "c-b1",
        "LLM request failed (429): {'type': 'insufficient_quota'}",
        monkeypatch,
    )
    assert call.analysis_failure_kind == BLOCKED
    assert call.analysis_attempts == 0
    assert call.analysis_next_retry_at is not None  # rechecked, just slowly


async def test_terminal_failure_is_parked_immediately(monkeypatch):
    call = await _fail_with(
        "c-t1", "LLM request failed (400): context_length_exceeded", monkeypatch
    )
    assert call.analysis_failure_kind == TERMINAL
    assert call.analysis_next_retry_at is None


async def test_retries_stop_at_max_attempts(monkeypatch):
    """Bounded, so a persistent failure cannot loop forever."""
    async def boom(call, config):
        raise RuntimeError("LLM request failed (500): server error")

    monkeypatch.setattr(worker, "analyze_call", boom)
    await _make_call("c-loop", analysis_status="processing")

    for _ in range(settings.analysis_max_attempts + 2):
        async with SessionLocal() as s:
            c = await s.get(Call, "c-loop")
            c.analysis_status = "processing"
            await s.commit()
        await worker._process_one("c-loop")

    call = await _get("c-loop")
    assert call.analysis_attempts == settings.analysis_max_attempts + 2
    assert call.analysis_next_retry_at is None, "should be parked after max attempts"
    assert "c-loop" not in await worker._claim_pending(50)


async def test_success_clears_requeue_state(monkeypatch):
    """A call that succeeds on retry must not keep a stale kind, or it pollutes
    the backlog count."""
    async def ok(call, config):
        return None

    monkeypatch.setattr(worker, "analyze_call", ok)
    monkeypatch.setattr(worker, "run_evaluators", lambda *a, **k: _noop())
    monkeypatch.setattr(worker, "check_call_alerts", lambda *a, **k: _noop())

    await _make_call(
        "c-ok",
        analysis_status="processing",
        analysis_failure_kind=RETRYABLE,
        analysis_attempts=2,
        analysis_next_retry_at=utcnow(),
    )
    await worker._process_one("c-ok")
    call = await _get("c-ok")
    assert call.analysis_status == "completed"
    assert call.analysis_failure_kind is None
    assert call.analysis_next_retry_at is None


async def _noop():
    return None

"""In-process analysis worker.

Polls for calls with analysis_status='pending' and runs the analysis engine.
No external queue needed; can be replaced by a dedicated worker service later.
"""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from .. import azure_logs
from ..buckets import DEFAULT_BUCKETS
from ..config import settings
from ..db import SessionLocal
from ..models import AnalysisConfig, Call, utcnow
from ..storage import delete_recording
from ..taxonomy import DEFAULT_NON_COMPLETION_REASONS, DEFAULT_TRANSFER_REASONS
from .alerts import check_call_alerts, check_window_alerts
from .engine import analyze_call
from .evaluators import run_evaluators
from .failure_kind import BLOCKED, RETRYABLE, classify_failure, retry_delay_seconds

logger = logging.getLogger("callharness.worker")

WINDOW_ALERT_INTERVAL_SECONDS = 60.0

# How often to log the parked-failure backlog. The Aug-2026 outage went unnoticed
# because a failed call produced one WARNING at the time and then nothing ever
# again — no running total, so 158 missing calls looked like silence. This makes
# the backlog impossible to miss in the logs without needing a UI change.
FAILURE_BACKLOG_INTERVAL_SECONDS = 300.0


async def get_or_create_config(session) -> AnalysisConfig:
    config = await session.get(AnalysisConfig, 1)
    changed = config is None
    if config is None:
        config = AnalysisConfig(id=1)
        session.add(config)
    # Materialize the default taxonomies onto the row so the Settings page has
    # something concrete to edit rather than an empty list. Also backfills installs
    # that predate these columns. Idempotent: only runs while the lists are empty.
    if not config.transfer_reasons:
        config.transfer_reasons = [dict(c) for c in DEFAULT_TRANSFER_REASONS]
        changed = True
    if not config.non_completion_reasons:
        config.non_completion_reasons = [dict(c) for c in DEFAULT_NON_COMPLETION_REASONS]
        changed = True
    if not config.buckets:
        config.buckets = [dict(c) for c in DEFAULT_BUCKETS]
        changed = True
    if changed:
        await session.commit()
    return config


async def _expire_recordings() -> int:
    """Delete recordings past the retention window. Returns how many were removed.

    Only the audio file goes: the call, its transcript, tool calls and analysis are
    kept indefinitely. Those are small and are what the dashboard is built on — it is
    the audio that would otherwise fill the disk.
    """
    days = settings.recording_retention_days
    if days <= 0:
        return 0

    cutoff = utcnow() - timedelta(days=days)
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(Call).where(
                    Call.recording_path.is_not(None), Call.started_at < cutoff
                )
            )
        ).scalars().all()
        if not rows:
            return 0
        for call in rows:
            delete_recording(call.recording_path)
            # Cleared even when the file was already gone, so has_recording stops
            # promising audio the dashboard cannot play.
            call.recording_path = None
        await session.commit()
    logger.info(
        "Deleted %d recording(s) older than %d days (transcripts kept)", len(rows), days
    )
    return len(rows)


async def _reconcile_logs() -> int:
    """Point recent calls at their raw agent log in Azure. Returns how many were matched.

    Scoped two ways so a steady-state instance costs nothing: it returns before touching
    Azure when no call needs looking up, and it ignores calls older than
    azure_log_lookback_days. Past that window the blob is never going to appear — the
    agent uploads once with no retry and deletes un-uploaded leftovers after a week — so
    scanning for them again would be pure waste. scripts/sync_azure_logs.py --recheck is
    the escape hatch for a one-off historical sweep.
    """
    if not azure_logs.enabled():
        return 0

    now = utcnow()
    cutoff = now - timedelta(days=settings.azure_log_lookback_days)
    # A call already looked for gets another chance an hour later: the usual reason for
    # a miss is that the agent hadn't finished uploading, and that resolves itself.
    retry_before = now - timedelta(hours=1)
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(Call).where(
                    Call.log_blob.is_(None),
                    Call.external_id.is_not(None),
                    Call.started_at >= cutoff,
                    or_(Call.log_checked_at.is_(None), Call.log_checked_at < retry_before),
                )
            )
        ).scalars().all()
        if not rows:
            return 0
        matched = await azure_logs.resolve(session, rows)
    if matched:
        logger.info("Linked %d of %d call(s) to their Azure log", matched, len(rows))
    return matched


async def _process_one(call_id: str) -> None:
    async with SessionLocal() as session:
        call = (
            await session.execute(
                select(Call).options(selectinload(Call.turns)).where(Call.id == call_id)
            )
        ).scalar_one_or_none()
        if call is None or call.analysis_status != "processing":
            return
        config = await get_or_create_config(session)
        try:
            await analyze_call(call, config)
            call.analysis_status = "completed"
            call.analysis_error = None
            # Clear the requeue bookkeeping so a call that succeeded on retry does
            # not keep a stale kind/next_retry that would confuse the backlog count.
            call.analysis_failure_kind = None
            call.analysis_next_retry_at = None
            call.analyzed_at = utcnow()
            call.llm_model = settings.resolved_model
            await run_evaluators(session, call)
        except Exception as exc:  # noqa: BLE001 - worker must never crash
            call.analysis_status = "failed"
            call.analysis_error = str(exc)[:2000]

            # Decide whether this call comes back on its own. Before this, every
            # failure was terminal and only scripts/reanalyze.py could recover it,
            # so a credit outage silently deleted 158 calls from the statistics.
            call.analysis_attempts = (call.analysis_attempts or 0) + 1
            kind = classify_failure(call.analysis_error)
            call.analysis_failure_kind = kind

            if kind == BLOCKED:
                # Waiting does not refill an account, so this is not on the retry
                # curve: re-checked slowly, and it does NOT consume the attempt
                # budget — otherwise a long outage would burn all five attempts on
                # calls that never got a real chance and park them for good.
                call.analysis_attempts -= 1
                delay = settings.analysis_blocked_recheck_seconds
                logger.error(
                    "Analysis BLOCKED for call %s (provider refusing requests — check "
                    "billing/API key); re-checking in %.0fs: %s",
                    call_id, delay, exc,
                )
            elif kind == RETRYABLE and call.analysis_attempts < settings.analysis_max_attempts:
                delay = retry_delay_seconds(
                    call.analysis_attempts - 1, base=settings.analysis_retry_base_seconds
                )
                logger.warning(
                    "Analysis failed for call %s (attempt %d/%d, retrying in %.0fs): %s",
                    call_id, call.analysis_attempts, settings.analysis_max_attempts,
                    delay, exc,
                )
            else:
                # Terminal, or retryable that ran out of attempts. Parked: no
                # next_retry_at means no automatic claim, same as the old behaviour.
                delay = None
                logger.error(
                    "Analysis failed for call %s and will NOT retry (kind=%s, "
                    "attempts=%d): %s",
                    call_id, kind, call.analysis_attempts, exc,
                )

            call.analysis_next_retry_at = (
                utcnow() + timedelta(seconds=delay) if delay is not None else None
            )
        await session.commit()
        if call.analysis_status == "completed":
            try:
                await check_call_alerts(session, call)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Alert check failed for call %s: %s", call_id, exc)


async def _log_failure_backlog() -> None:
    """Log how many analyses are stuck, grouped by why.

    `blocked` in this list is the actionable one: it means the provider is
    refusing requests (no credit, bad key) and calls are piling up waiting for a
    human to fix billing.
    """
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(Call.analysis_failure_kind, func.count(Call.id))
                .where(Call.analysis_status == "failed")
                .group_by(Call.analysis_failure_kind)
            )
        ).all()
    if not rows:
        return
    total = sum(n for _, n in rows)
    breakdown = ", ".join(f"{kind or 'untriaged'}={n}" for kind, n in sorted(rows, key=lambda r: str(r[0])))
    blocked = next((n for kind, n in rows if kind == BLOCKED), 0)
    if blocked:
        logger.error(
            "Analysis backlog: %d failed (%s) — %d BLOCKED on the provider, check "
            "billing/API key; they requeue automatically once it is fixed",
            total, breakdown, blocked,
        )
    else:
        logger.warning("Analysis backlog: %d failed (%s)", total, breakdown)


async def _check_skipped_alerts(call_ids: list[str]) -> None:
    """Keyword/latency alert rules work without an LLM, so evaluate them
    even for calls whose analysis was skipped."""
    async with SessionLocal() as session:
        for call_id in call_ids:
            call = (
                await session.execute(
                    select(Call).options(selectinload(Call.turns)).where(Call.id == call_id)
                )
            ).scalar_one_or_none()
            if call is not None:
                await check_call_alerts(session, call)


async def _claim_pending(limit: int) -> list[str]:
    """Claim work: fresh `pending` calls, plus `failed` ones whose retry is due.

    The second half is what makes a failure recoverable without a human. A failed
    call is claimable once analysis_next_retry_at has passed; rows parked by the
    terminal/out-of-attempts branch have it NULL and are never picked up, which is
    exactly the old behaviour for the cases where retrying is pointless.

    The IS NULL arm catches calls that failed BEFORE this feature shipped — they
    have no kind and no schedule — so deploying it recovers the existing backlog
    (158 calls stranded by the Aug-2026 credit outage) on the first poll.
    """
    now = utcnow()
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(Call.id)
                .where(
                    or_(
                        Call.analysis_status == "pending",
                        (Call.analysis_status == "failed")
                        & (
                            or_(
                                Call.analysis_next_retry_at <= now,
                                # never triaged: failed before auto-requeue existed
                                (Call.analysis_next_retry_at.is_(None))
                                & (Call.analysis_failure_kind.is_(None)),
                            )
                        ),
                    )
                )
                .order_by(Call.created_at)
                .limit(limit)
            )
        ).scalars().all()
        if not rows:
            return []
        new_status = "processing" if settings.resolved_provider != "none" else "skipped"
        for call_id in rows:
            call = await session.get(Call, call_id)
            call.analysis_status = new_status
            # Consume the schedule as part of claiming. _process_one rewrites it if
            # the attempt fails again; clearing it here means a call that dies
            # without reaching that code (process killed mid-analysis) is left
            # `processing` with no due date rather than becoming instantly
            # re-claimable in a tight loop.
            call.analysis_next_retry_at = None
        await session.commit()
    if new_status == "skipped":
        try:
            await _check_skipped_alerts(list(rows))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Alert check for skipped calls failed: %s", exc)
        return []
    return list(rows)


async def run_worker(stop_event: asyncio.Event) -> None:
    if not settings.analysis_enabled:
        logger.info("Analysis worker disabled (CALLHARNESS_ANALYSIS_ENABLED=false)")
        return
    logger.info(
        "Analysis worker started (provider=%s, model=%s)",
        settings.resolved_provider,
        settings.resolved_model if settings.resolved_provider != "none" else "-",
    )
    last_window_check = 0.0
    last_recording_cleanup = 0.0
    last_log_sync = 0.0
    last_backlog_log = 0.0
    while not stop_event.is_set():
        try:
            claimed = await _claim_pending(settings.analysis_concurrency)
            if claimed:
                await asyncio.gather(*(_process_one(cid) for cid in claimed))
                continue  # immediately look for more work
        except Exception as exc:  # noqa: BLE001
            logger.error("Worker loop error: %s", exc)
        now = asyncio.get_event_loop().time()
        if now - last_window_check >= WINDOW_ALERT_INTERVAL_SECONDS:
            last_window_check = now
            try:
                async with SessionLocal() as session:
                    await check_window_alerts(session)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Window alert check failed: %s", exc)
        if now - last_recording_cleanup >= settings.recording_cleanup_interval_seconds:
            last_recording_cleanup = now
            try:
                await _expire_recordings()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Recording cleanup failed: %s", exc)
        if now - last_backlog_log >= FAILURE_BACKLOG_INTERVAL_SECONDS:
            last_backlog_log = now
            try:
                await _log_failure_backlog()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failure backlog check failed: %s", exc)
        if now - last_log_sync >= settings.azure_log_sync_interval_seconds:
            last_log_sync = now
            try:
                await _reconcile_logs()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Azure log sync failed: %s", exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.analysis_poll_seconds)
        except asyncio.TimeoutError:
            pass

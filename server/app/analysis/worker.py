"""In-process analysis worker.

Polls for calls with analysis_status='pending' and runs the analysis engine.
No external queue needed; can be replaced by a dedicated worker service later.
"""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..config import settings
from ..db import SessionLocal
from ..models import AnalysisConfig, Call, utcnow
from ..storage import delete_recording
from ..taxonomy import DEFAULT_NON_COMPLETION_REASONS, DEFAULT_TRANSFER_REASONS
from .alerts import check_call_alerts, check_window_alerts
from .engine import analyze_call
from .evaluators import run_evaluators

logger = logging.getLogger("callharness.worker")

WINDOW_ALERT_INTERVAL_SECONDS = 60.0


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
            call.analyzed_at = utcnow()
            call.llm_model = settings.resolved_model
            await run_evaluators(session, call)
        except Exception as exc:  # noqa: BLE001 - worker must never crash
            logger.warning("Analysis failed for call %s: %s", call_id, exc)
            call.analysis_status = "failed"
            call.analysis_error = str(exc)[:2000]
        await session.commit()
        if call.analysis_status == "completed":
            try:
                await check_call_alerts(session, call)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Alert check failed for call %s: %s", call_id, exc)


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
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(Call.id)
                .where(Call.analysis_status == "pending")
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
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.analysis_poll_seconds)
        except asyncio.TimeoutError:
            pass

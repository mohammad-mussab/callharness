"""Verifying missing records: re-ask the lookup API, then track what happens next.

The reading of the report itself stays on `/analytics/knowledge-gaps`, which already
assembles the groups and now carries each one's verification status. This router owns the
things that *change* something: running a check, recording what a person did with the
result, and testing a probe's configuration.

The unit everywhere here is the GROUP — one missing record — never the call. See
`gap_verification` for why, and for all of the reasoning; this module is routing,
filtering and the one background task.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import gap_verification as gv
from ..analysis.worker import get_or_create_config
from ..auth import require_api_key
from ..db import SessionLocal, get_session
from ..gap_grouping import GAP_NEEDS_REVIEW
from ..models import Call, GapVerification, utcnow
from ..schemas import (
    GapGroupIdsIn,
    GapGroupStatusOut,
    GapStatusIn,
    GapVerificationOut,
    GapVerifyIn,
    GapVerifyPlanOut,
    GapVerifyRunOut,
    ProbeAttemptOut,
    ProbeTestIn,
    ProbeTestOut,
)

logger = logging.getLogger("callharness.gaps")

router = APIRouter(prefix="/api/v1/gaps", tags=["gaps"])


# ---------------------------------------------------------------------------
# Which records can be verified
# ---------------------------------------------------------------------------


async def _load_group(session: AsyncSession, group_id: str) -> dict:
    """One record and all of its calls, from the whole database.

    Deliberately unwindowed: a group's calls can be older than whatever range the page is
    showing, and verifying only the visible half would read the wrong call's transcript
    for the date the caller meant.
    """
    if group_id == GAP_NEEDS_REVIEW:
        raise HTTPException(
            status_code=400,
            detail="Needs-review questions cannot be verified — nobody could add a record "
            "for them. Open the calls and listen instead.",
        )
    calls = (
        (await session.execute(gv.eligible_calls_query(None, None).where(Call.gap_group_id == group_id)))
        .scalars()
        .all()
    )
    if not calls:
        raise HTTPException(
            status_code=404,
            detail="No missing-record calls belong to that group. It may have been "
            "ungrouped, or re-analysis may have moved its calls out.",
        )
    return gv.assemble_groups(list(calls))[group_id]


# ---------------------------------------------------------------------------
# Verify one
# ---------------------------------------------------------------------------


@router.post(
    "/{group_id}/verify",
    response_model=GapVerificationOut,
    dependencies=[Depends(require_api_key)],
)
async def verify_one(group_id: str, session: AsyncSession = Depends(get_session)):
    group = await _load_group(session, group_id)
    config = await get_or_create_config(session)
    try:
        verification = await gv.verify_gap_group(
            session,
            group_id=group_id,
            canonical=group["canonical"],
            members=group["members"],
            config=config,
        )
    except gv.ProbeConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    await session.refresh(verification)
    return GapVerificationOut.model_validate(verification)


@router.get("/{group_id}/verifications", response_model=list[GapVerificationOut])
async def group_verifications(group_id: str, session: AsyncSession = Depends(get_session)):
    """Full history for one record, newest first — including the runs a re-check replaced.

    The before/after pair is the proof the customer's fix landed, so nothing here is ever
    overwritten.
    """
    rows = (
        (
            await session.execute(
                select(GapVerification)
                .where(GapVerification.group_id == group_id)
                .order_by(GapVerification.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [GapVerificationOut.model_validate(r) for r in rows]


# ---------------------------------------------------------------------------
# Verify many, in the background
# ---------------------------------------------------------------------------

# One run at a time, process-wide. This is a cap on load against somebody else's
# production service — the same instance answering live phone calls — not a convenience.
# A batch is also far too slow to hold a request open: their graph endpoint is two or
# three sequential gpt-4.1 calls, so a record takes the better part of a minute and 166 of
# them takes hours.
_run_lock = asyncio.Lock()
_run_state = GapVerifyRunOut(running=False)
_run_task: asyncio.Task | None = None


@router.get("/verify/status", response_model=GapVerifyRunOut)
async def verify_status():
    return _run_state


async def _select_group_ids(
    session: AsyncSession, payload: GapVerifyIn
) -> tuple[list[str], dict[str, int]]:
    """The records a run would cover, and how many are unroutable, per region."""
    calls = (
        (await session.execute(gv.eligible_calls_query(payload.agent_id, payload.days))).scalars().all()
    )
    groups = gv.assemble_groups(list(calls))
    if payload.group_ids:
        groups = {gid: g for gid, g in groups.items() if gid in set(payload.group_ids)}

    config = await get_or_create_config(session)
    existing = await gv.load_groups(session, list(groups))
    wanted = set(payload.statuses) or {gv.NOT_VERIFIED, gv.VERIFY_ERROR}

    chosen: list[str] = []
    unroutable: dict[str, int] = defaultdict(int)
    for group_id, group in groups.items():
        if gv.status_of(existing.get(group_id)) not in wanted:
            continue
        if not gv.probes_for_agent(config, group["agent_id"]):
            # Counted and reported rather than attempted. A record whose region has no
            # source is not a finding about anybody's data.
            unroutable[group["agent_id"]] += 1
            continue
        chosen.append(group_id)
    return chosen[: payload.limit], dict(unroutable)


@router.post("/verify/plan", response_model=GapVerifyPlanOut)
async def verify_plan(payload: GapVerifyIn, session: AsyncSession = Depends(get_session)):
    """What a run would cost, before a single request is sent.

    Every probe lands on the customer's live service — no rate limiting, no caching, the
    same instance answering phone calls — and on our own LLM key. So the page shows this
    and asks, rather than starting and reporting afterwards.
    """
    group_ids, unroutable = await _select_group_ids(session, payload)
    config = await get_or_create_config(session)
    calls = (
        (await session.execute(gv.eligible_calls_query(payload.agent_id, payload.days))).scalars().all()
    )
    groups = gv.assemble_groups(list(calls))

    requests = 0
    sources: set[str] = set()
    for group_id in group_ids:
        agent_id = groups[group_id]["agent_id"]
        requests += gv.estimate_requests(config, agent_id)
        for probe in gv.probes_for_agent(config, agent_id):
            sources.add(probe.get("label") or probe.get("key") or "probe")

    return GapVerifyPlanOut(
        groups=len(group_ids),
        requests=requests,
        sources=sorted(sources),
        unroutable=unroutable,
    )


async def _run_batch(group_ids: list[str]) -> None:
    global _run_state
    _run_state = GapVerifyRunOut(
        running=True, total=len(group_ids), done=0, started_at=utcnow(), verdicts={}
    )
    try:
        for group_id in group_ids:
            _run_state.current_group_id = group_id
            # A session per record, committed as it goes: a batch that dies at record 90
            # must leave the first 89 verdicts on disk. One long transaction would lose
            # them, and this run costs the customer real API calls to reproduce.
            async with SessionLocal() as session:
                try:
                    group = await _load_group(session, group_id)
                    config = await get_or_create_config(session)
                    verification = await gv.verify_gap_group(
                        session,
                        group_id=group_id,
                        canonical=group["canonical"],
                        members=group["members"],
                        config=config,
                    )
                    await session.commit()
                    verdict = verification.verdict
                except gv.NoProbeForRegion as exc:
                    # One region's problem, not the run's: other regions in the same batch
                    # are still checkable, so record it and carry on.
                    await session.rollback()
                    logger.warning("gap verification skipped %s: %s", group_id, exc)
                    verdict = "unroutable"
                except HTTPException:
                    # The record went away between selection and its turn — somebody
                    # pressed ungroup, or a re-analysis moved its last call out. A long
                    # run makes that ordinary rather than exceptional, and it is not a
                    # finding about anybody's data.
                    await session.rollback()
                    logger.info("gap verification skipped %s: no longer a record", group_id)
                    verdict = "skipped"
                except gv.ProbeConfigError as exc:
                    # No probes at all, or a broken template: every remaining record would
                    # fail the same way, so stop rather than burn through the list.
                    await session.rollback()
                    _run_state.error = str(exc)
                    break
                except Exception as exc:  # noqa: BLE001
                    await session.rollback()
                    logger.exception("gap verification failed for %s", group_id)
                    verdict = gv.VERIFY_ERROR
                    _run_state.error = f"{group_id}: {exc}"[:500]
            _run_state.verdicts[verdict] = _run_state.verdicts.get(verdict, 0) + 1
            _run_state.done += 1
    finally:
        _run_state.running = False
        _run_state.current_group_id = None
        _run_state.finished_at = utcnow()


@router.post("/verify", response_model=GapVerifyRunOut, dependencies=[Depends(require_api_key)])
async def verify_many(payload: GapVerifyIn, session: AsyncSession = Depends(get_session)):
    global _run_task
    if _run_state.running:
        raise HTTPException(
            status_code=409,
            detail="A verification run is already in progress. Only one runs at a time, "
            "to keep the load on the lookup API bounded.",
        )

    config = await get_or_create_config(session)
    if not gv.enabled_probes(config):
        raise HTTPException(
            status_code=400,
            detail="No lookup probes are configured. Add one in Analysis Settings first — "
            "without a source to re-ask, a gap can only be assumed, not verified.",
        )

    group_ids, unroutable = await _select_group_ids(session, payload)
    if not group_ids:
        detail = "Nothing matched those filters."
        if unroutable:
            regions = ", ".join(f"{n} in {a}" for a, n in unroutable.items())
            detail = (
                f"Nothing can be checked: {regions}. Add a lookup source for "
                "those regions in Analysis Settings."
            )
        raise HTTPException(status_code=400, detail=detail)

    async with _run_lock:
        if _run_state.running:
            raise HTTPException(status_code=409, detail="A verification run just started.")
        _run_task = asyncio.create_task(_run_batch(group_ids))
    # Give the task a moment to publish its totals, so the first poll is not empty.
    await asyncio.sleep(0)
    return _run_state


# ---------------------------------------------------------------------------
# Statuses a person sets
# ---------------------------------------------------------------------------


@router.post(
    "/mark-sent", response_model=list[GapGroupStatusOut], dependencies=[Depends(require_api_key)]
)
async def mark_sent(payload: GapGroupIdsIn, session: AsyncSession = Depends(get_session)):
    """Stamp a batch as reported to the customer, so it is never reported again.

    One shared batch id across the whole press, because "what did we send on Tuesday" is
    the question this answers. Records that were not proved missing are refused rather
    than silently skipped: sending an unverified line is the mistake this feature exists to
    stop, and a silent skip would hide that it happened.
    """
    if not payload.group_ids:
        raise HTTPException(status_code=400, detail="No records given.")

    groups = await gv.load_groups(session, payload.group_ids)
    missing = [gid for gid in payload.group_ids if gid not in groups]
    unproven = [gid for gid, g in groups.items() if gv.status_of(g) != gv.SENDABLE]
    if missing or unproven:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{len(missing) + len(unproven)} of {len(payload.group_ids)} are not "
                f"{gv.SENDABLE}. Only records proved missing against the lookup API can be "
                "marked as sent."
            ),
        )

    batch = gv.new_batch_id()
    for group in groups.values():
        gv.set_status(group, gv.SENT, batch=batch)
    await session.commit()
    return [
        GapGroupStatusOut(
            group_id=g.id,
            status=gv.status_of(g),
            status_at=g.status_at,
            status_note=g.status_note,
            sent_batch=g.sent_batch,
        )
        for g in groups.values()
    ]


@router.post(
    "/{group_id}/status",
    response_model=GapGroupStatusOut,
    dependencies=[Depends(require_api_key)],
)
async def set_status(
    group_id: str, payload: GapStatusIn, session: AsyncSession = Depends(get_session)
):
    """Record what a person decided: sent, the customer added it, or start again.

    Verification verdicts are not settable here — see gap_verification.MANUAL_STATUSES.
    """
    group = await _load_group(session, group_id)
    row = await gv.get_or_create_group(
        session, group_id, agent_id=group["agent_id"], question=group["canonical"]
    )
    try:
        gv.set_status(row, payload.status, note=payload.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return GapGroupStatusOut(
        group_id=row.id,
        status=gv.status_of(row),
        status_at=row.status_at,
        status_note=row.status_note,
        sent_batch=row.sent_batch,
    )


# ---------------------------------------------------------------------------
# Settings helper
# ---------------------------------------------------------------------------


@router.post("/probe-test", response_model=ProbeTestOut, dependencies=[Depends(require_api_key)])
async def probe_test(payload: ProbeTestIn):
    """Send one question through one probe and show exactly what came back.

    Worth having its own button because a probe pointed at the wrong URL or carrying the
    wrong tool name still answers 200 OK with a polite sentence, and every downstream
    reading of that sentence turns into "this record is missing from your database".
    Seeing the raw reply once is the only way to know the config is right.
    """
    record = await gv.test_probe(payload.probe.model_dump(), payload.query)
    return ProbeTestOut(attempt=ProbeAttemptOut(**record))

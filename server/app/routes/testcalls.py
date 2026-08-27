"""Automated test calls: scenarios, runs, and the socket Twilio streams audio to.

Deployment note, because one endpoint here is different from every other one in this
project: ``/stream/{run_id}`` **must be reachable from the public internet**. Twilio
dials from its own cloud and opens the audio connection inwards; there is no polling
alternative. Everything else stays private.

On the Lazio VM that means one more location on the nginx that already terminates TLS
on 443, placed above the existing ``location /callharness/ { return 404; }``:

    location /callharness/api/v1/testcalls/stream/ {
        proxy_pass http://callharness_api/api/v1/testcalls/stream/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 3600s;
    }

and then ``CALLHARNESS_TESTCALL_STREAM_URL=wss://<host>/callharness/api/v1/testcalls/stream``.

Edit that file with ``cat tmp > nginx.conf`` — it is a single-file bind mount, and
``sed -i`` replaces the inode so the container keeps reading the old copy while
``nginx -t`` cheerfully validates it.

The socket is unauthenticated in the HTTP sense (Twilio sends no header we control), so
it is protected by a one-time token minted per run and checked below. A run that is not
dialing is refused outright, which closes the window to a few seconds per test call.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_api_key
from ..config import settings
from ..db import SessionLocal, get_session
from ..models import TestRun, TestScenario, utcnow
from ..schemas import (
    TestCallReadinessOut,
    TestRunOut,
    TestScenarioIn,
    TestScenarioOut,
)
from ..testcall import runner
from ..testcall.bridge import BridgeResult, run_bridge

logger = logging.getLogger("callharness.testcalls")

router = APIRouter(prefix="/api/v1/testcalls", tags=["test calls"])


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


@router.get("/readiness", response_model=TestCallReadinessOut)
async def readiness():
    """What the page needs to know before offering a button that spends money."""
    return TestCallReadinessOut(
        enabled=settings.testcall_enabled,
        missing=runner.missing_configuration(),
        running=runner.is_running(),
        max_duration_seconds=settings.testcall_max_duration_seconds,
        ttl_hours=settings.testcall_ttl_hours,
        realtime_model=settings.testcall_realtime_model,
    )


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


@router.get("/scenarios", response_model=list[TestScenarioOut])
async def list_scenarios(session: AsyncSession = Depends(get_session)):
    rows = (
        (await session.execute(select(TestScenario).order_by(TestScenario.id))).scalars().all()
    )
    return [TestScenarioOut.model_validate(r) for r in rows]


@router.post(
    "/scenarios",
    response_model=TestScenarioOut,
    status_code=201,
    dependencies=[Depends(require_api_key)],
)
async def create_scenario(payload: TestScenarioIn, session: AsyncSession = Depends(get_session)):
    scenario = TestScenario(**payload.model_dump())
    session.add(scenario)
    await session.commit()
    await session.refresh(scenario)
    return TestScenarioOut.model_validate(scenario)


@router.put(
    "/scenarios/{scenario_id}",
    response_model=TestScenarioOut,
    dependencies=[Depends(require_api_key)],
)
async def update_scenario(
    scenario_id: int, payload: TestScenarioIn, session: AsyncSession = Depends(get_session)
):
    scenario = await session.get(TestScenario, scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="Scenario not found")
    for field, value in payload.model_dump().items():
        setattr(scenario, field, value)
    await session.commit()
    await session.refresh(scenario)
    return TestScenarioOut.model_validate(scenario)


@router.delete(
    "/scenarios/{scenario_id}", status_code=204, dependencies=[Depends(require_api_key)]
)
async def delete_scenario(scenario_id: int, session: AsyncSession = Depends(get_session)):
    scenario = await session.get(TestScenario, scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="Scenario not found")
    # Runs keep their own copy of the name and number and their scenario_id goes NULL,
    # so deleting a scenario loses the recipe, never the history of what it found.
    await session.delete(scenario)
    await session.commit()


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@router.get("/runs", response_model=list[TestRunOut])
async def list_runs(
    limit: int = Query(default=25, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    rows = (
        (
            await session.execute(
                select(TestRun).order_by(TestRun.created_at.desc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [TestRunOut.model_validate(r) for r in rows]


@router.get("/runs/{run_id}", response_model=TestRunOut)
async def get_run(run_id: str, session: AsyncSession = Depends(get_session)):
    run = await session.get(TestRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return TestRunOut.model_validate(run)


@router.post(
    "/scenarios/{scenario_id}/run",
    response_model=TestRunOut,
    status_code=201,
    dependencies=[Depends(require_api_key)],
)
async def start_run(scenario_id: int, session: AsyncSession = Depends(get_session)):
    """Place the call. Returns as soon as it is ringing; the rest is watched by polling.

    This spends money on both sides — our telephony and Realtime audio, and the
    customer's speech, language model and database lookups — so it is manual only.
    Nothing here is scheduled.
    """
    scenario = await session.get(TestScenario, scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="Scenario not found")
    if not scenario.enabled:
        raise HTTPException(status_code=400, detail="This scenario is disabled.")
    try:
        run = await runner.start_run(session, scenario)
    except runner.TestCallError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return TestRunOut.model_validate(run)


@router.post(
    "/runs/{run_id}/cancel", response_model=TestRunOut, dependencies=[Depends(require_api_key)]
)
async def cancel_run(run_id: str, session: AsyncSession = Depends(get_session)):
    """Hang up now. The stop button for a call that has gone somewhere unwanted."""
    run = await session.get(TestRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    await runner.cancel_run(run)
    await session.commit()
    await session.refresh(run)
    return TestRunOut.model_validate(run)


# ---------------------------------------------------------------------------
# The audio socket — this is the one endpoint Twilio reaches from the internet
# ---------------------------------------------------------------------------


@router.websocket("/stream/{run_id}")
async def stream(websocket: WebSocket, run_id: str):
    """Twilio connects here when the call is answered and the digits have been sent.

    Note what is checked *where*, and why it cannot be otherwise. The run's existence
    and state are checked before accepting, so a replayed or stale connection is
    refused outright. **The token cannot be**: Twilio drops query strings from the
    stream URL, so it arrives inside the ``start`` message, which only exists after the
    socket is accepted. The bridge checks it there, before opening anything that costs
    money.
    """
    run_data = await _open_stream(run_id)
    if run_data is None:
        await websocket.close(code=1008)
        return
    instructions, max_seconds, expected_token = run_data

    await websocket.accept()
    try:
        result = await run_bridge(
            websocket,
            instructions=instructions,
            max_duration_seconds=max_seconds,
            expected_token=expected_token,
            voice=settings.testcall_realtime_voice,
        )
    except Exception as exc:  # noqa: BLE001 - the run must always be closed out
        logger.exception("Test call bridge crashed for run %s", run_id)
        result = BridgeResult(error=f"Bridge error: {exc}")
    finally:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass

    # Matching waits on the agent's own ingestion, which happens after teardown, so this
    # deliberately outlives the socket.
    asyncio.create_task(runner.finish_run(run_id, result))


async def _open_stream(run_id: str) -> tuple[str, int, str] | None:
    """Everything that can be decided before the socket is accepted.

    Returns the caller's instructions, the duration cap and the token the bridge must
    see, or None to refuse. Moving the run off "dialing" here is what makes the socket
    single-use: a second connection for the same run finds the wrong status and is
    turned away.
    """
    async with SessionLocal() as session:
        run = await session.get(TestRun, run_id)
        if run is None or not run.stream_token:
            return None
        if run.status != "dialing":
            logger.warning("Rejected test call stream for %s: status is %s", run_id, run.status)
            return None
        scenario = await session.get(TestScenario, run.scenario_id) if run.scenario_id else None
        if scenario is None:
            return None
        run.status = "talking"
        run.answered_at = utcnow()
        token = run.stream_token
        await session.commit()
        return (
            runner.build_instructions(scenario),
            scenario.max_duration_seconds or settings.testcall_max_duration_seconds,
            token,
        )

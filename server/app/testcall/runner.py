"""The life of one test call: dial, talk, find the agent's own record, judge, clean up.

The judging half deliberately reuses what already exists. CallHarness classifies every
call, renders transcripts with tool calls inlined as evidence, and talks to an LLM in
JSON — none of that is rebuilt here. What is new is only the part nothing else owns:
causing a call to happen, and deciding whether *this* scenario's expectations held.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..analysis.engine import build_transcript
from ..analysis.llm import chat_json
from ..config import settings
from ..db import SessionLocal
from ..models import Call, TestRun, TestScenario, utcnow
from ..storage import delete_recording
from . import twilio
from .bridge import BridgeResult

logger = logging.getLogger("callharness.testcall.runner")

# One call at a time, process-wide. Not a convenience: every test call occupies a line
# on the customer's production agent and spends their lookup budget, and two at once
# would also race for the same unmatched call row — both would match whichever call
# landed first.
#
# A plain lock is the wrong shape here, because the claim has to survive across three
# separate requests (dial, then Twilio's websocket, then the background judging) and be
# releasable by any of them. Hence an explicit claim with a staleness escape: a crash
# between dial and stream must not wedge the feature until the process restarts.
_active: dict = {"run_id": None, "since": 0.0}
_STALE_AFTER_SECONDS = 900.0


def is_running() -> bool:
    return _current_claim() is not None


def _current_claim() -> str | None:
    run_id = _active["run_id"]
    if run_id is None:
        return None
    if time.monotonic() - _active["since"] > _STALE_AFTER_SECONDS:
        logger.warning("Clearing stale test-call claim for run %s", run_id)
        _active["run_id"] = None
        return None
    return run_id


def _claim(run_id: str) -> None:
    _active["run_id"] = run_id
    _active["since"] = time.monotonic()


def _release(run_id: str) -> None:
    if _active["run_id"] == run_id:
        _active["run_id"] = None

# How long to keep looking for the row the agent posts. The SDK flushes at teardown, so
# it normally lands within seconds — but the agent finishes its own analysis first, and
# a slow region has been seen taking most of a minute.
MATCH_TIMEOUT_SECONDS = 120
MATCH_POLL_SECONDS = 5

# How far either side of the dial the matching window reaches. Wide enough to survive
# clock skew between two VMs, narrow enough that a genuine caller dialling in the same
# minute is not mistaken for ours — and the nearest-start tiebreak settles that anyway.
MATCH_BEFORE = timedelta(seconds=120)
MATCH_AFTER = timedelta(seconds=120)

# How long a run may sit on "dialing" before it is declared dead. Twilio gives up
# ringing at 25s and the menu pauses add a few more, so 60s means a real connection has
# had every chance while a failure is reported in under a minute instead of hanging.
DIAL_WATCHDOG_SECONDS = 60.0

JUDGE_SYSTEM_PROMPT = """You are grading one automated test call made against a live \
voice agent. You are given the criteria the call had to satisfy and a transcript.

Judge every criterion against the transcript, then give one overall verdict: the run \
passes only if EVERY criterion is met. Be strict and literal — a criterion that the \
transcript does not clearly show is met has not been met. Do not reward good intentions \
or partial answers.

Lines beginning "[tool call: ...]" are the agent's own lookups against its database and \
are ground truth: an answer with no tool call behind it was invented, however confident \
it sounds.

Respond with JSON only:
{"passed": true|false, "reason": "one or two sentences", \
"criteria": [{"criterion": "<verbatim>", "passed": true|false, "note": "short"}]}"""


class TestCallError(RuntimeError):
    """Something stopped the call being placed. The message is shown to the user."""


def build_instructions(scenario: TestScenario) -> str:
    """The scenario's persona, plus the rules that keep a robot caller well behaved.

    The rules are appended rather than left to whoever writes the persona, because
    forgetting one of them has consequences on somebody else's phone system: a caller
    that asks for a human occupies an operator, and one that will not stop talking
    spends the customer's money for as long as it rambles.
    """
    return (
        f"{scenario.persona.strip()}\n\n"
        "--- Rules for this call (these override anything above) ---\n"
        "You are a synthetic caller placing a TEST call to a live customer-service "
        "phone line. Speak naturally, like a person on the phone: short sentences, "
        "one question at a time, and wait for the answer before continuing.\n"
        "YOU PLACED THIS CALL. You are the caller and they are the service. Never "
        "offer help, never ask how you can help, never behave like the receptionist, "
        "and do not repeat their greeting back at them.\n"
        "Never ask to be put through to a human, an operator, or a member of staff.\n"
        "Never book, cancel, confirm or change any appointment, and never give real "
        "personal data. If you are asked for a name, invent an obvious test one.\n"
        "ENDING THE CALL: never call end_call before you have asked your question AND "
        "heard a complete answer — or been told plainly that they cannot answer it. "
        "Hanging up early wastes the whole test. Then, and only then, say one short "
        "goodbye and immediately call end_call. Say goodbye ONCE: do not thank them "
        "again, do not answer their goodbye, and do not wait for them to hang up. They "
        "never will, and the call is paid for by the second.\n"
        "If they are still speaking, or you have only heard part of the answer, keep "
        "listening. Silence from them is not the end of the call.\n"
        "Speak the same language the other side is speaking."
    )


async def start_run(session: AsyncSession, scenario: TestScenario) -> TestRun:
    """Create the run and dial. Returns as soon as the call is ringing."""
    if not settings.testcall_enabled:
        raise TestCallError(missing_configuration() or "Test calling is not configured.")
    if is_running():
        raise TestCallError("A test call is already running. Only one runs at a time.")

    run = TestRun(
        scenario_id=scenario.id,
        scenario_name=scenario.name,
        agent_id=scenario.agent_id,
        to_number=scenario.to_number,
        status="dialing",
        stream_token=secrets.token_urlsafe(24),
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    _claim(run.id)

    # No query string on this URL: Twilio's <Stream> drops them silently, so the token
    # travels as a <Parameter> instead and is checked from the start message.
    stream_url = f"{settings.testcall_stream_url.rstrip('/')}/{run.id}"
    twiml = twilio.build_twiml(
        stream_url, run.stream_token, scenario.dtmf_digits, scenario.dtmf_pause_seconds
    )
    try:
        run.provider_call_sid = await twilio.place_call(scenario.to_number, twiml)
    except Exception as exc:  # noqa: BLE001
        _release(run.id)
        run.status = "failed"
        run.verdict = "error"
        run.error = str(exc)[:2000]
        run.ended_at = utcnow()
        await session.commit()
        raise TestCallError(str(exc)) from exc

    await session.commit()
    await session.refresh(run)
    asyncio.create_task(_watch_dial(run.id))
    return run


async def _watch_dial(run_id: str) -> None:
    """Close out a run whose audio stream never arrived.

    Without this the run sits on "dialing" until the stale-claim timeout, which is what
    happened on the first live call: the token was being passed in the stream URL,
    Twilio dropped it, our own check refused the socket — and the page showed a call
    apparently still ringing for fifteen minutes. Nothing polls Twilio during a call, so
    a failure that happens *instead* of streaming has no other way to be noticed.

    Twilio is asked what became of the call so the message says something useful:
    "completed" with no stream means the connection was refused or unreachable, while
    "no-answer" or "busy" is a fact about the number.
    """
    await asyncio.sleep(DIAL_WATCHDOG_SECONDS)
    async with SessionLocal() as session:
        run = await session.get(TestRun, run_id)
        if run is None or run.status != "dialing":
            return  # streaming started, or somebody cancelled: not our business
        twilio_status = await twilio.call_status(run.provider_call_sid or "")
        run.status = "failed"
        run.verdict = "error"
        run.error = (
            f"The call never streamed any audio. Twilio reports its status as "
            f"'{twilio_status or 'unknown'}'."
        )
        run.verdict_reason = run.error
        run.ended_at = utcnow()
        await session.commit()
    _release(run_id)
    logger.warning("Test run %s closed by the dial watchdog", run_id)


async def finish_run(run_id: str, result: BridgeResult) -> None:
    """Store what the call produced, then match and judge it.

    Runs as a background task once the websocket closes: matching waits on the agent's
    own ingestion, which has not happened yet at the moment the audio stops.
    """
    async with SessionLocal() as session:
        run = await session.get(TestRun, run_id)
        if run is None:
            return
        run.ended_at = utcnow()
        run.duration_seconds = max(0.0, (run.ended_at - run.started_at).total_seconds())
        run.caller_transcript = result.transcript
        run.ended_on_transfer = result.ended_on_transfer
        if result.error:
            run.error = result.error[:2000]
        if run.provider_call_sid:
            await twilio.hangup(run.provider_call_sid)

        if not result.answered:
            # Nobody picked up, or the stream never opened. That is a finding about the
            # phone path, not about how the agent answered — so "error", never "fail".
            run.status = "failed"
            run.verdict = "error"
            run.verdict_reason = run.error or "The call was never answered."
            await session.commit()
            _release(run_id)
            return
        run.status = "talking"
        await session.commit()

    try:
        call = await _await_matching_call(run_id)
    finally:
        # Released before judging, not after: the phone line is free the moment the call
        # ends, and making the next test wait on an LLM would be an odd kind of queue.
        _release(run_id)

    async with SessionLocal() as session:
        run = await session.get(TestRun, run_id)
        if run is None:
            return
        if call is not None:
            run.call_id = call.id
            if settings.testcall_ttl_hours > 0:
                run.call_expires_at = utcnow() + timedelta(hours=settings.testcall_ttl_hours)
            await _mark_call_as_test(session, call.id, run.id, run.call_expires_at)
        try:
            await _judge(session, run, call)
        except Exception as exc:  # noqa: BLE001 - a broken judge is not a failing agent
            logger.warning("Judging test run %s failed: %s", run_id, exc)
            run.verdict = "error"
            run.verdict_reason = f"Could not judge this call: {exc}"[:1000]
        run.status = "completed"
        await session.commit()


async def cancel_run(run: TestRun) -> None:
    """Hang up a run that is still going, and stop waiting on it.

    Leaves a completed run alone: cancelling something that already reached a verdict
    would throw away the result, which is the opposite of what the button means.
    """
    if run.status in ("completed", "failed"):
        return
    if run.provider_call_sid:
        await twilio.hangup(run.provider_call_sid)
    run.status = "failed"
    run.verdict = run.verdict or "error"
    run.verdict_reason = run.verdict_reason or "Cancelled."
    run.ended_at = run.ended_at or utcnow()
    _release(run.id)


async def _await_matching_call(run_id: str) -> Call | None:
    """Find the row the production agent posted for the call we just made.

    Matched on region and time, not on caller number: the agents hash `from_number`
    before sending it, so the number we dialled from is not visible here. Nearest start
    time wins, the same tiebreak `azure_logs.resolve()` uses for the same reason — the
    window can legitimately contain more than one call.

    A row already claimed by another run is never taken, so a burst of tests cannot all
    point at the same call.
    """
    deadline = asyncio.get_event_loop().time() + MATCH_TIMEOUT_SECONDS
    while True:
        async with SessionLocal() as session:
            run = await session.get(TestRun, run_id)
            if run is None:
                return None
            claimed = set(
                (
                    await session.execute(
                        select(TestRun.call_id).where(TestRun.call_id.is_not(None))
                    )
                )
                .scalars()
                .all()
            )
            candidates = (
                (
                    await session.execute(
                        select(Call)
                        .options(selectinload(Call.turns))
                        .where(Call.agent_id == run.agent_id)
                        .where(Call.started_at >= run.started_at - MATCH_BEFORE)
                        .where(Call.started_at <= (run.ended_at or utcnow()) + MATCH_AFTER)
                    )
                )
                .scalars()
                .all()
            )
            fresh = [c for c in candidates if c.id not in claimed]
            if fresh:
                return min(fresh, key=lambda c: abs((c.started_at - run.started_at).total_seconds()))
        if asyncio.get_event_loop().time() >= deadline:
            return None
        await asyncio.sleep(MATCH_POLL_SECONDS)


async def _mark_call_as_test(
    session: AsyncSession, call_id: str, run_id: str, expires_at
) -> None:
    """Stamp the call so the dashboard can label it, and so nobody trusts it later.

    A new dict rather than a mutation: SQLAlchemy does not track changes inside a JSON
    column, so an in-place update would be silently dropped on commit.
    """
    call = await session.get(Call, call_id)
    if call is None:
        return
    meta = dict(call.meta or {})
    meta["test_call"] = True
    meta["test_run_id"] = run_id
    if expires_at is not None:
        meta["test_expires_at"] = expires_at.isoformat()
    call.meta = meta


async def _judge(session: AsyncSession, run: TestRun, call: Call | None) -> None:
    """Score the scenario's criteria against the best transcript available.

    The agent's own record is preferred over what our caller heard, and by some margin:
    it carries the tool calls, which are the difference between "the agent said the
    branch opens at 8" and "the agent looked it up and it said 8".
    """
    scenario = await session.get(TestScenario, run.scenario_id) if run.scenario_id else None
    criteria = list((scenario.criteria if scenario else None) or [])
    if not criteria:
        run.verdict = "pass" if run.caller_transcript else "error"
        run.verdict_reason = (
            "No criteria on this scenario — the call completed but nothing was checked."
            if run.caller_transcript
            else "The call produced no transcript."
        )
        return

    if call is not None:
        source = "the production agent's own record of the call (includes tool calls)"
        transcript = build_transcript(call)
    else:
        source = "our test caller's own recording of the conversation"
        transcript = _render_caller_transcript(run.caller_transcript or [])
    if not transcript.strip():
        run.verdict = "error"
        run.verdict_reason = "Nothing was said on this call."
        return

    listed = "\n".join(f"- {c}" for c in criteria)
    user_prompt = (
        f"Criteria:\n{listed}\n\nTranscript source: {source}\n\nTranscript:\n{transcript}"
    )
    result = await chat_json(JUDGE_SYSTEM_PROMPT, user_prompt)

    passed = result.get("passed")
    run.verdict = "pass" if passed is True else "fail"
    run.verdict_reason = str(result.get("reason") or "")[:2000]
    rows = result.get("criteria")
    run.criteria_results = rows if isinstance(rows, list) else []

    if call is None and run.verdict == "pass":
        # Worth saying out loud: a pass judged only on our side means the agent never
        # posted its call, which is itself a broken integration.
        run.verdict_reason = (
            "Judged from the test caller's transcript only — the agent did not report "
            "this call to CallHarness. " + (run.verdict_reason or "")
        ).strip()[:2000]


def _render_caller_transcript(lines: list[dict]) -> str:
    out = []
    for line in lines:
        speaker = "Agent" if line.get("speaker") == "agent" else "Caller"
        text = (line.get("text") or "").strip()
        if text:
            out.append(f"{speaker}: {text}")
    return "\n".join(out)


async def expire_test_calls() -> int:
    """Delete the production call rows created by test calls whose time is up.

    Deleting rather than flagging is the decision: a synthetic caller asking about a
    record the customer's database lacks would otherwise be filed as a real missing
    record and sent to them as work. A flag would have to be honoured by every analytics
    query, the gaps report, the disputes page and the alert rules, and being forgotten in
    one of them is the failure that matters. The run keeps both transcripts and the
    verdict, so nothing about the test is lost.
    """
    removed = 0
    async with SessionLocal() as session:
        runs = (
            (
                await session.execute(
                    select(TestRun)
                    .where(TestRun.call_id.is_not(None))
                    .where(TestRun.call_deleted == False)  # noqa: E712
                    .where(TestRun.call_expires_at.is_not(None))
                    .where(TestRun.call_expires_at <= utcnow())
                )
            )
            .scalars()
            .all()
        )
        for run in runs:
            call = await session.get(Call, run.call_id)
            if call is not None:
                # The audio file first, and before the row: `_expire_recordings` finds
                # files through `Call.recording_path`, so a row deleted with a recording
                # still attached leaves a WAV on disk that nothing will ever collect.
                if call.recording_path:
                    delete_recording(call.recording_path)
                await session.delete(call)
                removed += 1
            run.call_deleted = True
        if runs:
            await session.commit()
    if removed:
        logger.info("Deleted %d expired test call(s) from the call history", removed)
    return removed


def missing_configuration() -> str | None:
    """Which setting is missing, in the order somebody would fix them."""
    if not (settings.twilio_account_sid and settings.twilio_auth_token):
        return "Twilio credentials are not set (CALLHARNESS_TWILIO_ACCOUNT_SID / _AUTH_TOKEN)."
    if not settings.twilio_from_number:
        return "No number to call from (CALLHARNESS_TWILIO_FROM_NUMBER)."
    if not settings.testcall_stream_url:
        return (
            "No public audio address (CALLHARNESS_TESTCALL_STREAM_URL). Twilio dials from "
            "its own cloud and has to be able to reach this server."
        )
    if not settings.openai_api_key:
        return "OPENAI_API_KEY is not set, so there is no voice to place the call with."
    return None

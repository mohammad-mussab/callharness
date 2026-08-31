import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import String, cast, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .. import azure_logs
from ..analysis.translate import translate_call
from ..auth import require_api_key
from ..config import settings
from ..db import get_session
from ..gap_grouping import GAP_NEEDS_REVIEW
from ..models import Call, GapGroup, Turn
from ..quality import compute_quality
from ..schemas import (
    CallCreate,
    CallDetailOut,
    CallListOut,
    CallOut,
    EvaluationResultOut,
)
from ..storage import content_type_for, save_recording
from ..taxonomy import normalize_key

logger = logging.getLogger("callharness.calls")

router = APIRouter(prefix="/api/v1/calls", tags=["calls"])


def _to_out(call: Call, detail: bool = False) -> CallOut | CallDetailOut:
    cls = CallDetailOut if detail else CallOut
    out = cls.model_validate(call)
    out.has_recording = bool(call.recording_path)
    out.has_log = bool(call.log_blob)
    return out


async def _resolve_log_once(session: AsyncSession, call: Call) -> None:
    """Look for this call's log blob if we never have. Best-effort, never raises.

    The dashboard only renders the log panel when has_log is true, so a call whose
    blob hasn't been located yet would show nothing and never reach the log endpoint.
    Resolving here means a call opened seconds after ingest gets its log on first view
    instead of after the next worker tick. The log_checked_at guard makes this happen
    at most once per call, at the cost of a single folder listing.
    """
    if call.log_blob or call.log_checked_at is not None or not azure_logs.enabled():
        return
    try:
        await azure_logs.resolve(session, [call])
    except Exception as exc:  # noqa: BLE001 - a call detail must render without Azure
        logger.warning("Log lookup for call %s failed: %s", call.id, exc)


@router.post("", response_model=CallOut, status_code=201, dependencies=[Depends(require_api_key)])
async def ingest_call(payload: CallCreate, session: AsyncSession = Depends(get_session)):
    if payload.external_id:
        existing = (
            await session.execute(
                select(Call)
                .options(selectinload(Call.turns))
                .where(Call.external_id == payload.external_id)
            )
        ).scalars().first()
        if existing:
            return _to_out(existing)

    started_at = payload.started_at.replace(tzinfo=None) if payload.started_at else None
    ended_at = payload.ended_at.replace(tzinfo=None) if payload.ended_at else None
    duration = payload.duration_seconds
    if duration is None and started_at and ended_at:
        duration = max(0.0, (ended_at - started_at).total_seconds())

    # An agent that classifies its own calls sends the reason with the call; that is
    # ground truth and analysis won't overwrite it (see engine.apply_result). Only
    # the reason that matches the call's deterministic `transferred` flag is kept —
    # a transferred call can't also have a non-completion reason.
    transfer_reason = (
        normalize_key(payload.transfer_reason)
        if payload.transferred and payload.transfer_reason
        else None
    )
    non_completion_reason = (
        normalize_key(payload.non_completion_reason)
        if not payload.transferred and payload.non_completion_reason
        else None
    )

    call = Call(
        external_id=payload.external_id,
        agent_id=payload.agent_id,
        direction=payload.direction,
        from_number=payload.from_number,
        to_number=payload.to_number,
        ended_at=ended_at,
        duration_seconds=duration,
        transferred=payload.transferred,
        recording_url=payload.recording_url,
        meta=payload.metadata,
        transfer_reason=transfer_reason,
        non_completion_reason=non_completion_reason,
        reason_source="agent" if (transfer_reason or non_completion_reason) else None,
        analysis_status="pending" if payload.turns else "skipped",
    )
    if started_at:
        call.started_at = started_at
    for i, t in enumerate(payload.turns):
        call.turns.append(
            Turn(
                idx=i,
                role=t.role,
                text=t.text,
                start_time=t.start_time,
                end_time=t.end_time,
                latency_ms=t.latency_ms,
                stt_ms=t.stt_ms,
                llm_ttft_ms=t.llm_ttft_ms,
                tts_ttfb_ms=t.tts_ttfb_ms,
                interrupted=t.interrupted,
                tool_calls=[tc.model_dump() for tc in t.tool_calls] if t.tool_calls else None,
            )
        )
    quality = compute_quality(call.turns)
    call.quality = quality
    call.interruption_count = quality["interruption_count"] if quality else 0
    session.add(call)
    await session.commit()
    await session.refresh(call)
    return _to_out(call)


@router.get("", response_model=CallListOut)
async def list_calls(
    session: AsyncSession = Depends(get_session),
    agent_id: str | None = None,
    success: bool | None = None,
    sentiment: str | None = None,
    bucket: str | None = None,
    transfer_reason: str | None = None,
    non_completion_reason: str | None = None,
    outcome: str | None = None,
    analysis_status: str | None = None,
    q: str | None = None,
    date_from: datetime | None = Query(default=None, alias="from"),
    date_to: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, le=200),
    offset: int = 0,
):
    query = select(Call)
    if agent_id:
        query = query.where(Call.agent_id == agent_id)
    if success is not None:
        query = query.where(Call.success == success)
    if sentiment:
        query = query.where(Call.sentiment_label == sentiment)
    if bucket:
        query = query.where(Call.bucket == bucket)
    if transfer_reason:
        query = query.where(Call.transfer_reason == transfer_reason)
    if non_completion_reason:
        query = query.where(Call.non_completion_reason == non_completion_reason)
    if outcome:
        # Mirrors outcome.compute_outcome()'s precedence — kept in sync by hand
        # since it needs to run in SQL here, not Python.
        if outcome == "transferred":
            query = query.where(Call.transferred == True)  # noqa: E712
        elif outcome == "completed":
            query = query.where(
                Call.transferred == False,  # noqa: E712
                Call.success == True,  # noqa: E712
            )
        elif outcome == "non_completed":
            # success False *or* NULL — an unjudged call is not a completed one.
            query = query.where(
                Call.transferred == False,  # noqa: E712
                or_(Call.success == False, Call.success.is_(None)),  # noqa: E712
            )
    if analysis_status:
        query = query.where(Call.analysis_status == analysis_status)
    if date_from:
        query = query.where(Call.started_at >= date_from.replace(tzinfo=None))
    if date_to:
        query = query.where(Call.started_at <= date_to.replace(tzinfo=None))
    if q:
        pattern = f"%{q}%"
        in_transcript = exists(
            select(Turn.id).where(Turn.call_id == Call.id, Turn.text.ilike(pattern))
        )
        # One box, two kinds of input. Free text still searches the summary and the
        # transcript; an *identifier* is matched exactly instead, because somebody
        # holding an id minted by another system has nowhere else to paste it. A call
        # carries at least four ids for the same conversation -- our surrogate `id`
        # (what the URL shows), the agent's own uuid in `external_id`, and whatever the
        # agent stashed in `meta` (Lazio sends its Supabase `tb_stat.id_stat`, an
        # `interaction_id`, a booking code) -- and until this clause existed only the
        # first of them could find a call at all.
        #
        # The meta match is a text scan: `meta` is free-form JSON, so there is no column
        # to compare and no index to use. Matching '"<q>"' *with the quotes* is what
        # keeps it honest -- it hits a whole JSON string value rather than a digit run
        # buried inside some unrelated number, so searching 144394 cannot return a call
        # whose llm_token happens to be 1443940. The corollary is that meta values must
        # be stored as strings to be findable; see scripts/backfill_id_stat.py.
        query = query.where(
            Call.summary.ilike(pattern)
            | in_transcript
            | (Call.id == q)
            | (Call.external_id == q)
            | cast(Call.meta, String).ilike(f'%"{q}"%')
        )

    total = (
        await session.execute(select(func.count()).select_from(query.subquery()))
    ).scalar_one()
    rows = (
        await session.execute(
            query.order_by(Call.started_at.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()
    return CallListOut(
        items=[_to_out(c) for c in rows], total=total, limit=limit, offset=offset
    )


async def _get_call(
    session: AsyncSession,
    call_id: str,
    with_turns: bool = False,
    with_evaluations: bool = False,
) -> Call:
    """Load a call by our id, or failing that by the agent's own `external_id`.

    The fallback exists because the two ids name the same call but only one of them
    appears in a URL, and it is the one nobody outside CallHarness has. Somebody
    tracing a call from the agent's database or its raw log holds the agent's uuid;
    pasting it into /calls/<uuid> used to 404, which reads as "no such call" when the
    call is right there. Our own id is tried first so a real id can never be shadowed
    by an agent that chose to reuse it as its external_id.

    `external_id` is not unique-constrained (it is the sender's value, not ours), so
    the fallback takes the newest match rather than raising on a duplicate -- a
    lookup helper is the wrong place to fail over somebody else's id hygiene.
    """
    def _build(where):
        query = select(Call).where(where)
        if with_turns:
            query = query.options(selectinload(Call.turns))
        if with_evaluations:
            # Eager, not lazy: these relationships default to lazy="select", and an
            # implicit lazy load on an AsyncSession raises MissingGreenlet rather than
            # quietly issuing the query.
            query = query.options(selectinload(Call.evaluation_results))
        return query

    call = (await session.execute(_build(Call.id == call_id))).scalar_one_or_none()
    if call is None:
        call = (
            await session.execute(
                _build(Call.external_id == call_id).order_by(Call.started_at.desc()).limit(1)
            )
        ).scalars().first()
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    return call


@router.get("/{call_id}", response_model=CallDetailOut)
async def get_call(call_id: str, session: AsyncSession = Depends(get_session)):
    call = await _get_call(session, call_id, with_turns=True, with_evaluations=True)
    await _resolve_log_once(session, call)
    out = _to_out(call, detail=True)
    out.evaluations = [
        EvaluationResultOut.model_validate(r) for r in call.evaluation_results
    ]
    # Whether anybody has re-asked the lookup API about this call's missing record.
    # Read from the group it belongs to, not from the call: verification is about the
    # record, and several calls share one. See gap_verification.py.
    if call.gap_group_id and call.gap_group_id != GAP_NEEDS_REVIEW:
        group = await session.get(GapGroup, call.gap_group_id)
        if group is not None:
            out.gap_status = group.status or None
            out.gap_status_note = group.status_note
    return out


@router.post(
    "/{call_id}/recording",
    response_model=CallOut,
    dependencies=[Depends(require_api_key)],
)
async def upload_recording(
    call_id: str, file: UploadFile, session: AsyncSession = Depends(get_session)
):
    call = await _get_call(session, call_id)
    content = await file.read()
    if len(content) > 200 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Recording too large (max 200MB)")
    call.recording_path = save_recording(call_id, file.filename or "audio.wav", content)
    await session.commit()
    await session.refresh(call)
    return _to_out(call)


@router.get("/{call_id}/audio")
async def get_audio(call_id: str, session: AsyncSession = Depends(get_session)):
    call = await _get_call(session, call_id)
    if not call.recording_path:
        raise HTTPException(status_code=404, detail="No recording for this call")
    return FileResponse(call.recording_path, media_type=content_type_for(call.recording_path))


@router.get("/{call_id}/log")
async def get_log(
    call_id: str, download: bool = False, session: AsyncSession = Depends(get_session)
):
    """The agent's raw log for this call, streamed out of Azure.

    Unauthenticated, like /audio and every other GET here — the protection is the API
    being bound to a private IP (see docker-compose.colocated.yml). Worth naming what
    that means for this route specifically: these logs are the most PII-dense thing
    CallHarness serves. They carry plaintext patient names, fiscal codes, dates of
    birth and raw caller numbers, where the call row itself only ever holds a hashed
    from_number. Do not put this port on a public interface.
    """
    call = await _get_call(session, call_id)
    await _resolve_log_once(session, call)
    if not call.log_blob:
        # Distinguished from the 404 below so the dashboard can say which happened.
        raise HTTPException(status_code=404, detail="No log linked to this call")

    try:
        content = await azure_logs.fetch_log(call.log_blob)
    except azure_logs.LogUnavailable as exc:
        # Storage is misconfigured or unreachable — a server-side problem, and a
        # different thing from the log not existing. 502 so it can't be mistaken for
        # one, with the reason in the body so the panel can show it.
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if content is None:
        raise HTTPException(status_code=404, detail="Log is no longer available in Azure")

    headers = {}
    if download:
        headers["Content-Disposition"] = (
            f'attachment; filename="{call.log_blob.rsplit("/", 1)[-1]}"'
        )
    return Response(content, media_type="text/plain; charset=utf-8", headers=headers)


@router.post(
    "/{call_id}/reanalyze",
    response_model=CallOut,
    dependencies=[Depends(require_api_key)],
)
async def reanalyze(call_id: str, session: AsyncSession = Depends(get_session)):
    call = await _get_call(session, call_id)
    call.analysis_status = "pending"
    call.analysis_error = None
    await session.commit()
    await session.refresh(call)
    return _to_out(call)


@router.post(
    "/{call_id}/translate",
    response_model=CallDetailOut,
    dependencies=[Depends(require_api_key)],
)
async def translate(
    call_id: str,
    session: AsyncSession = Depends(get_session),
    language: str = Query(default="english", max_length=32),
    force: bool = False,
):
    """Translate the transcript to the target language (cached after first run)."""
    call = await _get_call(session, call_id, with_turns=True)
    if settings.resolved_provider == "none":
        raise HTTPException(
            status_code=400,
            detail="No LLM provider configured — translation needs an LLM key",
        )
    already_translated = all(t.translated_text for t in call.turns if t.text.strip())
    if not already_translated or force:
        try:
            await translate_call(call, target_language=language)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Translation failed: {exc}")
        await session.commit()
        await session.refresh(call)
    return _to_out(call, detail=True)


@router.delete("/{call_id}", status_code=204, dependencies=[Depends(require_api_key)])
async def delete_call(call_id: str, session: AsyncSession = Depends(get_session)):
    call = await _get_call(session, call_id)
    await session.delete(call)
    await session.commit()

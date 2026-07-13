from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..analysis.translate import translate_call
from ..auth import require_api_key
from ..config import settings
from ..db import get_session
from ..models import Call, Turn
from ..quality import compute_quality
from ..schemas import (
    CallCreate,
    CallDetailOut,
    CallListOut,
    CallOut,
    EvaluationResultOut,
)
from ..storage import content_type_for, save_recording

router = APIRouter(prefix="/api/v1/calls", tags=["calls"])


def _to_out(call: Call, detail: bool = False) -> CallOut | CallDetailOut:
    cls = CallDetailOut if detail else CallOut
    out = cls.model_validate(call)
    out.has_recording = bool(call.recording_path)
    return out


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

    call = Call(
        external_id=payload.external_id,
        agent_id=payload.agent_id,
        direction=payload.direction,
        from_number=payload.from_number,
        to_number=payload.to_number,
        ended_at=ended_at,
        duration_seconds=duration,
        end_reason=payload.end_reason,
        transferred=payload.transferred,
        recording_url=payload.recording_url,
        meta=payload.metadata,
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
    end_reason: str | None = None,
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
    if end_reason:
        query = query.where(Call.end_reason == end_reason)
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
        query = query.where(Call.summary.ilike(pattern) | in_transcript)

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


async def _get_call(session: AsyncSession, call_id: str, with_turns: bool = False) -> Call:
    query = select(Call).where(Call.id == call_id)
    if with_turns:
        query = query.options(selectinload(Call.turns))
    call = (await session.execute(query)).scalar_one_or_none()
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    return call


@router.get("/{call_id}", response_model=CallDetailOut)
async def get_call(call_id: str, session: AsyncSession = Depends(get_session)):
    query = (
        select(Call)
        .where(Call.id == call_id)
        .options(selectinload(Call.turns), selectinload(Call.evaluation_results))
    )
    call = (await session.execute(query)).scalar_one_or_none()
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    out = _to_out(call, detail=True)
    out.evaluations = [
        EvaluationResultOut.model_validate(r) for r in call.evaluation_results
    ]
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

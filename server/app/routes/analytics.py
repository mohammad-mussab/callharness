import math
from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Call, Turn, utcnow
from ..schemas import LatencyOut, OverviewOut, TimeseriesPoint


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return round(s[f], 1)
    return round(s[f] + (s[c] - s[f]) * (k - f), 1)


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])


def _range_filter(query, date_from: datetime | None, date_to: datetime | None):
    if date_from:
        query = query.where(Call.started_at >= date_from.replace(tzinfo=None))
    if date_to:
        query = query.where(Call.started_at <= date_to.replace(tzinfo=None))
    return query


@router.get("/overview", response_model=OverviewOut)
async def overview(
    session: AsyncSession = Depends(get_session),
    agent_id: str | None = None,
    date_from: datetime | None = Query(default=None, alias="from"),
    date_to: datetime | None = Query(default=None, alias="to"),
):
    query = select(
        Call.duration_seconds,
        Call.success,
        Call.sentiment_label,
        Call.sentiment_score,
        Call.transferred,
        Call.end_reason,
        Call.analysis_status,
    )
    if agent_id:
        query = query.where(Call.agent_id == agent_id)
    query = _range_filter(query, date_from, date_to)
    rows = (await session.execute(query)).all()

    total = len(rows)
    analyzed = sum(1 for r in rows if r.analysis_status == "completed")
    with_success = [r for r in rows if r.success is not None]
    durations = [r.duration_seconds for r in rows if r.duration_seconds is not None]
    sentiments = [r.sentiment_score for r in rows if r.sentiment_score is not None]

    sentiment_dist = {"positive": 0, "neutral": 0, "negative": 0}
    for r in rows:
        if r.sentiment_label in sentiment_dist:
            sentiment_dist[r.sentiment_label] += 1

    reasons: dict[str, int] = defaultdict(int)
    for r in rows:
        reasons[r.end_reason or "unknown"] += 1
    reason_breakdown = sorted(
        ({"reason": k, "count": v} for k, v in reasons.items()),
        key=lambda x: -x["count"],
    )

    agents = (
        (await session.execute(select(Call.agent_id).distinct().order_by(Call.agent_id)))
        .scalars()
        .all()
    )

    return OverviewOut(
        total_calls=total,
        analyzed_calls=analyzed,
        success_rate=(
            sum(1 for r in with_success if r.success) / len(with_success)
            if with_success
            else None
        ),
        transfer_rate=(sum(1 for r in rows if r.transferred) / total if total else None),
        avg_duration_seconds=(sum(durations) / len(durations) if durations else None),
        avg_sentiment_score=(sum(sentiments) / len(sentiments) if sentiments else None),
        sentiment_distribution=sentiment_dist,
        end_reason_breakdown=reason_breakdown,
        agents=list(agents),
    )


@router.get("/latency", response_model=LatencyOut)
async def latency(
    session: AsyncSession = Depends(get_session),
    days: int = Query(default=14, ge=1, le=90),
    agent_id: str | None = None,
):
    since = utcnow() - timedelta(days=days)

    turn_query = (
        select(
            Turn.latency_ms,
            Turn.stt_ms,
            Turn.llm_ttft_ms,
            Turn.tts_ttfb_ms,
            Call.started_at,
        )
        .join(Call, Turn.call_id == Call.id)
        .where(Turn.role == "assistant", Call.started_at >= since)
    )
    if agent_id:
        turn_query = turn_query.where(Call.agent_id == agent_id)
    turn_rows = (await session.execute(turn_query)).all()

    e2e = [r.latency_ms for r in turn_rows if r.latency_ms is not None]
    components = {}
    for key, attr in (("stt", "stt_ms"), ("llm", "llm_ttft_ms"), ("tts", "tts_ttfb_ms")):
        vals = [getattr(r, attr) for r in turn_rows if getattr(r, attr) is not None]
        components[key] = {
            "avg": _avg(vals),
            "p50": percentile(vals, 50),
            "p95": percentile(vals, 95),
        }

    # Daily p50/p95 of end-to-end response latency
    buckets: dict[str, list[float]] = defaultdict(list)
    for r in turn_rows:
        if r.latency_ms is not None:
            buckets[r.started_at.strftime("%Y-%m-%d")].append(r.latency_ms)
    day0 = (utcnow() - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    daily = []
    for i in range(days):
        day = (day0 + timedelta(days=i)).strftime("%Y-%m-%d")
        vals = buckets.get(day, [])
        daily.append(
            {
                "date": day,
                "p50": percentile(vals, 50),
                "p95": percentile(vals, 95),
                "count": len(vals),
            }
        )

    # Conversation-quality aggregates over calls in range
    call_query = select(Call.quality, Call.interruption_count).where(Call.started_at >= since)
    if agent_id:
        call_query = call_query.where(Call.agent_id == agent_id)
    call_rows = (await session.execute(call_query)).all()
    qualities = [r.quality for r in call_rows if r.quality]
    n_calls = len(call_rows)
    long_silence_calls = sum(1 for q in qualities if (q.get("long_silence_count") or 0) > 0)
    talk_ratios = [q["talk_ratio"] for q in qualities if q.get("talk_ratio") is not None]
    wpms = [q["assistant_wpm"] for q in qualities if q.get("assistant_wpm") is not None]
    quality_agg = {
        "calls": float(n_calls),
        "avg_interruptions": (
            round(sum(r.interruption_count or 0 for r in call_rows) / n_calls, 2)
            if n_calls
            else None
        ),
        "pct_calls_with_long_silence": (
            round(long_silence_calls / len(qualities), 3) if qualities else None
        ),
        "avg_talk_ratio": (round(sum(talk_ratios) / len(talk_ratios), 2) if talk_ratios else None),
        "avg_assistant_wpm": (round(sum(wpms) / len(wpms), 0) if wpms else None),
    }

    return LatencyOut(
        turn_count=len(turn_rows),
        e2e={
            "avg": _avg(e2e),
            "p50": percentile(e2e, 50),
            "p95": percentile(e2e, 95),
            "p99": percentile(e2e, 99),
        },
        components=components,
        daily=daily,
        quality=quality_agg,
    )


@router.get("/timeseries", response_model=list[TimeseriesPoint])
async def timeseries(
    session: AsyncSession = Depends(get_session),
    days: int = Query(default=14, ge=1, le=90),
    agent_id: str | None = None,
):
    since = (utcnow() - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    query = select(
        Call.started_at, Call.success, Call.sentiment_score, Call.duration_seconds
    ).where(Call.started_at >= since)
    if agent_id:
        query = query.where(Call.agent_id == agent_id)
    rows = (await session.execute(query)).all()

    buckets: dict[str, list] = defaultdict(list)
    for r in rows:
        buckets[r.started_at.strftime("%Y-%m-%d")].append(r)

    points: list[TimeseriesPoint] = []
    for i in range(days):
        day = (since + timedelta(days=i)).strftime("%Y-%m-%d")
        day_rows = buckets.get(day, [])
        with_success = [r for r in day_rows if r.success is not None]
        sentiments = [r.sentiment_score for r in day_rows if r.sentiment_score is not None]
        durations = [r.duration_seconds for r in day_rows if r.duration_seconds is not None]
        points.append(
            TimeseriesPoint(
                date=day,
                calls=len(day_rows),
                success_rate=(
                    sum(1 for r in with_success if r.success) / len(with_success)
                    if with_success
                    else None
                ),
                avg_sentiment=(sum(sentiments) / len(sentiments) if sentiments else None),
                avg_duration_seconds=(
                    sum(durations) / len(durations) if durations else None
                ),
            )
        )
    return points

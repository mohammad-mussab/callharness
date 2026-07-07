from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Call, utcnow
from ..schemas import OverviewOut, TimeseriesPoint

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

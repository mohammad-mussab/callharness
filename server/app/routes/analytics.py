import math
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..disputes import (
    AGREED,
    OUTCOME_DISPUTE,
    REASON_DISPUTE,
    agent_outcome,
    classify,
    is_overcount,
)
from ..models import Call, Turn, utcnow
from ..outcome import compute_outcome
from ..schemas import DisputedCallOut, DisputesOut, LatencyOut, OverviewOut, TimeseriesPoint


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


@router.get("/disputes", response_model=DisputesOut)
async def disputes(
    session: AsyncSession = Depends(get_session),
    agent_id: str | None = None,
    kind: str | None = Query(default=None, pattern="^(outcome|reason)$"),
    date_from: datetime | None = Query(default=None, alias="from"),
    date_to: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, le=200),
):
    """Where the agent's own verdict and CallHarness's analysis disagree.

    Only calls carrying an `agent_esito` in metadata AND a finished analysis are
    comparable; everything else is excluded rather than counted as agreement, so the
    agreement rate never flatters itself with calls nobody judged.
    """
    query = select(Call).where(Call.analysis_status == "completed")
    if agent_id:
        query = query.where(Call.agent_id == agent_id)
    query = _range_filter(query, date_from, date_to)
    rows = (await session.execute(query.order_by(Call.started_at.desc()))).scalars().all()

    counts = {AGREED: 0, OUTCOME_DISPUTE: 0, REASON_DISPUTE: 0}
    overcounted = 0
    matrix: dict[tuple[str, str], int] = defaultdict(int)
    disputed: list[tuple[Call, str, str]] = []  # (call, kind, callharness_outcome)

    for call in rows:
        oc_outcome = compute_outcome(call.success, call.transferred, call.end_reason)
        oc_reason = call.transfer_reason or call.non_completion_reason
        verdict = classify(
            meta=call.meta, callharness_outcome=oc_outcome, callharness_reason=oc_reason
        )
        if verdict is None:
            continue  # agent sent no verdict — nothing to compare

        counts[verdict] += 1
        matrix[(agent_outcome(call.meta) or "unknown", oc_outcome)] += 1
        if verdict == AGREED:
            continue
        if is_overcount(call.meta, oc_outcome):
            overcounted += 1
        if kind is None or verdict == kind:
            disputed.append((call, verdict, oc_outcome))

    comparable = sum(counts.values())

    # Load turns only for the page being returned — the failed-tool-call evidence is
    # the most useful column here, but fetching turns for every call in the window
    # would make this endpoint scale with total volume instead of with disputes.
    page = disputed[:limit]
    failures: dict[str, list[str]] = {}
    if page:
        turn_rows = (
            await session.execute(
                select(Turn.call_id, Turn.tool_calls).where(
                    Turn.call_id.in_([c.id for c, _, _ in page]),
                    Turn.tool_calls.is_not(None),
                )
            )
        ).all()
        for call_id, tool_calls in turn_rows:
            for tc in tool_calls or []:
                if isinstance(tc, dict) and tc.get("success") is False:
                    failures.setdefault(call_id, []).append(tc.get("name") or "unknown")

    items = [
        DisputedCallOut(
            id=call.id,
            started_at=call.started_at,
            agent_id=call.agent_id,
            duration_seconds=call.duration_seconds,
            kind=verdict,
            overcount=is_overcount(call.meta, oc_outcome),
            agent_esito=(call.meta or {}).get("agent_esito"),
            agent_motivazione=(call.meta or {}).get("agent_motivazione"),
            callharness_outcome=oc_outcome,
            callharness_reason=call.transfer_reason or call.non_completion_reason,
            summary=call.summary,
            success_rationale=call.success_rationale,
            failed_tool_calls=failures.get(call.id, []),
        )
        for call, verdict, oc_outcome in page
    ]

    return DisputesOut(
        comparable=comparable,
        agreed=counts[AGREED],
        disputed_outcome=counts[OUTCOME_DISPUTE],
        disputed_reason=counts[REASON_DISPUTE],
        overcounted=overcounted,
        agreement_rate=(counts[AGREED] / comparable if comparable else None),
        matrix=sorted(
            ({"agent": a, "callharness": o, "count": c} for (a, o), c in matrix.items()),
            key=lambda x: -x["count"],
        ),
        items=items,
    )


@router.get("/overview", response_model=OverviewOut)
async def overview(
    session: AsyncSession = Depends(get_session),
    agent_id: str | None = None,
    date_from: datetime | None = Query(default=None, alias="from"),
    date_to: datetime | None = Query(default=None, alias="to"),
):
    query = select(
        Call.agent_id,
        Call.duration_seconds,
        Call.success,
        Call.sentiment_label,
        Call.sentiment_score,
        Call.transferred,
        Call.end_reason,
        Call.transfer_reason,
        Call.non_completion_reason,
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

    outcome_dist = {"transferred": 0, "completed": 0, "non_completed": 0}
    for r in rows:
        outcome_dist[compute_outcome(r.success, r.transferred, r.end_reason)] += 1

    reasons: dict[str, int] = defaultdict(int)
    for r in rows:
        reasons[r.end_reason or "unknown"] += 1
    reason_breakdown = sorted(
        ({"reason": k, "count": v} for k, v in reasons.items()),
        key=lambda x: -x["count"],
    )

    def _breakdown(values: list[str | None]) -> list[dict[str, Any]]:
        counts: dict[str, int] = defaultdict(int)
        for v in values:
            if v:
                counts[v] += 1
        return sorted(
            ({"reason": k, "count": v} for k, v in counts.items()), key=lambda x: -x["count"]
        )

    transfer_reason_breakdown = _breakdown([r.transfer_reason for r in rows])
    non_completion_reason_breakdown = _breakdown([r.non_completion_reason for r in rows])

    agents = (
        (await session.execute(select(Call.agent_id).distinct().order_by(Call.agent_id)))
        .scalars()
        .all()
    )

    # Per-agent (per-region) comparison over the same filtered rows
    by_agent: dict[str, list] = defaultdict(list)
    for r in rows:
        by_agent[r.agent_id].append(r)
    agent_stats = []
    for name in sorted(by_agent):
        agent_rows = by_agent[name]
        agent_success = [r for r in agent_rows if r.success is not None]
        agent_sentiments = [
            r.sentiment_score for r in agent_rows if r.sentiment_score is not None
        ]
        agent_durations = [
            r.duration_seconds for r in agent_rows if r.duration_seconds is not None
        ]
        agent_stats.append(
            {
                "agent_id": name,
                "calls": len(agent_rows),
                "success_rate": (
                    round(sum(1 for r in agent_success if r.success) / len(agent_success), 3)
                    if agent_success
                    else None
                ),
                "avg_sentiment": (
                    round(sum(agent_sentiments) / len(agent_sentiments), 2)
                    if agent_sentiments
                    else None
                ),
                "avg_duration_seconds": (
                    round(sum(agent_durations) / len(agent_durations), 1)
                    if agent_durations
                    else None
                ),
                "transfer_rate": (
                    round(sum(1 for r in agent_rows if r.transferred) / len(agent_rows), 3)
                    if agent_rows
                    else None
                ),
            }
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
        outcome_distribution=outcome_dist,
        end_reason_breakdown=reason_breakdown,
        transfer_reason_breakdown=transfer_reason_breakdown,
        non_completion_reason_breakdown=non_completion_reason_breakdown,
        agents=list(agents),
        agent_stats=agent_stats,
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

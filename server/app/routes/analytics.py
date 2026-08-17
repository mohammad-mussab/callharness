import asyncio
import math
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..buckets import ANSWERED_BUCKET, NOT_ADDRESSABLE
from ..db import get_session
from ..disputes import (
    AGREED,
    OUTCOME_DISPUTE,
    REASON_DISPUTE,
    agent_outcome,
    classify,
    is_overcount,
)
from ..analysis.engine import non_completion_categories, transfer_categories
from ..analysis.worker import get_or_create_config
from ..auth import require_api_key
from .. import gap_verification as gv
from ..gap_grouping import GAP_NEEDS_REVIEW, group_gaps
from ..knowledge_gaps import RECORD_MISSING_BUCKET, extract_gaps
from ..models import Call, GapGroup, GapVerification, Turn, utcnow
from ..outcome import compute_outcome
from ..schemas import (
    BucketsOut,
    DisputedCallOut,
    DisputesOut,
    GapExampleOut,
    GapGroupingOut,
    GapUngroupOut,
    KnowledgeGapOut,
    KnowledgeGapsOut,
    LatencyOut,
    OverviewOut,
    TimeseriesPoint,
)


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


def answer_rates(buckets: list[str | None]) -> tuple[float | None, float | None]:
    """(raw, addressable) share of calls where the caller got what they asked for.

    Unbucketed calls (not analysed yet, or analysis disabled) are excluded from both
    denominators — counting them would make the rate a measure of how much analysis has
    run rather than of how the agent performed.

    The two differ only in the denominator. `addressable` drops the calls no amount of
    data or agent work could have rescued — a request that genuinely needs a person, one
    outside the agent's remit, and a call with no caller on it — so it answers "of the
    calls we could have won, how many did we". `partial_answered` stays in both
    denominators and out of both numerators: a caller who got two of three answers did
    not get what they asked for.
    """
    bucketed = [b for b in buckets if b]
    if not bucketed:
        return None, None
    answered = sum(1 for b in bucketed if b == ANSWERED_BUCKET)
    addressable = [b for b in bucketed if b not in NOT_ADDRESSABLE]
    return (
        round(answered / len(bucketed), 4),
        round(answered / len(addressable), 4) if addressable else None,
    )


router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])


def _range_filter(query, date_from: datetime | None, date_to: datetime | None):
    if date_from:
        query = query.where(Call.started_at >= date_from.replace(tzinfo=None))
    if date_to:
        query = query.where(Call.started_at <= date_to.replace(tzinfo=None))
    return query


@router.get("/knowledge-gaps", response_model=KnowledgeGapsOut)
async def knowledge_gaps(
    session: AsyncSession = Depends(get_session),
    agent_id: str | None = None,
    days: int = Query(default=7, ge=1, le=365),
    min_count: int = Query(default=1, ge=1),
    # Ungrouped, every gap-hitting call is its own row — 211 of them over the live Lazio
    # history at the time of writing, against the 50 this used to default to. A cap below
    # the real number silently drops missing records off the end of the customer's
    # report, which is the one failure this page cannot afford.
    limit: int = Query(default=500, le=2000),
):
    """Questions the agent couldn't answer because the record is missing.

    These are transfers the customer can eliminate by adding data, not by changing the
    agent — so the output carries call ids they can verify against their own dashboard
    before acting.

    NOTHING IS MERGED HERE. Each record_missing call is its own row until someone runs
    the grouping pass (POST ./group), whose answer is stored on the call rows and simply
    read back below. The string-similarity clustering this used to do merged four
    different Roman branches into one line on live data — knowledge_gaps.py has the
    measurement. A row's `count` is therefore 1 until it has been grouped, which is why
    `min_count` is only meaningful afterwards.
    """
    since = utcnow() - timedelta(days=days)
    query = (
        select(Call)
        .options(selectinload(Call.turns))
        .where(Call.started_at >= since)
        .order_by(Call.started_at.desc())
    )
    if agent_id:
        query = query.where(Call.agent_id == agent_id)
    calls = (await session.execute(query)).scalars().all()

    occurrences: list[dict[str, Any]] = []
    for call in calls:
        for gap in extract_gaps(call):
            occurrences.append(
                {
                    **gap,
                    "call": call,
                    "outcome": compute_outcome(call.success, call.transferred),
                }
            )

    calls_with_gaps = len(occurrences)
    ungrouped_count = sum(1 for o in occurrences if not o["call"].gap_group_id)

    # An ungrouped call is its own group, keyed on its id so it can never collide with a
    # stored "g<n>" and so the row is stable across reloads.
    #
    # Needs-review calls are keyed per call too, even though they all share one stored
    # group id. They are ONE SECTION of the page, not one record: "curva glicemica" and
    # "Fate analisi per la ricerca di Levico Butter?" are both unactionable for entirely
    # different reasons, and folding them into a single row would show one question as
    # the headline and bury the other — the same hiding that made string clustering
    # unusable in the first place.
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for occurrence in occurrences:
        stored = occurrence["call"].gap_group_id
        if stored == GAP_NEEDS_REVIEW:
            key = f"review:{occurrence['call'].id}"
        else:
            key = stored or f"call:{occurrence['call'].id}"
        buckets[key].append(occurrence)

    # Verification state, read from the GapGroup rows. Fetched in one query keyed on the
    # stored group ids — an ungrouped row has no record to have a verdict about, and a
    # needs-review row is never verifiable, so both simply come back with no status.
    stored_ids = {
        o["call"].gap_group_id
        for o in occurrences
        if o["call"].gap_group_id and o["call"].gap_group_id != GAP_NEEDS_REVIEW
    }
    verification = await gv.load_groups(session, sorted(stored_ids))
    config = await get_or_create_config(session)

    groups: list[dict[str, Any]] = []
    needs_review: list[dict[str, Any]] = []
    for group_id, items in buckets.items():
        is_ungrouped = group_id.startswith("call:")
        is_review = group_id.startswith("review:")
        variants = sorted({i["question"] for i in items}, key=len)
        # The canonical the grouping pass wrote, else the shortest real phrasing — which
        # is the question with the filler already stripped by the callers themselves.
        canonical = next(
            (i["call"].gap_group_question for i in items if i["call"].gap_group_question),
            None,
        )
        row = {
            "question": canonical or variants[0],
            # Grouping ignores the tool, so a merged row can span several. Name the one
            # they agree on and say so plainly when they do not.
            "tool": (
                items[0]["tool"]
                if len({i["tool"] for i in items}) == 1
                else f"{len({i['tool'] for i in items})} lookups"
            ),
            "count": len(items),
            "transferred": sum(1 for i in items if i["call"].transferred),
            "variants": variants[:5],
            # Every member is listed, not a sample: the whole point of the call id is
            # that somebody can open each call and check it before adding a record.
            "examples": [
                GapExampleOut(
                    call_id=i["call"].id,
                    external_id=i["call"].external_id,
                    started_at=i["call"].started_at,
                    agent_id=i["call"].agent_id,
                    question=i["question"],
                    outcome=i["outcome"],
                )
                for i in sorted(items, key=lambda i: i["call"].started_at, reverse=True)
            ],
            # "review:<call id>" is a display key, not a stored group, so report the
            # real stored value — otherwise the ungroup button would address nothing.
            "group_id": None if is_ungrouped else (GAP_NEEDS_REVIEW if is_review else group_id),
            "grouped": not is_ungrouped,
            "needs_review": is_review,
        }

        # How far this record has got towards being fixed, and whether it *can* be
        # checked. `probes_configured` is per region because a source only serves the
        # regions it lists — a page that offered Verify on a record with no source for its
        # region would fail on the press instead of explaining itself.
        agent_id = items[0]["call"].agent_id
        stored = None if (is_ungrouped or is_review) else verification.get(group_id)
        row["agent_id"] = agent_id
        row["probes_configured"] = len(gv.probes_for_agent(config, agent_id))
        row["status"] = gv.status_of(stored) if stored else gv.NOT_VERIFIED
        row["status_at"] = stored.status_at if stored else None
        row["status_note"] = stored.status_note if stored else None
        row["sent_batch"] = stored.sent_batch if stored else None
        (needs_review if is_review else groups).append(row)

    # Most-asked first, then most transfers, then newest. The last key is what stops the
    # page reshuffling on every reload before any grouping has run: at that point every
    # count is 1 and every transferred is 0, so without it the order is whatever the
    # database happened to return.
    def rank(group: dict[str, Any]) -> tuple:
        return (-group["count"], -group["transferred"], -group["examples"][0].started_at.timestamp())

    ranked = sorted((g for g in groups if g["count"] >= min_count), key=rank)[:limit]
    needs_review.sort(key=rank)

    return KnowledgeGapsOut(
        window_days=days,
        calls_scanned=len(calls),
        calls_with_gaps=calls_with_gaps,
        # Kept as the count of gap-hitting calls; it is no longer a count of records,
        # because a record is only known once the grouping pass has run.
        total_gaps=calls_with_gaps,
        gap_call_rate=(calls_with_gaps / len(calls) if calls else None),
        groups=[KnowledgeGapOut(**g) for g in ranked],
        needs_review=[KnowledgeGapOut(**g) for g in needs_review],
        ungrouped_count=ungrouped_count,
    )


# One grouping pass at a time, per API process.
#
# Two concurrent passes both read the ungrouped rows before either commits, so they send
# the SAME questions to the model — paying twice, and letting the second overwrite the
# first's assignments, which can split a record that had just been merged. This is not
# hypothetical: a pass takes minutes, and reloading the page mid-run leaves the server
# working while the browser starts a fresh one. Observed in production, twice against the
# same 105 rows.
#
# An asyncio.Lock is sufficient because the API is a single uvicorn process — the analysis
# worker runs inside it, which is the same reason scripts/reanalyze.py must not analyse.
# Running with --workers > 1 would need a database-level lock instead.
_grouping_lock = asyncio.Lock()


@router.post(
    "/knowledge-gaps/group",
    response_model=GapGroupingOut,
    dependencies=[Depends(require_api_key)],
)
async def group_knowledge_gaps(
    session: AsyncSession = Depends(get_session),
    agent_id: str | None = None,
    days: int = Query(default=7, ge=1, le=365),
):
    """Ask the LLM which of the ungrouped questions describe the same missing record.

    Incremental by design: only calls with no `gap_group_id` are sent, together with the
    canonical question of every group that already exists, so a new call joins an
    existing record instead of starting a duplicate of it. Already-grouped calls are
    never re-judged — undoing a bad merge is what DELETE ./group/{id} is for.

    Existing groups are collected across the WHOLE database rather than the requested
    window. Scoping them to the window would hide a group whose calls are all older than
    `days` and let the model create a second group for the same record.

    Refuses to run alongside another pass — see `_grouping_lock`. Rejecting is better than
    queueing here: a pass takes minutes, so a queued request would hold the connection
    long past any proxy's patience, and the caller would rather be told to wait.
    """
    if _grouping_lock.locked():
        raise HTTPException(
            status_code=409,
            detail=(
                "A grouping pass is already running. It keeps going even if you reload "
                "the page — wait for it to finish, then reload to see the result."
            ),
        )
    async with _grouping_lock:
        return await _run_grouping_pass(session, agent_id, days)


async def _run_grouping_pass(
    session: AsyncSession, agent_id: str | None, days: int
) -> GapGroupingOut:
    since = utcnow() - timedelta(days=days)
    window = select(Call).options(selectinload(Call.turns)).where(
        Call.started_at >= since, Call.bucket == RECORD_MISSING_BUCKET
    )
    if agent_id:
        window = window.where(Call.agent_id == agent_id)
    calls = (await session.execute(window)).scalars().all()

    ungrouped = [c for c in calls if not c.gap_group_id]
    items: list[dict[str, Any]] = []
    by_item_id: dict[int, Call] = {}
    for index, call in enumerate(ungrouped, start=1):
        gaps = extract_gaps(call)
        if not gaps:
            continue
        items.append(
            {
                "id": index,
                "question": gaps[0]["question"],
                "issue_note": call.issue_note,
            }
        )
        by_item_id[index] = call

    existing_rows = (
        await session.execute(
            select(Call.gap_group_id, Call.gap_group_question)
            .where(
                Call.gap_group_id.is_not(None),
                Call.gap_group_id != GAP_NEEDS_REVIEW,
                Call.gap_group_question.is_not(None),
            )
            .distinct()
        )
    ).all()
    existing = [{"group_id": g, "question": q} for g, q in existing_rows]

    assigned, warnings = await group_gaps(items, existing)

    new_groups: set[str] = set()
    review_calls = 0
    joined_existing = 0
    known_ids = {g["group_id"] for g in existing}
    for item_id, (group_id, canonical) in assigned.items():
        call = by_item_id[item_id]
        call.gap_group_id = group_id
        call.gap_group_question = canonical
        if group_id == GAP_NEEDS_REVIEW:
            review_calls += 1
        elif group_id in known_ids:
            joined_existing += 1
        else:
            new_groups.add(group_id)
    # Calls this pass put alongside another call from the same pass. Deliberately separate
    # from joined_existing: a pass that only slots one call into a record created earlier
    # reported "0 records created, 0 calls merged", which reads as though it did nothing.
    sizes: dict[str, int] = defaultdict(int)
    for group_id, _ in assigned.values():
        sizes[group_id] += 1
    grouped_calls = sum(n for g, n in sizes.items() if g != GAP_NEEDS_REVIEW and n > 1)

    await session.commit()
    return GapGroupingOut(
        # What this pass actually handled, not what was queued. group_gaps() caps the
        # batch, so len(items) is the size of the backlog — reporting that as
        # "considered" claimed 222 questions when 60 had been looked at, and the page
        # sums this across passes, which turned the running total into nonsense.
        considered=len(assigned),
        grouped=grouped_calls,
        joined_existing=joined_existing,
        needs_review=review_calls,
        new_groups=len(new_groups),
        remaining=len(items) - len(assigned),
        warnings=warnings,
    )


@router.delete(
    "/knowledge-gaps/group/{group_id}",
    response_model=GapUngroupOut,
    dependencies=[Depends(require_api_key)],
)
async def ungroup_knowledge_gap(
    group_id: str, session: AsyncSession = Depends(get_session)
):
    """Split a group back into individual calls.

    The escape hatch for a wrong merge. Grouping is incremental and never re-judges a
    call it has already placed, so without this a bad merge would be permanent and every
    later call matching it would pile in behind the wrong headline. Costs nothing: it
    clears the two columns and the calls reappear in the ungrouped pool for the next pass.

    THE VERDICT GOES WITH THE RECORD. What was proved missing was the *merged* question,
    and after this that question no longer exists — so the GapGroup row is deleted and its
    members come back unverified. Keeping the verdict on the released calls would let each
    one claim it had been checked in wording it never used, which is the same hiding that
    made string clustering unusable.

    The EVIDENCE survives: each GapVerification keeps its call, its variants and the raw
    replies, and only loses its pointer to the deleted record. So the call detail page can
    still show what was asked and what came back, even though the record it belonged to
    has been split.
    """
    calls = (
        await session.execute(select(Call).where(Call.gap_group_id == group_id))
    ).scalars().all()
    for call in calls:
        call.gap_group_id = None
        call.gap_group_question = None

    # Explicit rather than relying on ON DELETE SET NULL: SQLite only enforces foreign
    # keys when the pragma is on, which it is not by default.
    verifications = (
        await session.execute(
            select(GapVerification).where(GapVerification.group_id == group_id)
        )
    ).scalars().all()
    for verification in verifications:
        verification.group_id = None

    group = await session.get(GapGroup, group_id)
    if group is not None:
        await session.delete(group)

    await session.commit()
    return GapUngroupOut(group_id=group_id, calls_released=len(calls))


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

    # The configured taxonomy, so a free-text agent reason in another language isn't
    # mistaken for a disagreement. See disputes.classify().
    config = await get_or_create_config(session)
    known_reason_keys = {
        c["key"]
        for c in (transfer_categories(config) + non_completion_categories(config))
        if isinstance(c, dict) and c.get("key")
    }

    for call in rows:
        oc_outcome = compute_outcome(call.success, call.transferred)
        oc_reason = call.transfer_reason or call.non_completion_reason
        verdict = classify(
            meta=call.meta,
            callharness_outcome=oc_outcome,
            callharness_reason=oc_reason,
            known_reason_keys=known_reason_keys,
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
        Call.bucket,
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
        outcome_dist[compute_outcome(r.success, r.transferred)] += 1


    def _breakdown(values: list[str | None]) -> list[dict[str, Any]]:
        counts: dict[str, int] = defaultdict(int)
        for v in values:
            if v:
                counts[v] += 1
        return sorted(
            ({"reason": k, "count": v} for k, v in counts.items()), key=lambda x: -x["count"]
        )

    bucket_breakdown = _breakdown([r.bucket for r in rows])
    raw_rate, addressable_rate = answer_rates([r.bucket for r in rows])

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
        bucket_breakdown=bucket_breakdown,
        raw_answer_rate=raw_rate,
        addressable_answer_rate=addressable_rate,
        transfer_reason_breakdown=transfer_reason_breakdown,
        non_completion_reason_breakdown=non_completion_reason_breakdown,
        agents=list(agents),
        agent_stats=agent_stats,
    )


@router.get("/buckets", response_model=BucketsOut)
async def buckets(
    session: AsyncSession = Depends(get_session),
    agent_id: str | None = None,
    date_from: datetime | None = Query(default=None, alias="from"),
    date_to: datetime | None = Query(default=None, alias="to"),
):
    """What happened across every analysed call, plus the two answer rates.

    Split per agent as well as overall, because the regions run the same agent against
    different data — a bucket that dominates in one region and not another is a data
    problem, while one that dominates everywhere is an agent problem, and the split is
    what tells them apart.
    """
    query = select(Call.agent_id, Call.bucket)
    if agent_id:
        query = query.where(Call.agent_id == agent_id)
    query = _range_filter(query, date_from, date_to)
    rows = (await session.execute(query)).all()

    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        if r.bucket:
            counts[r.bucket] += 1
    distribution = sorted(
        ({"bucket": k, "count": v} for k, v in counts.items()), key=lambda x: -x["count"]
    )

    raw_rate, addressable_rate = answer_rates([r.bucket for r in rows])

    by_agent: dict[str, list[str | None]] = defaultdict(list)
    for r in rows:
        by_agent[r.agent_id].append(r.bucket)
    agent_stats = []
    for name in sorted(by_agent):
        agent_buckets = by_agent[name]
        a_raw, a_addressable = answer_rates(agent_buckets)
        agent_stats.append(
            {
                "agent_id": name,
                "calls": len(agent_buckets),
                "bucketed": sum(1 for b in agent_buckets if b),
                "raw_answer_rate": a_raw,
                "addressable_answer_rate": a_addressable,
            }
        )

    return BucketsOut(
        total_calls=len(rows),
        bucketed_calls=sum(1 for r in rows if r.bucket),
        distribution=distribution,
        raw_answer_rate=raw_rate,
        addressable_answer_rate=addressable_rate,
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

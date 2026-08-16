"""Requeue calls for analysis in bulk — the backfill after a change to the judge.

WHY THIS EXISTS
Changing anything the analysis depends on — the bucket taxonomy, the success criteria,
the model — leaves every existing call carrying a verdict produced by the old one. The
dashboard then mixes two populations and none of its numbers mean anything. There is a
per-call `POST /calls/{id}/reanalyze`, but doing that 665 times by hand is not a
procedure, and doing it in an unbounded loop walks straight into the provider's
per-minute token cap.

This marks calls `pending` and lets the in-process worker drain them at its own
`analysis_concurrency`. It talks to the database directly rather than over HTTP so it
needs no API key and no reachable port — on the VM it runs beside the container's
database, which is the only place a backfill is ever run.

    python -m scripts.reanalyze --dry-run                 # what would be requeued
    python -m scripts.reanalyze --all                     # everything analysed
    python -m scripts.reanalyze --missing-bucket          # resume an interrupted run
    python -m scripts.reanalyze --agent Lazio --days 30

ONE WORKER ONLY. The worker runs inside the API process, so two API processes means two
workers racing for the same pending rows — `_claim_pending` selects and then updates
without row locking, so both can claim the same call and one silently overwrites the
other. Check for a single container before starting: this script deliberately does not
analyse anything itself, precisely so it cannot become that second worker.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select, update  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Call, utcnow  # noqa: E402

# Measured on the real production prompt (2,519 in / 240 out on gpt-4.1). Only used to
# print an estimate before spending anything — deliberately not read from a live price
# list, which would be one more thing to go stale silently.
TOKENS_PER_CALL = 2_760
COST_PER_CALL = {
    "gpt-4.1": 0.0070,
    "gpt-4.1-mini": 0.0014,
    "gpt-5-mini": 0.0031,
    "gpt-4o-mini": 0.00043,
}


def _build_query(args):
    query = select(Call).where(Call.analysis_status.in_(("completed", "failed", "skipped")))
    if args.agent:
        query = query.where(Call.agent_id == args.agent)
    if args.days:
        query = query.where(Call.started_at >= utcnow() - timedelta(days=args.days))
    if args.missing_bucket:
        query = query.where(Call.bucket.is_(None))
    if args.failed_only:
        query = query.where(Call.analysis_status == "failed")
    # A call with no turns has nothing to analyse: the worker would bucket it
    # no_caller_audio without an LLM call, which is correct but pointless to requeue.
    if args.skip_empty:
        query = query.where(
            select(func.count()).select_from(Call.turns.property.mapper.class_)
            .where(Call.turns.property.mapper.class_.call_id == Call.id)
            .scalar_subquery() > 0
        )
    return query.order_by(Call.started_at.desc())


async def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--all", action="store_true", help="every analysed call")
    p.add_argument("--agent", default=None, help="restrict to one agent_id")
    p.add_argument("--days", type=int, default=None, help="only calls this recent")
    p.add_argument("--limit", type=int, default=None, help="cap how many are requeued")
    p.add_argument("--missing-bucket", action="store_true",
                   help="only calls with no bucket yet — the way to resume a part-done run")
    p.add_argument("--failed-only", action="store_true",
                   help="only calls whose last analysis failed")
    p.add_argument("--skip-empty", action="store_true", default=True,
                   help="skip calls with no turns (default on)")
    p.add_argument("--dry-run", action="store_true", help="report and change nothing")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = p.parse_args()

    if not (args.all or args.agent or args.days or args.missing_bucket or args.failed_only):
        p.error("refusing to requeue everything implicitly — pass --all or a filter")

    await init_db()

    async with SessionLocal() as session:
        query = _build_query(args)
        if args.limit:
            query = query.limit(args.limit)
        calls = (await session.execute(query)).scalars().all()

        if not calls:
            print("Nothing matches those filters.")
            return

        model = settings.resolved_model
        per_call = COST_PER_CALL.get(model)
        minutes = len(calls) * TOKENS_PER_CALL / 30_000

        print(f"calls to requeue : {len(calls):,}")
        print(f"model            : {model} @ temperature {settings.llm_temperature}")
        print(f"worker           : {settings.analysis_concurrency} at a time, "
              f"polling every {settings.analysis_poll_seconds}s")
        if per_call:
            print(f"estimated cost   : ${per_call * len(calls):,.2f} "
                  f"(${per_call:.5f}/call)")
        else:
            print(f"estimated cost   : unknown for {model}")
        # The floor, not the estimate: it assumes the provider cap is the only limit.
        print(f"at least         : {minutes:,.0f} min against a 30,000 TPM cap")
        by_agent: dict[str, int] = {}
        for c in calls:
            by_agent[c.agent_id] = by_agent.get(c.agent_id, 0) + 1
        print(f"by agent         : {by_agent}")

        if args.dry_run:
            print("\n--dry-run: nothing changed.")
            return
        if not args.yes:
            if input("\nRequeue these? [y/N] ").strip().lower() not in ("y", "yes"):
                print("Aborted.")
                return

        ids = [c.id for c in calls]
        for chunk_start in range(0, len(ids), 500):
            chunk = ids[chunk_start : chunk_start + 500]
            await session.execute(
                update(Call)
                .where(Call.id.in_(chunk))
                .values(analysis_status="pending", analysis_error=None)
            )
            await session.commit()
        print(f"\nRequeued {len(ids):,} calls. The worker drains them on its own.")

    # Progress is read from the database rather than tracked here, so closing this
    # script does not stop or corrupt the run — the worker owns it either way.
    print("Watching progress (Ctrl-C to stop watching; the run continues).\n")
    done_before = None
    while True:
        async with SessionLocal() as session:
            pending = (await session.execute(
                select(func.count()).select_from(Call)
                .where(Call.analysis_status.in_(("pending", "processing")))
            )).scalar_one()
            failed = (await session.execute(
                select(func.count()).select_from(Call)
                .where(Call.id.in_(ids), Call.analysis_status == "failed")
            )).scalar_one()
        done = len(ids) - pending
        if done != done_before:
            pct = done / len(ids) * 100 if ids else 100
            print(f"  {done:,}/{len(ids):,} ({pct:.0f}%)  pending={pending:,}  failed={failed:,}")
            done_before = done
        if pending == 0:
            break
        await asyncio.sleep(10)

    print("\nDone. Re-run with --failed-only if any failed.")


if __name__ == "__main__":
    asyncio.run(main())

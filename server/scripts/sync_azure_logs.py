"""Link existing calls to their raw agent log in Azure Blob Storage.

Usage (from the server/ directory, or `docker compose exec api ...` in production):
    python -m scripts.sync_azure_logs --dry-run                 # report, change nothing
    python -m scripts.sync_azure_logs --dry-run --verify        # ...and prove the matches
    python -m scripts.sync_azure_logs                           # link everything
    python -m scripts.sync_azure_logs --agent Lazio --days 30
    python -m scripts.sync_azure_logs --recheck                 # retry earlier misses

WHY THIS EXISTS
app/analysis/worker.py only looks back azure_log_lookback_days, because for a recent
call a miss usually means "the upload hasn't landed yet" while for an old one it means
"it never will". That is the right behaviour for the steady state and the wrong one for
the first run, where every call in the database is old. This script is the one-off sweep
that covers the backlog, and the same matcher (app/azure_logs.resolve) does the work in
both, so there is only one set of rules to get right.

Unlike backfill_from_supabase.py this talks to the database directly rather than over
the HTTP API. There is no write endpoint for log_blob — nothing outside CallHarness ever
sets it — and going direct also sidesteps the 200-row cap on GET /api/v1/calls.

--verify is the reason to trust the result. The join is a prefix match on the first 8
characters of the session uuid, which is inherently weaker than an exact key, but the
agent writes its own full session id into line 2 of every log AND, further down, the
CallHarness call id it received back ("CallHarness: sent call <uuid> -> <call id>").
--verify downloads each matched blob and checks both. A miss is ordinary (some calls
never got a log uploaded); a MISMATCH means the matcher is wrong and must be fixed.
"""

import argparse
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app import azure_logs  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Call, utcnow  # noqa: E402


async def verify(call: Call) -> str:
    """Confirm a matched blob really belongs to this call. Returns a status word."""
    content = await azure_logs.fetch_log(call.log_blob)
    if content is None:
        return "gone"
    text = content.decode("utf-8", errors="replace")
    # The agent logs its full session id at startup; external_id is that same uuid.
    if call.external_id and call.external_id in text:
        return "ok"
    # Older logs may not print the session id in a form we can find, but the
    # CallHarness id echoed back after ingest is just as conclusive.
    if call.id in text:
        return "ok"
    return "mismatch"


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--agent", help="Only calls for this agent_id (e.g. Lazio)")
    parser.add_argument(
        "--days", type=int, help="Only calls from the last N days (default: all)"
    )
    parser.add_argument(
        "--recheck",
        action="store_true",
        help="Also retry calls already looked for and not found",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Report what would be linked, then roll back"
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Download each matched log and confirm it names this call",
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="Verify every call already linked (instead of matching new ones), then exit",
    )
    args = parser.parse_args()

    if not azure_logs.enabled():
        sys.exit(
            "No Azure connection string configured. Set "
            "CALLHARNESS_AZURE_STORAGE_CONNECTION_STRING (or AZURE_STORAGE_CONNECTION_STRING)."
        )

    await init_db()

    if args.audit:
        # Re-check links that already exist. Worth having separately because the
        # periodic worker links calls on its own, so by the time anyone thinks to
        # check, there is usually nothing left for --verify to have looked at.
        async with SessionLocal() as session:
            query = select(Call).where(Call.log_blob.is_not(None))
            if args.agent:
                query = query.where(Call.agent_id == args.agent)
            linked = (await session.execute(query.order_by(Call.started_at))).scalars().all()

        print(f"Auditing {len(linked)} existing link(s)...")
        ok = bad = gone = 0
        for i, call in enumerate(linked, 1):
            status = await verify(call)
            if status == "ok":
                ok += 1
            elif status == "gone":
                gone += 1
                print(f"  [{i}/{len(linked)}] {call.id[:8]}  blob vanished: {call.log_blob}")
            else:
                bad += 1
                print(f"  [{i}/{len(linked)}] {call.id[:8]}  MISMATCH: {call.log_blob}")
        print(f"\nAudit: {ok} correct, {gone} blob missing, {bad} MISMATCHED")
        if bad:
            sys.exit("Existing links are wrong — investigate before trusting the panel")
        return

    async with SessionLocal() as session:
        query = select(Call).where(Call.log_blob.is_(None), Call.external_id.is_not(None))
        if args.agent:
            query = query.where(Call.agent_id == args.agent)
        if args.days:
            query = query.where(Call.started_at >= utcnow() - timedelta(days=args.days))
        if not args.recheck:
            query = query.where(Call.log_checked_at.is_(None))
        calls = (await session.execute(query.order_by(Call.started_at))).scalars().all()

        already = (
            await session.execute(
                select(Call.id).where(Call.log_blob.is_not(None))
            )
        ).scalars().all()

        print(
            f"{len(calls)} call(s) to look up "
            f"({len(already)} already linked) in container "
            f"{settings.azure_log_container!r}"
        )
        if not calls:
            return

        # resolve() lists each (prefix, day) folder once, so the whole set in one call
        # is far cheaper than iterating. It commits unless we ask it not to.
        matched = await azure_logs.resolve(session, calls, commit=not args.dry_run)
        found = [c for c in calls if c.log_blob]

        for call in found[:10]:
            print(f"  {call.id[:8]}  {call.agent_id:<10}  {call.log_blob}")
        if len(found) > 10:
            print(f"  ... and {len(found) - 10} more")

        verified = mismatched = gone = 0
        if args.verify and found:
            print(f"\nVerifying {len(found)} match(es) against the log contents...")
            for i, call in enumerate(found, 1):
                try:
                    status = await verify(call)
                except Exception as exc:  # noqa: BLE001 - one bad blob isn't fatal
                    print(f"  [{i}/{len(found)}] {call.id[:8]}  ERROR {exc}")
                    continue
                if status == "ok":
                    verified += 1
                elif status == "gone":
                    gone += 1
                    print(f"  [{i}/{len(found)}] {call.id[:8]}  blob vanished: {call.log_blob}")
                else:
                    mismatched += 1
                    print(
                        f"  [{i}/{len(found)}] {call.id[:8]}  MISMATCH — {call.log_blob} "
                        f"does not name external_id {call.external_id}"
                    )

        if args.dry_run:
            await session.rollback()

        print(
            f"\nDone: {matched} matched, {len(calls) - matched} no log found"
            + (f", {verified} verified" if args.verify else "")
            + (f", {gone} blob missing" if gone else "")
            + (f", {mismatched} MISMATCHED" if mismatched else "")
            + (" (dry run — nothing written)" if args.dry_run else "")
        )
        if mismatched:
            sys.exit("Matcher produced wrong matches — do not run without --dry-run")


if __name__ == "__main__":
    asyncio.run(main())

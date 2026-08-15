"""Create local call rows that mirror real production calls, read out of Azure.

Usage (from the server/ directory):
    python -m scripts.seed_from_azure_logs --agent Lazio --days 3
    python -m scripts.seed_from_azure_logs --agent Lazio --days 7 --limit 200
    python -m scripts.seed_from_azure_logs --reset          # remove what this created

WHY THIS EXISTS
The log panel can only be exercised against calls that actually have a log in Azure,
and a dev database seeded by seed_healthcare_demo.py has none — those calls are
invented, so their external_ids match nothing and every one of them correctly shows
no panel. Testing the feature then means deploying to production first, which is the
wrong order.

This walks real blobs in the container and, for each one, reads the identifying lines
out of the log itself — the agent prints "Session ID: <uuid>" and "Call started at:
<timestamp>" near the top — then writes a call row carrying exactly those values. The
result is a local database whose external_ids and timestamps are the real ones, so
scripts/sync_azure_logs.py has to solve the genuine matching problem rather than a
rehearsed one.

Only the first 16KB of each blob is downloaded, since everything needed is in the
opening lines.

WHAT THIS IS NOT
These rows have no transcript, no audio and no analysis — the log is the only source,
and reconstructing a conversation from it is a different job. They exist to exercise
the log panel end to end. analysis_status is set to "skipped" deliberately: leaving it
"pending" would hand every one of them to the LLM worker and bill you for analysing
calls you already have verdicts for in production.

Rows are tagged meta.seeded_from_azure = true, which is what --reset deletes.
"""

import argparse
import asyncio
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app import azure_logs  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Call, utcnow  # noqa: E402

# Two lines the agent writes near the top of every log.
SESSION_RE = re.compile(r"Session ID:\s*([0-9a-fA-F-]{36})")
STARTED_RE = re.compile(r"Call started at:\s*([\d-]{10}[ T][\d:.]+)")
HEAD_BYTES = 16 * 1024


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--agent", default="Lazio", help="agent_id to create calls under")
    parser.add_argument("--days", type=int, default=2, help="How many days back to walk")
    parser.add_argument("--limit", type=int, default=60, help="Max calls to create")
    parser.add_argument("--dry-run", action="store_true", help="Report, write nothing")
    parser.add_argument(
        "--reset", action="store_true", help="Delete rows this script created, then exit"
    )
    args = parser.parse_args()

    await init_db()

    if args.reset:
        async with SessionLocal() as session:
            rows = (await session.execute(select(Call))).scalars().all()
            doomed = [c for c in rows if (c.meta or {}).get("seeded_from_azure")]
            for call in doomed:
                await session.delete(call)
            await session.commit()
        print(f"Deleted {len(doomed)} call(s) seeded from Azure")
        return

    if not azure_logs.enabled():
        sys.exit(
            "No Azure connection string configured. Set "
            "CALLHARNESS_AZURE_STORAGE_CONNECTION_STRING in server/.env"
        )

    prefix = azure_logs.prefix_for(args.agent)
    today = utcnow().date()
    print(f"Walking {prefix or '<container root>'}call-logs/ for the last {args.days} day(s)")

    candidates: list[azure_logs.ParsedLog] = []
    for back in range(args.days):
        day = today - timedelta(days=back)
        index = await azure_logs.list_day(prefix, day)
        found = [p for group in index.values() for p in group]
        print(f"  {day}: {len(found)} log(s)")
        candidates.extend(found)
    candidates.sort(key=lambda p: p.started, reverse=True)
    candidates = candidates[: args.limit]

    if not candidates:
        print("No logs found — check --agent and --days.")
        return

    created = skipped = unreadable = 0
    async with SessionLocal() as session:
        existing = set(
            (await session.execute(select(Call.external_id))).scalars().all()
        )
        for i, parsed in enumerate(candidates, 1):
            head = await azure_logs.fetch_log(parsed.blob, head_bytes=HEAD_BYTES)
            if head is None:
                unreadable += 1
                continue
            text = head.decode("utf-8", errors="replace")
            session_match = SESSION_RE.search(text)
            if not session_match:
                unreadable += 1
                print(f"  [{i}] no session id in {parsed.blob.rsplit('/', 1)[-1]}")
                continue
            external_id = session_match.group(1)
            if external_id in existing:
                skipped += 1
                continue
            existing.add(external_id)

            started_match = STARTED_RE.search(text)
            started_at = (
                datetime.fromisoformat(started_match.group(1).strip())
                if started_match
                else parsed.started
            )

            session.add(
                Call(
                    external_id=external_id,
                    agent_id=args.agent,
                    direction="inbound",
                    from_number=parsed.label,
                    started_at=started_at,
                    # Skipped, not pending: these must never reach the LLM worker.
                    analysis_status="skipped",
                    meta={"seeded_from_azure": True, "source_blob": parsed.blob},
                )
            )
            created += 1

        if args.dry_run:
            await session.rollback()
        else:
            await session.commit()

    print(
        f"\nDone: {created} created, {skipped} already present, {unreadable} unreadable"
        + (" (dry run — nothing written)" if args.dry_run else "")
    )
    if created and not args.dry_run:
        print("Now link them:  python -m scripts.sync_azure_logs --verify")


if __name__ == "__main__":
    asyncio.run(main())

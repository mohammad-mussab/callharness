"""Prove the missing-record backlog, by re-asking the lookup API.

WHY A SCRIPT AS WELL AS A BUTTON

The dashboard verifies one record, or a batch, in the background. But their graph endpoint
is two or three sequential gpt-4.1 calls, so one record takes the better part of a minute
and the standing backlog is in the hundreds — hours of work, on a machine where the
operator wants to see progress and read the failures. That is a terminal job, and on the VM
this runs beside the container's database, so it needs no API key and no reachable port.

    python -m scripts.verify_gaps --agent Lazio --dry-run
    python -m scripts.verify_gaps --agent Lazio --days 7 --limit 5 --yes
    python -m scripts.verify_gaps --agent Lazio --status verify_error --yes   # retry
    python -m scripts.verify_gaps --group-id g7a3f21bc90 --yes

THE UNIT IS THE RECORD, NOT THE CALL

Only grouped records are checked — including a group of one, which the grouping pass
looked at and found nothing else like. Ungrouped calls are skipped (run the grouping pass
first, or you pay to re-ask a record another row already covers), and so is the reserved
needs-review group, whose questions nobody could add a record for anyway.

WHAT THIS SPENDS, AND WHOSE MONEY

Two kinds, and only one of them is ours:

  ours   ~2 LLM calls per record (read the question, judge the replies) — pennies.
  theirs every probe. Their RAG endpoint is an STT-cleanup call, an embedding and an
         answer-generation call; their graph endpoint is two or three gpt-4.1 calls plus
         a Neo4j round trip. Six or more probes per record, on a service with no rate
         limiting, no caching and no circuit breaker, that is simultaneously answering
         live phone calls.

So this prints the probe count and makes you confirm before sending anything, and
gap_verification holds the concurrency at 2 with a pause between requests. Do not raise
either to make a backfill finish sooner.

UNLIKE reanalyze.py, THIS SCRIPT DOES THE WORK ITSELF. There is no worker draining a
queue on the other side, so nothing happens unless this process is running. The flip side
is that it IS a second runner: do not start it while a batch is running from the
dashboard, or the lookup API takes double the load and two writers race the same rows.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import selectinload  # noqa: E402

from app import gap_verification as gv  # noqa: E402
from app.analysis.worker import get_or_create_config  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Call  # noqa: E402

# Our side only: the two chat_json calls per record, on the same prompt sizes the analysis
# pass measured. Printed as an estimate, not read from a live price list — one more thing
# that would go stale silently. Their side is not priced here because we cannot know
# their model or their rates; the probe count is printed instead, which is the number
# that actually matters when asking permission to run this.
COST_PER_RECORD = {"gpt-4.1": 0.012, "gpt-4.1-mini": 0.0024, "gpt-4o-mini": 0.0008}


async def _select_groups(session, args) -> dict[str, dict]:
    calls = (
        (await session.execute(gv.eligible_calls_query(args.agent, args.days))).scalars().all()
    )
    groups = gv.assemble_groups(list(calls))

    if args.group_id:
        groups = {gid: g for gid, g in groups.items() if gid in set(args.group_id)}

    stored = await gv.load_groups(session, list(groups))
    if not args.all_statuses:
        # Status filtering in Python, because a group with no row yet and one whose status
        # is NULL mean the same thing, and expressing that in SQL for both SQLite and
        # Postgres is more code than it saves.
        wanted = set(args.status) if args.status else {gv.NOT_VERIFIED, gv.VERIFY_ERROR}
        groups = {
            gid: g for gid, g in groups.items() if gv.status_of(stored.get(gid)) in wanted
        }

    for gid, group in groups.items():
        group["status"] = gv.status_of(stored.get(gid))

    ordered = sorted(
        groups.items(), key=lambda kv: kv[1]["members"][0].started_at, reverse=True
    )
    if args.limit:
        ordered = ordered[: args.limit]
    return dict(ordered)


async def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--agent", default=None, help="restrict to one agent_id / region")
    p.add_argument("--days", type=int, default=None, help="only records this recent")
    p.add_argument("--limit", type=int, default=None, help="cap how many are checked")
    p.add_argument("--group-id", action="append", default=None,
                   help="one specific record; repeatable")
    p.add_argument("--status", action="append", default=None,
                   help=f"statuses to (re)check. Default: {gv.NOT_VERIFIED} and "
                        f"{gv.VERIFY_ERROR}. Repeatable.")
    p.add_argument("--all-statuses", action="store_true",
                   help="re-check every grouped record, including ones already sent")
    p.add_argument("--dry-run", action="store_true", help="report and send nothing")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = p.parse_args()

    if not (args.agent or args.days or args.group_id or args.status or args.all_statuses):
        p.error(
            "refusing to sweep everything implicitly — pass a filter. Every record checked "
            "sends six or more requests to the customer's live lookup API."
        )

    await init_db()

    async with SessionLocal() as session:
        config = await get_or_create_config(session)
        groups = await _select_groups(session, args)
        # Grouped by region, because a source only serves the regions it lists and an
        # unroutable region is the one failure worth knowing about before starting.
        routable = {
            gid: g for gid, g in groups.items()
            if gv.probes_for_agent(config, g["agent_id"])
        }
        unroutable = {gid: g for gid, g in groups.items() if gid not in routable}

    if not gv.enabled_probes(config):
        print(
            "No lookup probes are configured.\n"
            "Add one in Analysis Settings (or via PUT /api/v1/config/analysis) first. "
            "Without a source to re-ask, a gap can only be assumed, not verified."
        )
        return

    if not groups:
        print("Nothing matches those filters. (Only GROUPED records can be verified — "
              "run the grouping pass on the Missing Information page first.)")
        return

    model = settings.resolved_model
    our_cost = COST_PER_RECORD.get(model)
    requests = sum(
        gv.estimate_requests(config, g["agent_id"]) for g in routable.values()
    )
    by_status: dict[str, int] = {}
    for group in groups.values():
        by_status[group["status"]] = by_status.get(group["status"], 0) + 1

    print(f"records to check : {len(routable):,}")
    print(f"current statuses : {by_status}")
    print(f"our LLM model    : {model}")
    print(f"requests to them : at most {requests:,} "
          f"({gv._PROBE_CONCURRENCY} at a time, {gv._PROBE_DELAY_SECONDS}s apart)")
    if our_cost:
        print(f"our cost         : ~${our_cost * len(routable):,.2f} (${our_cost:.4f}/record)")
    if unroutable:
        regions: dict[str, int] = {}
        for group in unroutable.values():
            regions[group["agent_id"]] = regions.get(group["agent_id"], 0) + 1
        print(f"SKIPPING         : {regions} — no lookup source configured for those regions")
    print()
    print("Those requests land on the customer's production lookup service, which is "
          "also answering live phone calls, and each one spends their LLM credits.")

    if args.dry_run:
        print("\n--dry-run: nothing sent.")
        for gid, group in list(routable.items())[:20]:
            print(f"  {gid}  [{group['status']}]  {len(group['members'])} call(s)  "
                  f"{group['canonical'][:80]}")
        if len(routable) > 20:
            print(f"  … and {len(routable) - 20:,} more")
        return

    if not routable:
        print("\nNothing is checkable. Add a lookup source for those regions first.")
        return

    if not args.yes:
        if input("\nSend these? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return

    print()
    verdicts: dict[str, int] = {}
    for i, group_id in enumerate(routable, 1):
        # A session per record, committed as it goes. A sweep that dies at record 90 must
        # leave the first 89 verdicts on disk — one long transaction would throw them
        # away, and this is the expensive kind of work to have to redo.
        async with SessionLocal() as session:
            calls = (
                (
                    await session.execute(
                        select(Call)
                        .options(selectinload(Call.turns))
                        .where(Call.gap_group_id == group_id)
                    )
                )
                .scalars()
                .all()
            )
            fresh = gv.assemble_groups(list(calls)).get(group_id)
            if not fresh:
                print(f"  [{i:>4}/{len(routable)}] skipped            {group_id[:10]}  "
                      "no calls left in this record")
                continue
            config = await get_or_create_config(session)
            try:
                verification = await gv.verify_gap_group(
                    session,
                    group_id=group_id,
                    canonical=fresh["canonical"],
                    members=fresh["members"],
                    config=config,
                )
                await session.commit()
                verdict, note = verification.verdict, (verification.question_note or "")
            except gv.NoProbeForRegion as exc:
                await session.rollback()
                verdict, note = "unroutable", str(exc)
            except gv.ProbeConfigError as exc:
                # Every remaining record would fail identically; stop rather than hammer
                # the API for nothing.
                print(f"\nStopping: {exc}")
                break
            except Exception as exc:  # noqa: BLE001
                await session.rollback()
                verdict, note = gv.VERIFY_ERROR, f"{type(exc).__name__}: {exc}"

        verdicts[verdict] = verdicts.get(verdict, 0) + 1
        print(f"  [{i:>4}/{len(routable)}] {verdict:<18} {group_id[:10]}  {note[:100]}")

    print()
    print("Results:")
    for verdict, count in sorted(verdicts.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>5}  {verdict}")
    sendable = verdicts.get(gv.CONFIRMED_MISSING, 0)
    print(
        f"\n{sendable:,} confirmed missing and ready to report. "
        "Review them on the Missing Information page before sending: nothing is sent "
        "from here, and 'Mark as sent' is what stops a record going out twice."
    )


if __name__ == "__main__":
    asyncio.run(main())

"""Copy Supabase's `tb_stat.id_stat` onto the CallHarness calls it already has.

Why this exists
---------------
A single phone call carries four different ids, and until you can move between them
you cannot follow one call across the two systems:

    tb_stat.id_stat      144394                                Supabase's own row number
    tb_stat.call_id      1912d832-fb20-48b3-9f1d-d51dedfe5166  the agent's uuid4
    calls.external_id    (the same uuid -- this is the join)
    calls.id             73589d5bb6bc4f51b60a0c94c7042f57      what /calls/<id> shows

The agent sends `call_id` as `external_id` but has never sent `id_stat`, so a person
holding a Supabase row number had no way into CallHarness at all. This walks the
mapping the other way and writes `id_stat` into each call's `meta`, after which the
dashboard search box finds it (see the metadata clause in routes/calls.py).

It writes to `meta` rather than to a new column on purpose. `id_stat` is one
integrator's Supabase primary key, not a concept CallHarness has; a column for it
would be a vendor-specific field in a product meant to ship generically, and `meta`
is exactly the free-form place agents already put their own identifiers.

The value is stored as a **string**, not the integer Supabase returns. The search
clause matches the value with its JSON quotes around it, so a query for 144394
cannot also return a call whose `llm_token` happens to contain those digits -- and
an unquoted JSON number would not match that pattern at all. Both halves have to
agree; changing one without the other silently breaks the lookup.

This is a backfill for history, not a permanent mechanism. The durable fix is one
line in the agent's own `services/callharness_service.py`, adding `id_stat` to the
metadata dict it already builds, so new calls arrive carrying it. Note re-ingestion
cannot substitute for that: `POST /calls` is idempotent on `external_id` and
first-write-wins, so re-posting an existing call is discarded silently.

Usage (from server/, venv active). Talks to the database directly, so on the VM run
it beside the container:

    python -m scripts.backfill_id_stat --days 90 --dry-run

Drop --dry-run once the sample lines look right. Safe to re-run: a call whose meta
already holds the same id_stat is skipped, so the second run reports 0 updates.
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Call  # noqa: E402

TABLE = "tb_stat"
ID_COLUMN = "id_stat"
CALL_ID_COLUMN = "call_id"
META_KEY = "id_stat"
# How many uuids go into one `call_id=in.(...)` filter. The bound is URL length, not
# Supabase: 150 uuids is a ~6KB query string, comfortably inside the 8KB most servers
# accept, and each request is then an indexed lookup rather than a scan.
CHUNK = 150


async def fetch_mapping(url: str, key: str, call_ids: list[str]) -> dict[str, str]:
    """{call_id: id_stat}, asked for by call_id rather than swept out of the table.

    The obvious implementation -- page through tb_stat and keep what matches -- does
    not survive contact with the real table. tb_stat holds every region and predates
    the CallHarness integration, so a 90-day window is 47,000+ rows against the 8,657
    calls that could possibly match; nearly everything fetched is discarded. Worse,
    deep `offset=` paging makes Postgres walk all the rows it is skipping, and
    Supabase answered offset=47000 with a 500 (a statement timeout) rather than a
    page -- so the sweep does not merely waste work, it fails outright.

    Asking by call_id inverts that: each request is an indexed lookup on the ~150 ids
    we actually hold, there is no offset to grow, and no unusable row is ever
    transferred. It also means a call is matched however old its tb_stat row is,
    because the query is driven by our rows rather than by a date on theirs.

    Only two columns are ever selected. tb_stat has ~46 and most carry patient
    identity (name, date of birth, fiscal code, address); pulling whole rows to read
    one number would copy all of that onto this machine for nothing.
    """
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    endpoint = f"{url.rstrip('/')}/rest/v1/{TABLE}"
    mapping: dict[str, str] = {}

    async with httpx.AsyncClient(timeout=60) as client:
        for start in range(0, len(call_ids), CHUNK):
            chunk = call_ids[start : start + CHUNK]
            params = {
                "select": f"{ID_COLUMN},{CALL_ID_COLUMN}",
                CALL_ID_COLUMN: "in.(" + ",".join(chunk) + ")",
            }
            resp = await client.get(endpoint, headers=headers, params=params)
            resp.raise_for_status()
            for row in resp.json():
                call_id = row.get(CALL_ID_COLUMN)
                stat = row.get(ID_COLUMN)
                # A tb_stat row with no call_id cannot be joined to anything; the
                # agent writes the uuid at call start, so such a row predates that.
                if call_id and stat is not None:
                    mapping[str(call_id)] = str(stat)
            done = min(start + CHUNK, len(call_ids))
            print(f"  looked up {done:,}/{len(call_ids):,}, matched {len(mapping):,}")
    return mapping


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--supabase-url", default=os.getenv("SUPABASE_URL"))
    ap.add_argument(
        "--supabase-key",
        default=os.getenv("SUPABASE_SECRET_KEY") or os.getenv("SUPABASE_KEY"),
    )
    ap.add_argument("--days", type=int, default=None, help="only calls started this recently")
    ap.add_argument("--limit", type=int, default=None, help="cap on calls, newest first")
    ap.add_argument("--agent", default=None, help="only calls with this agent_id")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.supabase_url or not args.supabase_key:
        print("Need --supabase-url and --supabase-key (or SUPABASE_URL / SUPABASE_SECRET_KEY).")
        return 2

    await init_db()
    updated = skipped = 0
    samples: list[str] = []

    async with SessionLocal() as session:
        # Driven from our own calls, not from tb_stat -- see fetch_mapping's docstring.
        query = select(Call).where(Call.external_id.is_not(None))
        if args.agent:
            query = query.where(Call.agent_id == args.agent)
        if args.days is not None:
            since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=args.days)
            query = query.where(Call.started_at >= since)
        query = query.order_by(Call.started_at.desc())
        if args.limit is not None:
            query = query.limit(args.limit)
        calls = (await session.execute(query)).scalars().all()

        print(f"{len(calls):,} CallHarness calls to look up.")
        if not calls:
            return 0

        print(f"Asking {TABLE} for their {ID_COLUMN}...")
        mapping = await fetch_mapping(
            args.supabase_url, args.supabase_key, [c.external_id for c in calls]
        )

        for call in calls:
            stat = mapping.get(call.external_id or "")
            if stat is None:
                continue
            meta = dict(call.meta or {})
            if meta.get(META_KEY) == stat:
                skipped += 1
                continue
            meta[META_KEY] = stat
            # Reassigned rather than mutated in place: `meta` is a plain JSON column,
            # so SQLAlchemy only notices a change when the attribute itself is set.
            # Mutating the dict would leave the row untouched and report success.
            call.meta = meta
            updated += 1
            if len(samples) < 5:
                samples.append(f"    id_stat {stat:>8}  ->  {call.id}  ({call.external_id})")

        unmatched = len(calls) - len(mapping)
        print()
        for line in samples:
            print(line)
        print(
            f"  update {updated:,} | already correct {skipped:,} "
            f"| no {TABLE} row {unmatched:,}"
        )

        if args.dry_run:
            print("\nDry run - nothing written. Re-run without --dry-run to apply.")
            await session.rollback()
            return 0
        await session.commit()
        print("\nWritten.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

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
clause matches '"<q>"' with the quotes so a query for 144394 cannot also return a
call whose `llm_token` happens to contain those digits -- and an unquoted JSON number
would not match that pattern at all. Both halves have to agree; changing one without
the other silently breaks the lookup.

This is a backfill for history, not a permanent mechanism. The durable fix is one
line in the agent's own `services/callharness_service.py`, adding `id_stat` to the
metadata dict it already builds, so new calls arrive carrying it. Note re-ingestion
cannot substitute for that: `POST /calls` is idempotent on `external_id` and
first-write-wins, so re-posting an existing call is discarded silently.

Usage (from server/, venv active). Talks to the database directly, so on the VM run
it beside the container:

    python -m scripts.backfill_id_stat --supabase-url https://xxxx.supabase.co \
        --supabase-key <service-role-key> --days 30 --dry-run

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
CREATED_AT_COLUMN = "created_at"
META_KEY = "id_stat"
PAGE = 1000


async def fetch_mapping(
    url: str, key: str, days: int | None, limit: int | None
) -> dict[str, str]:
    """{call_id: id_stat} straight from PostgREST, paged.

    Only two columns are ever selected. tb_stat has ~46 of them and most carry patient
    identity (name, date of birth, fiscal code, address); pulling the whole row to read
    one number would put all of that on this machine for no reason.
    """
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    params_base = {"select": f"{ID_COLUMN},{CALL_ID_COLUMN}", "order": f"{ID_COLUMN}.desc"}
    if days is not None:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        params_base[CREATED_AT_COLUMN] = f"gte.{since}"

    mapping: dict[str, str] = {}
    offset = 0
    async with httpx.AsyncClient(timeout=60) as client:
        while True:
            want = PAGE if limit is None else min(PAGE, limit - len(mapping))
            if want <= 0:
                break
            params = dict(params_base, limit=str(want), offset=str(offset))
            resp = await client.get(f"{url.rstrip('/')}/rest/v1/{TABLE}", headers=headers, params=params)
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                break
            for row in rows:
                call_id = row.get(CALL_ID_COLUMN)
                stat = row.get(ID_COLUMN)
                # A tb_stat row with no call_id cannot be joined to anything; the agent
                # writes the uuid at call start, so this means the row predates that.
                if call_id and stat is not None:
                    mapping[str(call_id)] = str(stat)
            offset += len(rows)
            if len(rows) < want:
                break
    return mapping


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--supabase-url", default=os.getenv("SUPABASE_URL"))
    ap.add_argument("--supabase-key", default=os.getenv("SUPABASE_SECRET_KEY") or os.getenv("SUPABASE_KEY"))
    ap.add_argument("--days", type=int, default=None, help="only tb_stat rows this recent (default: all)")
    ap.add_argument("--limit", type=int, default=None, help="cap on tb_stat rows read")
    ap.add_argument("--agent", default=None, help="only update calls with this agent_id")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.supabase_url or not args.supabase_key:
        print("Need --supabase-url and --supabase-key (or SUPABASE_URL / SUPABASE_SECRET_KEY).")
        return 2

    print(f"Reading {TABLE} from Supabase...")
    mapping = await fetch_mapping(args.supabase_url, args.supabase_key, args.days, args.limit)
    print(f"  {len(mapping):,} rows with a call_id")
    if not mapping:
        return 0

    await init_db()
    updated = skipped = unmatched = 0
    samples: list[str] = []

    async with SessionLocal() as session:
        query = select(Call).where(Call.external_id.in_(list(mapping.keys())))
        if args.agent:
            query = query.where(Call.agent_id == args.agent)
        calls = (await session.execute(query)).scalars().all()
        print(f"  {len(calls):,} of them exist in CallHarness")

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

        unmatched = len(mapping) - len(calls)
        for line in samples:
            print(line)
        print(f"\n  update {updated:,} | already correct {skipped:,} | in Supabase but not here {unmatched:,}")

        if args.dry_run:
            print("\nDry run - nothing written. Re-run without --dry-run to apply.")
            await session.rollback()
            return 0
        await session.commit()
        print("\nWritten.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

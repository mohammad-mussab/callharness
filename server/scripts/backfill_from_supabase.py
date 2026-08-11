"""Backfill historical calls from a Supabase table into OpenCall for analysis.

Your production agent may have been logging calls to Supabase (see the agent's
own `db.py` / `log_call()`) long before it was wired up to OpenCall's live
ingestion API. This script pulls a *bounded* window of those historical rows
(by age and/or count, so you control LLM analysis cost) and re-ingests them
through OpenCall's normal `POST /api/v1/calls` endpoint — the existing
analysis worker then picks them up exactly like a live call. Safe to re-run:
ingestion is idempotent on `external_id`, so already-imported rows are skipped
server-side instead of duplicated.

No new dependency: talks to Supabase's PostgREST REST API directly over
httpx, rather than requiring the `supabase` Python package.

IMPORTANT — verify these three constants against your actual table before
running for real. They're inferred from the calling agent's `log_call()`
insert shape, not from the table's DDL:
    ID_COLUMN, CREATED_AT_COLUMN, TRANSCRIPT_COLUMN

Usage (from the server/ directory, with the venv activated):
    python -m scripts.backfill_from_supabase \\
        --supabase-url https://xxxx.supabase.co --supabase-key <service-or-anon-key> \\
        --days 10 --limit 1000 --dry-run

Drop --dry-run to actually POST to OpenCall once the parsed output looks right.
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx

# --- adjust these to match your actual Supabase table -----------------------
TABLE = "call_logs"
ID_COLUMN = "id"
CREATED_AT_COLUMN = "created_at"
TRANSCRIPT_COLUMN = "transcript"
PHONE_COLUMN = "lead_phone"
ASSISTANT_LABEL = "Ava"
CALLER_LABEL = "Caller"
# Columns to carry over into the OpenCall call's `metadata` (rest are ignored)
METADATA_COLUMNS = [
    "lead_name",
    "lead_type",
    "timeline",
    "area",
    "price_range",
    "pre_approved",
    "property_needs",
    "hot_lead",
    "notes",
    "booking_id",
]
# -----------------------------------------------------------------------------


def parse_transcript(text: str) -> list[dict]:
    """Split a flattened "Speaker: text" transcript back into turns.

    Lines without a recognized speaker prefix are appended to the previous
    turn (handles wrapped/multi-line utterances).
    """
    turns: list[dict] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(f"{ASSISTANT_LABEL}:"):
            turns.append({"role": "assistant", "text": line[len(ASSISTANT_LABEL) + 1 :].strip()})
        elif line.startswith(f"{CALLER_LABEL}:"):
            turns.append({"role": "user", "text": line[len(CALLER_LABEL) + 1 :].strip()})
        elif turns:
            turns[-1]["text"] += " " + line
    return [t for t in turns if t["text"]]


async def fetch_rows(
    client: httpx.AsyncClient, supabase_url: str, since_iso: str, limit: int
) -> list[dict]:
    resp = await client.get(
        f"{supabase_url}/rest/v1/{TABLE}",
        params={
            "select": "*",
            f"{CREATED_AT_COLUMN}": f"gte.{since_iso}",
            "order": f"{CREATED_AT_COLUMN}.desc",
            "limit": str(limit),
        },
    )
    resp.raise_for_status()
    return resp.json()


def build_payload(row: dict, agent_id: str) -> dict | None:
    turns = parse_transcript(row.get(TRANSCRIPT_COLUMN, ""))
    if not turns:
        return None
    metadata = {k: row[k] for k in METADATA_COLUMNS if row.get(k) not in (None, "")}
    return {
        "external_id": f"supabase:{TABLE}:{row.get(ID_COLUMN)}",
        "agent_id": agent_id,
        "direction": "inbound",
        "from_number": row.get(PHONE_COLUMN),
        "started_at": row.get(CREATED_AT_COLUMN),
        "recording_url": row.get("recording_url") or None,
        "metadata": metadata or None,
        "turns": turns,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--supabase-url", default=os.environ.get("SUPABASE_URL"))
    parser.add_argument("--supabase-key", default=os.environ.get("SUPABASE_KEY"))
    parser.add_argument("--opencall-url", default=os.environ.get("OPENCALL_URL", "http://localhost:8010"))
    parser.add_argument("--opencall-api-key", default=os.environ.get("OPENCALL_API_KEY"))
    parser.add_argument("--agent-id", default=os.environ.get("OPENCALL_AGENT_ID", "default"))
    parser.add_argument("--days", type=int, default=10, help="Only import rows from the last N days")
    parser.add_argument("--limit", type=int, default=1000, help="Max rows to import in this run")
    parser.add_argument("--dry-run", action="store_true", help="Parse and print, don't POST to OpenCall")
    args = parser.parse_args()

    if not (args.supabase_url and args.supabase_key):
        sys.exit("Missing --supabase-url/--supabase-key (or SUPABASE_URL/SUPABASE_KEY env vars)")

    since_iso = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
    headers = {"apikey": args.supabase_key, "Authorization": f"Bearer {args.supabase_key}"}

    async with httpx.AsyncClient(headers=headers, timeout=30) as supa_client:
        rows = await fetch_rows(supa_client, args.supabase_url, since_iso, args.limit)

    print(f"Fetched {len(rows)} row(s) from {TABLE} since {since_iso}")

    imported = skipped = failed = 0
    opencall_headers = {"x-api-key": args.opencall_api_key} if args.opencall_api_key else {}
    async with httpx.AsyncClient(base_url=args.opencall_url, headers=opencall_headers, timeout=30) as oc_client:
        for row in rows:
            payload = build_payload(row, args.agent_id)
            if payload is None:
                skipped += 1
                print(f"  skip {row.get(ID_COLUMN)}: no parseable transcript")
                continue
            if args.dry_run:
                print(f"  [dry-run] {payload['external_id']}: {len(payload['turns'])} turns")
                continue
            try:
                resp = await oc_client.post("/api/v1/calls", json=payload)
                resp.raise_for_status()
                imported += 1
            except httpx.HTTPStatusError as exc:
                failed += 1
                print(f"  FAILED {row.get(ID_COLUMN)}: {exc.response.status_code} {exc.response.text[:200]}")

    if not args.dry_run:
        print(f"Done: {imported} imported/updated, {skipped} skipped, {failed} failed")
        print("Analysis will run automatically via the normal worker poll loop.")


if __name__ == "__main__":
    asyncio.run(main())

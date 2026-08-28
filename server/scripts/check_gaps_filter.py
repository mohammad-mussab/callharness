"""Regression check for the Missing Information page's status filter and row cap.

    cd server && python -m scripts.check_gaps_filter        # or: python scripts/check_gaps_filter.py

Runs the real /knowledge-gaps endpoint against a THROWAWAY SQLite database (no API key,
no LLM, no network, never the production database) seeded to reproduce the shape the live
Lazio data actually had: an OLD population of grouped-and-verified records and a NEWER
population of ungrouped ones, split by date.

WHAT IT CATCHES, AND WHY THAT SHAPE
Verification is a batch somebody runs occasionally, so verified records are always the
older ones. The report reached them only through their calls, so `days` aged them out —
and the rank key's newest-first tiebreak then sank the survivors into the tail that
`limit` discards. Together those hid 133 of 148 records already PROVED missing against
the customer's own lookup API, at every filter setting, with nothing on the page saying
so. The seeded split is what makes both failures reproduce; a fixture with one uniform
population passes even when both bugs are present.

Also pins the things the fix must NOT change: `calls_scanned`, `calls_with_gaps`,
`gap_call_rate` and `ungrouped_count` stay computed from the window alone, so the
headline percentage cannot acquire a different denominator from the list beneath it.
"""
import asyncio, os, pathlib, sys, uuid
from datetime import timedelta

DB_FILE = "_check_gaps_filter.db"
os.environ["CALLHARNESS_DATABASE_URL"] = f"sqlite+aiosqlite:///./{DB_FILE}"
os.environ["CALLHARNESS_ANALYSIS_ENABLED"] = "false"
os.environ["CALLHARNESS_LLM_PROVIDER"] = "none"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.db import SessionLocal as async_session, engine, init_db          # noqa: E402
from app.models import Call, GapGroup, utcnow              # noqa: E402
from app.routes.analytics import knowledge_gaps            # noqa: E402
from app.gap_grouping import GAP_NEEDS_REVIEW              # noqa: E402

NOW = utcnow()
FAILED = []

def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")
    if not ok:
        FAILED.append(name)

async def seed(s):
    def call(days_ago, *, bucket, group=None, q=None, transferred=False):
        c = Call(
            id=str(uuid.uuid4()), agent_id="Lazio",
            external_id=f"VFY-{uuid.uuid4().hex[:10]}",
            started_at=NOW - timedelta(days=days_ago),
            analysis_status="completed", bucket=bucket,
            unanswered_query=q, gap_group_id=group,
            gap_group_question=q if group else None,
            transferred=transferred, success=False if bucket else True,
        )
        s.add(c)
        return c

    # 20 OLD verified records (25 days ago) — outside every window the page offers by
    # default. This is the production shape: verification last ran weeks ago.
    for i in range(15):
        gid = f"g{i}"
        call(25, bucket="record_missing", group=gid, q=f"orari sede {i}")
        s.add(GapGroup(id=gid, agent_id="Lazio", question=f"orari sede {i}",
                       status="confirmed_missing", status_at=NOW))
    for i in range(15, 20):
        gid = f"g{i}"
        call(25, bucket="record_missing", group=gid, q=f"prezzo esame {i}")
        s.add(GapGroup(id=gid, agent_id="Lazio", question=f"prezzo esame {i}",
                       status="found_in_source", status_at=NOW))

    # 30 NEW unverified gap calls (2 days ago), inside the 7-day window.
    for i in range(30):
        call(2, bucket="record_missing", q=f"nuova domanda {i}", transferred=(i % 3 == 0))

    # One grouped+verified record whose call is INSIDE the window: returned by BOTH
    # queries, so this is the dedup test.
    s.add(GapGroup(id="gdup", agent_id="Lazio", question="dentro finestra",
                   status="confirmed_missing", status_at=NOW))
    call(2, bucket="record_missing", group="gdup", q="dentro finestra")

    # Needs-review, old: must NOT appear (the unbounded query excludes it).
    call(25, bucket="record_missing", group=GAP_NEEDS_REVIEW, q="curva glicemica")
    # Needs-review, in window: must appear.
    call(2, bucket="record_missing", group=GAP_NEEDS_REVIEW, q="Levico Butter?")

    # 70 answered calls in the window, so calls_scanned/gap_call_rate are checkable.
    for _ in range(70):
        call(2, bucket="answered")
    await s.commit()

async def main():
    for f in (DB_FILE,):
        if os.path.exists(f):
            os.remove(f)
    await init_db()
    async with async_session() as s:
        await seed(s)

    async def q(**kw):
        async with async_session() as s:
            return await knowledge_gaps(session=s, **kw)

    print("\n=== THE ORIGINAL BUG: default 7-day view shows verified records ===")
    r = await q(agent_id=None, days=7, min_count=1, status=None, limit=5000)
    st = {}
    for g in r.groups:
        st[g.status] = st.get(g.status, 0) + 1
    print(f"  statuses at days=7: {st}")
    check("confirmed_missing visible at days=7", st.get("confirmed_missing"), 16)
    check("found_in_source visible at days=7", st.get("found_in_source"), 5)
    check("not_verified visible at days=7", st.get("not_verified"), 30)

    print("\n=== THE DROPDOWN: status filter, at the DEFAULT window ===")
    r = await q(agent_id=None, days=7, min_count=1, status="confirmed_missing", limit=5000)
    check("rows returned", len(r.groups), 16)
    check("total_rows", r.total_rows, 16)
    check("all rows have that status", {g.status for g in r.groups}, {"confirmed_missing"})
    check("status_filter echoed", r.status_filter, ["confirmed_missing"])
    check("needs_review emptied by filter", len(r.needs_review), 0)

    print("\n=== filter is window-independent (the old failure) ===")
    for d in (1, 7, 30, 365):
        rr = await q(agent_id=None, days=d, min_count=1,
                     status="confirmed_missing", limit=5000)
        check(f"days={d} -> 16 confirmed_missing", len(rr.groups), 16)

    print("\n=== multi-status filter ===")
    r = await q(agent_id=None, days=7, min_count=1,
                status="confirmed_missing,found_in_source", limit=5000)
    check("confirmed+found rows", len(r.groups), 21)

    print("\n=== METRICS STAY WINDOWED (must not pick up the all-time rows) ===")
    r = await q(agent_id=None, days=7, min_count=1, status=None, limit=5000)
    check("calls_scanned (window only)", r.calls_scanned, 102)
    check("calls_with_gaps (window only)", r.calls_with_gaps, 32)
    check("gap_call_rate", round(r.gap_call_rate, 6), round(32 / 102, 6))
    check("ungrouped_count (window only)", r.ungrouped_count, 30)

    print("\n=== DEDUP: a grouped call inside the window is counted once ===")
    dup = [g for g in r.groups if g.group_id == "gdup"]
    check("gdup rows", len(dup), 1)
    check("gdup count", dup[0].count if dup else None, 1)
    check("gdup examples", len(dup[0].examples) if dup else None, 1)

    print("\n=== needs_review stays windowed ===")
    check("needs_review rows", len(r.needs_review), 1)
    check("needs_review is the in-window one",
          r.needs_review[0].question if r.needs_review else None, "Levico Butter?")

    print("\n=== TRUNCATION IS REPORTED (option D) ===")
    r = await q(agent_id=None, days=7, min_count=1, status=None, limit=10)
    check("groups capped", len(r.groups), 10)
    check("total_rows reports the real number", r.total_rows, 51)

    print("")
    print("=== A QUIET WINDOW STILL SHOWS VERIFIED RECORDS ===")
    # days=1: no seeded call is that recent, so calls_scanned is 0. The verified records
    # must survive it, or the fix reintroduces itself at a new setting.
    r = await q(agent_id=None, days=1, min_count=1, status=None, limit=5000)
    check("calls_scanned is 0 (quiet window)", r.calls_scanned, 0)
    check("gap_call_rate is null", r.gap_call_rate, None)
    check("grouped records still listed", len(r.groups), 21)
    check("verified still listed",
          sum(1 for g in r.groups if g.status == "confirmed_missing"), 16)

    print("\n=== a bad status fails loudly rather than returning everything ===")
    try:
        await q(agent_id=None, days=7, min_count=1, status="verifed", limit=100)
        check("rejects unknown status", "no error", "HTTP 400")
    except Exception as e:
        code = getattr(e, "status_code", None)
        check("rejects unknown status", code, 400)

    await engine.dispose()
    # The throwaway database is removed on the way out as well as on the way in, so a
    # failing run does not leave a stale file that the next one would silently reuse.
    if os.path.exists(DB_FILE):
        os.remove(DB_FILE)
    print("\n" + ("ALL CHECKS PASSED" if not FAILED else f"FAILURES: {FAILED}"))
    return 1 if FAILED else 0

sys.exit(asyncio.run(main()))

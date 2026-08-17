"""Prove that a "missing record" really is missing, by re-asking the lookup API.

WHY THIS EXISTS

The Missing Information page is the customer-facing deliverable: the list of questions
their data could not answer, which somebody then has to go and add. Until now nothing on
that list had been checked. A record is on it because the judge bucketed some calls
`record_missing`, and three different things produce that bucket:

    the record is genuinely absent          -> a content task for the customer
    the record exists, retrieval missed it  -> OUR bug, and sending it wastes their time
    the question was mis-heard              -> nobody can add "informazioni sul centro
                                               di Pietra"; the caller said Torre in Pietra

All three end the call identically and read identically in a transcript. The only way to
tell them apart is to ask the source again and look at what comes back.

THE UNIT IS THE RECORD, NOT THE CALL

Verification runs per gap group (gap_grouping.py), never per call. Two reasons, and both
matter:

  * The canonical question a group carries is already the clean phrasing — the grouping
    model wrote it to name the subject and the attribute plainly. The raw
    `unanswered_query` of one call is whatever the caller rambled or was cut off saying,
    and sending that to a semantic search tests the transcription, not the database.
  * A verdict is about a record. Storing it per call would let one call claim "verified
    missing" for a question that was only ever probed in another call's wording.

A group of ONE still qualifies: the model looked at that question and decided it matched
nothing else, which is a finished judgement. Only calls that never went through grouping,
and the reserved needs-review group, are excluded.

THE FOUR THINGS THAT MAKE THIS MORE THAN "SEND THE STRING AGAIN"

1. **Ask more than one way.** A single phrasing that returns nothing does not show the
   record is absent — semantic search misses. So we send the canonical wording plus two
   paraphrases that mean the same thing in different words. One hit anywhere and the
   record exists, whatever the call transcript suggested.

2. **Ask every source, not just the one the agent used.** The graph answers a miss with
   "Non ho una risposta per questo cerca nel RAG" ("I have no answer for this, look in
   the RAG") — a routing instruction telling the agent to try the other tool. Measured
   over 135 live record_missing calls, 44 of them (32.6%) rest on nothing but that
   sentence, with the agent never making the follow-up call. Probing both sources settles
   every one of those without a human reading a transcript.

3. **Ask the right region's sources.** See `probes_for_agent`.

4. **Check the question is answerable before believing the answer.** A garbled question
   comes back empty because it is nonsense, not because the data is absent. That verdict
   outranks everything else here, so a mis-heard phrase can never reach the customer as a
   record to add.

WHAT THIS DELIBERATELY DOES NOT DO

It never writes `Call.bucket`. That is the judge's verdict about the conversation, and
`scripts/reanalyze.py` rewrites it — a verification result stored there would be silently
destroyed by the next backfill. Verification state lives on `GapGroup` and in the
`gap_verifications` table.

It is never triggered automatically. Every probe spends the API owner's money (their RAG
endpoint is an STT-cleanup call, an embedding and an answer-generation call; their graph
endpoint is two or three gpt-4.1 calls plus a Neo4j round trip) and lands on a service
that is also answering live phone calls, with no rate limiting or caching on that side.
So it runs when a person asks it to, two requests at a time, and never on a timer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .analysis.engine import build_transcript
from .analysis.llm import LLMError, chat_json
from .config import settings
from .gap_grouping import GAP_NEEDS_REVIEW
from .knowledge_gaps import EMPTY, ERROR, OK, RECORD_MISSING_BUCKET, classify_tool_result  # noqa: F401  (EMPTY re-exported)
from .models import AnalysisConfig, Call, GapGroup, GapVerification, utcnow

logger = logging.getLogger("callharness.gap_verification")

# ---------------------------------------------------------------------------
# Statuses
# ---------------------------------------------------------------------------

NOT_VERIFIED = "not_verified"
CONFIRMED_MISSING = "confirmed_missing"
FOUND_IN_SOURCE = "found_in_source"
BAD_QUESTION = "bad_question"
VERIFY_ERROR = "verify_error"
SENT = "sent"
ADDED = "added"
ADDED_CONFIRMED = "added_confirmed"

# The four a verification run can produce. The other four are set by a person.
VERDICTS = (CONFIRMED_MISSING, FOUND_IN_SOURCE, BAD_QUESTION, VERIFY_ERROR)

# Everything a status may hold. NULL is equivalent to NOT_VERIFIED.
GAP_STATUSES = (NOT_VERIFIED, *VERDICTS, SENT, ADDED, ADDED_CONFIRMED)

# Statuses a person is allowed to set by hand. Verification verdicts are excluded on
# purpose: claiming "confirmed_missing" without a probe behind it is exactly the
# unevidenced assertion this module exists to remove.
MANUAL_STATUSES = (SENT, ADDED, NOT_VERIFIED)

# The only status that may be reported to the customer. Everything else is either
# unproven, our own bug, or already sent.
SENDABLE = CONFIRMED_MISSING

# How many paraphrases a record is worth. Two plus the canonical is enough to distinguish
# "semantic search missed one phrasing" from "the record is absent", without multiplying
# spend on someone else's API — each variant goes to every source, so this is already 6
# requests per record against two sources.
_PARAPHRASE_COUNT = 2

# Their graph endpoint is 2-3 sequential gpt-4.1 calls plus a Neo4j round trip, so a
# response can legitimately take a long time. A timeout here would be recorded as
# `verify_error`, which is honest but useless, so the limit is generous.
_PROBE_TIMEOUT_SECONDS = 90.0

# Two at a time, with a pause between. This is a load limit on somebody else's production
# service, not a throughput tuning knob — do not raise it to make a backfill finish sooner.
_PROBE_CONCURRENCY = 2
_PROBE_DELAY_SECONDS = 0.5

# Enough of a response to judge it and to show on the row. Their graph answers carry the
# full weekly hours plus an upcoming-closures block, which is exactly the part that proves
# a hit, so this matches the 2000 the analysis prompt allows rather than the 200 that
# truncated 43% of real results mid-sentence.
_RESPONSE_CHAR_LIMIT = 2000

# How many member questions to show the model when reading a group. Enough to spot the
# one that named a day; not so many that a large group turns into a wall of near-identical
# sentences.
_MAX_MEMBER_QUESTIONS = 6

# Replies that mean "this request never reached a lookup", checked BEFORE anything else.
#
# This exists because of a specific, live near-miss. These backends dispatch on the tool
# name inside the request body, and answer an unrecognised one with 200 OK and the plain
# sentence "Tool non supportato: <name>" ("tool not supported"). It matters which way
# that gets read:
#
#   read as a hit   -> the LLM judge says it does not answer the question, and the run
#                      falls through to confirmed_missing
#   read as empty   -> confirmed_missing directly
#
# Either path puts "we pointed at the wrong URL" onto the customer's list as "this record
# is missing from your database", for every single gap, with full confidence and an
# evidence trail that looks correct. The Lazio agent's own .env posts knowledge_base_lazio
# to /query_new, which on the deployed build matches only knowledge_base_new — so this is
# the configuration we would most likely have started from.
#
# The same applies to a source that is down ("Sistema RAG non disponibile al momento") or
# a body template missing its argument: no lookup ran, so nothing was learned. These are
# faults in our setup, not findings about their data, and they belong in verify_error.
_PROBE_FAULT_MARKERS = (
    "tool non supportato",
    "tool not supported",
    "non supportato in",
    "errore: manca il parametro",
    "manca l'argomento",
    "non disponibile al momento",
    "si è verificato un errore",
    "si e' verificato un errore",
)


class ProbeConfigError(ValueError):
    """A configured probe is unusable — bad template, missing URL, unreadable path."""


class NoProbeForRegion(ProbeConfigError):
    """No source is configured for this record's region.

    Distinct from ProbeConfigError because the two mean different things to a batch: an
    unroutable region is one record's problem and the run carries on, while no probes at
    all would fail every remaining record identically and the run stops.
    """


# ---------------------------------------------------------------------------
# Probe configuration
# ---------------------------------------------------------------------------


def _usable(probe: Any) -> bool:
    if not isinstance(probe, dict) or probe.get("enabled") is False:
        return False
    return bool(probe.get("url")) and bool(probe.get("body_template"))


def enabled_probes(config: AnalysisConfig) -> list[dict[str, Any]]:
    """Every usable source, ignoring region. For counting and for the Settings page."""
    return [p for p in (config.lookup_probes or []) if _usable(p)]


def probes_for_agent(config: AnalysisConfig, agent_id: str | None) -> list[dict[str, Any]]:
    """The sources to re-ask for a record belonging to this region.

    A probe with an empty `agent_ids` serves every region, so a single-region install
    needs no extra setup. A probe that names regions serves only those.

    THIS FILTER IS A CORRECTNESS CONTROL, NOT A CONVENIENCE. These backends dispatch on a
    region-specific tool name inside the request body: the Lazio agent sends
    `knowledge_base_lazio` to /lazio/rag_lazio, while /query_new matches only Piemonte's
    `knowledge_base_new` and answers anything else with 200 OK and "Tool non supportato".
    Probing across regions therefore does not fail loudly — it returns a polite sentence
    that reads as "nothing found", which becomes "this record is missing from your
    database" for every gap checked. _PROBE_FAULT_MARKERS catches that sentence, but only
    the ones we have seen; not sending the request at all is the control that does not
    depend on a phrase list.
    """
    wanted = (agent_id or "").strip().lower()
    out: list[dict[str, Any]] = []
    for probe in enabled_probes(config):
        agents = probe.get("agent_ids") or []
        if not isinstance(agents, list) or not agents:
            out.append(probe)
            continue
        if wanted and any(str(a).strip().lower() == wanted for a in agents):
            out.append(probe)
    return out


# Guard against a pathological template whose strings keep parsing as JSON. Three is
# already one more level than the deepest real shape (the VAPI envelope nests once).
_MAX_TEMPLATE_DEPTH = 3


def _substitute(node: Any, query: str, depth: int = 0) -> Any:
    """Walk a parsed body and put the question wherever {{query}} appears."""
    if isinstance(node, dict):
        return {k: _substitute(v, query, depth) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute(v, query, depth) for v in node]
    if not isinstance(node, str) or "{{query}}" not in node:
        return node
    # A string that is itself a JSON document is a nested payload, not text — the VAPI
    # envelope these backends use carries its `arguments` that way. Substitute inside it
    # and re-serialize, so json.dumps does the escaping at each level.
    if depth < _MAX_TEMPLATE_DEPTH:
        try:
            inner = json.loads(node)
        except ValueError:
            pass
        else:
            return json.dumps(_substitute(inner, query, depth + 1), ensure_ascii=False)
    return node.replace("{{query}}", query)


def render_body(body_template: str, query: str) -> Any:
    """Substitute the question into a probe's request body.

    Done by parsing the template FIRST and substituting into the parsed structure, rather
    than by pasting escaped text into the template string. The difference is not stylistic.

    These backends take a VAPI envelope whose `arguments` field is a JSON document
    serialized into a string inside the outer JSON, so a question sitting in there is
    escaped twice over. Escaping it once produces an outer document that parses cleanly
    and an inner one that does not — and Italian questions routinely carry apostrophes
    ("Fate l'esame del PSA?") and quoted terms, so this is the common case, not an edge
    case. Every affected record would come back `verify_error`, or worse, reach the server
    truncated at the quote and get answered as if it were a different question.

    Parsing first sidesteps the whole problem: the placeholder is inert text to the JSON
    parser, and json.dumps applies exactly the right amount of escaping at each level on
    the way back out.
    """
    try:
        template = json.loads(body_template)
    except ValueError as exc:
        raise ProbeConfigError(f"body_template is not valid JSON: {exc}") from exc
    return _substitute(template, query)


def read_path(payload: Any, path: str) -> Any:
    """Pull the answer out of a response using a dotted path like "results.0.result".

    Deliberately tiny and dependency-free: a full JSONPath implementation would be a new
    dependency for a feature whose realistic paths are two segments long. An unresolvable
    path returns None, which the caller reads as "nothing came back" rather than raising —
    a mistyped path in Settings should show up as a probe that finds nothing, not as a
    crashed sweep.
    """
    if not path:
        return payload
    current = payload
    for part in path.split("."):
        if current is None:
            return None
        if part.isdigit() and isinstance(current, (list, tuple)):
            index = int(part)
            current = current[index] if index < len(current) else None
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def resolve_probe_date(date_meant: date | None, today: date) -> tuple[date | None, bool]:
    """Which date to actually ask about, and whether it had to be moved.

    A large share of these questions name a day — "orari della sede di Boccea domani"
    ("Boccea branch hours tomorrow"). Asking that today asks about a different day, so
    the word is first resolved against the call's own start time to the calendar date the
    caller meant. Then:

      still ahead of us -> ask about it unchanged. It is the caller's real question.
      already gone      -> ask about the next occurrence of the same weekday instead.

    The shift matters because an empty answer about a day that is over is not evidence of
    a missing record — plenty of sources simply stop returning past days — and a false
    "missing" is the one outcome this module exists to prevent. Same weekday rather than
    "tomorrow" because opening hours are a weekly pattern: a Friday question answered
    about a Tuesday tests the wrong row.

    The cost of the shift is real and is recorded on the row: a one-off closure on the
    original date (a public holiday, say) is invisible to the substituted date. Both
    dates are stored so the substitution is never silent.
    """
    if date_meant is None:
        return None, False
    if date_meant >= today:
        return date_meant, False
    # Next same weekday strictly after today, so "today" is never the answer either — a
    # question about opening hours asked at 18:00 today is as unanswerable as a past one.
    days_ahead = (date_meant.weekday() - today.weekday()) % 7 or 7
    return today + timedelta(days=days_ahead), True


def _parse_date(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _format_date_it(value: date) -> str:
    months = (
        "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
        "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre",
    )
    return f"{value.day} {months[value.month - 1]} {value.year}"


# ---------------------------------------------------------------------------
# Step 1 — read the question
# ---------------------------------------------------------------------------

_PREPARE_SYSTEM = """You prepare a failed lookup for re-testing against the knowledge \
source it was originally sent to. You are given a phone call between a caller and an AI \
assistant, and the question which came back with nothing. Respond with a single JSON \
object and nothing else."""


def _prepare_prompt(call: Call, canonical: str, member_questions: list[str]) -> str:
    started = call.started_at
    weekday = started.strftime("%A") if started else "unknown"
    date_str = started.date().isoformat() if started else "unknown"
    transcript = build_transcript(call)

    asked_block = ""
    others = [q for q in member_questions if q.strip() and q.strip() != canonical.strip()]
    if others:
        listed = "\n".join(f"  - {q}" for q in others[:_MAX_MEMBER_QUESTIONS])
        asked_block = f"""
THE SAME RECORD WAS ASKED FOR ON OTHER CALLS, IN THESE WORDS:
{listed}
"""

    return f"""The call below took place on {date_str} ({weekday}).

QUESTION THAT RETURNED NOTHING (the wording to re-test):
{canonical}
{asked_block}
FULL CALL, including the assistant's tool calls and their real results:
{transcript}

Produce this JSON:

- "sensible" (boolean): true if the question is something a person could actually look up \
and answer. false if it was built from mis-heard speech — a place name that is not a real \
place, a service name that is not a real service, words that do not form a question. Look \
at the call for the evidence: an assistant asking the same thing several times, saying it \
did not understand, or the caller's words arriving as fragments all mean the question that \
finally went out was assembled from noise. If the caller was heard correctly, this is true \
even when the question is unusual or very specific.
- "reason" (string): one sentence saying why, quoting the part of the call that shows it.
- "corrected" (string or null): when "sensible" is false and the call makes the intended \
question recoverable, the question as the caller most likely meant it. Otherwise null. Do \
not guess from nothing.
- "date_meant" (string "YYYY-MM-DD", or null): if the question — or any of the other \
wordings above — refers to a particular day ("domani", "oggi", "il 14 agosto", a weekday \
name), the calendar date that means, worked out from the call date above. null if no day \
is named anywhere.
- "normalized" (string): the question to re-test exactly as written above, with only one \
change: any word or phrase naming a day replaced by "{{{{date}}}}". If it names no day, \
repeat it unchanged. Change nothing else — not the wording, not the spelling, not the word \
order.
- "dated_variant" (string or null): only when "date_meant" is set AND the question to \
re-test does not itself name a day. Take the wording from the list above that DOES name a \
day, and repeat it with "{{{{date}}}}" in place of the day. This exists because a source \
can hold a branch's normal weekly hours while missing the closure calendar for the \
specific day the caller asked about, and asking only the undated question would read that \
as an answer. null when the question already names a day, or when no day was ever named.
- "paraphrases" (array of exactly {_PARAPHRASE_COUNT} strings): the question to re-test \
asked in different words. Keep the meaning identical and keep every specific detail — the \
branch, the exam, the service — exactly as written, including the spelling. Vary the \
wording and the sentence shape, not the facts. These exist to find out whether the source \
holds the record under different phrasing, so a paraphrase that only reorders the original \
words is useless.

If a day is involved, write "{{{{date}}}}" in place of the date in every paraphrase too, \
and leave "date_meant" to carry the actual day. Do not write a date yourself."""


def _clean_str(value: Any, limit: int) -> str | None:
    return str(value).strip()[:limit] if isinstance(value, str) and value.strip() else None


async def _prepare(call: Call, canonical: str, member_questions: list[str]) -> dict[str, Any]:
    result = await chat_json(_PREPARE_SYSTEM, _prepare_prompt(call, canonical, member_questions))
    paraphrases = [
        str(p).strip()
        for p in (result.get("paraphrases") or [])
        if isinstance(p, str) and p.strip()
    ]
    return {
        # Missing/unparseable reads as sensible: refusing to check a question because the
        # model did not answer the question about the question would hide a real gap.
        "sensible": result.get("sensible") is not False,
        "reason": str(result.get("reason") or "").strip()[:1000],
        "corrected": _clean_str(result.get("corrected"), 500),
        "date_meant": _parse_date(result.get("date_meant")),
        # Falls back to the question as it stands. Losing the placeholder only means the
        # canonical wording keeps its relative day word, which is worse than the
        # paraphrases but still a real question — better than probing nothing.
        "normalized": _clean_str(result.get("normalized"), 500),
        "dated_variant": _clean_str(result.get("dated_variant"), 500),
        "paraphrases": paraphrases[:_PARAPHRASE_COUNT],
    }


# ---------------------------------------------------------------------------
# Step 2 — send the probes
# ---------------------------------------------------------------------------


async def _send_probe(
    client: httpx.AsyncClient,
    probe: dict[str, Any],
    variant: str,
    variant_kind: str,
) -> dict[str, Any]:
    """One request. Never raises — a dead endpoint is a result, not an exception.

    Same contract as analysis/alerts.py's delivery helper: everything that can go wrong
    is recorded on the row instead of aborting the sweep, because a batch that dies on
    record 12 of 135 leaves you worse off than one that records 3 failures.
    """
    record: dict[str, Any] = {
        "probe_key": probe.get("key") or probe.get("label") or "probe",
        "probe_label": probe.get("label") or probe.get("key") or "probe",
        "variant": variant,
        "variant_kind": variant_kind,
        "url": probe.get("url"),
        "http_status": None,
        "ms": None,
        "response": None,
        "verdict": ERROR,
    }
    try:
        body = render_body(probe["body_template"], variant)
    except ProbeConfigError as exc:
        record["response"] = str(exc)[:_RESPONSE_CHAR_LIMIT]
        return record

    started = time.monotonic()
    try:
        resp = await client.request(
            (probe.get("method") or "POST").upper(),
            probe["url"],
            json=body,
            headers=probe.get("headers") or None,
        )
    except Exception as exc:  # noqa: BLE001
        record["ms"] = round((time.monotonic() - started) * 1000)
        record["response"] = f"{type(exc).__name__}: {exc}"[:_RESPONSE_CHAR_LIMIT]
        return record

    record["ms"] = round((time.monotonic() - started) * 1000)
    record["http_status"] = resp.status_code
    if resp.status_code >= 300:
        record["response"] = resp.text[:_RESPONSE_CHAR_LIMIT]
        return record

    try:
        payload = resp.json()
    except ValueError:
        # A source that answers plain text rather than JSON is legitimate, and its body is
        # still the answer — so it goes through the same reading below rather than being
        # scored here. Scoring it here as "content, therefore a hit" would skip both the
        # fault markers and the empty markers, so a plain-text "Non ho trovato..." would
        # be read as the record existing.
        answer: Any = resp.text
    else:
        answer = read_path(payload, probe.get("result_path") or "")

    text = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False, default=str)
    record["response"] = text[:_RESPONSE_CHAR_LIMIT]

    # Our fault before their data: a request that never reached a lookup teaches nothing
    # about whether the record exists. Checked first because several of these phrases
    # would otherwise be swallowed by the empty-marker list below and reported as a
    # confirmed missing record.
    lowered = text.lower()
    if any(marker in lowered for marker in _PROBE_FAULT_MARKERS):
        record["verdict"] = ERROR
        return record

    # classify_tool_result is the same OK/EMPTY/ERROR reader the rest of the codebase
    # uses on tool results, and this is the same kind of payload — the tool result, just
    # fetched again. Sharing it keeps one list of "means nothing was found" phrases
    # rather than a second copy that drifts.
    record["verdict"] = classify_tool_result(answer)
    return record


async def test_probe(probe: dict[str, Any], query: str) -> dict[str, Any]:
    """Send one question through one probe and hand back the raw attempt record.

    Exists for the Settings "Test" button. A probe pointed at the wrong URL, or carrying
    a tool name the server does not dispatch on, still answers 200 OK with a polite
    sentence — and read as data, that sentence becomes "this record is missing from your
    database" for every gap. Seeing the reply once is the only way to know.
    """
    async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SECONDS) as client:
        return await _send_probe(client, probe, query, "test")


async def _run_probes(
    probes: list[dict[str, Any]], variants: list[tuple[str, str]]
) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(_PROBE_CONCURRENCY)

    async def one(client, probe, variant, kind):
        async with semaphore:
            record = await _send_probe(client, probe, variant, kind)
            # Spacing requests out inside the semaphore rather than between batches keeps
            # a steady low rate instead of bursts, which is gentler on a service that is
            # simultaneously handling phone calls.
            await asyncio.sleep(_PROBE_DELAY_SECONDS)
            return record

    async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SECONDS) as client:
        return list(
            await asyncio.gather(
                *(
                    one(client, probe, variant, kind)
                    for probe in probes
                    for variant, kind in variants
                )
            )
        )


# ---------------------------------------------------------------------------
# Step 3 — decide what came back
# ---------------------------------------------------------------------------

_JUDGE_SYSTEM = """You decide whether a knowledge source actually answered a question. \
You are given one question and the replies the source gave to several rewordings of it. \
Respond with a single JSON object and nothing else."""


def _judge_prompt(question: str, found: list[dict[str, Any]]) -> str:
    blocks = []
    for i, record in enumerate(found, 1):
        blocks.append(
            f"REPLY {i} (source: {record['probe_label']}, asked as: {record['variant']})\n"
            f"{record['response']}"
        )
    joined = "\n\n".join(blocks)
    return f"""QUESTION:
{question}

REPLIES:
{joined}

Produce this JSON:

- "answered" (boolean): true if at least one reply gives the information the question \
asked for. false if every reply is a refusal, an apology, a request for clarification, an \
instruction to look somewhere else, a hand-off to a human operator, or an answer about \
something other than what was asked. A fluent, confident reply that does not contain the \
requested fact is NOT an answer.
- "which" (integer or null): the number of the first reply that answers it, or null.
- "reason" (string): one sentence, quoting the part of the reply that decides it."""


async def _judge_found(question: str, found: list[dict[str, Any]]) -> tuple[bool, str]:
    """Second opinion on responses the marker list called a hit.

    classify_tool_result matches phrases, and a phrase list cannot see a rewording coming
    — the same weakness that made knowledge_gaps stop using it to find gaps in the first
    place. Here it is used only as a cheap filter: anything it calls empty is empty, and
    anything it calls a hit gets read properly. That ordering matters, because the RAG
    routinely returns a fluent paragraph that answers a different question, and reading
    that as "the record exists" would quietly drop a real gap off the customer's list.
    """
    try:
        result = await chat_json(_JUDGE_SYSTEM, _judge_prompt(question, found))
    except LLMError as exc:
        # Fall back to the marker verdict rather than failing the whole check. Treating
        # it as a hit is the safe direction: it keeps an unproven line off the customer's
        # list instead of putting a wrong one on it.
        return True, f"a reply came back with content (could not double-check: {exc})"
    answered = bool(result.get("answered"))
    return answered, str(result.get("reason") or "").strip()[:1000]


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------


def eligible_calls_query(agent_id: str | None, days: int | None):
    """Calls belonging to a verifiable record.

    A record is verifiable when the grouping pass has placed it — INCLUDING a group of
    one, which means the model looked at that question and found nothing else like it.
    That is a finished judgement, not an unprocessed row.

    Two things are excluded. Calls with no `gap_group_id` have never been through
    grouping, so verifying one risks re-asking a record another row already covers and
    paying twice for the same answer. `GAP_NEEDS_REVIEW` is the reserved group for
    questions nobody can act on — mis-heard speech, a subject with no attribute, an
    internal search string — and probing those spends the customer's API budget on
    sentences no record could ever answer.

    Shared by the API and by scripts/verify_gaps.py deliberately: two copies of "which
    records may be checked" would drift, and the direction they drift in costs somebody
    else money.
    """
    query = (
        select(Call)
        .options(selectinload(Call.turns))
        .where(Call.bucket == RECORD_MISSING_BUCKET)
        .where(Call.gap_group_id.is_not(None))
        .where(Call.gap_group_id != GAP_NEEDS_REVIEW)
    )
    if days:
        query = query.where(Call.started_at >= utcnow() - timedelta(days=days))
    if agent_id:
        query = query.where(Call.agent_id == agent_id)
    return query


def assemble_groups(calls: list[Call]) -> dict[str, dict[str, Any]]:
    """{group_id: {canonical, agent_id, members}}, members newest first.

    The canonical is whichever member carries `gap_group_question` — every member is
    written the same value by the grouping pass, and the fallback to a raw
    `unanswered_query` only matters for a row written before that was stored.
    """
    by_group: dict[str, list[Call]] = defaultdict(list)
    for call in calls:
        by_group[call.gap_group_id].append(call)

    groups: dict[str, dict[str, Any]] = {}
    for group_id, members in by_group.items():
        members.sort(key=lambda c: c.started_at, reverse=True)
        canonical = next(
            (c.gap_group_question for c in members if (c.gap_group_question or "").strip()),
            None,
        ) or (members[0].unanswered_query or "")
        groups[group_id] = {
            "canonical": canonical.strip(),
            "agent_id": members[0].agent_id,
            "members": members,
        }
    return groups


async def get_or_create_group(
    session: AsyncSession, group_id: str, *, agent_id: str, question: str | None
) -> GapGroup:
    """The GapGroup row for a grouping-pass group id, created on first verification.

    Lazy on purpose: gap_grouping.py writes only the two columns on Call and knows nothing
    about this table, so grouping and verification stay independent. The canonical
    question is refreshed each time, because a re-grouped record must not keep a verdict
    labelled with wording it no longer uses.
    """
    group = await session.get(GapGroup, group_id)
    if group is None:
        group = GapGroup(id=group_id, agent_id=agent_id, question=question)
        session.add(group)
    else:
        group.agent_id = agent_id or group.agent_id
        if question:
            group.question = question
    return group


async def verify_gap_group(
    session: AsyncSession,
    *,
    group_id: str,
    canonical: str,
    members: list[Call],
    config: AnalysisConfig,
    today: date | None = None,
) -> GapVerification:
    """Re-ask one record's question and record what came back.

    `members` are the calls in the group, newest first. The newest is the one whose
    transcript is read for context — which day the caller meant, and whether the question
    was heard correctly — and it is named on the verification row so the evidence says
    which call it was reasoning about.

    Writes a GapVerification row and updates the group's status. Does not commit — the
    caller decides transaction boundaries, so a batch can commit per record and a single
    verify can commit once.
    """
    if not members:
        raise ProbeConfigError("This record has no calls left in it — nothing to verify.")

    context_call = members[0]
    agent_id = context_call.agent_id
    probes = probes_for_agent(config, agent_id)
    if not probes:
        if not enabled_probes(config):
            raise ProbeConfigError(
                "No lookup probes are configured. Add one in Analysis Settings — without a "
                "source to re-ask, a gap cannot be verified, only assumed."
            )
        raise NoProbeForRegion(
            f"No lookup source is configured for region {agent_id!r}. Add one in Analysis "
            "Settings, or clear the region list on an existing source so it serves every "
            "region. Probing another region's endpoint would answer 'Tool non supportato' "
            "and be recorded as a missing record."
        )

    canonical = (canonical or "").strip()
    member_questions = [(c.unanswered_query or "").strip() for c in members]
    today = today or utcnow().date()

    group = await get_or_create_group(
        session, group_id, agent_id=agent_id, question=canonical
    )

    verification = GapVerification(
        group_id=group_id,
        call_id=context_call.id,
        question_original=canonical,
        llm_model=settings.resolved_model,
    )

    try:
        prepared = await _prepare(context_call, canonical, member_questions)
    except LLMError as exc:
        # No paraphrases and no date resolution means no meaningful check. Recording the
        # failure and stopping is better than probing the raw string once and calling the
        # result proof.
        verification.verdict = VERIFY_ERROR
        verification.question_resolved = canonical
        verification.question_note = f"Could not read the question: {exc}"
        verification.probes = []
        _finish(session, group, verification)
        return verification

    date_meant = prepared["date_meant"]
    date_probed, shifted = resolve_probe_date(date_meant, today)
    date_text = _format_date_it(date_probed) if date_probed else ""

    def with_date(text: str) -> str:
        return text.replace("{{date}}", date_text).strip()

    # The canonical wording first: it is the phrasing the report shows and the one the
    # verdict is about. The paraphrases exist to catch a retrieval miss, not to replace it.
    variants: list[tuple[str, str]] = [
        (with_date(prepared["normalized"] or canonical), "canonical")
    ]
    variants += [(with_date(p), "paraphrase") for p in prepared["paraphrases"]]

    # A group's canonical deliberately drops the date — "orari … il 16 agosto" and
    # "… il 18 agosto" are ONE record, and one entry covers every date. But a source can
    # hold a branch's normal weekly hours while missing the closure calendar for the day
    # the caller actually asked about, and probing only the undated question would read
    # the weekly hours as an answer and close a real gap. So when a member named a day and
    # the canonical did not, that member's own wording goes in too. Records with no date
    # pay nothing for this.
    if prepared["dated_variant"] and date_text:
        variants.append((with_date(prepared["dated_variant"]), "dated"))

    if not prepared["sensible"] and prepared["corrected"]:
        # Probed even though the verdict is already decided: if the corrected wording
        # answers, that is proof the lookup failed because of speech recognition and not
        # because the record is absent — which is a different team's bug and worth
        # knowing.
        variants.append((with_date(prepared["corrected"]), "corrected"))

    # Drop duplicates while keeping order; a paraphrase identical to the canonical is a
    # wasted request against somebody else's API.
    seen: set[str] = set()
    variants = [
        (text, kind) for text, kind in variants if text and not (text in seen or seen.add(text))
    ]

    records = await _run_probes(probes, variants)

    verification.question_resolved = variants[0][0] if variants else canonical
    verification.date_meant = date_meant.isoformat() if date_meant else None
    verification.date_probed = date_probed.isoformat() if date_probed else None
    verification.probes = records
    verification.question_note = prepared["reason"]

    completed = [r for r in records if r["verdict"] != ERROR]
    hits = [r for r in records if r["verdict"] == OK]

    answered = False
    judge_reason = ""
    if hits:
        answered, judge_reason = await _judge_found(canonical, hits)

    note_parts: list[str] = []
    if shifted and date_meant and date_probed:
        note_parts.append(
            f"The caller asked about {date_meant.isoformat()}, which has passed; "
            f"checked {date_probed.isoformat()} (same weekday) instead."
        )

    if not prepared["sensible"]:
        # Ranked above everything else on purpose. A garbled question comes back empty
        # because it is nonsense, and putting it on the customer's list asks them to add
        # a record for a question nobody asked. This is the "centro di Pietra" case,
        # where the caller had said Torre in Pietra.
        verification.verdict = BAD_QUESTION
        note_parts.insert(0, prepared["reason"] or "The question looks mis-heard.")
        if prepared["corrected"]:
            corrected_hit = any(
                r["variant_kind"] == "corrected" and r["verdict"] == OK for r in records
            )
            note_parts.append(
                f'Corrected to "{prepared["corrected"]}", the source '
                + ("did answer — this is a speech-recognition failure, not missing data."
                   if corrected_hit
                   else "still returned nothing.")
            )
    elif hits and answered:
        verification.verdict = FOUND_IN_SOURCE
        note_parts.insert(0, judge_reason or "A reply contained the requested information.")
    elif completed:
        verification.verdict = CONFIRMED_MISSING
        sources = sorted({r["probe_label"] for r in completed})
        note_parts.insert(
            0,
            f"{len(completed)} of {len(records)} lookups completed across "
            f"{', '.join(sources)}; none returned the information."
            + (f" ({judge_reason})" if hits and judge_reason else ""),
        )
    else:
        verification.verdict = VERIFY_ERROR
        first = records[0]["response"] if records else "no probes ran"
        note_parts.insert(0, f"No lookup completed. First error: {first}")

    verification.question_note = " ".join(p for p in note_parts if p)[:2000]
    _finish(session, group, verification)
    return verification


def _finish(session: AsyncSession, group: GapGroup, verification: GapVerification) -> None:
    session.add(verification)
    group.status_at = utcnow()
    group.status_note = verification.question_note

    if group.sent_batch:
        # ONCE REPORTED, A RECORD CAN ONLY MOVE FORWARD. This is what stops the same
        # missing record going to the customer twice.
        #
        # Without it, re-checking a record that was sent last week and is still absent
        # would write `confirmed_missing` back over `sent` — putting it straight back into
        # the eligible-to-send set, so the next report repeats a record the customer is
        # already working on. Re-checking is a normal thing to do (it is how you find out
        # whether they have added it yet), so this would happen routinely rather than
        # rarely.
        #
        # The record turning up IS the confirmation the fix landed, so that one transition
        # is allowed. Every other verdict leaves the status alone; the evidence still
        # lands in the GapVerification row and the note above, so nothing is hidden.
        if verification.verdict == FOUND_IN_SOURCE:
            group.status = ADDED_CONFIRMED
        return

    group.status = verification.verdict


def set_status(
    group: GapGroup, status: str, *, note: str | None = None, batch: str | None = None
) -> None:
    """Record a decision a person made, rather than one a probe produced."""
    if status not in MANUAL_STATUSES:
        raise ValueError(
            f"{status!r} cannot be set by hand. Allowed: {', '.join(MANUAL_STATUSES)}. "
            "Verification verdicts come from verify_gap_group()."
        )
    group.status = None if status == NOT_VERIFIED else status
    group.status_at = utcnow()
    if note is not None:
        group.status_note = note.strip()[:2000] or None
    if status == SENT:
        group.sent_batch = batch or new_batch_id()
    elif status == NOT_VERIFIED:
        group.sent_batch = None


def new_batch_id(now: datetime | None = None) -> str:
    """A label shared by everything reported to the customer together.

    Minute resolution, because a batch is one person pressing one button — and a readable
    id is worth more here than a uuid nobody can match against a sent email.
    """
    return (now or utcnow()).strftime("batch-%Y%m%d-%H%M")


async def load_groups(session: AsyncSession, group_ids: list[str]) -> dict[str, GapGroup]:
    """Existing GapGroup rows for these ids, keyed by id. Missing ids are simply absent."""
    if not group_ids:
        return {}
    rows = (
        (await session.execute(select(GapGroup).where(GapGroup.id.in_(group_ids))))
        .scalars()
        .all()
    )
    return {row.id: row for row in rows}


def status_of(group: GapGroup | None) -> str:
    return (group.status if group else None) or NOT_VERIFIED


def estimate_requests(config: AnalysisConfig, agent_id: str | None) -> int:
    """Upper bound on requests one record costs against this region's sources.

    Canonical + two paraphrases + at most one dated variant, times the sources. Reported
    before a run starts, because every one of these lands on the customer's live service.
    """
    return (_PARAPHRASE_COUNT + 2) * len(probes_for_agent(config, agent_id))

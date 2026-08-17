"""Find questions the agent could not answer because the data wasn't there.

WHY THIS EXISTS
A large share of transfers are not agent bugs. The caller asks something reasonable
("what are the opening hours in Lombardia?"), the agent queries the knowledge base or
a lookup API, and it comes back with nothing — because that record does not exist in
the customer's database. The agent has no answer, so it transfers.

Nobody on the customer's side knows which records are missing. This module extracts
exactly that: the questions that hit an empty lookup, grouped by how often they recur,
each with call IDs so the customer can pull up the recording in their own system and
verify it before adding the data.

THE DISTINCTION THAT MATTERS
    {"error": "timeout"}      -> your infrastructure broke. Not a knowledge gap.
    {} / [] / "no results"    -> the data is missing. This is the report.

Both end the call the same way, and both look identical in a transcript. Separating
them is the entire point: one is a bug for the engineering team, the other is a content
task for the customer, and sending the wrong list to the wrong person wastes everyone's
time.
"""

import json
from typing import Any

# Result payloads that mean "the lookup itself failed" rather than "nothing matched".
# Kept deliberately narrow: anything not clearly infrastructure is treated as a
# content gap, because a missed content gap is invisible while a misfiled technical
# error is merely noise the engineer recognises immediately.
_TECHNICAL_MARKERS = (
    "timeout",
    "timed out",
    "connection",
    "refused",
    "unreachable",
    "502",
    "503",
    "504",
    "500 internal",
    "traceback",
    "exception",
    "ssl",
    "unauthorized",
    "forbidden",
    "rate limit",
)

# Phrases that mean "the query ran fine and matched nothing", in the languages these
# agents actually run in.
_EMPTY_MARKERS = (
    "no results",
    "no result",
    "not found",
    "no data",
    "no match",
    "empty",
    "nessun risultato",
    "non trovato",
    "non disponibile",
    "nessun dato",
    "non ho trovato",
    # Taken verbatim from Piemonte's production logs — these are what the real
    # knowledge_base_new / call_graph tools return on a miss. Neither matched the
    # generic markers above ("non ho una risposta" shares no substring with "non ho
    # trovato"), so every gap of the second kind was being scored "ok" and silently
    # dropped from the report. Match on the distinctive stem rather than the whole
    # sentence, so a reworded message keeps being caught.
    "non ho una risposta",
    "cerca nel rag",
    "informazioni specifiche",
)

# Keys whose value is the question the agent actually asked the lookup. Preferred over
# the transcript: it is the resolved query, without filler or misrecognised speech.
_QUERY_KEYS = ("query", "question", "q", "search", "search_term", "term", "text", "prompt")

# Keys that commonly hold the payload inside a wrapper object.
_PAYLOAD_KEYS = ("results", "result", "data", "items", "answer", "records", "matches")

OK = "ok"
EMPTY = "empty"
ERROR = "error"

# The bucket that means "a lookup ran and the record was not there" — see buckets.py.
# extract_gaps() keys off this rather than off the markers above; the docstring there
# explains what went wrong with matching phrases.
RECORD_MISSING_BUCKET = "record_missing"


def _text_of(value: Any) -> str:
    if isinstance(value, str):
        return value.lower()
    try:
        return json.dumps(value, default=str, ensure_ascii=False).lower()
    except (TypeError, ValueError):
        return str(value).lower()


def _is_blank(value: Any) -> bool:
    """True when a payload carries no usable content."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set)):
        return len(value) == 0
    if isinstance(value, dict):
        if not value:
            return True
        # A wrapper whose payload is empty is itself empty: {"results": []}
        for key in _PAYLOAD_KEYS:
            if key in value:
                return _is_blank(value[key])
    return False


def classify_tool_result(result: Any) -> str:
    """OK / EMPTY / ERROR for one tool result.

    EMPTY is the interesting one — the lookup worked and found nothing, which is a
    missing record in the customer's data rather than a fault in ours.
    """
    if _is_blank(result):
        return EMPTY

    text = _text_of(result)

    # An explicit error field decides between the two buckets by its *content*:
    # "No results found" is a content gap even though it arrives as an error.
    if isinstance(result, dict):
        error = result.get("error")
        if error:
            error_text = _text_of(error)
            if any(marker in error_text for marker in _TECHNICAL_MARKERS):
                return ERROR
            if any(marker in error_text for marker in _EMPTY_MARKERS):
                return EMPTY
            return ERROR

    if any(marker in text for marker in _TECHNICAL_MARKERS):
        return ERROR
    if any(marker in text for marker in _EMPTY_MARKERS):
        return EMPTY
    return OK


def question_from_tool_call(tool_call: dict) -> str | None:
    """The query the agent sent to the lookup, if it carried one."""
    args = tool_call.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (TypeError, ValueError):
            return args.strip() or None
    if not isinstance(args, dict):
        return None
    for key in _QUERY_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    # Single-string-argument tools: use it rather than losing the question entirely.
    strings = [v.strip() for v in args.values() if isinstance(v, str) and v.strip()]
    return strings[0] if len(strings) == 1 else None


# WHY THERE IS NO CLUSTERING HERE ANY MORE
#
# Until now this module merged occurrences with a stopword list, a synonym table and an
# overlap coefficient at 0.7. Measured against the live Lazio database (211 gaps, Aug
# 2026) it merged 16 groups, of which 11 were wrong, and the mechanism was always the
# same: the branch name is a single token outvoted by generic ones. "orari della sede di
# via Librogame" and "orari apertura sede di via Voliere" share {orari, sede, via} — 3 of
# 4 tokens, 0.75, over the threshold — so four different Roman branches merged into one
# line. The customer adds hours for Librogame, believes the list is finished, and three
# branches keep failing with nobody able to see why. That is the exact harm the report
# exists to prevent, and it is invisible: the three hidden questions are shown only as
# "also asked as" under a headline naming a different place.
#
# It failed in the other direction too, because occurrences were clustered *within a
# tool* and the tool attributed to a call is the last tool it called — usually
# `request_transfer`, which is not a lookup at all. Two byte-identical questions
# ("Quanto costano le analisi del sangue?") sat in two separate groups for that reason.
#
# So grouping is no longer derived from the words. Every record_missing call is its own
# row, and merging is an explicit, on-demand LLM pass (gap_grouping.py) whose answer is
# stored on the call rows. Token overlap cannot see that two branch names differ in
# kind from two spellings of one exam, and no threshold fixes that — it is a judgement
# about what the words refer to, which is what the LLM is for.


def extract_gaps(call: Any) -> list[dict[str, Any]]:
    """The missing-record moment in one call, or nothing.

    WHY THIS NO LONGER MATCHES PHRASES
    This used to walk every tool call and treat any result matching _EMPTY_MARKERS as a
    gap. A marker reads one result in isolation with no idea what followed, and that is
    not enough information to make the call. Measured over 120 live Lazio calls it
    flagged 24 as having a gap, of which 3 had *completed successfully* because a later
    tool answered the same question. The worst case is the graph replying "Non ho una
    risposta per questo cerca nel RAG" ("I have no answer for this, search in the RAG"),
    which is an instruction routing the agent to the other tool — internal plumbing,
    reported to the customer as a missing record.

    The judge reads the whole sequence and does not make that mistake, so a gap is now
    exactly a call it bucketed `record_missing`, and the question is the one it named in
    `unanswered_query`. The marker functions above are kept for other callers (and for
    scripts/build_label_sheet.py, which has its own copy) but are off this path.

    Falls back to the query argument of a tool call, then to the last user turn, when
    the judge left `unanswered_query` empty — better a gap with rough wording than a
    missing line in the report.
    """
    if getattr(call, "bucket", None) != RECORD_MISSING_BUCKET:
        return []

    question = (getattr(call, "unanswered_query", None) or "").strip()
    tool = "unknown_tool"

    if not question:
        last_user_text: str | None = None
        for turn in call.turns:
            if turn.role == "user" and turn.text and turn.text.strip():
                last_user_text = turn.text.strip()
            for tool_call in turn.tool_calls or []:
                if not isinstance(tool_call, dict):
                    continue
                candidate = question_from_tool_call(tool_call)
                if candidate:
                    question, tool = candidate, tool_call.get("name") or tool
        question = question or (last_user_text or "")
    else:
        tool = _tool_that_was_asked(call, question) or tool

    if not question:
        return []
    return [{"question": question, "tool": tool}]


# Tool names that are never a lookup, so attributing a missing record to one says
# nothing about where the record should be added. `request_transfer` is the big one:
# it is usually the LAST tool a failed call invokes, which is exactly why the old
# "keep overwriting with whatever comes next" attribution below landed on it for 50 of
# 192 live groups.
_NON_LOOKUP_TOOLS = frozenset({"request_transfer", "transfer_call", "end_call", "hangup"})


def _tool_that_was_asked(call: Any, question: str) -> str | None:
    """Which lookup was actually asked `question`, as best as the turns can say.

    Prefers the tool whose own arguments carry the question text — the judge is told to
    word `unanswered_query` the way the tool was queried, so this usually matches
    outright. Falls back to the last tool that could plausibly be a lookup, and gives up
    rather than naming a transfer as the source of a missing record.
    """
    wanted = question.strip().lower()
    fallback: str | None = None
    for turn in call.turns:
        for tool_call in turn.tool_calls or []:
            if not isinstance(tool_call, dict):
                continue
            name = tool_call.get("name")
            if not name or name in _NON_LOOKUP_TOOLS:
                continue
            asked = (question_from_tool_call(tool_call) or "").strip().lower()
            if asked and (asked == wanted or asked in wanted or wanted in asked):
                return name
            fallback = name
    return fallback

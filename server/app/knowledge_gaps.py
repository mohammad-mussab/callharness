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
import re
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
)

# Keys whose value is the question the agent actually asked the lookup. Preferred over
# the transcript: it is the resolved query, without filler or misrecognised speech.
_QUERY_KEYS = ("query", "question", "q", "search", "search_term", "term", "text", "prompt")

# Keys that commonly hold the payload inside a wrapper object.
_PAYLOAD_KEYS = ("results", "result", "data", "items", "answer", "records", "matches")

OK = "ok"
EMPTY = "empty"
ERROR = "error"


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


def normalize_question(text: str) -> str:
    """Collapse wording differences so the same missing record groups together.

    Deliberately crude — lowercase, strip punctuation and accents, drop filler. Two
    callers rarely phrase a question identically, so this over-groups slightly rather
    than producing a list of near-duplicates nobody will read.
    """
    text = text.lower().strip()
    for a, b in (("à", "a"), ("è", "e"), ("é", "e"), ("ì", "i"), ("ò", "o"), ("ù", "u")):
        text = text.replace(a, b)
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    stop = {
        "il", "lo", "la", "i", "gli", "le", "un", "uno", "una", "di", "a", "da", "in",
        "con", "su", "per", "tra", "fra", "e", "che", "del", "della", "dei", "delle",
        "al", "alla", "ai", "alle", "quali", "quale", "sono", "e", "ho", "vorrei",
        "the", "of", "for", "at", "is", "are", "what", "which", "please",
    }
    words = [w for w in text.split() if w not in stop]
    return " ".join(words) or text


def extract_gaps(call: Any) -> list[dict[str, Any]]:
    """Every unanswered-because-missing-data moment in one call.

    Requires `call.turns` loaded. Falls back to the nearest preceding user turn when a
    tool call carried no query argument, so a gap is still reported with context even
    for tools that take structured parameters.
    """
    gaps: list[dict[str, Any]] = []
    last_user_text: str | None = None

    for turn in call.turns:
        if turn.role == "user" and turn.text and turn.text.strip():
            last_user_text = turn.text.strip()
        for tool_call in turn.tool_calls or []:
            if not isinstance(tool_call, dict):
                continue
            if classify_tool_result(tool_call.get("result")) != EMPTY:
                continue
            question = question_from_tool_call(tool_call) or last_user_text
            if not question:
                continue
            gaps.append(
                {
                    "question": question,
                    "normalized": normalize_question(question),
                    "tool": tool_call.get("name") or "unknown_tool",
                }
            )
    return gaps

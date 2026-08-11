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


# Filler that carries no information about *which* record is missing. Without a list
# this long, "Mi sa dire gli orari di apertura per la Lombardia?" and "orari apertura
# Lombardia" look like two different gaps and the report fragments into near-duplicates.
_STOPWORDS = {
    # Italian articles, prepositions, conjunctions
    "il", "lo", "la", "i", "gli", "le", "un", "uno", "una", "di", "del", "della",
    "dello", "dei", "delle", "degli", "a", "al", "alla", "ai", "alle", "allo", "da",
    "dal", "dalla", "in", "nel", "nella", "con", "su", "sul", "sulla", "per", "tra",
    "fra", "e", "ed", "o", "che", "chi", "cui", "non", "ma", "se", "come", "dove",
    # Question openers and polite filler
    "quali", "quale", "quanto", "quanta", "quanti", "quante", "cosa", "qual",
    "mi", "sa", "dire", "sapere", "saprebbe", "potrebbe", "vorrei", "volevo",
    "posso", "puo", "può", "devo", "deve", "serve", "servono", "ho", "avete",
    "siete", "sono", "e'", "essere", "c", "ci", "si", "per favore", "favore",
    "grazie", "buongiorno", "salve", "scusi", "senta", "gentilmente", "cortesemente",
    "informazioni", "informazione", "info", "riguardo", "circa",
    # English equivalents, so the same logic works for other deployments
    "the", "of", "for", "at", "is", "are", "was", "were", "what", "which", "who",
    "how", "much", "many", "do", "does", "did", "can", "could", "would", "please",
    "tell", "me", "know", "about", "there", "any", "your", "you", "i", "it",
}

# Different words for the same idea. Only pairs where conflating them cannot hide a
# distinct missing record — "price" and "cost" refer to the same absent field, whereas
# two different cities never should be merged.
_SYNONYMS = {
    "costa": "prezzo", "costo": "prezzo", "costi": "prezzo", "tariffa": "prezzo",
    "tariffe": "prezzo", "price": "prezzo", "cost": "prezzo",
    "orario": "orari", "apre": "orari", "apertura": "orari", "chiude": "orari",
    "chiusura": "orari", "hours": "orari", "opening": "orari",
    "indirizzo": "sede", "dove": "sede", "address": "sede", "location": "sede",
    "convenzionati": "convenzione", "convenzionato": "convenzione",
    "analisi": "esame", "esami": "esame", "test": "esame", "exam": "esame",
    "appuntamento": "prenotazione", "prenotare": "prenotazione",
    "booking": "prenotazione", "appointment": "prenotazione",
}

# NOTE ON THE LIMIT OF THIS APPROACH
# Token overlap plus a synonym table catches the variation callers actually produce —
# filler, word order, and a handful of domain equivalents. It cannot catch genuine
# paraphrase with no shared vocabulary ("posso mangiare prima del prelievo?" against
# "serve il digiuno?"). Those stay separate, which fragments the report rather than
# corrupting it: the failure is visible as two similar lines, not a wrong merge.
# If that starts happening often, the upgrade is one LLM call over the *report* (not
# per call) to merge remaining clusters — cheap, because it runs weekly over ~50 lines
# rather than over every conversation.


def question_tokens(text: str) -> set[str]:
    """Meaningful words only: lowercased, de-accented, de-duplicated, synonym-folded."""
    text = text.lower().strip()
    for a, b in (("à", "a"), ("è", "e"), ("é", "e"), ("ì", "i"), ("ò", "o"),
                 ("ù", "u"), ("'", " "), ("’", " ")):
        text = text.replace(a, b)
    text = re.sub(r"[^\w\s]", " ", text)
    tokens = set()
    for word in text.split():
        if len(word) <= 1 or word in _STOPWORDS:
            continue
        tokens.add(_SYNONYMS.get(word, word))
    return tokens


def normalize_question(text: str) -> str:
    """Stable key for a question — sorted meaningful tokens."""
    tokens = question_tokens(text)
    return " ".join(sorted(tokens)) or text.lower().strip()


def similarity(a: set[str], b: set[str]) -> float:
    """Overlap coefficient: shared tokens over the smaller set.

    Chosen over Jaccard deliberately. Callers pad questions with filler to wildly
    different lengths ("orari Lombardia" vs "mi saprebbe dire gli orari di apertura
    per la sede della Lombardia"), and Jaccard punishes that length difference even
    when one question fully contains the other. Overlap does not.
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


# 0.7 keeps "prezzo risonanza novara" together with "quanto costa risonanza novara"
# (3 shared of 4 = 0.75) while keeping "orari lombardia" and "orari torino" apart
# (2 shared of 3 = 0.67). Two different cities are two different missing records, and
# merging them would send the customer a report that hides one of them.
SIMILARITY_THRESHOLD = 0.7
MIN_SHARED_TOKENS = 2


def cluster_questions(items: list[dict]) -> list[list[dict]]:
    """Group gap occurrences that refer to the same missing record.

    `items` need a "question" key. Greedy single-pass clustering against each
    cluster's accumulated token set — good enough at report sizes, and deterministic,
    which matters when a customer asks why two lines merged.
    """
    clusters: list[dict] = []
    # Longest first, so a fully-specified question anchors the cluster and shorter
    # fragments join it rather than seeding a competing one.
    for item in sorted(items, key=lambda x: -len(question_tokens(x["question"]))):
        tokens = question_tokens(item["question"])
        if not tokens:
            continue
        best, best_score = None, 0.0
        for cluster in clusters:
            score = similarity(tokens, cluster["tokens"])
            if score > best_score and len(tokens & cluster["tokens"]) >= MIN_SHARED_TOKENS:
                best, best_score = cluster, score
        if best is not None and best_score >= SIMILARITY_THRESHOLD:
            best["items"].append(item)
            # Intersect rather than union: the shared core is what the cluster is
            # about. Union would let a cluster drift wider with every member it
            # absorbs until unrelated questions start matching it.
            best["tokens"] &= tokens
        else:
            clusters.append({"tokens": set(tokens), "items": [item]})
    return [c["items"] for c in clusters]


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

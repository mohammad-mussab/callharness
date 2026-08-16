"""The single call-classification taxonomy: one bucket per analysed call.

WHY THIS REPLACED transfer_reason / non_completion_reason
Those two taxonomies were conditional on how the call ended. `transfer_reason` only
ever applied to transferred calls, `non_completion_reason` only to calls that were
neither transferred nor successful — so a *successful* call got no classification at
all. Three consequences, all measured on live Lazio data:

  - The same root cause landed in two different keys. A missing record filed as
    ``knowledge_gap`` if the call transferred and ``agent_error`` if it didn't, so
    one problem showed up as two chart slices that could never be added together.
  - 22% of calls (the completed ones) carried no diagnostic information whatsoever.
    A caller who asked three questions and got one was a "success" with nothing
    recorded about the two that failed.
  - A knowledge gap where the caller hung up before the transfer fired was invisible:
    not transferred, so no transfer_reason; often "successful", so no
    non_completion_reason either.

`bucket` applies to every analysed call regardless of outcome. It answers "what
actually happened", which is a different axis from `outcome` (transferred / completed /
non_completed, see outcome.py) — that one answers "how did it end" and is unchanged.

RELATIONSHIP TO THE OLD COLUMNS
`Call.transfer_reason` and `Call.non_completion_reason` are kept in the database with
their existing values, frozen. Nothing writes them any more. They are preserved rather
than dropped because DROP COLUMN destroys history with no undo, and because disputes.py
still reads them. The freeze is achieved by turning `AnalysisConfig.classification_enabled`
off — see the note in analysis/engine.py apply_result(), where doing it any other way
silently nulls every stored value on re-analysis.
"""

from __future__ import annotations

from typing import Any

from .taxonomy import normalize_key  # noqa: F401  (re-exported for callers)

# Set in code, never by the LLM: if the caller never appears in the transcript there is
# nothing for a judge to read, so asking one is both unreliable and a wasted API call.
NO_CALLER_AUDIO = "no_caller_audio"

# The catch-all. An answer outside the configured set lands here rather than becoming a
# new chart slice — uniqueness belongs in `issue_note`, not in the key.
FALLBACK_BUCKET = "other"

# The one bucket that counts as a win, and the ones excluded from the addressable rate
# because no amount of data or agent work would have changed them.
ANSWERED_BUCKET = "answered"
NOT_ADDRESSABLE = (NO_CALLER_AUDIO, "needs_human", "out_of_scope")


# Descriptions are not documentation — they are the prompt. The LLM decides membership
# by reading these, so vagueness here produces vague classification. Each one names the
# observable evidence rather than a feeling about the call.
#
# Two of them carry a rule that was added after measuring 674 live Lazio calls, and both
# are load-bearing:
#
#   answered / BACKED. The first version of this description only checked that the caller
#   HEARD the answer, which is a question about delivery, not truth. A fluent, confident,
#   false answer passed it. Five calls were found where call_graph deflected with an
#   aggregate ("chiusure straordinarie in 5 sedi — per quale sede?", which names no
#   branch) and the assistant then told the caller a specific branch was closed; three of
#   those scored `answered`, and across all 674 calls exactly ONE was ever classified
#   agent_invented_answer. On 8447a26d the assistant asserted a lab was shut on a named
#   morning with no second tool call at all. Checking delivery without checking evidence
#   turns a fabricated closure into a reported success.
#
#   record_missing / FINISHED CHAIN. A routing instruction is not a report of absent data.
#   "Non ho una risposta per questo cerca nel RAG" tells the agent to ask the other tool.
#   Of 70 calls containing it: 22 were then answered by the RAG (not a gap at all), 11
#   came back empty from both (a real gap), and in 37 the agent never made the follow-up
#   call (our bug). 45 of the 135 record_missing calls — a third of the list emailed to
#   the customer as records they must add — came from the first and third groups.
#
# Note what is NOT here: lab-test prices. Those correctly go to knowledge_base_new (lab
# work is not bookable through the slot API, so there are no slots to read a price from —
# only poliambulatorio/diagnostica prices live there, and sports medicine has its own
# endpoint). 36 of 54 price questions were correctly routed and genuinely had no price in
# the KB. That is a real content gap and must keep reaching the customer.
DEFAULT_BUCKETS: list[dict[str, str]] = [
    {
        "key": "answered",
        "description": (
            "the caller got what they asked for. This INCLUDES the normal multi-turn "
            "round trip: a tool returned a question or asked for more detail, the "
            "assistant relayed it, the caller supplied the detail, and a later tool call "
            "containing that detail returned real data. That is correct behaviour, not a "
            "fault. TWO checks before choosing this, and both must pass. (1) HEARD: "
            "find the assistant turn that states the answer. If the only assistant turn "
            'after the relevant tool result breaks off as a fragment — "Il", "Per gli '
            'esami del" — and no earlier assistant turn stated the answer in full, then '
            "the tool succeeded but the caller received nothing; that is "
            '"caller_abandoned", not this. A fragment is fine when the full answer was '
            "already delivered earlier and what got cut was a follow-up offer or a "
            "goodbye. (2) BACKED: the specific fact the assistant stated — an opening "
            "hour, a closure, a price, an address, availability — must actually appear "
            "in one of the tool results above it. A confident, fluent answer is not the "
            "same as a true one. The commonest failure is an AGGREGATE: a tool replying "
            '"ci sono chiusure straordinarie in 5 sedi della regione. Per quale sede?" '
            "has named NO branch, so an assistant that then says a particular branch is "
            'closed made that up — that is "agent_invented_answer", not this'
        ),
    },
    {
        "key": "partial_answered",
        "description": (
            "the caller asked about several things and received a real answer to some "
            "but not all of them, with nothing identifiable having gone wrong on the "
            "rest. If the unanswered part has a diagnosable cause, use that bucket "
            "instead"
        ),
    },
    {
        "key": "agent_invented_answer",
        "description": (
            "the assistant told the caller a specific fact they could act on, and no "
            "preceding tool result contains it. ANY kind of fact counts — an opening "
            "hour, a closure, a price, how long a result takes, how a sample must be "
            "collected, how long a prescription stays valid. There is no fixed list of "
            "which facts qualify; the test is whether it was backed. HOW TO CHECK — do "
            "this instead of judging by tone: take the assistant's main factual claim "
            "and find the tool result that contains it, quoting the matching words to "
            "yourself. If you cannot point to one, it was not backed and this is the "
            "bucket. Do not credit a claim because the assistant sounded certain, or "
            "because a tool call happened nearby — the tool result must actually "
            "contain the fact. Worked example, an illustration and not the boundary: a "
            'tool replying "ci sono chiusure straordinarie in 5 sedi della regione. Per '
            'quale sede?" names NO branch, so an assistant that then says a particular '
            "branch is closed cannot quote it from anywhere — that is this bucket"
        ),
    },
    {
        "key": "tool_kept_asking",
        "description": (
            "the tool asked for a detail, a LATER tool call's query ALREADY CONTAINED "
            "that detail, and the tool still responded by asking for the same thing "
            "again or by returning an aggregate instead of the specific answer. Read the "
            "query arguments, not the caller's words — the detail is usually supplied by "
            "the assistant rewriting the query. If the tool asks 'which branch?' and the "
            "next query names the branch and it asks 'which branch?' again, the loop "
            "never closed and this is the bucket, even if the tool answered a DIFFERENT "
            "question earlier in the same call. This is NOT record_missing: the data "
            "demonstrably exists — the tool can see it and says so in aggregate — the "
            "lookup just will not narrow to what was asked. Our bug, not the customer's "
            "missing data"
        ),
    },
    {
        "key": "caller_abandoned",
        "description": (
            "the caller left before the exchange finished. Either (a) the assistant "
            "asked a reasonable clarifying question and they never answered it, or "
            "(b) they hung up while the answer was still being delivered — the last "
            "assistant turn breaks off mid-sentence or mid-word and the substantive "
            "answer had NOT yet reached them. The transcript contains only what the "
            "caller actually heard, so a truncated final turn is evidence, not a "
            "glitch. IMPORTANT: a tool returning the right data is not the same as the "
            'caller receiving it — do not score such a call "answered" on the strength '
            "of the tool result alone. The exception is a call where the substantive "
            "answer had already landed and only a closing pleasantry was cut off; that "
            'is "answered"'
        ),
    },
    {
        "key": "not_understood",
        "description": (
            "the assistant could not make out what the caller was asking, because "
            "speech-to-text mangled it or the caller's words arrived as fragments — "
            "invented-sounding service names, half-words, a branch name that is not a "
            "real branch. The give-away is the assistant asking for clarification "
            "repeatedly, or a tool being queried with something that is not a coherent "
            "question. This is OUR failure to hear, not the caller giving up and not a "
            "missing record: ranked above record_missing precisely because a mis-heard "
            "query returns nothing and would otherwise be reported to the customer as "
            "data they need to add"
        ),
    },
    {
        "key": "record_missing",
        "description": (
            "a tool ran correctly and honestly returned nothing: no results, empty "
            "payload, or a message meaning it has no answer for this. The lookup worked; "
            "the record is absent from the customer's data. THIS IS STILL THE ANSWER when "
            "the assistant then handed the caller to a human or ended the call because it "
            "had nothing — an empty lookup is WHY the call failed, and being transferred "
            "is merely HOW it ended. Do NOT use this when a later tool call went on to "
            "answer the same question. A lookup only counts as finished when the chain "
            "it started has finished. A tool that replies with a ROUTING INSTRUCTION has "
            'not reported absent data: "Non ho una risposta per questo cerca nel RAG" '
            "tells the agent to ask the other tool, it does not say the record is "
            "missing. So: if the agent then called that other tool and it answered, the "
            "question was answered and this is NOT the bucket; if it called it and that "
            "came back empty too, this IS the bucket; if it never made the follow-up "
            'call at all, nothing was ever looked up — that is "lookup_error", which '
            "ranks above this one"
        ),
    },
    {
        "key": "lookup_error",
        "description": (
            "the lookup never completed, so we never found out whether the record "
            "exists. Two shapes. (a) A tool failed technically — timeout, 5xx, "
            "connection refused, exception, traceback. (b) A tool DEFERRED to another "
            'tool and the agent never made that call: "Non ho una risposta per questo '
            'cerca nel RAG" tells the agent to ask the knowledge base, so if no such '
            "call follows, nothing was ever actually looked up. Either way this is OUR "
            "failure, not the customer's data being absent — which is why it ranks "
            "above record_missing. Filing it lower would send the customer a record to "
            "add for a question we never asked"
        ),
    },
    {
        "key": "needs_human",
        "description": (
            "the request needed a person NO MATTER WHAT DATA EXISTED: making or changing "
            "a booking, NHS/prescription flows, the caller explicitly asking for an "
            "operator, or the agent correctly reporting no operator is available. The "
            "test is whether a human would still have been required if every record were "
            "present. Do NOT use this merely because the call was transferred — if the "
            'handoff happened because a lookup came back empty, that is "record_missing"; '
            'if a tool failed technically, that is "lookup_error". This bucket is about '
            "what the CALLER needed. A TOOL RESULT saying a person is required — "
            '"per questa richiesta è necessario parlare con un operatore" — is that tool '
            "reporting it has no data for the question. It is not evidence a human was "
            "ever required, and in most such calls the caller never asked for one. "
            "Classify those by the failure behind the referral, usually "
            '"record_missing", not by the referral itself. Only the caller asking for a '
            "person, or a request that genuinely needs one, counts here"
        ),
    },
    {
        "key": "out_of_scope",
        "description": (
            "the caller wanted something the agent may never do no matter what data "
            "existed: patient records, referrals, report status, issuing prescriptions, "
            "clinical advice"
        ),
    },
    {
        "key": NO_CALLER_AUDIO,
        "description": (
            "the caller never appears in the transcript at all. Normally decided before "
            "the judge runs; listed so the taxonomy is complete"
        ),
    },
    {
        "key": FALLBACK_BUCKET,
        "description": (
            "genuinely none of the above. Expected and fine — describe what happened in "
            "issue_note so recurring cases can be promoted to their own bucket"
        ),
    },
]


# Most severe first. A call can satisfy several buckets at once and only stores one, so
# this decides which wins. Without a fixed order the model picks arbitrarily and the
# same call classifies differently on different runs, which moves the charts while
# nothing has actually changed.
#
# Two placements that are deliberate and easy to get wrong:
#   - caller_abandoned outranks needs_human. A booking call where the caller vanished
#     during a clarifying question never got far enough to prove a human was required,
#     and needs_human is excluded from the addressable rate — so filing it there would
#     quietly inflate the headline number.
#   - partial_answered sits below every fault bucket. If part of a multi-question call
#     hit a missing record, "missing record" is the actionable label; "partly answered"
#     tells the customer nothing they can act on.
#   - not_understood outranks record_missing. A mis-heard question ("informazioni sul
#     centro di Pietra", where the caller meant Torre in Pietra) reaches the lookup as
#     nonsense and comes back empty, so it looks exactly like absent data. Below
#     record_missing it would put a question no customer can answer onto the list of
#     records they are asked to add. Observed on a real call.
#   - lookup_error outranks record_missing, same reasoning one step further: if the
#     lookup never completed we never learned whether the record exists, so calling it
#     missing is a guess billed to the customer. This placement is what makes the
#     "deferred to another tool, never called it" case reachable at all. It first lived
#     inside `other`, which cannot work: `other` is last, the prompt says take the FIRST
#     match, so record_missing (higher) always won and the instruction never fired —
#     verified on cd2a5a8e and 1dbdaacf, neither of which moved. 37 live calls sit in
#     this shape, 34 of them filed as record_missing and reaching the customer as gaps.
BUCKET_PRECEDENCE: tuple[str, ...] = (
    "agent_invented_answer",
    "tool_kept_asking",
    "not_understood",
    "lookup_error",
    "record_missing",
    "caller_abandoned",
    "needs_human",
    "out_of_scope",
    "partial_answered",
    ANSWERED_BUCKET,
    FALLBACK_BUCKET,
)


def buckets_or_default(configured: list | None) -> list[dict[str, str]]:
    """The usable bucket list, dropping malformed entries.

    Mirrors taxonomy.categories_or_default: an empty/absent list means "never
    configured" and falls back to the defaults, and a list containing only junk also
    falls back, so a bad save cannot silently empty the taxonomy.
    """
    cleaned = [
        {"key": normalize_key(c["key"]), "description": str(c.get("description") or "")}
        for c in (configured or [])
        if isinstance(c, dict) and str(c.get("key") or "").strip()
    ]
    return cleaned or DEFAULT_BUCKETS


def ordered_for_prompt(categories: list[dict[str, str]]) -> list[dict[str, str]]:
    """Configured buckets sorted by BUCKET_PRECEDENCE, most severe first.

    The prompt presents them in this order and tells the judge the first match wins, so
    the ordering has to survive whatever order they happen to sit in on the config row.
    Keys not in BUCKET_PRECEDENCE — ones the user added in Settings — sort to the end
    just above `other`, since we have no basis for ranking them higher than a bucket
    whose severity we defined.
    """
    rank = {key: i for i, key in enumerate(BUCKET_PRECEDENCE)}
    unknown = len(BUCKET_PRECEDENCE) - 1  # just before `other`
    return sorted(categories, key=lambda c: rank.get(c["key"], unknown))


def has_caller_audio(call: Any) -> bool:
    """True when the caller actually says something in this call.

    Requires `call.turns` loaded. A call with assistant turns but no user text is the
    common shape here — the agent greeted a line that was silent, or the caller's audio
    never reached STT — and it is indistinguishable from a caller who hung up instantly.
    Either way there is nothing to judge.
    """
    return any(
        t.role == "user" and t.text and t.text.strip() for t in (getattr(call, "turns", None) or [])
    )

"""Merge missing-record questions that refer to the same absent record, with an LLM.

WHY THIS IS NOT DONE WITH STRING SIMILARITY
knowledge_gaps.py used to cluster on token overlap plus a synonym table. Measured
against the live Lazio database (211 gaps, Aug 2026) it produced 16 merges of which 11
were wrong, always the same way: a branch name is one token, and it gets outvoted by the
generic ones around it. "orari della sede di via Librogame" and "orari apertura sede di
via Voliere" share {orari, sede, via} — 3 of 4, above the 0.7 threshold — so four
different Roman branches collapsed into a single line. The customer adds hours for
Librogame, believes the list is finished, and three branches keep failing forever.

Deciding that "Librogame" and "Voliere" are two places while "lipasi" and "lipasi
pancreatica" are one exam is a judgement about what words refer to, not about how many
they share. No threshold fixes it. So the merge is an explicit, on-demand pass over the
report — a few hundred lines, pressed by a human, not per call — and its answer is
stored on the call rows so it is paid for once.

WHAT IT IS ALLOWED TO GET WRONG
A wrong merge hides a record: the customer fixes one, thinks the list is done, and the
hidden one never surfaces again. A wrong split is two similar lines next to each other,
which is visible and harmless. The prompt is therefore biased hard towards leaving
things separate, and _apply_response() below refuses to merge anything the model did not
explicitly and validly ask to merge.
"""

import logging
from typing import Any

from .analysis.llm import chat_json
from .config import settings

logger = logging.getLogger("callharness.gap_grouping")

# The reserved group for questions nobody can act on: mis-heard speech, a subject with
# no attribute, or an internal search string the software generated. These are kept out
# of the customer's report and its counts entirely (routes/analytics.py) — sending
# someone "Fate analisi per la ricerca di Levico Butter?" as a record to add wastes their
# time, and the underlying call needs a person to listen to it instead.
GAP_NEEDS_REVIEW = "needs_review"

# One id per call in the batch; the model only ever sees these short integers, never a
# call id, so a hallucinated identifier cannot silently address the wrong row.
_MAX_BATCH = 400


SYSTEM_PROMPT = """\
You group questions that a voice assistant could not answer because the record was
missing from the customer's database.

Each question comes from one phone call where a lookup ran correctly and came back with
nothing. The output is a report sent to the people who own that database, telling them
which records to add. Your only job is to decide which questions ask for the SAME
missing record, so the report has one line per record instead of one line per call.

WHAT MAKES TWO QUESTIONS THE SAME MISSING RECORD

A record is identified by two things together:
  (a) the SUBJECT   - which branch or location, which exam or service, which doctor
  (b) the ATTRIBUTE - what was asked about it: opening hours, price, address,
                      preparation instructions, whether the exam is offered at all,
                      reporting times, whether it is covered by the health service

Two questions are the same record only when BOTH (a) and (b) match. If either differs,
they are different records and stay separate.

Shared wording is not required, and shared wording is not enough. Judge what is being
asked, not which words appear.

SAME RECORD - merge:
  "Quanto costano le analisi del sangue?" / "Quanto costano le analisi del sangue?"
      identical. You may be told these came from different tools. IGNORE THE TOOL
      COMPLETELY - it is unreliable and often records the transfer tool rather than
      the lookup. Judge the question only.
  "tempi massimi custodia feci in frigorifero per ricerca sangue occulto"
  "Quanto tempo si possono conservare in frigo i campioni di feci per la ricerca del
   sangue occulto"
      same subject, same attribute, barely any shared words. Merge.
  "Per il controllo delle urine dopo terapia antibiotica vie urinarie, quanti giorni
   bisogna aspettare?"
  "Dopo quanti giorni dalla fine dell'antibiotico si puo fare l'esame delle urine per
   controllo?"
      same. This is the case you are here for.
  "Eseguite l'analisi della lipasi pancreatica?" / "Fate analisi della lipasi?"

DIFFERENT RECORDS - keep separate:
  "orari della sede di via Librogame" / "orari apertura sede di via Voliere" /
  "orari apertura sede Roma via Belardinelli"
      THREE DIFFERENT BRANCHES. Every one needs its own record. A branch name is often
      a single word inside an otherwise identical sentence - that one word decides, and
      it outweighs everything the two questions have in common.
  "orari prelievi sede via Boccea 628" / "orari apertura Cerba via Boccea 678"
      same street, DIFFERENT STREET NUMBER, so different branch.
  "orari laboratorio Dragoncello oggi" / "orari di apertura laboratorio Supino oggi"
      different towns.
  "orari chiusura sede di Acilia oggi" / "orari ritiro referti sede di Acilia"
      same branch, different attribute: closing time vs when reports can be collected.
  "Quanto costa l'analisi del sangue occulto nelle feci?" /
  "fate esame sangue occulto nelle feci con tre campioni?"
      same exam, different attribute: price vs whether it is offered.

DATES DO NOT IDENTIFY A RECORD

"orari apertura sede Torre in Pietra il 16 agosto" and "orari apertura sede Torre in
Pietra il 18 agosto" are ONE record: what is missing is that branch's hours and closure
calendar, and one entry covers every date. Ignore the date, the day of the week, and
words like "oggi", "domani", "agosto", "Ferragosto" when deciding.

This applies ONLY to the date. Everything else still decides: the branch must still be
the same branch, and the attribute must still be the same attribute. At one branch,
"orari prelievi", "orari apertura" and "orari ritiro referti" are THREE different
records, whatever dates they mention.

WHEN YOU ARE NOT SURE, KEEP THEM SEPARATE.

This is the most important instruction here. A wrong merge deletes a record from the
report: the customer adds one record, believes the list is finished, and the second
question keeps failing forever with nobody able to see why. Two lines that should have
been one is visible and harmless by comparison. Never merge two questions because they
are on a similar theme, in the same category, or about the same kind of thing. Merge
only when you could point at ONE SINGLE database record that answers both.

QUESTIONS THAT CANNOT BE ACTED ON  ->  "needs_review"

Nobody can add a record for these; a person has to open the call and listen. Flag them
in "needs_review" and never group them. Three kinds:

  1. GARBLED SPEECH the system mis-heard. These reach you looking like real questions.
       "Fate analisi per la ricerca di Levico Butter?"   (not a real test)
       "esame del sangue valore Aldo Blasi"
       "Virtuale imprese numero 100"
       "informazioni sul centro di Pietra"   (not a real branch name)
       "Effettuate dosaggio monoteiste, immunoglobuline G e beta globuline?"
     If a test name, branch name or word is not a real thing you recognise in Italian
     healthcare, treat it as mis-heard. Do NOT guess what the caller meant, do NOT
     correct it, and do NOT merge it with the real question it resembles.

  2. NO ATTRIBUTE - the question names a subject but never says what was wanted about
     it. Price? Hours? Preparation? Whether you offer it? Nobody can fill in a field
     without knowing which field.
       "curva glicemica"
       "visita morfologica"
       "analisi per il tetano"
       "anticorpi anti insulina"
     A question naming an ATTRIBUTE but no subject ("quali sono gli orari?") also goes
     here, for the same reason in reverse.

  3. INTERNAL STRINGS the software generated, not something a caller said. These are
     usually in English while real questions are in Italian.
       "centers within 42km of Villanova offering Visita otorinolaringoiatrica ..."
       "RX del Ginocchio Sinistro at Fiumicino"
       "operator transfer"

EXISTING GROUPS

You may be shown groups that already exist. If a question belongs to one, put it there
by its group id rather than creating a second group for the same record. The same rules
apply: same subject AND same attribute, and when unsure do not join - make a new group.

THE ISSUE NOTE

Each question comes with a one-sentence note from the analyst who reviewed that call.
The notes are written in ENGLISH; the questions are in ITALIAN. Use a note to work out
which branch or exam was meant when the question alone is unclear. It is evidence about
the call, not the question - never group by the note alone, and never let it justify a
merge the questions themselves do not support.

OUTPUT

Return JSON only, in exactly this shape:

{
  "groups": [
    {"group_id": "g3", "members": [12, 19]},
    {"group_id": null, "canonical": "...", "reason": "...", "members": [4, 8]}
  ],
  "needs_review": [7, 21]
}

  - Every id you were given appears EXACTLY ONCE, either in one group's "members" or in
    "needs_review". Do not drop any and do not put one in two groups.
  - "group_id" is an existing group's id when the members join it, otherwise null.
  - "canonical" is required when group_id is null and omitted when it is not. It is a
    plain question naming the subject and the attribute, so whoever reads the report
    knows which record to add. Write it in the SAME LANGUAGE AS THE QUESTIONS
    (Italian), never in the language of the notes.
  - "reason" is one short sentence saying why these are one record. Omit it for
    single-member groups.
  - A question that matches nothing else is a group of one, with itself as canonical.
  - Do not invent, correct or expand anything the questions do not contain. If no branch
    is named, your canonical must not name one.
"""


def build_user_prompt(
    items: list[dict[str, Any]],
    existing: list[dict[str, str]],
) -> str:
    """`items` are {id, question, issue_note}; `existing` are {group_id, question}."""
    parts: list[str] = []
    if existing:
        parts.append("EXISTING GROUPS")
        for group in existing:
            parts.append(f"{group['group_id']}: {group['question']}")
        parts.append("")
    parts.append("NEW QUESTIONS")
    for item in items:
        parts.append(f"{item['id']}: {item['question']}")
        note = (item.get("issue_note") or "").strip()
        if note:
            parts.append(f"   note: {note}")
    return "\n".join(parts)


def _apply_response(
    response: dict[str, Any],
    items: list[dict[str, Any]],
    existing_ids: set[str],
    next_index: int,
) -> tuple[dict[int, tuple[str, str | None]], list[str]]:
    """Turn the model's reply into {item id: (group id, canonical or None)}.

    Every input id comes back assigned to something. An id the model dropped, repeated,
    or attached to a group id that does not exist becomes its OWN group — never a merge.
    That direction is the safe one: the worst case is a line the report shows separately
    that could have been merged, against a missing record nobody ever sees again.

    Returns the assignment plus a list of human-readable warnings, which the endpoint
    passes back so a silently degraded run is visible rather than looking like a clean one.
    """
    by_id = {item["id"]: item for item in items}
    assigned: dict[int, tuple[str, str | None]] = {}
    warnings: list[str] = []
    seen: set[int] = set()

    def claim(item_id: Any) -> int | None:
        """Accept an id only once, and only if we actually sent it."""
        if not isinstance(item_id, int) or item_id not in by_id:
            warnings.append(f"model returned unknown id {item_id!r}; ignored")
            return None
        if item_id in seen:
            warnings.append(f"model listed id {item_id} twice; only the first counted")
            return None
        seen.add(item_id)
        return item_id

    for raw in response.get("needs_review") or []:
        item_id = claim(raw)
        if item_id is not None:
            assigned[item_id] = (GAP_NEEDS_REVIEW, None)

    counter = next_index
    for group in response.get("groups") or []:
        if not isinstance(group, dict):
            continue
        members = [c for c in (claim(m) for m in group.get("members") or []) if c is not None]
        if not members:
            continue
        group_id = group.get("group_id")
        canonical = (group.get("canonical") or "").strip() or None

        if group_id:
            # Joining a group that was never offered would invent an id and quietly
            # merge these calls with whatever later happens to reuse it.
            if group_id not in existing_ids:
                warnings.append(
                    f"model joined unknown group {group_id!r}; kept those "
                    f"{len(members)} question(s) separate instead"
                )
                for item_id in members:
                    counter += 1
                    assigned[item_id] = (f"g{counter}", by_id[item_id]["question"])
                continue
        else:
            counter += 1
            group_id = f"g{counter}"
            # A merged group with no canonical would show a blank headline, so fall back
            # to the shortest real question rather than dropping the group.
            if not canonical:
                canonical = min((by_id[m]["question"] for m in members), key=len)
                warnings.append(f"group {group_id} had no canonical; used the shortest question")

        for item_id in members:
            assigned[item_id] = (group_id, canonical)

    for item in items:
        if item["id"] not in assigned:
            counter += 1
            assigned[item["id"]] = (f"g{counter}", item["question"])
            warnings.append(
                f"model never placed id {item['id']}; kept it as its own record"
            )

    return assigned, warnings


async def group_gaps(
    items: list[dict[str, Any]],
    existing: list[dict[str, str]],
    next_index: int,
) -> tuple[dict[int, tuple[str, str | None]], list[str]]:
    """One LLM pass over the ungrouped questions. Returns assignments and warnings."""
    if not items:
        return {}, []
    if len(items) > _MAX_BATCH:
        # Better a truthful partial pass than a request large enough that the model
        # starts dropping ids wholesale; the caller reports what was left for next time.
        items = items[:_MAX_BATCH]

    user = build_user_prompt(items, existing)
    response = await chat_json(
        SYSTEM_PROMPT,
        user,
        model=settings.gap_grouping_model or None,
    )
    assigned, warnings = _apply_response(
        response, items, {g["group_id"] for g in existing}, next_index
    )
    for warning in warnings:
        logger.warning("gap grouping: %s", warning)
    return assigned, warnings

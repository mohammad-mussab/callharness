"""Seed a realistic Italian healthcare-booking dataset so every page has data.

Usage (from the server/ directory):
    python -m scripts.seed_healthcare_demo            # add ~180 calls over 14 days
    python -m scripts.seed_healthcare_demo --reset    # delete seeded calls first
    python -m scripts.seed_healthcare_demo --days 30 --calls 400

WHY THIS EXISTS
Waiting for production traffic to find out whether a feature works is a slow, painful
loop: deploy, wait a day, discover a gap, redeploy, wait again. This generates calls
that exercise every path deliberately — missing records asked in different words,
technical failures that must NOT be reported as missing records, judge disagreements
in both directions, all three outcomes, and a latency distribution with real outliers.

Everything is written with analysis_status="completed" and the verdict fields already
filled, so no LLM key is needed and no API cost is incurred. Seeded calls carry a
"SEED-" external_id prefix so --reset can remove exactly them and nothing else.
"""

import argparse
import asyncio
import random
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Call, Turn, utcnow  # noqa: E402
from app.quality import compute_quality  # noqa: E402

PREFIX = "SEED-"
REGIONS = ["Piemonte", "Lombardia", "Lazio", "Trentino"]

# ── Missing records ─────────────────────────────────────────────────────────────
# Each entry is one record absent from the customer's database, asked in several
# different ways. The wordings must collapse to the same normalized key or the report
# fragments into near-duplicates nobody reads — which is exactly what this exercises.
MISSING_RECORDS = [
    {
        "record": "orari apertura Lombardia",
        "askings": [
            "Quali sono gli orari di apertura a Lombardia?",
            "orari apertura Lombardia",
            "Mi sa dire gli orari di apertura per la Lombardia?",
            "gli orari di apertura in Lombardia",
        ],
        "weight": 9,
    },
    {
        "record": "prezzo risonanza magnetica Novara",
        "askings": [
            "Quanto costa una risonanza magnetica a Novara?",
            "prezzo risonanza magnetica Novara",
            "Il prezzo della risonanza magnetica a Novara",
        ],
        "weight": 6,
    },
    {
        "record": "parcheggio centro Torino",
        "askings": [
            "C'è il parcheggio al centro di Torino?",
            "parcheggio centro Torino",
        ],
        "weight": 4,
    },
    {
        "record": "preparazione esame sangue digiuno",
        "askings": [
            "Devo stare a digiuno per l'esame del sangue?",
            "preparazione esame sangue digiuno",
            "Per le analisi del sangue serve il digiuno?",
        ],
        "weight": 5,
    },
    {
        "record": "convenzione assicurazione UniSalute",
        "askings": [
            "Siete convenzionati con UniSalute?",
            "convenzione assicurazione UniSalute",
        ],
        "weight": 3,
    },
]

ANSWERED_QUESTIONS = [
    ("A che ora apre il centro di Torino?", "Il centro di Torino apre alle 7:30."),
    ("Dove si trova il centro di Novara?", "Il centro di Novara è in Via Roma 12."),
    ("Serve la ricetta per l'ecografia?", "Sì, serve l'impegnativa del medico."),
    ("Posso disdire l'appuntamento?", "Certo, la disdetta è possibile fino a 24 ore prima."),
]

TECHNICAL_FAILURES = [
    ("Vorrei prenotare un'ecografia", "get_slots", {"error": "timeout"}),
    ("Disponibilità per una visita", "booking_api", {"error": "502 Bad Gateway"}),
    ("Vorrei spostare l'appuntamento", "cerba_api", {"error": "Connection refused"}),
]


def _latency(slow: bool = False) -> dict:
    """Realistic TTFB. A tenth of turns are slow, so P95 separates from P50 the way
    it does in production — otherwise the latency page looks deceptively healthy."""
    if slow:
        return {
            "stt_ms": round(random.uniform(280, 600), 1),
            "llm_ttft_ms": round(random.uniform(1800, 4200), 1),
            "tts_ttfb_ms": round(random.uniform(300, 900), 1),
        }
    return {
        "stt_ms": round(random.uniform(90, 220), 1),
        "llm_ttft_ms": round(random.uniform(300, 900), 1),
        "tts_ttfb_ms": round(random.uniform(70, 180), 1),
    }


def _turn(idx, role, text, t, tools=None, slow=False):
    kwargs = {"idx": idx, "role": role, "text": text, "start_time": round(t, 2)}
    if role == "assistant":
        lat = _latency(slow)
        kwargs.update(lat)
        kwargs["latency_ms"] = round(sum(lat.values()), 1)
        if tools:
            kwargs["tool_calls"] = tools
    return Turn(**kwargs)


def _make_call(seq, started_at, region, scenario):
    """Build one call. `scenario` decides which paths in the product light up."""
    kind = scenario["kind"]
    turns = []
    t = 0.0
    slow_call = random.random() < 0.12

    turns.append(_turn(0, "assistant", "Buongiorno, sono l'assistente virtuale. Come posso aiutarla?", t, slow=slow_call))
    t += 4.5

    esito = motivazione = None
    transferred = False
    success = None
    transfer_reason = non_completion_reason = None
    sentiment, sentiment_score = "neutral", 0.0

    if kind == "gap":
        record = scenario["record"]
        question = random.choice(record["askings"])
        turns.append(_turn(1, "user", question, t))
        t += 3.2
        turns.append(_turn(
            2, "assistant", "Attenda qualche secondo, sto cercando queste informazioni.", t,
            tools=[{
                "name": "knowledge_base_new",
                "arguments": {"query": question},
                # The lookup ran and matched nothing — a missing record, not a fault.
                "result": random.choice([
                    {"success": False, "error": "No results found"},
                    {"results": []},
                    {},
                ]),
                "success": None,
            }],
            slow=slow_call,
        ))
        t += 6.0
        if random.random() < 0.75:
            turns.append(_turn(3, "assistant", "Non ho questa informazione, le passo un operatore.", t))
            transferred, esito, motivazione = True, "TRASFERITA", "Argomento sconosciuto"
            transfer_reason = "argomento_sconosciuto"
            sentiment, sentiment_score = "neutral", -0.2
        else:
            turns.append(_turn(3, "user", "Va bene, richiamo più tardi.", t))
            success, esito, motivazione = False, "NON COMPLETATA", "Interrotta dal paziente"
            non_completion_reason = "interrotta_dal_paziente"
            sentiment, sentiment_score = "negative", -0.5

    elif kind == "technical":
        question, tool, result = scenario["failure"]
        turns.append(_turn(1, "user", question, t))
        t += 3.0
        turns.append(_turn(
            2, "assistant", "Un attimo, verifico la disponibilità.", t,
            tools=[{"name": tool, "arguments": {"service": "ecografia"},
                    "result": result, "success": False}],
            slow=True,
        ))
        t += 8.0
        turns.append(_turn(3, "assistant", "Si è verificato un problema tecnico, le passo un operatore.", t))
        transferred, esito, motivazione = True, "TRASFERITA", "Mancata comprensione"
        transfer_reason = "technical_error" if random.random() < 0.4 else "mancata_comprensione"
        sentiment, sentiment_score = "negative", -0.6

    elif kind == "answered":
        question, answer = scenario["qa"]
        turns.append(_turn(1, "user", question, t))
        t += 3.0
        turns.append(_turn(
            2, "assistant", answer, t,
            tools=[{"name": "knowledge_base_new", "arguments": {"query": question},
                    "result": {"answer": answer}, "success": True}],
            slow=slow_call,
        ))
        t += 4.0
        turns.append(_turn(3, "user", "Perfetto, grazie mille.", t))
        success, esito, motivazione = True, "COMPLETATA", "Info fornite"
        sentiment, sentiment_score = "positive", 0.7

    elif kind == "booking":
        turns.append(_turn(1, "user", "Vorrei prenotare una visita cardiologica.", t))
        t += 3.5
        turns.append(_turn(
            2, "assistant", "Ho trovato disponibilità giovedì alle 10:30. Confermo?", t,
            tools=[{"name": "get_slots", "arguments": {"service": "cardiologia"},
                    "result": {"slots": ["2026-08-20T10:30"]}, "success": True}],
            slow=slow_call,
        ))
        t += 5.0
        turns.append(_turn(3, "user", "Sì, va bene.", t))
        t += 2.0
        turns.append(_turn(
            4, "assistant", "Prenotazione confermata, codice ABC123.", t,
            tools=[{"name": "booking_api", "arguments": {"slot": "2026-08-20T10:30"},
                    "result": {"booking_code": "ABC123"}, "success": True}],
        ))
        success, esito, motivazione = True, "COMPLETATA", "Pren. effettuata"
        sentiment, sentiment_score = "positive", 0.8

    elif kind == "hangup":
        turns.append(_turn(1, "user", "Buongiorno, volevo sapere...", t))
        t += 2.0
        turns.append(_turn(2, "assistant", "Mi dica pure, come posso aiutarla?", t, slow=slow_call))
        t += 14.0  # long silence, then nothing — shows up in quality metrics
        success, esito, motivazione = False, "NON COMPLETATA", "Interrotta dal paziente"
        non_completion_reason = "interrotta_dal_paziente"
        sentiment, sentiment_score = "neutral", -0.1

    else:  # human_requested
        turns.append(_turn(1, "user", "Vorrei parlare con un operatore per favore.", t))
        t += 2.5
        turns.append(_turn(2, "assistant", "Certamente, le passo subito un operatore.", t, slow=slow_call))
        transferred, esito, motivazione = True, "TRASFERITA", "Richiesta paziente"
        transfer_reason = "richiesta_paziente"
        sentiment, sentiment_score = "neutral", 0.1

    # ── Disagreement between the agent's judge and CallHarness ──────────────────
    # ~18% of calls, weighted toward the overcount direction (agent claims COMPLETATA,
    # CallHarness disagrees) because that is the case that inflates a reported success
    # rate and the one the disputed view exists to surface.
    if random.random() < 0.18:
        if kind in ("gap", "hangup") and random.random() < 0.7:
            esito, motivazione = "COMPLETATA", "Info fornite"   # agent over-claims
        elif kind == "answered":
            esito, motivazione = "NON COMPLETATA", "Interrotta dal paziente"

    ended_at = started_at + timedelta(seconds=t + 5)
    call = Call(
        external_id=f"{PREFIX}{seq:05d}",
        agent_id=region,
        direction="inbound",
        from_number=f"hash_{random.randint(1000, 9999)}",
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=round(t + 5, 1),
        end_reason="transferred" if transferred else None,
        transferred=transferred,
        analysis_status="completed",
        analyzed_at=ended_at,
        llm_model="gpt-4o-mini",
        language="italian",
        success=success,
        success_rationale=(
            "The caller's question was answered with concrete information."
            if success is True else
            "No bot turn delivered the requested facts before the call ended."
            if success is False else None
        ),
        summary=f"Caller contacted the {region} centre. "
                + ("Question answered." if success else "Handed to an operator."
                   if transferred else "Call ended without resolution."),
        sentiment_label=sentiment,
        sentiment_score=sentiment_score,
        transfer_reason=transfer_reason,
        non_completion_reason=non_completion_reason,
        reason_source="llm" if (transfer_reason or non_completion_reason) else None,
        meta={
            "region": region,
            "call_id": f"seed-{seq:05d}",
            "interaction_id": f"int-{seq:05d}",
            "assistant_id": f"pipecat-{region.lower()}-001",
            "agent_esito": esito,
            "agent_motivazione": motivazione,
            "queue_code": "2|2|5",
        },
    )
    call.turns = turns
    quality = compute_quality(turns)
    call.quality = quality
    call.interruption_count = quality["interruption_count"] if quality else 0
    return call


def _scenarios():
    """Weighted scenario pool, roughly matching the production mix: about half the
    calls transfer, and missing records are the single largest cause."""
    pool = []
    for record in MISSING_RECORDS:
        pool += [{"kind": "gap", "record": record}] * record["weight"]
    for f in TECHNICAL_FAILURES:
        pool += [{"kind": "technical", "failure": f}] * 2
    for qa in ANSWERED_QUESTIONS:
        pool += [{"kind": "answered", "qa": qa}] * 3
    pool += [{"kind": "booking"}] * 6
    pool += [{"kind": "hangup"}] * 7
    pool += [{"kind": "human_requested"}] * 5
    return pool


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calls", type=int, default=180)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--reset", action="store_true", help="delete seeded calls first")
    parser.add_argument("--seed", type=int, default=7, help="rng seed for reproducibility")
    args = parser.parse_args()

    random.seed(args.seed)
    await init_db()

    async with SessionLocal() as session:
        if args.reset:
            existing = (
                await session.execute(select(Call).where(Call.external_id.like(f"{PREFIX}%")))
            ).scalars().all()
            for call in existing:
                await session.delete(call)
            await session.commit()
            print(f"removed {len(existing)} previously seeded calls")

        pool = _scenarios()
        now = utcnow()
        calls = []
        for i in range(args.calls):
            # Spread across the window, busier during office hours.
            started = now - timedelta(
                days=random.uniform(0, args.days),
                hours=random.uniform(-4, 4),
            )
            calls.append(_make_call(i + 1, started, random.choice(REGIONS), random.choice(pool)))
        session.add_all(calls)
        await session.commit()

    print(f"seeded {len(calls)} calls across {len(REGIONS)} regions over {args.days} days")
    print("\nnow open:")
    print("  /gaps      — missing records, grouped across phrasings")
    print("  /disputes  — where the agent's verdict and CallHarness's differ")
    print("  /latency   — P50 vs P95, with deliberate slow outliers")
    print("  /          — outcome split and per-region comparison")


if __name__ == "__main__":
    asyncio.run(main())

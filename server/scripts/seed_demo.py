"""Seed the database with realistic demo calls so the dashboard has data.

Usage (from the server/ directory):  python -m scripts.seed_demo
"""

import asyncio
import random
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Call, Turn, utcnow  # noqa: E402

random.seed(42)

AGENTS = ["dental-receptionist", "support-line"]

SCENARIOS = [
    {
        "topic": "appointment booking",
        "success": True,
        "sentiment": ("positive", 0.7),
        "end_reason": "completed",
        "summary": "Caller booked a dental cleaning appointment for next Tuesday at 2pm. "
        "The assistant confirmed insurance details and sent an SMS confirmation.",
        "structured": {"intent": "book_appointment", "callback_needed": False},
        "turns": [
            ("assistant", "Thanks for calling Brightsmile Dental, how can I help you today?"),
            ("user", "Hi, I'd like to book a cleaning sometime next week."),
            ("assistant", "Of course! We have Tuesday at 2pm or Thursday at 10am available. Which works better?"),
            ("user", "Tuesday at 2pm works great."),
            ("assistant", "Perfect. Can I get your name and date of birth to pull up your file?"),
            ("user", "Sarah Mitchell, March 12th 1990."),
            ("assistant", "Found you, Sarah. You're booked for Tuesday at 2pm. I've sent a confirmation text. Anything else?"),
            ("user", "No that's all, thank you!"),
            ("assistant", "You're welcome! See you Tuesday."),
        ],
    },
    {
        "topic": "billing question escalated",
        "success": False,
        "sentiment": ("negative", -0.6),
        "end_reason": "transfer",
        "transferred": True,
        "summary": "Caller disputed a charge on their invoice. The assistant could not resolve "
        "the dispute and transferred the caller to the billing team.",
        "structured": {"intent": "billing_dispute", "callback_needed": True},
        "turns": [
            ("assistant", "Thanks for calling support, how can I help?"),
            ("user", "I was charged twice this month and I want a refund."),
            ("assistant", "I'm sorry about that. Let me look at your account. Can you confirm your email?"),
            ("user", "It's jake@example.com. This is the second time this happened!"),
            ("assistant", "I understand your frustration, Jake. I can see two charges. I'm not able to issue refunds directly, so I'll transfer you to our billing team."),
            ("user", "Fine, but this is really annoying."),
            ("assistant", "I completely understand. Transferring you now, one moment."),
        ],
    },
    {
        "topic": "opening hours",
        "success": True,
        "sentiment": ("neutral", 0.1),
        "end_reason": "completed",
        "summary": "Caller asked for opening hours. The assistant provided weekday and weekend "
        "hours and the caller ended the call satisfied.",
        "structured": {"intent": "general_info", "callback_needed": False},
        "turns": [
            ("assistant", "Hello! How can I help you today?"),
            ("user", "What are your hours on Saturday?"),
            ("assistant", "We're open Saturdays from 9am to 1pm, and weekdays 8am to 6pm."),
            ("user", "Okay, thanks."),
            ("assistant", "You're welcome! Anything else I can help with?"),
            ("user", "No, bye."),
        ],
    },
    {
        "topic": "caller gave up",
        "success": False,
        "sentiment": ("negative", -0.8),
        "end_reason": "user_hangup",
        "summary": "Caller tried to reschedule an appointment but the assistant repeatedly "
        "misunderstood the requested date. The caller hung up frustrated.",
        "structured": {"intent": "reschedule", "callback_needed": True},
        "turns": [
            ("assistant", "Thanks for calling, how can I help?"),
            ("user", "I need to move my appointment from Friday to Monday."),
            ("assistant", "Sure — I can book you a new appointment on Friday. What time?"),
            ("user", "No, I said move it FROM Friday TO Monday."),
            ("assistant", "I understand you'd like an appointment on Friday. We have 3pm available."),
            ("user", "Are you kidding me? Forget it."),
        ],
    },
    {
        "topic": "prescription refill",
        "success": True,
        "sentiment": ("positive", 0.5),
        "end_reason": "completed",
        "summary": "Caller requested a prescription refill. The assistant verified their "
        "identity, confirmed the pharmacy on file, and submitted the refill request.",
        "structured": {"intent": "refill", "callback_needed": False},
        "turns": [
            ("assistant", "Hello, how can I help you today?"),
            ("user", "I need to refill my prescription."),
            ("assistant", "Happy to help. Can you verify your name and date of birth?"),
            ("user", "Tom Alvarez, June 3rd 1985."),
            ("assistant", "Thanks Tom. I see one active prescription. Should I send the refill to your pharmacy on file, GreenCross on Main St?"),
            ("user", "Yes please."),
            ("assistant", "Done! It'll be ready in about an hour. Anything else?"),
            ("user", "Nope, that's it. Thanks!"),
        ],
    },
    {
        "topic": "voicemail",
        "success": None,
        "sentiment": (None, None),
        "end_reason": "voicemail",
        "summary": None,
        "structured": None,
        "turns": [
            ("assistant", "Hi, this is the outbound reminder call from Brightsmile Dental. We're calling to confirm your appointment tomorrow at 3pm. Please call us back if you need to reschedule."),
        ],
        "skip_analysis": True,
        "direction": "outbound",
    },
]


def make_turns(spec: list[tuple[str, str]]) -> tuple[list[Turn], float]:
    turns = []
    t = 0.8
    for i, (role, text) in enumerate(spec):
        speak_seconds = max(1.2, len(text) / 14.0)
        turns.append(
            Turn(
                idx=i,
                role=role,
                text=text,
                start_time=round(t, 2),
                end_time=round(t + speak_seconds, 2),
                latency_ms=round(random.gauss(850, 200), 0) if role == "assistant" else None,
                interrupted=random.random() < 0.06,
            )
        )
        t += speak_seconds + random.uniform(0.4, 1.4)
    return turns, t


async def main() -> None:
    await init_db()
    now = utcnow()
    created = 0
    async with SessionLocal() as session:
        for day_offset in range(13, -1, -1):
            # busier weekdays, ramping volume in recent days
            n_calls = random.randint(3, 6) + (2 if day_offset < 5 else 0)
            for _ in range(n_calls):
                scenario = random.choice(SCENARIOS)
                turns, total_seconds = make_turns(scenario["turns"])
                started = now - timedelta(
                    days=day_offset,
                    hours=random.randint(0, 9),
                    minutes=random.randint(0, 59),
                )
                sentiment_label, sentiment_score = scenario["sentiment"]
                jitter = random.uniform(-0.15, 0.15)
                call = Call(
                    agent_id=random.choice(AGENTS),
                    direction=scenario.get("direction", "inbound"),
                    from_number=f"+1555{random.randint(1000000, 9999999)}",
                    to_number="+15550100200",
                    started_at=started,
                    ended_at=started + timedelta(seconds=total_seconds),
                    duration_seconds=round(total_seconds, 1),
                    end_reason=scenario["end_reason"],
                    transferred=scenario.get("transferred", False),
                    analysis_status="skipped" if scenario.get("skip_analysis") else "completed",
                    summary=scenario["summary"],
                    sentiment_label=sentiment_label,
                    sentiment_score=(
                        round(max(-1, min(1, sentiment_score + jitter)), 2)
                        if sentiment_score is not None
                        else None
                    ),
                    success=scenario["success"],
                    success_rationale=(
                        "Demo data: seeded rationale for " + scenario["topic"]
                        if scenario["success"] is not None
                        else None
                    ),
                    structured_data=scenario["structured"],
                    llm_model="seed-demo",
                    turns=turns,
                    meta={"seed": True, "topic": scenario["topic"]},
                )
                session.add(call)
                created += 1
        await session.commit()
    print(f"Seeded {created} demo calls.")


if __name__ == "__main__":
    asyncio.run(main())

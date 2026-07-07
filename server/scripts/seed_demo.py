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
from app.models import (  # noqa: E402
    AlertEvent,
    AlertRule,
    Call,
    EvaluationResult,
    Evaluator,
    Turn,
    utcnow,
)
from app.quality import compute_quality  # noqa: E402

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


def make_turns(spec: list[tuple[str, str]], slow_day: bool = False) -> tuple[list[Turn], float]:
    turns = []
    t = 0.8
    lat_scale = 1.5 if slow_day else 1.0
    for i, (role, text) in enumerate(spec):
        speak_seconds = max(1.2, len(text) / 14.0)
        stt = llm = tts = latency = None
        if role == "assistant":
            stt = max(80.0, random.gauss(300, 70) * lat_scale)
            llm = max(150.0, random.gauss(550, 160) * lat_scale)
            tts = max(60.0, random.gauss(200, 50) * lat_scale)
            latency = stt + llm + tts + max(0.0, random.gauss(60, 30))
        turns.append(
            Turn(
                idx=i,
                role=role,
                text=text,
                start_time=round(t, 2),
                end_time=round(t + speak_seconds, 2),
                latency_ms=round(latency, 0) if latency else None,
                stt_ms=round(stt, 0) if stt else None,
                llm_ttft_ms=round(llm, 0) if llm else None,
                tts_ttfb_ms=round(tts, 0) if tts else None,
                interrupted=random.random() < 0.06,
            )
        )
        t += speak_seconds + random.uniform(0.4, 1.4)
    return turns, t


EVALUATORS = [
    {
        "name": "Greeted the caller properly",
        "prompt": "The assistant opened the call with a polite greeting and identified the business before asking anything of the caller.",
        "pass_bias": 0.9,
    },
    {
        "name": "No unresolved caller frustration",
        "prompt": "The call did not end with the caller expressing unresolved frustration or giving up on their request.",
        "pass_bias": 0.7,
    },
]


async def seed_evaluators_and_alerts(session, calls: list[Call]) -> None:
    evaluators = []
    for spec in EVALUATORS:
        evaluator = Evaluator(name=spec["name"], prompt=spec["prompt"], enabled=True)
        session.add(evaluator)
        evaluators.append((evaluator, spec["pass_bias"]))
    await session.flush()

    for call in calls:
        if call.analysis_status != "completed":
            continue
        for evaluator, bias in evaluators:
            effective_bias = bias if call.success else bias - 0.5
            passed = random.random() < max(0.05, effective_bias)
            session.add(
                EvaluationResult(
                    call_id=call.id,
                    evaluator_id=evaluator.id,
                    evaluator_name=evaluator.name,
                    passed=passed,
                    reason=(
                        "Demo data: criterion met."
                        if passed
                        else "Demo data: criterion not met on this call."
                    ),
                )
            )

    rule = AlertRule(
        name="Angry caller alert (example)",
        enabled=False,
        trigger="negative_sentiment_call",
        threshold=-0.5,
        channel="slack",
        target_url="https://hooks.slack.com/services/REPLACE/ME",
        cooldown_minutes=15,
    )
    session.add(rule)
    await session.flush()
    now = utcnow()
    negative_calls = [c for c in calls if (c.sentiment_score or 0) <= -0.5][:3]
    for i, call in enumerate(negative_calls):
        session.add(
            AlertEvent(
                rule_id=rule.id,
                rule_name=rule.name,
                call_id=call.id,
                message=f"Negative sentiment ({call.sentiment_score:+.2f}) on call "
                f"{call.id[:8]} (agent {call.agent_id}): {call.summary}",
                fired_at=now - timedelta(hours=6 * (i + 1)),
                delivered=True,
            )
        )


async def main() -> None:
    await init_db()
    now = utcnow()
    created = 0
    all_calls: list[Call] = []
    async with SessionLocal() as session:
        for day_offset in range(13, -1, -1):
            # busier weekdays, ramping volume in recent days
            n_calls = random.randint(3, 6) + (2 if day_offset < 5 else 0)
            slow_day = day_offset in (6, 7)  # simulate a latency regression window
            for _ in range(n_calls):
                scenario = random.choice(SCENARIOS)
                turns, total_seconds = make_turns(scenario["turns"], slow_day=slow_day)
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
                quality = compute_quality(turns)
                call.quality = quality
                call.interruption_count = quality["interruption_count"] if quality else 0
                session.add(call)
                all_calls.append(call)
                created += 1
        await session.flush()
        await seed_evaluators_and_alerts(session, all_calls)
        await session.commit()
    print(f"Seeded {created} demo calls, {len(EVALUATORS)} evaluators, 1 alert rule.")


if __name__ == "__main__":
    asyncio.run(main())

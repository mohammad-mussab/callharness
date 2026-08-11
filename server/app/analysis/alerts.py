"""Alerting engine.

Two kinds of triggers:
- Per-call: evaluated right after a call finishes analysis (or is skipped).
- Windowed: evaluated periodically by the worker over a rolling time window.

Delivery: generic JSON webhook, Slack incoming webhook, or email (SMTP).
Every firing is logged as an AlertEvent regardless of delivery outcome.
"""

import asyncio
import logging
import smtplib
from datetime import timedelta
from email.message import EmailMessage

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import AlertEvent, AlertRule, Call, utcnow

logger = logging.getLogger("opencall.alerts")

PER_CALL_TRIGGERS = {
    "negative_sentiment_call",
    "failed_call",
    "keyword_match",
    "high_latency_call",
}
WINDOW_TRIGGERS = {"success_rate_window", "sentiment_window"}


def _in_cooldown(rule: AlertRule) -> bool:
    if rule.last_fired_at is None or rule.cooldown_minutes <= 0:
        return False
    return utcnow() < rule.last_fired_at + timedelta(minutes=rule.cooldown_minutes)


def _send_email_sync(recipients: list[str], subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from or settings.smtp_user or "opencall@localhost"
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
        if settings.smtp_starttls:
            smtp.starttls()
        if settings.smtp_user and settings.smtp_password:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)


async def _deliver(rule: AlertRule, message: str) -> tuple[bool, str | None]:
    try:
        if rule.channel == "email":
            if not settings.smtp_host:
                return False, (
                    "Email channel needs SMTP configured on the server "
                    "(OPENCALL_SMTP_HOST, OPENCALL_SMTP_USER, OPENCALL_SMTP_PASSWORD, "
                    "OPENCALL_SMTP_FROM)"
                )
            recipients = [a.strip() for a in rule.target_url.split(",") if a.strip()]
            if not recipients:
                return False, "No recipient email addresses configured"
            await asyncio.to_thread(
                _send_email_sync, recipients, f"OpenCall alert: {rule.name}", message
            )
            return True, None

        payload = (
            {"text": f":rotating_light: OpenCall alert — {message}"}
            if rule.channel == "slack"
            else {"rule": rule.name, "trigger": rule.trigger, "message": message}
        )
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(rule.target_url, json=payload)
            if resp.status_code >= 300:
                return False, f"HTTP {resp.status_code}"
            return True, None
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:500]


async def _fire(
    session: AsyncSession, rule: AlertRule, message: str, call_id: str | None = None
) -> None:
    delivered, error = await _deliver(rule, message)
    session.add(
        AlertEvent(
            rule_id=rule.id,
            rule_name=rule.name,
            call_id=call_id,
            message=message,
            delivered=delivered,
            delivery_error=error,
        )
    )
    rule.last_fired_at = utcnow()
    await session.commit()
    logger.info("Alert fired: %s (%s) delivered=%s", rule.name, rule.trigger, delivered)


def _avg_assistant_latency(call: Call) -> float | None:
    vals = [t.latency_ms for t in call.turns if t.role == "assistant" and t.latency_ms is not None]
    return sum(vals) / len(vals) if vals else None


def _match_per_call(rule: AlertRule, call: Call) -> str | None:
    """Return alert message if the rule matches this call, else None."""
    caller = f", caller {call.from_number}" if call.from_number else ""
    label = f"call {call.id[:8]} (agent {call.agent_id}{caller})"
    if rule.trigger == "negative_sentiment_call":
        threshold = rule.threshold if rule.threshold is not None else -0.5
        if call.sentiment_score is not None and call.sentiment_score <= threshold:
            return f"Negative sentiment ({call.sentiment_score:+.2f}) on {label}: {call.summary or 'no summary'}"
    elif rule.trigger == "failed_call":
        if call.success is False:
            return f"Failed {label}: {call.success_rationale or call.summary or 'no rationale'}"
    elif rule.trigger == "keyword_match":
        if rule.keyword:
            needle = rule.keyword.lower()
            for turn in call.turns:
                if needle in turn.text.lower():
                    return f"Keyword '{rule.keyword}' mentioned in {label}: \"{turn.text[:120]}\""
    elif rule.trigger == "high_latency_call":
        threshold = rule.threshold if rule.threshold is not None else 2000
        avg = _avg_assistant_latency(call)
        if avg is not None and avg >= threshold:
            return f"High response latency (avg {avg:.0f}ms >= {threshold:.0f}ms) on {label}"
    return None


async def check_call_alerts(session: AsyncSession, call: Call) -> None:
    """Evaluate all enabled per-call rules against a finished call.
    Requires call.turns to be loaded."""
    rules = (
        (
            await session.execute(
                select(AlertRule).where(
                    AlertRule.enabled == True,  # noqa: E712
                    AlertRule.trigger.in_(PER_CALL_TRIGGERS),
                )
            )
        )
        .scalars()
        .all()
    )
    for rule in rules:
        if _in_cooldown(rule):
            continue
        message = _match_per_call(rule, call)
        if message:
            await _fire(session, rule, message, call_id=call.id)


async def check_window_alerts(session: AsyncSession) -> None:
    """Evaluate windowed rules over their rolling windows."""
    rules = (
        (
            await session.execute(
                select(AlertRule).where(
                    AlertRule.enabled == True,  # noqa: E712
                    AlertRule.trigger.in_(WINDOW_TRIGGERS),
                )
            )
        )
        .scalars()
        .all()
    )
    for rule in rules:
        if _in_cooldown(rule):
            continue
        since = utcnow() - timedelta(minutes=rule.window_minutes)
        rows = (
            await session.execute(
                select(Call.success, Call.sentiment_score).where(Call.started_at >= since)
            )
        ).all()
        if rule.trigger == "success_rate_window":
            threshold = rule.threshold if rule.threshold is not None else 0.7
            evaluated = [r for r in rows if r.success is not None]
            if len(evaluated) >= rule.min_calls:
                rate = sum(1 for r in evaluated if r.success) / len(evaluated)
                if rate < threshold:
                    await _fire(
                        session,
                        rule,
                        f"Success rate {rate:.0%} over last {rule.window_minutes}m "
                        f"({len(evaluated)} calls) is below {threshold:.0%}",
                    )
        elif rule.trigger == "sentiment_window":
            threshold = rule.threshold if rule.threshold is not None else -0.2
            scores = [r.sentiment_score for r in rows if r.sentiment_score is not None]
            if len(scores) >= rule.min_calls:
                avg = sum(scores) / len(scores)
                if avg < threshold:
                    await _fire(
                        session,
                        rule,
                        f"Average sentiment {avg:+.2f} over last {rule.window_minutes}m "
                        f"({len(scores)} calls) is below {threshold:+.2f}",
                    )

"""Custom LLM-as-judge evaluators.

Each enabled evaluator is a user-authored criterion run against every
analyzed call's transcript, producing pass/fail + a one-sentence reason.
"""

import logging

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Call, EvaluationResult, Evaluator
from .engine import build_transcript
from .llm import chat_json

logger = logging.getLogger("callharness.evaluators")

EVAL_SYSTEM_PROMPT = """You are a strict QA judge for AI voice agent calls. You are given \
one evaluation criterion and a call transcript. Decide whether the call meets the criterion. \
Respond with a single JSON object: {"passed": true or false, "reason": "one short sentence"}. \
Respond with JSON only."""


async def run_evaluators(session: AsyncSession, call: Call) -> None:
    """Run all enabled evaluators against a call and store results.
    Replaces any previous results for the call (re-analysis)."""
    evaluators = (
        (await session.execute(select(Evaluator).where(Evaluator.enabled == True)))  # noqa: E712
        .scalars()
        .all()
    )
    if not evaluators:
        return

    await session.execute(delete(EvaluationResult).where(EvaluationResult.call_id == call.id))

    transcript = build_transcript(call)
    for evaluator in evaluators:
        user_prompt = f"Criterion: {evaluator.prompt}\n\nTranscript:\n{transcript}"
        passed: bool | None
        try:
            result = await chat_json(EVAL_SYSTEM_PROMPT, user_prompt)
            raw = result.get("passed")
            passed = bool(raw) if raw is not None else None
            reason = str(result.get("reason") or "")[:1000]
        except Exception as exc:  # noqa: BLE001 - one bad evaluator must not sink the rest
            logger.warning("Evaluator '%s' failed on call %s: %s", evaluator.name, call.id, exc)
            passed, reason = None, f"Evaluator error: {str(exc)[:200]}"
        session.add(
            EvaluationResult(
                call_id=call.id,
                evaluator_id=evaluator.id,
                evaluator_name=evaluator.name,
                passed=passed,
                reason=reason,
            )
        )

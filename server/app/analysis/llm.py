"""Provider-agnostic LLM client for post-call analysis.

Supports OpenAI-compatible endpoints (OpenAI, Ollama, vLLM, ...) and the
Anthropic Messages API. One entry point: chat_json(system, user) -> dict.
"""

import asyncio
import json
import logging
import re

import httpx

from ..config import settings

logger = logging.getLogger("callharness.llm")


class LLMError(Exception):
    pass


# Rate limits are the normal failure mode of a backfill, not an exceptional one: the
# org cap is per-minute, so re-analysing a few hundred calls will hit it repeatedly
# however carefully concurrency is tuned. Without a retry the worker marks each one
# `failed` and someone has to requeue them by hand, which is exactly the pass you were
# trying to run. Waits are bounded so a genuine outage still surfaces as an error.
_RATE_LIMIT_RETRIES = 4
_RATE_LIMIT_BACKOFF = (5.0, 15.0, 30.0, 60.0)


def _retry_after_seconds(resp: httpx.Response, attempt: int) -> float:
    """How long to wait before retrying a 429, preferring the server's own answer."""
    header = resp.headers.get("retry-after") or resp.headers.get(
        "x-ratelimit-reset-tokens"
    )
    if header:
        try:
            # Seconds, or the "1.5s" / "6ms" form OpenAI uses on the reset headers.
            text = header.strip().lower()
            if text.endswith("ms"):
                return min(float(text[:-2]) / 1000, 60.0)
            if text.endswith("s"):
                return min(float(text[:-1]), 60.0)
            return min(float(text), 60.0)
        except ValueError:
            pass
    return _RATE_LIMIT_BACKOFF[min(attempt, len(_RATE_LIMIT_BACKOFF) - 1)]


def _parse_json(text: str) -> dict:
    text = text.strip()
    # Strip markdown code fences if present
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Fall back to the first {...} block in the response
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise LLMError(f"Model did not return valid JSON: {text[:200]}")


# Models that accept only the default temperature. Sending one anyway costs a wasted
# round-trip: the request 400s and the retry below strips the parameter and re-sends.
# Cheap to avoid, and worth avoiding on the grouping pass, whose prompt is large.
_FIXED_TEMPERATURE_PREFIXES = ("gpt-5", "o1", "o3", "o4")


def _rejects_temperature(model: str) -> bool:
    return model.lower().startswith(_FIXED_TEMPERATURE_PREFIXES)


async def _openai_chat(system: str, user: str, model: str | None = None) -> str:
    headers = {"Content-Type": "application/json"}
    if settings.openai_api_key:
        headers["Authorization"] = f"Bearer {settings.openai_api_key}"
    model = model or settings.resolved_model
    payload = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if not _rejects_temperature(model):
        payload["temperature"] = settings.llm_temperature
    url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, json=payload, headers=headers)

        # Drop parameters the endpoint rejects and try again, one at a time. Two real
        # cases this covers: some OpenAI-compatible servers (Ollama, older vLLM) have no
        # response_format, and the gpt-5 family rejects any temperature other than the
        # default — so setting CALLHARNESS_LLM_MODEL to one of those would otherwise
        # fail every analysis with an opaque 400. Only retries when the error names the
        # parameter, so a genuine failure still surfaces instead of being retried blind.
        for param in ("response_format", "temperature"):
            if resp.status_code == 400 and param in resp.text and param in payload:
                payload.pop(param)
                resp = await client.post(url, json=payload, headers=headers)

        for attempt in range(_RATE_LIMIT_RETRIES):
            if resp.status_code != 429:
                break
            wait = _retry_after_seconds(resp, attempt)
            logger.info(
                "Rate limited by the LLM provider; retrying in %.1fs (attempt %d/%d)",
                wait, attempt + 1, _RATE_LIMIT_RETRIES,
            )
            await asyncio.sleep(wait)
            resp = await client.post(url, json=payload, headers=headers)

        if resp.status_code != 200:
            raise LLMError(f"LLM request failed ({resp.status_code}): {resp.text[:300]}")
        return resp.json()["choices"][0]["message"]["content"]


async def _anthropic_chat(system: str, user: str, model: str | None = None) -> str:
    headers = {
        "x-api-key": settings.anthropic_api_key or "",
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model or settings.resolved_model,
        "max_tokens": 2048,
        "temperature": 0.1,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages", json=payload, headers=headers
        )
        if resp.status_code != 200:
            raise LLMError(f"LLM request failed ({resp.status_code}): {resp.text[:300]}")
        blocks = resp.json().get("content", [])
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


async def chat_json(system: str, user: str, model: str | None = None) -> dict:
    """`model` overrides the configured one for this call only.

    Used by the knowledge-gap grouping pass, which runs once over the whole report
    rather than per call, so it can afford a stronger model than per-call analysis
    without moving that cost onto every call.
    """
    provider = settings.resolved_provider
    if provider == "anthropic":
        raw = await _anthropic_chat(system, user, model)
    elif provider == "openai":
        raw = await _openai_chat(system, user, model)
    else:
        raise LLMError("No LLM provider configured")
    return _parse_json(raw)

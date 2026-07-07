"""Provider-agnostic LLM client for post-call analysis.

Supports OpenAI-compatible endpoints (OpenAI, Ollama, vLLM, ...) and the
Anthropic Messages API. One entry point: chat_json(system, user) -> dict.
"""

import json
import re

import httpx

from ..config import settings


class LLMError(Exception):
    pass


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


async def _openai_chat(system: str, user: str) -> str:
    headers = {"Content-Type": "application/json"}
    if settings.openai_api_key:
        headers["Authorization"] = f"Bearer {settings.openai_api_key}"
    payload = {
        "model": settings.resolved_model,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{settings.llm_base_url.rstrip('/')}/chat/completions",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 400 and "response_format" in resp.text:
            # Some OpenAI-compatible servers reject response_format; retry without it.
            payload.pop("response_format")
            resp = await client.post(
                f"{settings.llm_base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers=headers,
            )
        if resp.status_code != 200:
            raise LLMError(f"LLM request failed ({resp.status_code}): {resp.text[:300]}")
        return resp.json()["choices"][0]["message"]["content"]


async def _anthropic_chat(system: str, user: str) -> str:
    headers = {
        "x-api-key": settings.anthropic_api_key or "",
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.resolved_model,
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


async def chat_json(system: str, user: str) -> dict:
    provider = settings.resolved_provider
    if provider == "anthropic":
        raw = await _anthropic_chat(system, user)
    elif provider == "openai":
        raw = await _openai_chat(system, user)
    else:
        raise LLMError("No LLM provider configured")
    return _parse_json(raw)

"""HTTP clients for the OpenCall ingestion API."""

from pathlib import Path
from typing import Any

import httpx


def _headers(api_key: str | None) -> dict[str, str]:
    return {"x-api-key": api_key} if api_key else {}


def _clean(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if v is not None}


def _build_call_payload(
    agent_id: str,
    turns: list[dict[str, Any]],
    **kwargs: Any,
) -> dict[str, Any]:
    return _clean({"agent_id": agent_id, "turns": turns, **kwargs})


class OpenCallClient:
    """Synchronous client. Suitable for scripts and batch imports."""

    def __init__(self, base_url: str = "http://localhost:8010", api_key: str | None = None):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url, headers=_headers(api_key), timeout=30
        )

    def ingest_call(
        self, agent_id: str = "default", turns: list[dict[str, Any]] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        resp = self._client.post(
            "/api/v1/calls", json=_build_call_payload(agent_id, turns or [], **kwargs)
        )
        resp.raise_for_status()
        return resp.json()

    def upload_recording(self, call_id: str, path: str) -> dict[str, Any]:
        p = Path(path)
        with p.open("rb") as f:
            resp = self._client.post(
                f"/api/v1/calls/{call_id}/recording", files={"file": (p.name, f)}
            )
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self._client.close()


class AsyncOpenCallClient:
    """Async client. Use inside voice agent processes (Pipecat, LiveKit)."""

    def __init__(self, base_url: str = "http://localhost:8010", api_key: str | None = None):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url, headers=_headers(api_key), timeout=30
        )

    async def ingest_call(
        self, agent_id: str = "default", turns: list[dict[str, Any]] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        resp = await self._client.post(
            "/api/v1/calls", json=_build_call_payload(agent_id, turns or [], **kwargs)
        )
        resp.raise_for_status()
        return resp.json()

    async def upload_recording(self, call_id: str, path: str) -> dict[str, Any]:
        p = Path(path)
        resp = await self._client.post(
            f"/api/v1/calls/{call_id}/recording",
            files={"file": (p.name, p.read_bytes())},
        )
        resp.raise_for_status()
        return resp.json()

    async def close(self) -> None:
        await self._client.aclose()

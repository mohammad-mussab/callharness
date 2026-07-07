"""Framework-agnostic call recorder.

Collects transcript turns during a live call and uploads the finished call to
OpenCall. The Pipecat integration in opencall_sdk.pipecat builds on this.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from .client import AsyncOpenCallClient

logger = logging.getLogger("opencall.sdk")


class CallRecorder:
    def __init__(
        self,
        client: AsyncOpenCallClient,
        agent_id: str = "default",
        external_id: str | None = None,
        direction: str = "inbound",
        from_number: str | None = None,
        to_number: str | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        self.client = client
        self.agent_id = agent_id
        self.external_id = external_id
        self.direction = direction
        self.from_number = from_number
        self.to_number = to_number
        self.metadata = metadata or {}
        self.started_at: datetime = datetime.now(timezone.utc)
        self.turns: list[dict[str, Any]] = []
        self._flushed = False
        self._pending_components: dict[str, float] = {}

    def record_component_latency(self, component: str, ms: float) -> None:
        """Record STT/LLM/TTS latency for the *next* assistant turn.
        component: "stt" | "llm" | "tts"."""
        if component in ("stt", "llm", "tts") and ms >= 0:
            self._pending_components[component] = round(ms, 1)

    def add_turn(
        self,
        role: str,
        text: str,
        start_time: float | None = None,
        latency_ms: float | None = None,
        stt_ms: float | None = None,
        llm_ttft_ms: float | None = None,
        tts_ttfb_ms: float | None = None,
        interrupted: bool = False,
    ) -> None:
        if role not in ("user", "assistant") or not text.strip():
            return
        # Merge consecutive fragments from the same speaker into one turn
        if self.turns and self.turns[-1]["role"] == role:
            self.turns[-1]["text"] += " " + text.strip()
            return
        if start_time is None:
            start_time = (datetime.now(timezone.utc) - self.started_at).total_seconds()
        if role == "assistant" and self._pending_components:
            stt_ms = stt_ms if stt_ms is not None else self._pending_components.get("stt")
            llm_ttft_ms = (
                llm_ttft_ms if llm_ttft_ms is not None else self._pending_components.get("llm")
            )
            tts_ttfb_ms = (
                tts_ttfb_ms if tts_ttfb_ms is not None else self._pending_components.get("tts")
            )
            self._pending_components = {}
        if latency_ms is None:
            # Approximate voice-to-voice latency as the sum of component latencies
            components = [v for v in (stt_ms, llm_ttft_ms, tts_ttfb_ms) if v is not None]
            latency_ms = round(sum(components), 1) if components else None
        self.turns.append(
            {
                "role": role,
                "text": text.strip(),
                "start_time": round(start_time, 2),
                "latency_ms": latency_ms,
                "stt_ms": stt_ms,
                "llm_ttft_ms": llm_ttft_ms,
                "tts_ttfb_ms": tts_ttfb_ms,
                "interrupted": interrupted,
            }
        )

    async def flush(
        self,
        end_reason: str | None = None,
        transferred: bool = False,
        recording_path: str | None = None,
    ) -> dict[str, Any] | None:
        """Upload the call. Safe to call multiple times; only uploads once."""
        if self._flushed:
            return None
        self._flushed = True
        if not self.turns:
            logger.info("OpenCall: no turns recorded, skipping upload")
            return None
        ended_at = datetime.now(timezone.utc)
        try:
            call = await self.client.ingest_call(
                agent_id=self.agent_id,
                turns=self.turns,
                external_id=self.external_id,
                direction=self.direction,
                from_number=self.from_number,
                to_number=self.to_number,
                started_at=self.started_at.isoformat(),
                ended_at=ended_at.isoformat(),
                end_reason=end_reason,
                transferred=transferred,
                metadata=self.metadata or None,
            )
            if recording_path:
                await self.client.upload_recording(call["id"], recording_path)
            logger.info("OpenCall: uploaded call %s (%d turns)", call["id"], len(self.turns))
            return call
        except Exception as exc:  # noqa: BLE001 - never crash the host agent
            logger.warning("OpenCall: failed to upload call: %s", exc)
            return None

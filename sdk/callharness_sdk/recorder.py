"""Framework-agnostic call recorder.

Collects transcript turns during a live call and uploads the finished call to
CallHarness. The Pipecat integration in callharness_sdk.pipecat builds on this.
"""

import io
import logging
import wave
from datetime import datetime, timezone
from typing import Any

from .client import AsyncCallHarnessClient

logger = logging.getLogger("callharness.sdk")


class CallRecorder:
    def __init__(
        self,
        client: AsyncCallHarnessClient,
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
        self._pending_tool_calls: list[dict[str, Any]] = []
        # Raw PCM accumulated from the pipeline; wrapped in a WAV header at flush().
        # Kept as bytes rather than a file so nothing is written to the agent's disk —
        # a voice agent container is usually ephemeral and often read-only.
        self._audio = bytearray()
        self._audio_sample_rate = 16000
        self._audio_channels = 1

    def record_component_latency(self, component: str, ms: float) -> None:
        """Record STT/LLM/TTS latency for the *next* assistant turn.
        component: "stt" | "llm" | "tts"."""
        if component in ("stt", "llm", "tts") and ms >= 0:
            self._pending_components[component] = round(ms, 1)

    def record_tool_call(
        self, name: str, arguments: Any = None, result: Any = None, success: bool | None = None
    ) -> None:
        """Record a tool/function call, attached to the *next* assistant turn
        (tool calls happen mid-processing, before the assistant's spoken reply)."""
        self._pending_tool_calls.append(
            {"name": name, "arguments": arguments, "result": result, "success": success}
        )

    def add_audio(self, pcm: bytes, sample_rate: int = 16000, num_channels: int = 1) -> None:
        """Append raw PCM captured from the pipeline.

        Safe to call repeatedly: Pipecat's AudioBufferProcessor fires once at
        stop_recording() when buffer_size=0, but repeatedly while the call runs when
        buffer_size is set, so chunks are appended rather than replaced.
        """
        if not pcm:
            return
        self._audio.extend(pcm)
        self._audio_sample_rate = sample_rate or self._audio_sample_rate
        self._audio_channels = num_channels or self._audio_channels

    @property
    def has_audio(self) -> bool:
        return len(self._audio) > 0

    def audio_wav(self) -> bytes | None:
        """The accumulated audio as a playable WAV, or None if nothing was captured.

        Uses the stdlib `wave` module — the PCM is already the right shape, so this
        only adds a 44-byte header and the SDK stays dependency-free.
        """
        if not self._audio:
            return None
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(self._audio_channels)
            wav.setsampwidth(2)  # AudioBufferProcessor emits 16-bit PCM
            wav.setframerate(self._audio_sample_rate)
            wav.writeframes(bytes(self._audio))
        return buffer.getvalue()

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
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> None:
        if role not in ("user", "assistant") or not text.strip():
            return
        # Merge consecutive fragments from the same speaker into one turn
        if self.turns and self.turns[-1]["role"] == role:
            self.turns[-1]["text"] += " " + text.strip()
            if tool_calls:
                self.turns[-1].setdefault("tool_calls", []).extend(tool_calls)
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
        if role == "assistant" and self._pending_tool_calls:
            tool_calls = (tool_calls or []) + self._pending_tool_calls
            self._pending_tool_calls = []
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
                "tool_calls": tool_calls or None,
            }
        )

    async def flush(
        self,
        end_reason: str | None = None,
        transferred: bool = False,
        recording_path: str | None = None,
        recording_bytes: bytes | None = None,
        recording_filename: str = "recording.wav",
        transfer_reason: str | None = None,
        non_completion_reason: str | None = None,
    ) -> dict[str, Any] | None:
        """Upload the call. Safe to call multiple times; only uploads once.

        `transfer_reason` / `non_completion_reason` are optional and only for agents
        that already classify their own calls. Leave them unset and CallHarness infers
        the reason during analysis, using the taxonomy configured in the dashboard.
        When set, the value is authoritative and analysis will not overwrite it — so
        it should match one of the configured category keys.
        """
        if self._flushed:
            return None
        self._flushed = True
        if not self.turns:
            logger.info("CallHarness: no turns recorded, skipping upload")
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
                transfer_reason=transfer_reason,
                non_completion_reason=non_completion_reason,
            )
            # Audio captured via attach_audio() uploads itself, so the integrator
            # never has to remember a second call. An explicit recording_path or
            # recording_bytes still wins if one was passed.
            if recording_path:
                await self.client.upload_recording(call["id"], recording_path)
            elif recording_bytes:
                await self.client.upload_recording_bytes(
                    call["id"], recording_bytes, recording_filename
                )
            elif self.has_audio:
                wav = self.audio_wav()
                if wav:
                    await self.client.upload_recording_bytes(
                        call["id"], wav, recording_filename
                    )
                    logger.info(
                        "CallHarness: uploaded %.1fs of audio for call %s",
                        len(self._audio) / (self._audio_sample_rate * 2 * self._audio_channels),
                        call["id"],
                    )
            logger.info("CallHarness: uploaded call %s (%d turns)", call["id"], len(self.turns))
            return call
        except Exception as exc:  # noqa: BLE001 - never crash the host agent
            logger.warning("CallHarness: failed to upload call: %s", exc)
            return None

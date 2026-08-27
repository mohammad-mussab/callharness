"""The voice on our end of the call: an OpenAI Realtime session.

Speech-to-speech rather than the usual transcribe → think → speak chain, for one
reason: this has to hold a phone conversation, and three hops of latency is the
difference between a caller and something the agent talks over.

Two details carry most of the weight.

**Audio format.** Both sides are told ``audio/pcmu`` — G.711 µ-law at 8kHz, which is
exactly what Twilio puts on the wire. So the bridge forwards base64 strings between
two sockets and never decodes, resamples or buffers audio. Ask for anything else and
you own a resampler.

**Who is "user".** From this model's point of view the *production agent* is the
user and our synthetic caller is the assistant. Every transcript here is relabelled
at the boundary (``speaker: "agent" | "tester"``), because a stored transcript where
"user" means the thing being tested is unreadable a week later.
"""

from __future__ import annotations


import json
import logging
from typing import Any, AsyncIterator

import websockets

from ..config import settings

logger = logging.getLogger("callharness.testcall.realtime")

REALTIME_URL = "wss://api.openai.com/v1/realtime"

# Event names moved when the Realtime API went GA. Accepting both spellings costs two
# tuple entries and means a model pinned to an older snapshot still produces a
# transcript instead of a silent, apparently-successful, empty run.
AUDIO_DELTA_EVENTS = ("response.output_audio.delta", "response.audio.delta")
TESTER_TRANSCRIPT_EVENTS = (
    "response.output_audio_transcript.done",
    "response.audio_transcript.done",
)
AGENT_TRANSCRIPT_EVENTS = ("conversation.item.input_audio_transcription.completed",)

# The one tool the caller is given: hanging up.
END_CALL_TOOL = "end_call"


class RealtimeError(RuntimeError):
    pass


class RealtimeSession:
    """One conversation. Open it, configure it, then relay."""

    def __init__(self, instructions: str, voice: str | None = None):
        self.instructions = instructions
        self.voice = voice or settings.testcall_realtime_voice
        self._ws: Any = None

    async def connect(self) -> None:
        if not settings.openai_api_key:
            raise RealtimeError("OPENAI_API_KEY is not set.")
        url = f"{REALTIME_URL}?model={settings.testcall_realtime_model}"
        headers = {"Authorization": f"Bearer {settings.openai_api_key}"}
        # websockets renamed this keyword in v14. Supporting both keeps the SDK's
        # dependency range wide rather than pinning the whole server to one release.
        try:
            self._ws = await websockets.connect(url, additional_headers=headers)
        except TypeError:
            self._ws = await websockets.connect(url, extra_headers=headers)  # type: ignore[call-arg]
        await self._configure()

    async def _configure(self) -> None:
        await self._send(
            {
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "instructions": self.instructions,
                    "audio": {
                        "input": {
                            "format": {"type": "audio/pcmu"},
                            # Server-side voice activity detection: the model decides
                            # when the agent has stopped talking and answers on its own.
                            # Without it nothing would ever prompt a reply, because
                            # there is no user pressing anything on this end.
                            "turn_detection": {"type": "server_vad"},
                            # Transcribing the agent's side is the only way the run has
                            # a record of what it heard when the production agent fails
                            # to post its own call row — which is itself a test result.
                            "transcription": {"model": "whisper-1"},
                        },
                        "output": {
                            "format": {"type": "audio/pcmu"},
                            "voice": self.voice,
                        },
                    },
                    # The caller needs a way to put the phone down. Without it neither
                    # side ever ends the call: observed on the first live run, where the
                    # agent and our caller exchanged "arrivederci" six times and the
                    # call only stopped at the duration cap, three minutes in. Both
                    # sides are polite machines, so somebody has to hang up on purpose.
                    "tools": [
                        {
                            "type": "function",
                            "name": END_CALL_TOOL,
                            "description": (
                                "Hang up. Call this immediately after you have said "
                                "goodbye, once you have the information you called for "
                                "or it is clear you will not get it. Do not keep "
                                "exchanging pleasantries."
                            ),
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "reason": {
                                        "type": "string",
                                        "description": "Briefly, why the call is over.",
                                    }
                                },
                                "required": [],
                            },
                        }
                    ],
                    "tool_choice": "auto",
                },
            }
        )

    async def send_audio(self, payload_b64: str) -> None:
        """Forward one Twilio media frame, still base64, still µ-law."""
        await self._send({"type": "input_audio_buffer.append", "audio": payload_b64})

    async def cancel_response(self) -> None:
        """Stop talking. Sent when the agent starts speaking over our caller.

        A real caller stops mid-word when interrupted; a model that keeps going talks
        across the agent's answer and the transcript becomes two monologues.
        """
        await self._send({"type": "response.cancel"})

    async def events(self) -> AsyncIterator[dict]:
        if self._ws is None:
            raise RealtimeError("Session is not connected.")
        async for raw in self._ws:
            try:
                yield json.loads(raw)
            except (TypeError, ValueError):
                logger.debug("Unparseable realtime frame ignored")

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None

    async def _send(self, payload: dict) -> None:
        if self._ws is None:
            raise RealtimeError("Session is not connected.")
        await self._ws.send(json.dumps(payload))


def audio_delta(event: dict) -> str | None:
    """The base64 µ-law chunk in an output-audio event, if this is one."""
    if event.get("type") in AUDIO_DELTA_EVENTS:
        delta = event.get("delta")
        if isinstance(delta, str) and delta:
            return delta
    return None


def wants_to_hang_up(event: dict) -> bool:
    """Did the caller invoke the end_call tool?

    Two event shapes are accepted because the Realtime API reports a finished function
    call both as its own event and inside a completed output item, and which one a
    given model snapshot emits is not worth betting a live phone call on.
    """
    kind = event.get("type")
    if kind in ("response.function_call_arguments.done", "response.function_call_arguments.delta"):
        return event.get("name") == END_CALL_TOOL
    if kind == "response.output_item.done":
        item = event.get("item") or {}
        return item.get("type") == "function_call" and item.get("name") == END_CALL_TOOL
    return False


def transcript_line(event: dict) -> tuple[str, str] | None:
    """``(speaker, text)`` for the events that complete a spoken turn.

    Only the ``.done``/``.completed`` events are read, never the deltas: a transcript
    assembled from deltas duplicates text whenever a turn is cancelled mid-sentence,
    which is exactly what the barge-in handling above causes.
    """
    kind = event.get("type")
    if kind in TESTER_TRANSCRIPT_EVENTS:
        text = (event.get("transcript") or "").strip()
        return ("tester", text) if text else None
    if kind in AGENT_TRANSCRIPT_EVENTS:
        text = (event.get("transcript") or "").strip()
        return ("agent", text) if text else None
    return None

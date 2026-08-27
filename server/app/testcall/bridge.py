"""Relaying audio between Twilio and the Realtime model, for the length of one call.

Deliberately free of database access. It is handed everything it needs, returns what
happened, and the caller decides what that means — which keeps the one piece with
hard real-time behaviour (two sockets, 50 frames a second each way) out of the
transaction lifecycle.

Twilio's side of the protocol is four message types: ``connected``, ``start`` (which
carries the ``streamSid`` every outbound frame must quote), ``media`` (a base64
µ-law payload), and ``stop``. Ours adds ``clear``, which discards audio Twilio has
buffered but not yet played — the only way to make an interruption sound immediate.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

from . import realtime as rt
from .realtime import RealtimeSession

logger = logging.getLogger("callharness.testcall.bridge")

# Phrases that mean the agent is about to put a human on the line. A synthetic caller
# reaching an operator is not a test result, it is a person being taken away from real
# callers — so the run ends here and says why.
#
# Italian, because that is what these agents speak. ("la metto in contatto" = "I'll put
# you in touch"; "le passo" = "I'll pass you to"; "attenda in linea" = "hold the line".)
TRANSFER_MARKERS = (
    "la metto in contatto",
    "la trasferisco",
    "le passo",
    "passo la chiamata",
    "un operatore",
    "un nostro operatore",
    "attenda in linea",
    "resti in linea",
)


@dataclass
class BridgeResult:
    """What the call turned into. No judgement — that is the judge's job."""

    transcript: list[dict] = field(default_factory=list)
    answered: bool = False
    ended_on_transfer: bool = False
    hit_duration_cap: bool = False
    error: str | None = None
    stream_sid: str | None = None


async def run_bridge(
    websocket,
    instructions: str,
    max_duration_seconds: int,
    voice: str | None = None,
) -> BridgeResult:
    """Talk to whoever answered, until somebody hangs up or the cap is reached."""
    result = BridgeResult()
    session = RealtimeSession(instructions=instructions, voice=voice)
    try:
        await session.connect()
    except Exception as exc:  # noqa: BLE001 - a dead model must not look like a dead agent
        result.error = f"Realtime session failed to open: {exc}"
        return result

    deadline = time.monotonic() + max_duration_seconds
    stop = asyncio.Event()

    async def pump_twilio_to_model() -> None:
        """Caller audio in, plus the stream lifecycle."""
        while not stop.is_set():
            try:
                raw = await websocket.receive_text()
            except Exception:  # noqa: BLE001 - normal: the socket closed
                break
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            event = message.get("event")
            if event == "start":
                result.stream_sid = message.get("streamSid") or (
                    message.get("start") or {}
                ).get("streamSid")
                result.answered = True
            elif event == "media":
                payload = (message.get("media") or {}).get("payload")
                if payload:
                    try:
                        await session.send_audio(payload)
                    except Exception as exc:  # noqa: BLE001
                        result.error = f"Realtime send failed: {exc}"
                        break
            elif event == "stop":
                break
        stop.set()

    async def pump_model_to_twilio() -> None:
        """Our caller's voice out, transcripts collected on the way past."""
        try:
            async for event in session.events():
                if stop.is_set():
                    break
                kind = event.get("type")

                delta = rt.audio_delta(event)
                if delta and result.stream_sid:
                    await websocket.send_text(
                        json.dumps(
                            {
                                "event": "media",
                                "streamSid": result.stream_sid,
                                "media": {"payload": delta},
                            }
                        )
                    )
                    continue

                if kind == "input_audio_buffer.speech_started":
                    # The agent started talking while we were. Drop what Twilio has
                    # queued and stop generating, or the two of them talk over each
                    # other for as long as the buffered audio lasts.
                    if result.stream_sid:
                        await websocket.send_text(
                            json.dumps({"event": "clear", "streamSid": result.stream_sid})
                        )
                    await session.cancel_response()
                    continue

                line = rt.transcript_line(event)
                if line:
                    speaker, text = line
                    result.transcript.append({"speaker": speaker, "text": text})
                    if speaker == "agent" and _is_transfer(text):
                        logger.info("Test call ending early: agent announced a transfer")
                        result.ended_on_transfer = True
                        stop.set()
                        break
                    continue

                if kind == "error":
                    detail = (event.get("error") or {}).get("message") or str(event.get("error"))
                    result.error = f"Realtime error: {detail}"
                    logger.warning("Realtime error during test call: %s", detail)
        except Exception as exc:  # noqa: BLE001
            if not stop.is_set():
                result.error = f"Realtime stream ended: {exc}"
        stop.set()

    async def enforce_deadline() -> None:
        """The spend limit. Every second past this costs both sides money."""
        while not stop.is_set():
            if time.monotonic() >= deadline:
                result.hit_duration_cap = True
                stop.set()
                return
            try:
                await asyncio.wait_for(stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    tasks = [
        asyncio.create_task(pump_twilio_to_model()),
        asyncio.create_task(pump_model_to_twilio()),
        asyncio.create_task(enforce_deadline()),
    ]
    try:
        await stop.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await session.close()
    return result


def _is_transfer(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in TRANSFER_MARKERS)

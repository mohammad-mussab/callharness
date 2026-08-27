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
import hmac
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
# How long to wait for Twilio's `start` after the socket opens. It normally arrives
# immediately; a socket that connects and then says nothing must not hold a paid call
# open, and on a real failure this is what turns a hang into a reported error.
START_TIMEOUT_SECONDS = 15.0

# Said by OUR caller, not the agent: the point at which the conversation is over and
# only politeness remains. Deliberately narrow — "grazie" alone appears mid-call all
# the time ("thanks, I'll wait"), and cutting a call off there would lose the answer.
# ("arrivederci" = goodbye; "buona giornata" = have a good day; "a presto" = see you soon.)
FAREWELL_MARKERS = (
    "arrivederci",
    "buona giornata",
    "buona serata",
    "a presto",
    "goodbye",
)

# How long the agent gets to say its own goodbye before we hang up anyway. Long enough
# for one closing sentence, short enough that the loop cannot restart.
FAREWELL_GRACE_SECONDS = 6.0

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
    # The caller hung up deliberately, which is what a finished call should look like.
    # Distinguished from the duration cap because the cap means nobody ever stopped.
    caller_hung_up: bool = False
    error: str | None = None
    stream_sid: str | None = None


async def run_bridge(
    websocket,
    instructions: str,
    max_duration_seconds: int,
    expected_token: str,
    voice: str | None = None,
) -> BridgeResult:
    """Talk to whoever answered, until somebody hangs up or the cap is reached.

    Begins by waiting for Twilio's ``start`` message, because that is the only place
    the shared token can arrive — the URL cannot carry it (see ``build_twiml``). The
    Realtime session is opened *after* that check, so an unauthorised socket never
    costs an API session, and neither does a call Twilio abandons before streaming.
    """
    result = BridgeResult()

    if not await _await_authorized_start(websocket, expected_token, result):
        return result

    session = RealtimeSession(instructions=instructions, voice=voice)
    try:
        await session.connect()
    except Exception as exc:  # noqa: BLE001 - a dead model must not look like a dead agent
        result.error = f"Realtime session failed to open: {exc}"
        return result

    deadline = time.monotonic() + max_duration_seconds
    stop = asyncio.Event()
    # Set once our caller says goodbye; a one-element list because the pumps are
    # closures and this has to be writable from one and readable from another.
    farewell_deadline: list[float | None] = [None]

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
            if event == "media":
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
        # Whether the model is mid-answer. Cancelling when it is not draws an error
        # event back ("Cancellation failed: no active response found") which would be
        # stored on the run and make a perfectly good call look broken. Observed on
        # every local run before this flag existed.
        responding = False
        try:
            async for event in session.events():
                if stop.is_set():
                    break
                kind = event.get("type")

                if kind == "response.created":
                    responding = True
                elif kind in ("response.done", "response.cancelled"):
                    responding = False

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
                    # The agent started talking. Drop what Twilio has queued, or the two
                    # of them talk over each other for as long as the buffered audio
                    # lasts. Only cancel a response that is actually running.
                    if result.stream_sid:
                        await websocket.send_text(
                            json.dumps({"event": "clear", "streamSid": result.stream_sid})
                        )
                    if responding:
                        await session.cancel_response()
                        responding = False
                    continue

                if rt.wants_to_hang_up(event):
                    logger.info("Test call ending: the caller used end_call")
                    result.caller_hung_up = True
                    stop.set()
                    break

                line = rt.transcript_line(event)
                if line:
                    speaker, text = line
                    result.transcript.append({"speaker": speaker, "text": text})
                    if speaker == "agent" and _is_transfer(text):
                        logger.info("Test call ending early: agent announced a transfer")
                        result.ended_on_transfer = True
                        stop.set()
                        break
                    if speaker == "tester" and _is_farewell(text):
                        # Backstop for when the model says goodbye but does not call
                        # end_call. Two polite machines will otherwise thank each other
                        # until the duration cap — six exchanges of "arrivederci" on the
                        # first live call. The agent is allowed one closing reply, then
                        # the line goes down.
                        farewell_deadline[0] = time.monotonic() + FAREWELL_GRACE_SECONDS
                    continue

                if kind == "error":
                    detail = (event.get("error") or {}).get("message") or str(event.get("error"))
                    if "cancellation failed" in detail.lower():
                        # The response ended between our decision to cancel and the
                        # cancel arriving. Nothing went wrong, and recording it would
                        # put a scary line on a call that went fine.
                        logger.debug("Ignoring benign cancel race: %s", detail)
                        continue
                    result.error = f"Realtime error: {detail}"
                    logger.warning("Realtime error during test call: %s", detail)
        except Exception as exc:  # noqa: BLE001
            if not stop.is_set():
                result.error = f"Realtime stream ended: {exc}"
        stop.set()

    async def enforce_deadline() -> None:
        """The spend limit, and the goodbye backstop."""
        while not stop.is_set():
            now = time.monotonic()
            if now >= deadline:
                result.hit_duration_cap = True
                stop.set()
                return
            due = farewell_deadline[0]
            if due is not None and now >= due:
                logger.info("Test call ending: goodbye said and nobody hung up")
                result.caller_hung_up = True
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


async def _await_authorized_start(websocket, expected_token: str, result: BridgeResult) -> bool:
    """Read frames until Twilio's ``start``, and check the token it carries.

    Twilio sends ``connected`` first and may send nothing else for a moment, so this
    reads past anything that is not ``start`` rather than assuming an order. The wait
    is bounded: a socket that connects and then says nothing must not hold a call open.
    """
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            result.error = "Twilio opened the audio socket but never sent a start message."
            return False
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=remaining)
        except asyncio.TimeoutError:
            result.error = "Twilio opened the audio socket but never sent a start message."
            return False
        except Exception:  # noqa: BLE001 - the socket closed before streaming began
            result.error = "Twilio closed the audio socket before the call started."
            return False
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        if message.get("event") != "start":
            continue

        start = message.get("start") or {}
        token = str((start.get("customParameters") or {}).get("token") or "")
        if not (token and hmac.compare_digest(token, expected_token)):
            # Deliberately specific: the token going missing is exactly what happens
            # when it is put in the stream URL, which Twilio silently drops.
            result.error = (
                "The audio socket did not carry the expected token "
                f"({'wrong value' if token else 'no token at all'}); connection refused."
            )
            logger.warning("Refused a test call stream: %s", result.error)
            return False

        result.stream_sid = message.get("streamSid") or start.get("streamSid")
        result.answered = True
        return True


def _is_transfer(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in TRANSFER_MARKERS)


def _is_farewell(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in FAREWELL_MARKERS)

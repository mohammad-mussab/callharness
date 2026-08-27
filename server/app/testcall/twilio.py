"""Placing the call, and pressing the keypad on the way in.

Twilio's REST API over plain httpx rather than their SDK — this is two form-encoded
POSTs and a dependency that ships its own HTTP stack would be the larger change.
"""

from __future__ import annotations

import logging
from xml.sax.saxutils import escape

import httpx

from ..config import settings

logger = logging.getLogger("callharness.testcall.twilio")

API_ROOT = "https://api.twilio.com/2010-04-01"


class TwilioError(RuntimeError):
    """Twilio refused the request. The message carries their own wording."""


def build_twiml(
    stream_url: str, token: str, digits: str | None, pause_seconds: float
) -> str:
    """The instructions Twilio runs on the called leg once somebody answers.

    Two things here are not free choices.

    **The keypad presses must come BEFORE ``<Connect><Stream>``.** A bidirectional
    media stream carries keypad presses inward only — Twilio will not send a digit
    on behalf of our media server once the stream is running. So the menu is
    navigated blind, on a timer, and only then is the audio handed to us.

    **The token goes in a ``<Parameter>``, never in the URL.** Twilio's docs are
    explicit that the ``url`` attribute *does not support query string parameters*,
    and it does not fail loudly: it silently opens the socket without them. That
    cost a live call — our own authorization refused the tokenless connection with a
    403 and Twilio hung up nine seconds in. ``<Parameter>`` values arrive in the
    ``start`` message instead, which is why the token can only be checked after the
    socket is accepted.

    ``<Connect>`` (not ``<Start>``) is deliberate: it is the two-way form, and it
    blocks until the socket closes, which is what keeps the call up while the
    caller talks.
    """
    parts: list[str] = []
    for digit in _digit_list(digits):
        # A pause before each press, not after: the menu is already talking when the
        # call connects, and a digit sent into the greeting is simply discarded.
        parts.append(f'<Pause length="{max(1, int(round(pause_seconds)))}"/>')
        parts.append(f'<Play digits="{escape(digit)}"/>')
    url = escape(stream_url, {chr(34): "&quot;"})
    tok = escape(token, {chr(34): "&quot;"})
    parts.append(
        f'<Connect><Stream url="{url}">'
        f'<Parameter name="token" value="{tok}"/>'
        f"</Stream></Connect>"
    )
    return "<Response>" + "".join(parts) + "</Response>"


def _digit_list(digits: str | None) -> list[str]:
    """"2,2" and "22" both mean press 2 then press 2.

    Accepting both matters because the natural way to write it is whichever way you
    happen to think of the menu, and a scenario that silently pressed "22" as one
    tone would fail in a way nobody would look for.
    """
    if not digits:
        return []
    raw = digits.replace(",", " ").replace("-", " ").split()
    if len(raw) == 1 and len(raw[0]) > 1:
        return list(raw[0])
    return raw


async def place_call(to_number: str, twiml: str) -> str:
    """Dial, and return Twilio's call sid.

    ``Twiml`` is sent inline rather than as a URL Twilio fetches, so the only thing
    that has to be publicly reachable is the audio socket — no webhook endpoint, no
    second door into the VM.
    """
    if not (settings.twilio_account_sid and settings.twilio_auth_token):
        raise TwilioError("Twilio credentials are not configured.")
    if not settings.twilio_from_number:
        raise TwilioError("CALLHARNESS_TWILIO_FROM_NUMBER is not set.")

    url = f"{API_ROOT}/Accounts/{settings.twilio_account_sid}/Calls.json"
    data = {
        "To": to_number,
        "From": settings.twilio_from_number,
        "Twiml": twiml,
        # Give up if nobody picks up. The default is 60s of ringing, which on a
        # call-centre number means a minute of queue music billed as a test.
        "Timeout": "25",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            url, data=data, auth=(settings.twilio_account_sid, settings.twilio_auth_token)
        )
    if resp.status_code >= 300:
        raise TwilioError(_message(resp))
    return str(resp.json().get("sid") or "")


async def hangup(call_sid: str) -> None:
    """End the call from our side. Best effort — never raises.

    Called from the duration cap and the transfer guardrail, both of which are
    already handling an abnormal situation; a failure here must not mask it. The
    call also ends on its own when the websocket closes, so this is belt and braces.
    """
    if not (call_sid and settings.twilio_account_sid and settings.twilio_auth_token):
        return
    url = f"{API_ROOT}/Accounts/{settings.twilio_account_sid}/Calls/{call_sid}.json"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                url,
                data={"Status": "completed"},
                auth=(settings.twilio_account_sid, settings.twilio_auth_token),
            )
    except Exception as exc:  # noqa: BLE001 - hanging up is not worth an exception
        logger.warning("Twilio hangup for %s failed: %s", call_sid, exc)


async def call_status(call_sid: str) -> str | None:
    """Twilio's own word on what happened to a call. Best effort, never raises.

    Used by the dial watchdog to say *why* a run is being closed: "no-answer" is a fact
    about the number, while "completed" with no audio ever streamed points at the
    websocket instead. Guessing between those two wastes a debugging session.
    """
    if not (call_sid and settings.twilio_account_sid and settings.twilio_auth_token):
        return None
    url = f"{API_ROOT}/Accounts/{settings.twilio_account_sid}/Calls/{call_sid}.json"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                url, auth=(settings.twilio_account_sid, settings.twilio_auth_token)
            )
        if resp.status_code >= 300:
            return None
        return str(resp.json().get("status") or "") or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Twilio status lookup for %s failed: %s", call_sid, exc)
        return None


def _message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        detail = body.get("message") or body.get("detail") or ""
        code = body.get("code")
        return f"Twilio {resp.status_code}: {detail}" + (f" (code {code})" if code else "")
    except Exception:  # noqa: BLE001
        return f"Twilio {resp.status_code}: {resp.text[:300]}"

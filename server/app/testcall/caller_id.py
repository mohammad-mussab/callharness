"""Working out what our own caller ID looks like once the agent has hashed it.

The agents never send a phone number. They send
``HMAC-SHA256(PHONE_HASH_KEY, normalized E.164)`` as ``from_number``, so a test run
cannot recognise its own call by comparing numbers — but it can compare hashes, as
long as it hashes the number exactly the way they do.

This is a deliberate copy of the agents' ``utils/phone_hash.py``, not an import: they
are separate deployments on separate machines. **If their normalisation changes, this
must change with it**, and the symptom would be silent — every run reporting that the
agent never posted its call.

Why it matters more than it looks: without it, matching falls back to region and time,
and on 9 Sep 2026 that claimed a real patient's call. Our test never reached the agent
(it was stuck in the phone menu, so no row existed to match), and the nearest call in
the window was a genuine caller asking for an operator forty seconds later. It was
matched, stamped as a test call, and scheduled for deletion.
"""

from __future__ import annotations

import hashlib
import hmac
import re

_PLACEHOLDERS = {"", "N/A", "n/a", "null", "NULL", "None"}
_E164_RE = re.compile(r"^\+?\d+$")
_SEPARATORS_RE = re.compile(r"[\s\-().]")


def normalize_phone(raw: str | None) -> str | None:
    """"+39 333 123 4567", "00393331234567" and "3331234567" all become "+393331234567"."""
    if raw is None:
        return None
    s = _SEPARATORS_RE.sub("", str(raw).strip())
    if not s or s in _PLACEHOLDERS:
        return None
    if s.startswith("00"):
        s = "+" + s[2:]
    if not _E164_RE.match(s):
        return None
    if not s.startswith("+"):
        # A bare national number is assumed Italian, as on their side.
        if len(s) < 6:
            return None
        s = "+39" + s
    digits = s[1:]
    if len(digits) < 8 or len(digits) > 15:
        return None
    return "+" + digits


def phone_hash(raw: str | None, key: str | None) -> str | None:
    """The value the agent will put in ``from_number`` for this number, or None.

    None whenever it cannot be computed — no key, or a number that is not a number —
    and the caller must treat that as "cannot identify our call", never as a licence to
    match on something weaker.
    """
    normalized = normalize_phone(raw)
    if normalized is None or not key:
        return None
    return hmac.new(key.encode("utf-8"), normalized.encode("utf-8"), hashlib.sha256).hexdigest()

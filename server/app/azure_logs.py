"""Locate and fetch each call's raw agent log from Azure Blob Storage.

The region agents write a full DEBUG-level loguru log per call and upload it at
teardown to::

    {region-prefix}call-logs/{YYYY-MM-DD}/{YYYYMMDD}_{HHMMSS}_{uuid8}_{phone}.log

That log is the only place the whole story of a call exists — the verbatim telephony
start event, every pipecat frame, every tool call's request and response body,
tracebacks. This module is the entire Azure surface of CallHarness; nothing else
imports the SDK.

WHY THE MATCHING IS A PREFIX MATCH, NOT A LOOKUP
The agent generates one uuid4 per call and uses it for everything: CallHarness's
``external_id`` is the full uuid, but the blob filename carries only ``session_id[:8]``.
The full path *is* computed agent-side, but inside ``_upload_log_async()``, where it is
used once and discarded — never written to the agent's own database, never set as blob
metadata, never sent here. So the agent cannot tell us where the log is, and CallHarness
has to find it. The join is ``external_id[:8] == blob_name.split("_")[2]``, disambiguated
by the day folder and the ``HHMMSS`` in the filename.

The timestamp tiebreak is not decoration. Piemonte's test harness pins one session id,
so its ``uuid8`` repeats across hundreds of blobs and the prefix alone is ambiguous
there. Everywhere else it is a safety net against an 8-hex-char collision.

Both timestamps come from ``datetime.now()`` in the same process on the same VM (the
filename is stamped at ``start_call_logging``, ``started_at`` a beat later in
``start_call()``), and the agent sends that naive value straight through, so the two are
directly comparable with no timezone handling — the measured gap is ~230ms.

TWO THINGS THAT LOOK LIKE BUGS AND AREN'T
- **Midnight.** The filename timestamp is call *start*, but the day folder comes from
  the clock at *upload* time, i.e. call end. A call starting 23:58 and ending 00:01 is
  named ``20260814_2358...`` and sits in ``2026-08-15/``. Hence _NEXT_DAY_FALLBACK.
- **Calls with no log at all.** The upload is one attempt with no retry, a sweeper in
  the agent deletes un-uploaded leftovers after 7 days rather than retrying them, and a
  crashed process never flushes loguru's queue. Those calls will never have a blob, so
  ``resolve()`` stamps ``log_checked_at`` on a miss too and callers bound how far back
  they keep looking. Without that the reconciler would re-scan Azure for them forever.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .models import Call, utcnow

logger = logging.getLogger("callharness.azure_logs")

# {YYYYMMDD}_{HHMMSS}_{uuid8}_{phone-or-label}.log — the trailing segment is absent
# entirely when the agent had no caller id, so it is optional here.
_BLOB_NAME_RE = re.compile(
    r"^(?P<stamp>\d{8}_\d{6})_(?P<uuid8>[0-9a-f]{8})(?:_(?P<label>.*))?\.log$",
    re.IGNORECASE,
)

# How far the filename stamp may sit from Call.started_at and still be the same call.
# Measured delta on production calls is ~230ms; a minute is generous enough to absorb
# a slow pipeline setup without ever reaching a neighbouring call.
_MATCH_TOLERANCE = timedelta(seconds=60)

# Also look in the following day's folder when the day of the call comes up empty.
# See the midnight note above.
_NEXT_DAY_FALLBACK = True

_client_cache: object | None = None
_client_resolved = False


class LogUnavailable(RuntimeError):
    """Azure would not serve the blob for a reason that isn't "it doesn't exist".

    Almost always a misconfigured connection string. Kept distinct from the
    blob-is-gone case so the route can answer 502-with-a-reason instead of a bare
    500 — the first time this feature is wired up in a new environment, a rejected
    credential is by far the likeliest thing to go wrong, and "Internal Server Error"
    tells whoever is setting it up nothing at all.
    """


@dataclass(frozen=True)
class ParsedLog:
    """One blob in a day folder, decomposed into the parts we can match on."""

    blob: str  # full blob name, ready to hand back to Azure
    started: datetime  # from the filename stamp — call start, not upload time
    uuid8: str
    label: str | None  # caller phone digits, or a harness label like "daily_test"


def enabled() -> bool:
    """True when a connection string is configured. Everything here no-ops without one."""
    return bool(settings.azure_storage_connection_string)


def _client():
    """Memoized BlobServiceClient, or None when Azure isn't configured.

    The import is deferred so that installs without the optional dependency — or
    without any Azure at all — still start cleanly.
    """
    global _client_cache, _client_resolved
    if _client_resolved:
        return _client_cache

    _client_resolved = True
    if not enabled():
        return None
    try:
        from azure.storage.blob import BlobServiceClient
    except ImportError:  # pragma: no cover - depends on the install
        logger.warning(
            "CALLHARNESS_AZURE_STORAGE_CONNECTION_STRING is set but azure-storage-blob "
            "is not installed; call logs will not be linked"
        )
        return None
    try:
        _client_cache = BlobServiceClient.from_connection_string(
            settings.azure_storage_connection_string
        )
    except Exception as exc:  # noqa: BLE001 - a bad connection string must not crash boot
        logger.error("Could not build the Azure blob client: %s", exc)
    return _client_cache


def prefix_for(agent_id: str) -> str:
    """Blob prefix for an agent. Unlisted agents get "<agent_id lowercased>/".

    Note the fallback cannot cover Lombardia, whose logs live at the container root —
    an empty prefix has to be an explicit entry in the map, which it is by default.
    """
    prefixes = settings.azure_log_prefixes
    if agent_id in prefixes:
        return prefixes[agent_id]
    return f"{agent_id.lower()}/"


def parse_blob_name(blob: str) -> ParsedLog | None:
    """Decompose a blob name. None for anything that isn't a call log.

    Returning None rather than raising means a stray file dropped into a day folder
    is skipped instead of taking a whole sweep down with it.
    """
    match = _BLOB_NAME_RE.match(blob.rsplit("/", 1)[-1])
    if not match:
        return None
    try:
        started = datetime.strptime(match.group("stamp"), "%Y%m%d_%H%M%S")
    except ValueError:
        return None
    return ParsedLog(
        blob=blob,
        started=started,
        uuid8=match.group("uuid8").lower(),
        label=match.group("label") or None,
    )


def _list_day_blocking(prefix: str, day: date) -> dict[str, list[ParsedLog]]:
    client = _client()
    if client is None:
        return {}
    from azure.core.exceptions import ClientAuthenticationError

    container = client.get_container_client(settings.azure_log_container)
    folder = f"{prefix}call-logs/{day:%Y-%m-%d}/"
    index: dict[str, list[ParsedLog]] = {}
    try:
        for blob in container.list_blobs(name_starts_with=folder):
            parsed = parse_blob_name(blob.name)
            if parsed is not None:
                index.setdefault(parsed.uuid8, []).append(parsed)
    except ClientAuthenticationError as exc:
        # Distinguished from an empty folder deliberately: silently reporting
        # "no logs found" for what is actually a bad credential would send someone
        # hunting through the agent's upload code for a problem that is in .env.
        raise LogUnavailable(
            "Azure rejected the storage credentials — check "
            "CALLHARNESS_AZURE_STORAGE_CONNECTION_STRING"
        ) from exc
    return index


async def list_day(prefix: str, day: date) -> dict[str, list[ParsedLog]]:
    """Every call log in one day folder, indexed by uuid8.

    One listing serves every call in that folder, which is what keeps a backfill of
    hundreds of calls down to a handful of Azure round trips.
    """
    return await asyncio.to_thread(_list_day_blocking, prefix, day)


def _fetch_blocking(blob: str, head_bytes: int | None = None) -> bytes | None:
    client = _client()
    if client is None:
        return None
    from azure.core.exceptions import ClientAuthenticationError, ResourceNotFoundError

    blob_client = client.get_blob_client(settings.azure_log_container, blob)
    try:
        if head_bytes:
            return blob_client.download_blob(offset=0, length=head_bytes).readall()
        return blob_client.download_blob().readall()
    except ResourceNotFoundError:
        return None
    except ClientAuthenticationError as exc:
        logger.error("Azure rejected our credentials reading %s: %s", blob, exc)
        raise LogUnavailable(
            "Azure rejected the storage credentials — check "
            "CALLHARNESS_AZURE_STORAGE_CONNECTION_STRING"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - network, throttling, container typo, ...
        logger.error("Could not read %s from Azure: %s", blob, exc)
        raise LogUnavailable("Could not read the log from Azure storage") from exc


async def fetch_log(blob: str, head_bytes: int | None = None) -> bytes | None:
    """The raw log bytes, or None if the blob is gone.

    None is reserved for "the log genuinely isn't there" — a pruned blob is a normal
    outcome, not an error. Anything else (bad credentials, unreachable storage) raises
    LogUnavailable, so a configuration problem cannot masquerade as a missing log.

    head_bytes fetches only a prefix of the blob. The identifying lines (session id,
    call start) are all in the first few KB, so tooling that only needs those can skip
    downloading ~200KB per call.
    """
    return await asyncio.to_thread(_fetch_blocking, blob, head_bytes)


def _best_match(call: Call, candidates: Iterable[ParsedLog]) -> ParsedLog | None:
    """The candidate whose filename stamp sits nearest started_at, within tolerance."""
    best: ParsedLog | None = None
    best_delta: timedelta | None = None
    for candidate in candidates:
        delta = abs(candidate.started - call.started_at)
        if delta > _MATCH_TOLERANCE:
            continue
        if best_delta is None or delta < best_delta:
            best, best_delta = candidate, delta
    return best


async def resolve(session: AsyncSession, calls: Sequence[Call], *, commit: bool = True) -> int:
    """Point each call at its log blob. Returns how many were newly matched.

    Calls are grouped by (prefix, day) so each folder is listed once no matter how many
    calls fall in it. `log_checked_at` is stamped on every call considered — a miss is
    recorded just as firmly as a hit, so unmatched calls stop being re-scanned.

    Pass commit=False to have the caller own the transaction (the sync script's
    --dry-run does this, then rolls back).
    """
    if not calls or not enabled():
        return 0

    # (prefix, day) -> the calls to try against that folder.
    groups: dict[tuple[str, date], list[Call]] = {}
    for call in calls:
        if not call.external_id or call.started_at is None:
            continue
        groups.setdefault((prefix_for(call.agent_id), call.started_at.date()), []).append(call)

    matched = 0
    for (prefix, day), group in groups.items():
        try:
            index = await list_day(prefix, day)
        except LogUnavailable:
            # A credential problem affects every folder, so grinding through the rest
            # would just produce a wall of identical warnings. Let it out.
            raise
        except Exception as exc:  # noqa: BLE001 - one bad folder must not sink the sweep
            logger.warning("Listing %scall-logs/%s/ failed: %s", prefix, day, exc)
            continue

        unmatched: list[Call] = []
        for call in group:
            found = _best_match(call, index.get(call.external_id[:8].lower(), []))
            if found is None:
                unmatched.append(call)
                continue
            call.log_blob = found.blob
            call.log_checked_at = utcnow()
            matched += 1

        # A call that started late in the day may have finished after midnight, and the
        # folder is named for the *upload*, so its log is filed under the next day.
        if unmatched and _NEXT_DAY_FALLBACK:
            try:
                next_index = await list_day(prefix, day + timedelta(days=1))
            except LogUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Listing %scall-logs/%s/ failed: %s", prefix, day + timedelta(days=1), exc
                )
                next_index = {}
            for call in unmatched:
                found = _best_match(call, next_index.get(call.external_id[:8].lower(), []))
                if found is not None:
                    call.log_blob = found.blob
                    matched += 1
                call.log_checked_at = utcnow()
        else:
            for call in unmatched:
                call.log_checked_at = utcnow()

    if commit:
        await session.commit()
    return matched

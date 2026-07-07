"""Pipecat integration for OpenCall.

Usage — add ~6 lines to an existing Pipecat bot:

    from pipecat.processors.transcript_processor import TranscriptProcessor
    from opencall_sdk import AsyncOpenCallClient
    from opencall_sdk.pipecat import PipecatCallRecorder

    transcript = TranscriptProcessor()          # put transcript.user() after STT
                                                # and transcript.assistant() after TTS
    client = AsyncOpenCallClient("http://localhost:8010")
    recorder = PipecatCallRecorder(client, agent_id="my-agent")
    recorder.attach(transcript)

    # when the session ends (e.g. on_client_disconnected or after task.run()):
    await recorder.flush(end_reason="completed")

The recorder listens to TranscriptProcessor's ``on_transcript_update`` event,
so it works with any Pipecat pipeline that includes the transcript processors —
no changes to your STT/LLM/TTS services required.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from .client import AsyncOpenCallClient
from .recorder import CallRecorder

logger = logging.getLogger("opencall.sdk.pipecat")


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class PipecatCallRecorder(CallRecorder):
    """CallRecorder that wires itself into a Pipecat TranscriptProcessor."""

    def attach(self, transcript_processor: Any) -> "PipecatCallRecorder":
        """Register on a pipecat TranscriptProcessor (or a single user/assistant
        transcript processor instance that supports event handlers)."""

        @transcript_processor.event_handler("on_transcript_update")
        async def _on_transcript_update(processor: Any, frame: Any) -> None:
            self.on_transcript_update(frame)

        return self

    def on_transcript_update(self, frame: Any) -> None:
        """Handle a pipecat TranscriptionUpdateFrame (has .messages)."""
        messages = getattr(frame, "messages", None) or []
        for message in messages:
            role = getattr(message, "role", None)
            text = getattr(message, "content", None)
            if not role or not text:
                continue
            start_time = None
            ts = _parse_timestamp(getattr(message, "timestamp", None))
            if ts:
                start_time = max(0.0, (ts - self.started_at).total_seconds())
            self.add_turn(role=role, text=text, start_time=start_time)


def create_recorder(
    base_url: str = "http://localhost:8010",
    api_key: str | None = None,
    agent_id: str = "default",
    **kwargs: Any,
) -> PipecatCallRecorder:
    """Convenience factory: builds the client and recorder in one call."""
    client = AsyncOpenCallClient(base_url=base_url, api_key=api_key)
    return PipecatCallRecorder(client, agent_id=agent_id, **kwargs)

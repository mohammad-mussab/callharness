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


try:  # Observers require pipecat to be installed
    import asyncio

    from pipecat.frames.frames import (
        BotStartedSpeakingFrame,
        BotStoppedSpeakingFrame,
        CancelFrame,
        EndFrame,
        ErrorFrame,
        FunctionCallResultFrame,
        MetricsFrame,
        TranscriptionFrame,
        TTSTextFrame,
        UserStartedSpeakingFrame,
        UserStoppedSpeakingFrame,
    )
    from pipecat.metrics.metrics import TTFBMetricsData
    from pipecat.observers.base_observer import BaseObserver, FramePushed

    class OpenCallMetricsObserver(BaseObserver):
        """Pipecat observer that captures per-service TTFB metrics and feeds
        them to the recorder, so assistant turns get STT/LLM/TTS latency.

        Usage:
            task = PipelineTask(
                pipeline,
                params=PipelineParams(enable_metrics=True),
                observers=[OpenCallMetricsObserver(recorder)],
            )
        """

        def __init__(self, recorder: PipecatCallRecorder, **kwargs: Any):
            super().__init__(**kwargs)
            self._recorder = recorder

        async def on_push_frame(self, data: "FramePushed") -> None:
            frame = getattr(data, "frame", None)
            if not isinstance(frame, MetricsFrame):
                return
            for metric in getattr(frame, "data", None) or []:
                if not isinstance(metric, TTFBMetricsData):
                    continue
                processor = (getattr(metric, "processor", "") or "").lower()
                value = getattr(metric, "value", None)
                if value is None:
                    continue
                ms = float(value) * 1000
                if "stt" in processor:
                    self._recorder.record_component_latency("stt", ms)
                elif "llm" in processor:
                    self._recorder.record_component_latency("llm", ms)
                elif "tts" in processor:
                    self._recorder.record_component_latency("tts", ms)

    class OpenCallFrameObserver(BaseObserver):
        """All-in-one Pipecat observer for pipelines that don't use
        TranscriptProcessor. Captures everything OpenCall needs at the frame
        level:

        - user turns from TranscriptionFrame (at the STT service)
        - assistant turns from TTSTextFrame (at the TTS service)
        - end-to-end response latency (user stopped speaking -> bot audio started)
        - STT / LLM / TTS TTFB component latency from MetricsFrames
        - interruptions (user starts speaking while the bot is speaking)
        - transfers (a tool from transfer_tool_names returned a result)
        - every tool/function call the agent makes (name, arguments, result),
          attached to the assistant turn it led to
        - end_reason, derived from the pipeline's own shutdown signal instead of
          a string the integrator has to guess: "error" if a fatal ErrorFrame was
          seen, else the explicit `reason=` on an EndFrame/CancelFrame/EndTaskFrame/
          CancelTaskFrame if the bot set one, else "transferred" if a transfer
          fired, else "completed" (or "cancelled" for an unreasoned CancelFrame).

        Usage:
            recorder = create_recorder("http://localhost:8010", agent_id="my-agent")
            observer = OpenCallFrameObserver(
                recorder, stt=stt, tts=tts, transfer_tool_names={"transfer_to_human"}
            )
            # add `observer` to your PipelineTask/PipelineWorker observers, then
            # when the call ends, call finalize_end_reason() rather than reading
            # .end_reason directly — most integrations (e.g. a transport's
            # on_client_disconnected) learn the call ended *before* an
            # EndFrame/CancelFrame has actually propagated through the pipeline,
            # so .end_reason may still be None at that point:
            await recorder.flush(
                end_reason=observer.finalize_end_reason(),
                transferred=observer.transferred,
            )

        Pass your STT and TTS service instances so transcript frames are only
        captured at their origin (observers see every frame once per pipeline
        hop); without them, frames are de-duplicated by frame id instead.
        """

        def __init__(
            self,
            recorder: PipecatCallRecorder | Any,
            stt: Any = None,
            tts: Any = None,
            transfer_tool_names: set[str] | None = None,
            **kwargs: Any,
        ):
            super().__init__(**kwargs)
            self._recorder = recorder
            self._stt = stt
            self._tts = tts
            self._transfer_tools = set(transfer_tool_names or ())
            self.transferred = False
            self._seen_frame_ids: set[int] = set()
            self._user_stopped_at: float | None = None
            self._bot_speaking = False
            # latency values waiting for the next assistant turn to be created
            self._pending: dict[str, float] = {}
            # deterministic end-of-call reason (see class docstring)
            self.end_reason: str | None = None
            self.last_error: str | None = None
            self._had_fatal_error = False

        def _set_end_reason(self, explicit: str | None, is_cancel: bool) -> None:
            if self._had_fatal_error:
                self.end_reason = "error"
            elif explicit:
                self.end_reason = explicit
            elif self.transferred:
                self.end_reason = "transferred"
            else:
                self.end_reason = "cancelled" if is_cancel else "completed"

        def finalize_end_reason(self, default: str = "completed") -> str:
            """Best end_reason available *right now* — call this from your own
            disconnect/teardown handler instead of reading `.end_reason` directly.

            Many integrations (e.g. a transport's `on_client_disconnected`) learn
            the call has ended before an EndFrame/CancelFrame has propagated
            through the pipeline — `.end_reason` would still be None at that
            point. This returns the same priority (fatal error > an
            already-observed explicit reason > transferred > `default`) using
            only state that's tracked live during the call, so it's correct
            regardless of teardown ordering.
            """
            if self._had_fatal_error:
                return "error"
            if self.end_reason:
                return self.end_reason
            if self.transferred:
                return "transferred"
            return default

        def _is_origin(self, data: "FramePushed", service: Any) -> bool:
            if service is not None:
                return data.source is service
            frame_id = getattr(data.frame, "id", None)
            if frame_id is None:
                return True
            if frame_id in self._seen_frame_ids:
                return False
            self._seen_frame_ids.add(frame_id)
            if len(self._seen_frame_ids) > 10000:
                self._seen_frame_ids.clear()
            return True

        def _apply_pending(self, turn: dict[str, Any]) -> None:
            if not self._pending:
                return
            for key, field in (("stt", "stt_ms"), ("llm", "llm_ttft_ms"), ("tts", "tts_ttfb_ms")):
                if self._pending.get(key) is not None and turn.get(field) is None:
                    turn[field] = round(self._pending[key], 1)
            if self._pending.get("e2e") is not None:
                turn["latency_ms"] = round(self._pending["e2e"], 1)
            elif turn.get("latency_ms") is None:
                components = [turn.get(f) for f in ("stt_ms", "llm_ttft_ms", "tts_ttfb_ms")]
                components = [c for c in components if c is not None]
                if components:
                    turn["latency_ms"] = round(sum(components), 1)
            self._pending = {}

        def _last_assistant_turn(self) -> dict[str, Any] | None:
            turns = self._recorder.turns
            if turns and turns[-1]["role"] == "assistant":
                return turns[-1]
            return None

        async def on_push_frame(self, data: "FramePushed") -> None:
            frame = data.frame

            if isinstance(frame, TranscriptionFrame):
                if self._is_origin(data, self._stt) and frame.text:
                    self._recorder.add_turn("user", frame.text)
                return

            if isinstance(frame, TTSTextFrame):
                if self._is_origin(data, self._tts) and frame.text:
                    before = len(self._recorder.turns)
                    self._recorder.add_turn("assistant", frame.text)
                    if len(self._recorder.turns) > before:  # new turn, not a merged chunk
                        self._apply_pending(self._recorder.turns[-1])
                return

            if isinstance(frame, UserStoppedSpeakingFrame):
                self._user_stopped_at = asyncio.get_running_loop().time()
                return

            if isinstance(frame, UserStartedSpeakingFrame):
                if self._bot_speaking:
                    turn = self._last_assistant_turn()
                    if turn is not None:
                        turn["interrupted"] = True
                return

            if isinstance(frame, BotStartedSpeakingFrame):
                self._bot_speaking = True
                if self._user_stopped_at is not None:
                    e2e_ms = (asyncio.get_running_loop().time() - self._user_stopped_at) * 1000
                    self._user_stopped_at = None
                    turn = self._last_assistant_turn()
                    if turn is not None and turn.get("latency_ms") is None:
                        turn["latency_ms"] = round(e2e_ms, 1)
                    else:
                        self._pending["e2e"] = e2e_ms
                return

            if isinstance(frame, BotStoppedSpeakingFrame):
                self._bot_speaking = False
                return

            if isinstance(frame, MetricsFrame):
                for metric in getattr(frame, "data", None) or []:
                    if not isinstance(metric, TTFBMetricsData):
                        continue
                    processor = (getattr(metric, "processor", "") or "").lower()
                    value = getattr(metric, "value", None)
                    if value is None:
                        continue
                    kind = next((k for k in ("stt", "llm", "tts") if k in processor), None)
                    if kind:
                        self._pending[kind] = float(value) * 1000
                return

            if isinstance(frame, FunctionCallResultFrame):
                if frame.function_name in self._transfer_tools:
                    self.transferred = True
                result = frame.result
                success = None
                if isinstance(result, dict) and "error" in result:
                    success = False
                elif isinstance(result, Exception):
                    success = False
                self._recorder.record_tool_call(
                    name=frame.function_name,
                    arguments=frame.arguments,
                    result=str(result)[:500] if result is not None else None,
                    success=success,
                )
                return

            if isinstance(frame, ErrorFrame):
                self.last_error = str(getattr(frame, "error", ""))[:500]
                if getattr(frame, "fatal", False):
                    self._had_fatal_error = True
                return

            if isinstance(frame, EndFrame):
                self._set_end_reason(getattr(frame, "reason", None), is_cancel=False)
                return

            if isinstance(frame, CancelFrame):
                self._set_end_reason(getattr(frame, "reason", None), is_cancel=True)
                return

except ImportError:  # pragma: no cover - pipecat not installed

    class OpenCallMetricsObserver:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any):
            raise ImportError(
                "OpenCallMetricsObserver requires pipecat: pip install pipecat-ai"
            )

    class OpenCallFrameObserver:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any):
            raise ImportError(
                "OpenCallFrameObserver requires pipecat: pip install pipecat-ai"
            )

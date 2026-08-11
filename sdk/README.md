# callharness-sdk

Python SDK for [CallHarness](https://github.com/callharness) — open-source call analytics for voice AI agents.

## Install

```bash
pip install callharness-sdk
```

## Direct ingestion

```python
from callharness_sdk import CallHarnessClient

client = CallHarnessClient("http://localhost:8010")
call = client.ingest_call(
    agent_id="my-agent",
    end_reason="completed",
    turns=[
        {"role": "assistant", "text": "Hi, how can I help?"},
        {"role": "user", "text": "I'd like to book an appointment."},
    ],
)
client.upload_recording(call["id"], "recording.wav")
```

## Pipecat integration

```python
from pipecat.processors.transcript_processor import TranscriptProcessor
from callharness_sdk.pipecat import create_recorder

transcript = TranscriptProcessor()
recorder = create_recorder("http://localhost:8010", agent_id="my-agent")
recorder.attach(transcript)

# include transcript.user() after STT and transcript.assistant() after TTS
# in your pipeline, then when the call ends:
await recorder.flush(end_reason="completed")
```

To capture per-turn STT/LLM/TTS latency, add the metrics observer to your task:

```python
from callharness_sdk.pipecat import CallHarnessMetricsObserver

task = PipelineTask(
    pipeline,
    params=PipelineParams(enable_metrics=True),
    observers=[CallHarnessMetricsObserver(recorder)],
)
```

## Pipecat without TranscriptProcessor

If your pipeline doesn't use `TranscriptProcessor` (e.g. you capture transcripts at
the frame level), use the all-in-one frame observer instead — it captures transcript
turns, end-to-end response latency, STT/LLM/TTS components, interruptions, tool calls,
transfers, and a deterministic `end_reason`, all from one observer:

```python
from callharness_sdk.pipecat import CallHarnessFrameObserver, create_recorder

recorder = create_recorder("http://localhost:8010", agent_id="my-agent")
observer = CallHarnessFrameObserver(
    recorder, stt=stt, tts=tts, transfer_tool_names={"transfer_to_human"}
)
# add `observer` to your PipelineTask/PipelineWorker observers, then on call end:
await recorder.flush(
    end_reason=observer.finalize_end_reason(),  # "completed" | "transferred" | "error" | ...
    transferred=observer.transferred,
    recording_bytes=wav_bytes,   # optional in-memory recording upload
)
```

`finalize_end_reason()` gives you the best reason available *right now*: `"error"`
if a fatal `ErrorFrame` occurred, otherwise the explicit `reason=` you passed to
`EndTaskFrame`/`CancelTaskFrame` if the pipeline already saw one before you called
this (e.g. `EndTaskFrame(reason="silence_timeout")` from a `UserIdleProcessor`
callback), otherwise `"transferred"` if a transfer fired, otherwise `"completed"`
(pass a different `default=` if that's not right for your integration).

Call `finalize_end_reason()` — not the raw `observer.end_reason` attribute — from
your own disconnect/teardown handler (e.g. a transport's `on_client_disconnected`).
That fires *before* an `EndFrame`/`CancelFrame` has necessarily propagated through
the pipeline, so `.end_reason` may still be `None` at that point; `finalize_end_reason()`
only depends on state (fatal error, transferred) that's already known live during the
call, so it's correct regardless of teardown ordering. `observer.last_error` holds the
most recent error message, useful to drop into `metadata` for debugging.

See [examples/pipecat_bot.py](../examples/pipecat_bot.py) for a full working bot.

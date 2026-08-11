# callharness-sdk

Python SDK for [CallHarness](https://github.com/mohammad-mussab/callharness) — open-source call analytics for voice AI agents.

Send your agent's calls to a CallHarness server, which runs post-call LLM analysis
(summary, sentiment, outcome, why a call transferred or didn't complete) and shows it
on a dashboard. Self-hosted, so your transcripts stay on your own infrastructure.

## Install

```bash
pip install callharness-sdk            # REST client + turn assembly
pip install "callharness-sdk[pipecat]" # also installs pipecat-ai for the observers
```

The `[pipecat]` extra only adds `pipecat-ai`. Skip it if you're on LiveKit, a custom
stack, or calling the REST API directly — `callharness_sdk.pipecat` still imports
cleanly without it, and only raises if you actually instantiate an observer.

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

## Call recording

Two lines. The audio uploads itself when you `flush()` — there is no second call to
remember:

```python
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
from callharness_sdk.pipecat import attach_audio

audio_buffer = AudioBufferProcessor(num_channels=2)
attach_audio(recorder, audio_buffer)

pipeline = Pipeline([
    transport.input(), stt, llm, tts,
    transport.output(),
    audio_buffer,          # ← AFTER transport.output()
])
await audio_buffer.start_recording()
```

Two placement details decide whether the recording is worth having:

- **After `transport.output()`.** Placed earlier it records what the bot *intended* to
  say, so a sentence the caller interrupted is captured in full even though nobody
  heard it — and the recording then disagrees with the transcript at exactly the
  moments you are most likely to be investigating.
- **`num_channels=2`** puts the caller on the left and the bot on the right. With a
  mono mix, overlapping speech is unusable precisely when there was an interruption.

The dashboard plays it inline on the call page, and clicking a transcript line seeks
the audio to that moment. Recordings expire on the server after
`CALLHARNESS_RECORDING_RETENTION_DAYS` (default 30); transcripts and analysis are kept
indefinitely.

## If your agent already has its own call pipeline

Mature agents usually already collect a transcript, their own record of tool calls,
and write to their own database. Adopting `CallRecorder` would mean maintaining two
sources of truth — so instead, hand CallHarness what you already have:

```python
from callharness_sdk import CallHarnessClient, LatencyCollector, assemble_turns
from callharness_sdk.pipecat import CallHarnessMetricsObserver

latency = LatencyCollector()          # satisfies what the observer expects
task = PipelineTask(
    pipeline,
    params=PipelineParams(enable_metrics=True),   # required, or no metrics are emitted
    observers=[CallHarnessMetricsObserver(latency)],
)

# ...at the end of the call, from your own save routine:
turns = assemble_turns(
    transcript=my_transcript,        # [{role, content, timestamp}, ...]
    tool_calls=my_function_calls,    # [{function_name, parameters, result, timestamp}]
    latency=latency,
    started_at=call_started_at,
)
CallHarnessClient("http://localhost:8010").ingest_call(
    agent_id="my-agent", turns=turns, external_id=my_call_id,
    transferred=..., metadata={"my_own_verdict": ...},
)
```

`assemble_turns()` does the fiddly part: a tool call and a latency sample both happen
*while* a reply is being produced, before its text exists, so both are matched by
timestamp to the assistant turn they actually belong to. It accepts either field
naming (`name`/`function_name`, `arguments`/`parameters`, `content`/`text`), truncates
oversized tool results, and never records a tool as successful unless it can prove it.

Anything you send in `metadata` is stored alongside the call — useful if your agent
already classifies its own calls and you want to compare that against CallHarness's
independent verdict.

## Full example

See [examples/pipecat_bot.py](https://github.com/mohammad-mussab/callharness/blob/main/examples/pipecat_bot.py)
for a complete working bot.

## Licence

MIT

# opencall-sdk

Python SDK for [OpenCall](https://github.com/opencall) — open-source call analytics for voice AI agents.

## Install

```bash
pip install opencall-sdk
```

## Direct ingestion

```python
from opencall_sdk import OpenCallClient

client = OpenCallClient("http://localhost:8010")
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
from opencall_sdk.pipecat import create_recorder

transcript = TranscriptProcessor()
recorder = create_recorder("http://localhost:8010", agent_id="my-agent")
recorder.attach(transcript)

# include transcript.user() after STT and transcript.assistant() after TTS
# in your pipeline, then when the call ends:
await recorder.flush(end_reason="completed")
```

To capture per-turn STT/LLM/TTS latency, add the metrics observer to your task:

```python
from opencall_sdk.pipecat import OpenCallMetricsObserver

task = PipelineTask(
    pipeline,
    params=PipelineParams(enable_metrics=True),
    observers=[OpenCallMetricsObserver(recorder)],
)
```

## Pipecat without TranscriptProcessor

If your pipeline doesn't use `TranscriptProcessor` (e.g. you capture transcripts at
the frame level), use the all-in-one frame observer instead — it captures transcript
turns, end-to-end response latency, STT/LLM/TTS components, interruptions, and
transfers, all from one observer:

```python
from opencall_sdk.pipecat import OpenCallFrameObserver, create_recorder

recorder = create_recorder("http://localhost:8010", agent_id="my-agent")
observer = OpenCallFrameObserver(
    recorder, stt=stt, tts=tts, transfer_tool_names={"transfer_to_human"}
)
# add `observer` to your PipelineTask/PipelineWorker observers, then on call end:
await recorder.flush(
    end_reason="transfer" if observer.transferred else "completed",
    transferred=observer.transferred,
    recording_bytes=wav_bytes,   # optional in-memory recording upload
)
```

See [examples/pipecat_bot.py](../examples/pipecat_bot.py) for a full working bot.

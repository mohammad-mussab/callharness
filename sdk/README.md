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

See [examples/pipecat_bot.py](../examples/pipecat_bot.py) for a full working bot.

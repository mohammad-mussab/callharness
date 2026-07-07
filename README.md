# OpenCall

**Open-source call analytics for voice AI agents.**

You built a voice agent with [Pipecat](https://github.com/pipecat-ai/pipecat), LiveKit, or your own stack. It's taking hundreds or thousands of calls a day — and you have no idea which calls succeed, where callers get frustrated, or why they hang up. Platforms like Retell and Vapi bundle this analysis into their closed ecosystems; standalone tools (Coval, Hamming, Roark, Cekura) are paid SaaS.

OpenCall is the self-hosted alternative: send it your calls, and it gives you the same post-call analysis loop those platforms offer.

## Features

- **Call ingestion API + Python SDK** — send transcripts, recordings, and metadata from any voice agent; ~6 lines to integrate with a Pipecat bot
- **LLM post-call analysis** (bring your own key — OpenAI, Anthropic, or any OpenAI-compatible endpoint like Ollama):
  - **Summary** — configurable call summaries
  - **Success evaluation** — define what "success" means for your agent; Pass/Fail or 1-10 rubric with rationale
  - **Structured data extraction** — define custom fields (text / boolean / number / enum), extracted from every call
  - **Sentiment analysis** — label + score from -1.0 to 1.0
- **Dashboard** — call volume, success rate, sentiment distribution, transfer rate, end-reason breakdown, 14-day trends, per-agent filtering
- **Call explorer** — search transcripts, filter by outcome/sentiment, drill into any call with a synced audio player + turn-by-turn transcript with latency annotations
- **Self-hosted** — SQLite + local files by default, Postgres via Docker Compose; your call data never leaves your infrastructure

## Quickstart (local dev)

Backend (Python 3.10+):

```bash
cd server
python -m venv .venv && .venv/Scripts/activate   # Windows; use bin/activate on Unix
pip install -r requirements.txt
python scripts/seed_demo.py                       # optional: demo data
set OPENAI_API_KEY=sk-...                         # optional: enables analysis
uvicorn app.main:app --port 8010
```

Dashboard (Node 18+):

```bash
cd web
npm install
npm run dev        # http://localhost:3010
```

### Docker

```bash
OPENAI_API_KEY=sk-... docker compose up --build
```

Dashboard at `http://localhost:3010`, API at `http://localhost:8010` (OpenAPI docs at `/docs`).

## Sending calls

### From a Pipecat agent

```python
from pipecat.processors.transcript_processor import TranscriptProcessor
from opencall_sdk.pipecat import create_recorder

transcript = TranscriptProcessor()   # transcript.user() after STT, transcript.assistant() after TTS
recorder = create_recorder("http://localhost:8010", agent_id="my-agent")
recorder.attach(transcript)

# ... run your pipeline ...

await recorder.flush(end_reason="completed")
```

Full example: [examples/pipecat_bot.py](examples/pipecat_bot.py)

### From anything else

```bash
curl -X POST http://localhost:8010/api/v1/calls -H "Content-Type: application/json" -d '{
  "agent_id": "my-agent",
  "end_reason": "completed",
  "turns": [
    {"role": "assistant", "text": "Hi! How can I help?"},
    {"role": "user", "text": "I want to reschedule my appointment."}
  ]
}'
```

Then upload the recording (optional): `POST /api/v1/calls/{id}/recording` (multipart).

## Configuration

| Env var | Default | Description |
|---|---|---|
| `OPENCALL_DATABASE_URL` | `sqlite+aiosqlite:///./opencall.db` | SQLAlchemy URL; use `postgresql+asyncpg://...` for Postgres |
| `OPENCALL_DATA_DIR` | `./data` | Where call recordings are stored |
| `OPENCALL_API_KEY` | *(unset)* | If set, write endpoints require this key |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | *(unset)* | Enables LLM post-call analysis |
| `OPENCALL_LLM_PROVIDER` | `auto` | `openai`, `anthropic`, or `none` |
| `OPENCALL_LLM_MODEL` | per provider | Override the analysis model |
| `OPENCALL_LLM_BASE_URL` | OpenAI | Point at Ollama/vLLM for fully local analysis |

Analysis behavior (what to summarize, success criteria, extraction fields) is configured in the dashboard under **Analysis Settings**, or via `PUT /api/v1/config/analysis`.

## Architecture

```
voice agent (Pipecat / LiveKit / custom)
        │  opencall-sdk / REST
        ▼
FastAPI server ──► SQLite / Postgres
        │              ▲
        │   in-process analysis worker
        │   (summary · success · sentiment · extraction)
        ▼
Next.js dashboard (overview · call explorer · settings)
```

## Roadmap

- **Phase 2 — voice-specific depth**: OpenTelemetry trace ingestion (Pipecat/LiveKit emit OTel natively) with per-turn STT/LLM/TTS latency breakdowns and p50/p95/p99 dashboards; interruption/silence/talk-ratio metrics; alerting rules (Slack/webhook/email); custom LLM-as-judge evaluators
- **Phase 3 — closing the loop**: analytics query API + custom dashboards; caller journey / drop-off analysis; failed-call → test-case pipeline; Vapi/Retell importers; S3 recording storage; multi-tenant auth

## License

MIT

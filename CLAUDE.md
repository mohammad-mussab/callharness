# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

OpenCall is a self-hosted call analytics platform for voice AI agents (Pipecat, LiveKit, or custom stacks). Agents send call transcripts/recordings to a FastAPI ingestion API; an in-process worker runs LLM post-call analysis (summary, sentiment, success, structured extraction, custom evaluators); a Next.js dashboard visualizes the results. Three parts, each independently runnable:

- `server/` — FastAPI backend + analysis worker + SQLite/Postgres (Python 3.10+)
- `web/` — Next.js dashboard (Node 18+)
- `sdk/` — `opencall_sdk` Python package for instrumenting voice agents (published separately from `server/`)

## Commands

### Backend (`server/`)

```bash
cd server
python -m venv .venv && .venv/Scripts/activate   # Windows; bin/activate on Unix
pip install -r requirements.txt
python scripts/seed_demo.py                       # optional: populate demo data
uvicorn app.main:app --port 8010 --reload
```

No test suite or linter is currently configured for `server/` or `sdk/`. Verify backend changes by hitting the running API directly (`http://localhost:8010/docs` for interactive OpenAPI) or via `curl`, as shown in the README.

`scripts/backfill_from_supabase.py` bulk-imports historical calls from an existing Supabase `call_logs` table (bounded by `--days`/`--limit` for LLM-cost control) through the normal ingestion API — for agents that were logging to their own Supabase table before wiring up OpenCall live. Idempotent via `external_id`; column/table names at the top of the file are inferred from one specific agent's schema and should be checked against any other agent's table before use.

Enable LLM analysis by setting `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` (or both `server/.env` — an `OPENCALL_`-prefixed env file is loaded automatically per `app/config.py`).

### Dashboard (`web/`)

```bash
cd web
npm install
npm run dev     # http://localhost:3010, proxies /api/* to the backend (see next.config.mjs)
npm run build
npm run start
```

No test suite is configured. There is no separate lint script wired into `package.json`; rely on `next build`'s type-checking (TypeScript) to catch errors.

### Docker (full stack, Postgres)

```bash
OPENAI_API_KEY=sk-... docker compose up --build
```

### SDK (`sdk/`)

Installed independently by voice-agent authors (`pip install opencall-sdk[pipecat]`). No build/test commands beyond standard `pip install -e .` for local development against the backend.

## Architecture

```
voice agent (Pipecat / LiveKit / custom)
        │  opencall-sdk / REST
        ▼
FastAPI server ──► SQLite / Postgres
        │              ▲
        │   in-process analysis worker
        │   (summary · success · sentiment · extraction · evaluators · alerts)
        ▼
Next.js dashboard (overview · call explorer · latency · alerts · evaluators · settings)
```

### Backend (`server/app/`)

- `main.py` — FastAPI app; the `lifespan` context starts `run_worker()` as a background `asyncio` task alongside the request-serving process. There is no separate worker deployment — analysis runs in-process.
- `db.py` — SQLAlchemy async engine/session. **Migrations are hand-rolled**: `init_db()` calls `Base.metadata.create_all` (creates missing tables only) followed by `_apply_column_migrations()`, which walks the `COLUMN_MIGRATIONS` list and adds columns to existing tables via raw `ALTER TABLE` if missing. There is no Alembic. **When adding a column to an existing model, you must also append an entry to `COLUMN_MIGRATIONS`** or existing SQLite/Postgres databases won't pick it up.
- `models.py` — SQLAlchemy models: `Call` (+ `Turn` children), `AlertRule`/`AlertEvent`, `Evaluator`/`EvaluationResult`, `AnalysisConfig` (singleton row, id=1, holds the user-configured analysis behavior — prompts, enabled toggles, extraction field definitions, output language, classification taxonomies). `Turn.tool_calls` (JSON, nullable) holds `[{name, arguments, result, success}, ...]` for function/tool calls made while producing that turn — populated by `OpenCallFrameObserver`, rendered as expandable chips on the call detail page, and folded into the transcript sent to the analysis LLM (see `engine.py`). `Call.transfer_reason`/`Call.non_completion_reason` classify *why* a transferred/non-completed call ended that way, against the taxonomies on `AnalysisConfig` (see `taxonomy.py`). `Call.reason_source` records who decided — `"agent"` (sent with the call at ingest) or `"llm"` (inferred during analysis).
- `routes/` — one router per resource (`calls`, `analytics`, `config`, `alerts`, `evaluators`), all mounted in `main.py`. Write/mutation endpoints depend on `require_api_key` (`auth.py`), a no-op unless `OPENCALL_API_KEY` is set.
- `analysis/` — the post-call pipeline:
  - `worker.py` — polling loop (`analysis_poll_seconds`). Claims calls with `analysis_status == "pending"` in batches of `analysis_concurrency`, flips them to `processing`, runs `analyze_call()` + `run_evaluators()`, then `check_call_alerts()`. Also runs `check_window_alerts()` on a separate ~60s interval. If no LLM provider is configured, pending calls are marked `skipped` instead (but keyword/latency alerts still run on them via `_check_skipped_alerts`).
  - `engine.py` — builds the single LLM prompt/schema from `AnalysisConfig` (which sections are enabled, custom prompts, extraction field defs) and parses the JSON result back onto the `Call` row (`apply_result`). One LLM call produces summary + sentiment + success + structured_data + detected language + transfer/non-completion classification together. `build_transcript()` inlines each turn's `tool_calls` as `[tool call: name(args) -> result]` lines, and the system prompt tells the judge to treat those as ground truth rather than inferring outcomes from dialogue tone alone — the main lever for reducing hallucinated success/transfer rationale. `transfer_reason`/`non_completion_reason` are only ever persisted when the *deterministic* fields agree they're applicable (`call.transferred` / `call.success is not True`, both set independently of this LLM call) — the LLM classifies *why*, never *whether*. Only the one applicable dimension is ever asked for (a transferred call can't have a non-completion reason, so that field is left out of the prompt entirely), and neither is asked for when `call.reason_source == "agent"` or classification is disabled in config.
  - `taxonomy.py` — **default** transfer/non-completion categories, plus `normalize_key()` and `categories_or_default()`. These are only seeds: the live taxonomies are `AnalysisConfig.transfer_reasons`/`non_completion_reasons` (lists of `{key, description}`), editable from Settings with no code change — the no-code path competitors offer. `worker.get_or_create_config()` materializes the defaults onto the config row on first access (and backfills pre-existing installs), an empty saved list resets to defaults, and an LLM answer outside the configured set lands in `other`. **Category `key`s are persisted on call rows and used as chart slices and filter values, so renaming one orphans every call already classified under it** — add a category instead of renaming. Agents that classify their own calls can instead send `transfer_reason`/`non_completion_reason` on `CallCreate` (`reason_source="agent"`); that value is authoritative and re-analysis will not overwrite it.
  - `llm.py` — provider-agnostic `chat_json(system, user) -> dict`. Supports OpenAI-compatible chat-completions endpoints (works with Ollama/vLLM via `llm_base_url`) and the Anthropic Messages API. Provider resolution (`Settings.resolved_provider`) auto-picks based on which API key is set unless `OPENCALL_LLM_PROVIDER` forces one.
  - `evaluators.py` — runs user-defined pass/fail criteria (`Evaluator` rows) as additional LLM judge calls per analyzed call; replaces prior `EvaluationResult`s on re-analysis.
  - `alerts.py` — per-call triggers (negative sentiment, failed call, keyword match, high latency) evaluated right after analysis; windowed triggers (success-rate/sentiment drop over a rolling window) evaluated periodically. Delivery channels: webhook, Slack incoming webhook, or SMTP email; every firing is logged as `AlertEvent` regardless of delivery success, and rules respect a per-rule cooldown. Per-call alert messages include `call.from_number` when present, so e.g. a negative-sentiment alert is directly actionable as a callback.
  - `translate.py` — on-demand transcript translation, cached onto `Turn.translated_text`.
- `quality.py` — pure functions computing conversation-quality metrics (talk ratio, WPM, silence gaps, interruption count) from turn timings at ingest time. No LLM involved; degrades gracefully (estimates speaking duration from word count) when `start_time`/`end_time` are missing.
- `outcome.py` — `compute_outcome(success, transferred, end_reason) -> "transferred"|"completed"|"non_completed"`. Three buckets, deliberately mirroring the `esito_chiamata` taxonomy (COMPLETATA/TRASFERITA/NON COMPLETATA) that the target production agents already use; "success" is **not** a separate outcome (a successful call is a completed call). Not a stored column: `CallOut.outcome` is a Pydantic `computed_field` calling this, and `routes/analytics.py`'s `outcome_distribution` calls it per-row in Python. `routes/calls.py`'s `?outcome=` filter re-expresses the same precedence directly in SQL (can't reuse the Python function inside a query) — the two are verified to agree across all 30 combinations of `transferred` × `success` × `end_reason` with no overlap or gap, but a change to one **must** be mirrored in the other.
- `disputes.py` — compares an agent's *own* verdict against OpenCall's. Agents that already classify their calls (e.g. a regional agent's `esito_chiamata`/`motivazione`) send it as `agent_esito`/`agent_motivazione` inside `Call.meta` — deliberately **not** as `transfer_reason`/`reason_source="agent"`, since that path makes OpenCall trust the value and skip its own LLM call, which would defeat the comparison. `classify()` returns `agreed` / `outcome` (different bucket) / `reason` (same bucket, different why) / `None` (no agent verdict → excluded from the denominator, never counted as agreement). `is_overcount()` singles out agent-said-completed / OpenCall-disagreed, the asymmetric case that inflates the reported success rate. Surfaced by `GET /api/v1/analytics/disputes` and the `/disputes` dashboard page, which also shows failed tool calls per disputed call — the evidence the agent's own transcript-only judge never had.
- `storage.py` — recordings are saved to `OPENCALL_DATA_DIR/recordings/{call_id}.{ext}` on local disk; served back via `FileResponse`.

Config (`config.py`) is a single `pydantic-settings` `Settings` object read once at import time (module-level `settings` singleton), sourced from `OPENCALL_`-prefixed env vars plus a `.env` file. `OPENAI_API_KEY`/`ANTHROPIC_API_KEY` are accepted both with and without the `OPENCALL_` prefix via `AliasChoices`. Notable env vars: `OPENCALL_DATABASE_URL` (default `sqlite+aiosqlite:///./opencall.db`), `OPENCALL_DATA_DIR` (recording storage root), `OPENCALL_API_KEY` (gates write endpoints via `require_api_key`), `OPENCALL_LLM_PROVIDER` (`auto`/`openai`/`anthropic`/`none`), `OPENCALL_LLM_MODEL`, `OPENCALL_LLM_BASE_URL` (point at Ollama/vLLM), and `OPENCALL_SMTP_*` (email alert channel).

### SDK (`sdk/opencall_sdk/`)

- `client.py` — thin sync/async `httpx` wrappers over the ingestion API (`OpenCallClient`, `AsyncOpenCallClient`).
- `recorder.py` — `CallRecorder`: accumulates turns/timing/metadata in memory for one call and `flush()`es it via the client at call end. `record_component_latency()` and `record_tool_call()` stash STT/LLM/TTS latency and tool-call results against the *next* assistant turn (both happen mid-processing, before that turn's text exists yet), merged in on `add_turn()`.
- `pipecat.py` — Pipecat-specific integration, with two levels of instrumentation:
  - `PipecatCallRecorder.attach()` — hooks `TranscriptProcessor.on_transcript_update`; minimal integration (~6 lines), no latency/interruption/tool-call data.
  - `OpenCallFrameObserver` — full frame-level `BaseObserver` capturing transcripts, end-to-end + per-component (STT/LLM/TTS TTFB) latency, interruptions, tool-based transfer detection (matches against `transfer_tool_names`), and every function/tool call the agent makes (name, arguments, result) from `FunctionCallResultFrame`, directly from the pipeline. Both classes degrade to raising `ImportError` at instantiation if `pipecat-ai` isn't installed (the module itself always imports cleanly). `observer.finalize_end_reason(default="completed")` — call this from your own teardown/disconnect handler, not `.end_reason` directly — returns `"error"` on a fatal `ErrorFrame`, else an explicit `reason=` already observed on an `EndFrame`/`CancelFrame` (set via `EndTaskFrame`/`CancelTaskFrame`, e.g. from a `UserIdleProcessor` timeout), else `"transferred"` if a transfer fired, else `default`. It's deliberately not just "read `.end_reason`": most integrations learn the call ended (e.g. a transport's `on_client_disconnected`) *before* any EndFrame/CancelFrame has propagated through the pipeline — confirmed against a real integration where `PipelineWorker.cancel()` only queues the CancelFrame and returns without awaiting propagation — so `finalize_end_reason()` uses only state tracked live during the call and is correct regardless of teardown ordering.

### Dashboard (`web/`)

Next.js App Router. Pages under `app/` correspond 1:1 to top-level nav sections (`calls/`, `disputes/`, `alerts/`, `evaluators/`, `latency/`, `settings/`, plus overview at `app/page.tsx`). `web/lib/api.ts` defines every API response shape as a hand-written TypeScript type (no codegen from the backend's Pydantic schemas — **keep these in sync manually** when changing `server/app/schemas.py`) plus `fetcher`/`apiSend` helpers used with SWR. In dev, Next rewrites `/api/*` to the backend via `next.config.mjs` (`OPENCALL_API_URL`, default `http://127.0.0.1:8010`); in Docker Compose this points at the `api` service.

## Conventions worth knowing

- Call lifecycle: `analysis_status` moves `pending → processing → completed|failed`, or `pending → skipped` if no LLM provider is configured at claim time. `POST /calls/{id}/reanalyze` resets it to `pending` to requeue.
- All datetimes are stored naive UTC (`utcnow()` in `models.py` strips tzinfo); incoming timezone-aware timestamps from `CallCreate` payloads are normalized with `.replace(tzinfo=None)` before persisting.
- Ingestion is idempotent on `external_id`: re-posting a call with the same `external_id` returns the existing row instead of creating a duplicate.
- There is no dedicated "region" concept anywhere in the schema. The dashboard's "By agent / region" panel and `OverviewOut.agent_stats` both just group by `Call.agent_id` — multi-region deployments are expected to encode region into the `agent_id` string at ingest time (e.g. `"support-eu"`, `"support-us"`).

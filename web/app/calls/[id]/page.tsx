"use client";

import { use, useRef, useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import { fetcher, textFetcher, type CallDetail } from "@/lib/api";
import { formatClock, formatDate, formatDuration, titleCase } from "@/lib/format";
import {
  BucketBadge,
  NonCompletionReasonBadge,
  OutcomeBadge,
  SentimentBadge,
  StatusBadge,
  TransferReasonBadge,
} from "@/components/Badges";
import WaveformPlayer from "@/components/WaveformPlayer";
import LogViewer from "@/components/LogViewer";

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4">
      <h2 className="mb-3 text-sm font-medium text-zinc-300">{title}</h2>
      {children}
    </div>
  );
}

export default function CallDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { data: call, mutate } = useSWR<CallDetail>(`/api/v1/calls/${id}`, fetcher, {
    refreshInterval: (data) =>
      data && ["pending", "processing"].includes(data.analysis_status) ? 3000 : 0,
  });
  const audioRef = useRef<HTMLAudioElement>(null);
  const [currentTime, setCurrentTime] = useState(0);
  const [reanalyzing, setReanalyzing] = useState(false);
  const [translating, setTranslating] = useState(false);
  const [translateError, setTranslateError] = useState<string | null>(null);
  const [showTranslation, setShowTranslation] = useState(true);
  const [expandedTools, setExpandedTools] = useState<Set<string>>(new Set());
  const [logOpen, setLogOpen] = useState(false);
  // Conditional key: nothing is fetched until the panel is expanded. That matters
  // because the detail endpoint above re-polls every 3s while analysis is running,
  // and a ~200KB log riding along with it would be pulled over and over.
  const { data: logText, error: logError } = useSWR<string>(
    logOpen ? `/api/v1/calls/${id}/log` : null,
    textFetcher,
    { revalidateOnFocus: false }
  );

  function toggleTool(key: string) {
    setExpandedTools((prev) => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });
  }

  function formatToolValue(value: unknown): string {
    if (value == null) return "—";
    return typeof value === "string" ? value : JSON.stringify(value);
  }

  if (!call) return <div className="p-8 text-zinc-500">Loading…</div>;

  const activeTurn = call.turns.findLast?.(
    (t) => t.start_time != null && t.start_time <= currentTime
  );
  const hasTranslation = call.turns.some((t) => t.translated_text);
  // Offer translation when the call isn't already in English
  const offerTranslation =
    call.turns.length > 0 && call.language != null && call.language !== "english";

  async function reanalyze() {
    setReanalyzing(true);
    try {
      await fetch(`/api/v1/calls/${id}/reanalyze`, { method: "POST" });
      await mutate();
    } finally {
      setReanalyzing(false);
    }
  }

  async function translate() {
    setTranslating(true);
    setTranslateError(null);
    try {
      const res = await fetch(`/api/v1/calls/${id}/translate?language=english`, {
        method: "POST",
      });
      // Report what actually went wrong. This used to show one fixed "check your
      // LLM key" message for every failure, which sent a real 401 investigation
      // down entirely the wrong path.
      if (!res.ok) {
        if (res.status === 401)
          throw new Error(
            "Not authorised (401). The dashboard could not authenticate to the API — " +
              "set CALLHARNESS_API_KEY on the web container to the same value as the server."
          );
        if (res.status === 502)
          throw new Error("Cannot reach the CallHarness API (502). Is the api container up?");
        const detail = await res.text().catch(() => "");
        throw new Error(
          `Translation failed (${res.status}). ${detail.slice(0, 160)}`.trim()
        );
      }
      await mutate();
      setShowTranslation(true);
    } catch (e) {
      setTranslateError(e instanceof Error ? e.message : "Translation failed.");
    } finally {
      setTranslating(false);
    }
  }

  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <Link href="/calls" className="text-sm text-zinc-500 hover:text-zinc-300">
            ← Calls
          </Link>
          <h1 className="mt-1 text-xl font-semibold text-zinc-100">
            {call.agent_id} · {formatDate(call.started_at)}
          </h1>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <StatusBadge status={call.analysis_status} />
            {call.analysis_status === "completed" && <OutcomeBadge outcome={call.outcome} />}
            <BucketBadge bucket={call.bucket} note={call.issue_note} />
            <SentimentBadge label={call.sentiment_label} />
            <TransferReasonBadge reason={call.transfer_reason} source={call.reason_source} />
            <NonCompletionReasonBadge
              reason={call.non_completion_reason}
              source={call.reason_source}
            />
            {call.language && (
              <span className="inline-flex items-center rounded-full bg-sky-500/15 px-2 py-0.5 text-xs font-medium text-sky-300">
                {titleCase(call.language)}
              </span>
            )}
            <span className="text-xs text-zinc-500">
              {formatDuration(call.duration_seconds)} · {titleCase(call.direction)}
              {call.from_number ? ` · ${call.from_number}` : ""}
            </span>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {offerTranslation && !hasTranslation && (
            <button
              onClick={translate}
              disabled={translating}
              className="rounded-lg border border-sky-800 px-3 py-1.5 text-sm text-sky-300 hover:bg-sky-950/40 disabled:opacity-40"
            >
              {translating ? "Translating…" : "Translate to English"}
            </button>
          )}
          {hasTranslation && (
            <button
              onClick={() => setShowTranslation((v) => !v)}
              className="rounded-lg border border-sky-800 px-3 py-1.5 text-sm text-sky-300 hover:bg-sky-950/40"
            >
              {showTranslation ? `Show original (${titleCase(call.language ?? "original")})` : "Show English"}
            </button>
          )}
          <button
            onClick={reanalyze}
            disabled={reanalyzing || ["pending", "processing"].includes(call.analysis_status)}
            className="rounded-lg border border-zinc-700 px-3 py-1.5 text-sm text-zinc-300 hover:bg-zinc-900 disabled:opacity-40"
          >
            {["pending", "processing"].includes(call.analysis_status) ? "Analyzing…" : "Re-analyze"}
          </button>
        </div>
      </div>

      {translateError && (
        <div className="rounded-xl border border-red-900/50 bg-red-950/30 p-3 text-sm text-red-300">
          {translateError}
        </div>
      )}

      {call.has_recording && (
        <div className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4">
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
            <div className="text-sm font-medium text-zinc-200">Call recording</div>
            <div className="text-xs text-zinc-500">
              Click any line in the transcript to jump to that moment
            </div>
          </div>
          <WaveformPlayer
            src={`/api/v1/calls/${call.id}/audio`}
            audioRef={audioRef}
            onTimeUpdate={setCurrentTime}
          />
          <div className="mt-2 text-xs text-zinc-600">
            Recordings are kept for a limited period and deleted automatically; the
            transcript and analysis are kept indefinitely.
          </div>
        </div>
      )}

      {call.has_log && (
        <div className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4">
          <button
            onClick={() => setLogOpen((v) => !v)}
            className="flex w-full items-center justify-between gap-2 text-left"
          >
            <span className="text-sm font-medium text-zinc-200">Agent log</span>
            <span className="text-xs text-zinc-500">
              {logOpen ? "Hide" : "Show"} the agent's full log for this call
            </span>
          </button>
          {logOpen && (
            <div className="mt-3">
              {logError ? (
                <div className="text-sm text-red-300">{logError.message}</div>
              ) : logText === undefined ? (
                <div className="text-sm text-zinc-500">Loading log…</div>
              ) : (
                <LogViewer text={logText} downloadUrl={`/api/v1/calls/${call.id}/log?download=1`} />
              )}
            </div>
          )}
        </div>
      )}

      {call.analysis_error && (
        <div className="rounded-xl border border-red-900/50 bg-red-950/30 p-4 text-sm text-red-300">
          Analysis failed: {call.analysis_error}
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-5">
        <div className="space-y-4 lg:col-span-2">
          {call.summary && (
            <Panel title="Summary">
              <p className="text-sm leading-relaxed text-zinc-300">{call.summary}</p>
            </Panel>
          )}
          {call.issue_note && (
            <Panel title="What happened">
              <div className="space-y-2">
                <BucketBadge bucket={call.bucket} />
                <p className="text-sm leading-relaxed text-zinc-300">{call.issue_note}</p>
                {call.unanswered_query && (
                  // The exact wording the lookup was given. Shown verbatim because this
                  // is the line that reaches the customer's Missing Information report,
                  // and it is what gets re-run against the API to verify the gap.
                  <div className="rounded-lg border border-amber-900/60 bg-amber-950/20 p-2">
                    <div className="text-xs uppercase tracking-wide text-amber-500/80">
                      Query that found nothing
                    </div>
                    <p className="mt-0.5 font-mono text-xs text-amber-200">
                      {call.unanswered_query}
                    </p>
                  </div>
                )}
              </div>
            </Panel>
          )}
          {call.success_rationale && (
            <Panel title={`Success evaluation${call.success_score != null ? ` — ${call.success_score}/10` : ""}`}>
              <p className="text-sm leading-relaxed text-zinc-300">{call.success_rationale}</p>
            </Panel>
          )}
          {call.structured_data && Object.keys(call.structured_data).length > 0 && (
            <Panel title="Extracted data">
              <dl className="space-y-2">
                {Object.entries(call.structured_data).map(([key, value]) => (
                  <div key={key} className="flex items-start justify-between gap-3 text-sm">
                    <dt className="text-zinc-500">{titleCase(key)}</dt>
                    <dd className="text-right text-zinc-200">
                      {value === null || value === "" ? "—" : String(value)}
                    </dd>
                  </div>
                ))}
              </dl>
            </Panel>
          )}
          {call.evaluations.length > 0 && (
            <Panel title="Custom checks">
              <div className="space-y-3">
                {call.evaluations.map((ev) => (
                  <div key={ev.evaluator_id} className="text-sm">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-zinc-300">{ev.evaluator_name}</span>
                      {ev.passed === null ? (
                        <span className="text-xs text-zinc-500">error</span>
                      ) : ev.passed ? (
                        <span className="rounded-full bg-emerald-500/15 px-2 py-0.5 text-xs font-medium text-emerald-400">Pass</span>
                      ) : (
                        <span className="rounded-full bg-red-500/15 px-2 py-0.5 text-xs font-medium text-red-400">Fail</span>
                      )}
                    </div>
                    {ev.reason && <p className="mt-0.5 text-xs text-zinc-500">{ev.reason}</p>}
                  </div>
                ))}
              </div>
            </Panel>
          )}
          {call.quality && (
            <Panel title="Conversation quality">
              <dl className="space-y-2 text-sm">
                <div className="flex justify-between">
                  <dt className="text-zinc-500">Interruptions</dt>
                  <dd className="text-zinc-200">{call.quality.interruption_count ?? 0}</dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-zinc-500">Longest silence</dt>
                  <dd className="text-zinc-200">
                    {call.quality.max_silence_seconds != null ? `${call.quality.max_silence_seconds}s` : "—"}
                  </dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-zinc-500">Talk ratio (agent : caller)</dt>
                  <dd className="text-zinc-200">
                    {call.quality.talk_ratio != null ? `${call.quality.talk_ratio} : 1` : "—"}
                  </dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-zinc-500">Agent pace</dt>
                  <dd className="text-zinc-200">
                    {call.quality.assistant_wpm != null ? `${call.quality.assistant_wpm} wpm` : "—"}
                  </dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-zinc-500">Longest agent monologue</dt>
                  <dd className="text-zinc-200">
                    {call.quality.longest_monologue_words != null
                      ? `${call.quality.longest_monologue_words} words`
                      : "—"}
                  </dd>
                </div>
              </dl>
            </Panel>
          )}
          {call.metadata && Object.keys(call.metadata).length > 0 && (
            <Panel title="Metadata">
              <pre className="overflow-x-auto text-xs text-zinc-400">
                {JSON.stringify(call.metadata, null, 2)}
              </pre>
            </Panel>
          )}
        </div>

        <div className="lg:col-span-3">
          <Panel title={`Transcript — ${call.turns.length} turns`}>
            <div className="space-y-3">
              {call.turns.map((turn) => {
                const isActive = activeTurn?.idx === turn.idx && call.has_recording;
                return (
                  <div
                    key={turn.idx}
                    onClick={() => {
                      if (audioRef.current && turn.start_time != null) {
                        audioRef.current.currentTime = turn.start_time;
                        audioRef.current.play();
                      }
                    }}
                    className={`rounded-lg p-3 text-sm ${
                      call.has_recording && turn.start_time != null ? "cursor-pointer" : ""
                    } ${
                      turn.role === "assistant"
                        ? "bg-indigo-500/5 border border-indigo-500/15"
                        : "bg-zinc-800/40 border border-zinc-700/30"
                    } ${isActive ? "ring-1 ring-indigo-400" : ""}`}
                  >
                    <div className="mb-1 flex items-center gap-2 text-xs">
                      <span
                        className={
                          turn.role === "assistant" ? "font-medium text-indigo-300" : "font-medium text-zinc-300"
                        }
                      >
                        {turn.role === "assistant" ? "Assistant" : "Caller"}
                      </span>
                      {turn.start_time != null && (
                        <span className="text-zinc-600">{formatClock(turn.start_time)}</span>
                      )}
                      {turn.latency_ms != null && (
                        <span
                          className="text-zinc-600"
                          title={[
                            turn.stt_ms != null ? `STT ${Math.round(turn.stt_ms)}ms` : null,
                            turn.llm_ttft_ms != null ? `LLM ${Math.round(turn.llm_ttft_ms)}ms` : null,
                            turn.tts_ttfb_ms != null ? `TTS ${Math.round(turn.tts_ttfb_ms)}ms` : null,
                          ]
                            .filter(Boolean)
                            .join(" · ")}
                        >
                          · {Math.round(turn.latency_ms)}ms
                        </span>
                      )}
                      {turn.interrupted && <span className="text-amber-500">· interrupted</span>}
                    </div>
                    <p className="leading-relaxed text-zinc-200">
                      {showTranslation && turn.translated_text ? turn.translated_text : turn.text}
                    </p>
                    {turn.tool_calls && turn.tool_calls.length > 0 && (
                      <div className="mt-2 space-y-1" onClick={(e) => e.stopPropagation()}>
                        {turn.tool_calls.map((tc, i) => {
                          const key = `${turn.idx}-${i}`;
                          const expanded = expandedTools.has(key);
                          const failed = tc.success === false;
                          return (
                            <div key={key}>
                              <button
                                onClick={() => toggleTool(key)}
                                className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs font-medium ${
                                  failed
                                    ? "border-red-800/60 bg-red-950/30 text-red-300"
                                    : "border-amber-800/50 bg-amber-950/20 text-amber-300"
                                }`}
                              >
                                🔧 {tc.name}
                                {failed ? " · failed" : ""}
                                <span className="text-zinc-500">{expanded ? "▲" : "▼"}</span>
                              </button>
                              {expanded && (
                                <dl className="mt-1 space-y-1 rounded-md border border-zinc-700/40 bg-zinc-950/40 p-2 text-xs">
                                  <div className="flex gap-2">
                                    <dt className="shrink-0 text-zinc-500">Arguments</dt>
                                    <dd className="break-all text-zinc-300">
                                      {formatToolValue(tc.arguments)}
                                    </dd>
                                  </div>
                                  <div className="flex gap-2">
                                    <dt className="shrink-0 text-zinc-500">Result</dt>
                                    <dd className="break-all text-zinc-300">
                                      {formatToolValue(tc.result)}
                                    </dd>
                                  </div>
                                </dl>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>
                );
              })}
              {call.turns.length === 0 && (
                <p className="py-6 text-center text-sm text-zinc-500">No transcript available.</p>
              )}
            </div>
          </Panel>
        </div>
      </div>
    </div>
  );
}

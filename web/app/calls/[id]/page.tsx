"use client";

import { use, useRef, useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import { fetcher, type CallDetail } from "@/lib/api";
import { formatClock, formatDate, formatDuration, titleCase } from "@/lib/format";
import { EndReasonBadge, SentimentBadge, StatusBadge, SuccessBadge } from "@/components/Badges";

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

  if (!call) return <div className="p-8 text-zinc-500">Loading…</div>;

  const activeTurn = call.turns.findLast?.(
    (t) => t.start_time != null && t.start_time <= currentTime
  );

  async function reanalyze() {
    setReanalyzing(true);
    try {
      await fetch(`/api/v1/calls/${id}/reanalyze`, { method: "POST" });
      await mutate();
    } finally {
      setReanalyzing(false);
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
            {call.analysis_status === "completed" && <SuccessBadge success={call.success} />}
            <SentimentBadge label={call.sentiment_label} />
            <EndReasonBadge reason={call.end_reason} />
            <span className="text-xs text-zinc-500">
              {formatDuration(call.duration_seconds)} · {titleCase(call.direction)}
              {call.from_number ? ` · ${call.from_number}` : ""}
            </span>
          </div>
        </div>
        <button
          onClick={reanalyze}
          disabled={reanalyzing || ["pending", "processing"].includes(call.analysis_status)}
          className="rounded-lg border border-zinc-700 px-3 py-1.5 text-sm text-zinc-300 hover:bg-zinc-900 disabled:opacity-40"
        >
          {["pending", "processing"].includes(call.analysis_status) ? "Analyzing…" : "Re-analyze"}
        </button>
      </div>

      {call.has_recording && (
        <audio
          ref={audioRef}
          controls
          src={`/api/v1/calls/${call.id}/audio`}
          className="w-full"
          onTimeUpdate={(e) => setCurrentTime(e.currentTarget.currentTime)}
        />
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
                        <span className="text-zinc-600">· {Math.round(turn.latency_ms)}ms</span>
                      )}
                      {turn.interrupted && <span className="text-amber-500">· interrupted</span>}
                    </div>
                    <p className="leading-relaxed text-zinc-200">{turn.text}</p>
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

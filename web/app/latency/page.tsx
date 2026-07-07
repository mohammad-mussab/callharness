"use client";

import useSWR from "swr";
import { fetcher, type LatencyStats } from "@/lib/api";
import StatCard from "@/components/StatCard";
import { LatencyTrend } from "@/components/charts";

function ms(value: number | null | undefined): string {
  return value == null ? "—" : `${Math.round(value)}ms`;
}

const COMPONENT_LABELS: Record<string, string> = {
  stt: "Speech-to-text",
  llm: "LLM first token",
  tts: "TTS first byte",
};

const COMPONENT_COLORS: Record<string, string> = {
  stt: "bg-sky-500",
  llm: "bg-indigo-500",
  tts: "bg-fuchsia-500",
};

export default function LatencyPage() {
  const { data } = useSWR<LatencyStats>("/api/v1/analytics/latency?days=14", fetcher, {
    refreshInterval: 30000,
  });

  const maxAvg = data
    ? Math.max(...Object.values(data.components).map((c) => c.avg ?? 0), 1)
    : 1;

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-zinc-100">Latency & Quality</h1>
        <p className="text-sm text-zinc-500">
          Assistant response latency and conversation quality, last 14 days
          {data ? ` · ${data.turn_count} assistant turns` : ""}
        </p>
      </div>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatCard label="Avg response" value={data ? ms(data.e2e.avg) : "…"} />
        <StatCard label="p50" value={data ? ms(data.e2e.p50) : "…"} />
        <StatCard label="p95" value={data ? ms(data.e2e.p95) : "…"} />
        <StatCard label="p99" value={data ? ms(data.e2e.p99) : "…"} />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4">
          <h2 className="mb-3 text-sm font-medium text-zinc-300">
            Response latency trend <span className="text-zinc-500">(p50 / p95)</span>
          </h2>
          {data && <LatencyTrend data={data.daily} />}
        </div>

        <div className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4">
          <h2 className="mb-3 text-sm font-medium text-zinc-300">Component breakdown</h2>
          <div className="space-y-4 pt-2">
            {data &&
              Object.entries(data.components).map(([key, stats]) => (
                <div key={key}>
                  <div className="mb-1 flex items-center justify-between text-sm">
                    <span className="text-zinc-400">{COMPONENT_LABELS[key] ?? key}</span>
                    <span className="text-zinc-300">
                      avg {ms(stats.avg)} <span className="text-zinc-600">· p95 {ms(stats.p95)}</span>
                    </span>
                  </div>
                  <div className="h-2 rounded-full bg-zinc-800">
                    <div
                      className={`h-2 rounded-full ${COMPONENT_COLORS[key] ?? "bg-zinc-500"}`}
                      style={{ width: `${Math.min(100, ((stats.avg ?? 0) / maxAvg) * 100)}%` }}
                    />
                  </div>
                </div>
              ))}
            <p className="pt-2 text-xs text-zinc-600">
              Send per-turn component latency from the SDK (stt_ms, llm_ttft_ms, tts_ttfb_ms) —
              the Pipecat metrics observer captures these automatically.
            </p>
          </div>
        </div>
      </div>

      <div>
        <h2 className="mb-3 text-sm font-medium text-zinc-300">Conversation quality</h2>
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          <StatCard
            label="Interruptions / call"
            value={data?.quality.avg_interruptions != null ? String(data.quality.avg_interruptions) : "—"}
          />
          <StatCard
            label="Calls with 3s+ silence"
            value={
              data?.quality.pct_calls_with_long_silence != null
                ? `${Math.round(data.quality.pct_calls_with_long_silence * 100)}%`
                : "—"
            }
          />
          <StatCard
            label="Talk ratio (agent : caller)"
            value={data?.quality.avg_talk_ratio != null ? `${data.quality.avg_talk_ratio} : 1` : "—"}
          />
          <StatCard
            label="Agent pace"
            value={data?.quality.avg_assistant_wpm != null ? `${data.quality.avg_assistant_wpm} wpm` : "—"}
          />
        </div>
      </div>
    </div>
  );
}

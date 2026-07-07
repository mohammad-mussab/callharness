"use client";

import useSWR from "swr";
import { fetcher, type Overview, type TimeseriesPoint } from "@/lib/api";
import { formatDuration, formatPercent } from "@/lib/format";
import StatCard from "@/components/StatCard";
import { ReasonBars, SentimentDonut, SuccessChart, VolumeChart } from "@/components/charts";
import { useState } from "react";

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4">
      <h2 className="mb-3 text-sm font-medium text-zinc-300">{title}</h2>
      {children}
    </div>
  );
}

export default function DashboardPage() {
  const [agent, setAgent] = useState("");
  const agentQuery = agent ? `?agent_id=${encodeURIComponent(agent)}` : "";
  const { data: overview } = useSWR<Overview>(`/api/v1/analytics/overview${agentQuery}`, fetcher, {
    refreshInterval: 15000,
  });
  const { data: series } = useSWR<TimeseriesPoint[]>(
    `/api/v1/analytics/timeseries?days=14${agent ? `&agent_id=${encodeURIComponent(agent)}` : ""}`,
    fetcher,
    { refreshInterval: 30000 }
  );

  const sentimentValue =
    overview?.avg_sentiment_score == null
      ? "—"
      : overview.avg_sentiment_score.toFixed(2);

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-zinc-100">Dashboard</h1>
          <p className="text-sm text-zinc-500">Last 14 days of call activity</p>
        </div>
        <select
          value={agent}
          onChange={(e) => setAgent(e.target.value)}
          className="rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-1.5 text-sm text-zinc-300"
        >
          <option value="">All agents</option>
          {overview?.agents.map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </select>
      </div>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
        <StatCard label="Total calls" value={overview ? String(overview.total_calls) : "…"} />
        <StatCard
          label="Success rate"
          value={overview ? formatPercent(overview.success_rate) : "…"}
          hint={overview ? `${overview.analyzed_calls} calls analyzed` : undefined}
        />
        <StatCard
          label="Avg duration"
          value={overview ? formatDuration(overview.avg_duration_seconds) : "…"}
        />
        <StatCard
          label="Avg sentiment"
          value={overview ? sentimentValue : "…"}
          hint="-1.0 to 1.0"
        />
        <StatCard
          label="Transfer rate"
          value={overview ? formatPercent(overview.transfer_rate) : "…"}
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="Call volume">{series ? <VolumeChart data={series} /> : null}</Panel>
        <Panel title="Success rate">{series ? <SuccessChart data={series} /> : null}</Panel>
        <Panel title="Sentiment distribution">
          {overview ? <SentimentDonut distribution={overview.sentiment_distribution} /> : null}
        </Panel>
        <Panel title="End reasons">
          {overview ? <ReasonBars data={overview.end_reason_breakdown} /> : null}
        </Panel>
      </div>
    </div>
  );
}

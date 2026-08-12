"use client";

import { useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import { fetcher, type CallList, type Overview } from "@/lib/api";
import { formatDate, formatDuration } from "@/lib/format";
import { label } from "@/lib/labels";
import {
  EndReasonBadge,
  NonCompletionReasonBadge,
  OutcomeBadge,
  SentimentBadge,
  StatusBadge,
  TransferReasonBadge,
} from "@/components/Badges";

const PAGE_SIZE = 25;

export default function CallsPage() {
  const [agent, setAgent] = useState("");
  const [outcome, setOutcome] = useState("");
  const [sentiment, setSentiment] = useState("");
  const [transferReason, setTransferReason] = useState("");
  const [nonCompletionReason, setNonCompletionReason] = useState("");
  const [q, setQ] = useState("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(0);

  const params = new URLSearchParams();
  if (agent) params.set("agent_id", agent);
  if (outcome) params.set("outcome", outcome);
  if (sentiment) params.set("sentiment", sentiment);
  if (transferReason) params.set("transfer_reason", transferReason);
  if (nonCompletionReason) params.set("non_completion_reason", nonCompletionReason);
  if (search) params.set("q", search);
  params.set("limit", String(PAGE_SIZE));
  params.set("offset", String(page * PAGE_SIZE));

  const { data } = useSWR<CallList>(`/api/v1/calls?${params}`, fetcher, {
    refreshInterval: 15000,
    keepPreviousData: true,
  });
  const { data: overview } = useSWR<Overview>("/api/v1/analytics/overview", fetcher);

  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;

  const selectClass =
    "rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-1.5 text-sm text-zinc-300";

  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <div>
        <h1 className="text-xl font-semibold text-zinc-100">Calls</h1>
        <p className="text-sm text-zinc-500">{data ? `${data.total} calls` : "Loading…"}</p>
      </div>

      <form
        className="flex flex-wrap items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          setPage(0);
          setSearch(q);
        }}
      >
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search transcripts & summaries…"
          className="w-64 rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-1.5 text-sm text-zinc-300 placeholder:text-zinc-600"
        />
        <select value={agent} onChange={(e) => { setAgent(e.target.value); setPage(0); }} className={selectClass}>
          <option value="">All agents</option>
          {overview?.agents.map((a) => (
            <option key={a} value={a}>{a}</option>
          ))}
        </select>
        <select value={outcome} onChange={(e) => { setOutcome(e.target.value); setPage(0); }} className={selectClass}>
          <option value="">Any outcome</option>
          <option value="completed">Completed</option>
          <option value="transferred">Transferred</option>
          <option value="non_completed">Non-completed</option>
        </select>
        <select value={sentiment} onChange={(e) => { setSentiment(e.target.value); setPage(0); }} className={selectClass}>
          <option value="">Any sentiment</option>
          <option value="positive">Positive</option>
          <option value="neutral">Neutral</option>
          <option value="negative">Negative</option>
        </select>
        {(overview?.transfer_reason_breakdown?.length ?? 0) > 0 && (
          <select
            value={transferReason}
            onChange={(e) => { setTransferReason(e.target.value); setPage(0); }}
            className={selectClass}
          >
            <option value="">Any transfer reason</option>
            {overview!.transfer_reason_breakdown.map((r) => (
              <option key={r.reason} value={r.reason}>{label(r.reason)} ({r.count})</option>
            ))}
          </select>
        )}
        {(overview?.non_completion_reason_breakdown?.length ?? 0) > 0 && (
          <select
            value={nonCompletionReason}
            onChange={(e) => { setNonCompletionReason(e.target.value); setPage(0); }}
            className={selectClass}
          >
            <option value="">Any non-completion reason</option>
            {overview!.non_completion_reason_breakdown.map((r) => (
              <option key={r.reason} value={r.reason}>{label(r.reason)} ({r.count})</option>
            ))}
          </select>
        )}
        <button type="submit" className="rounded-lg bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500">
          Filter
        </button>
      </form>

      <div className="overflow-x-auto rounded-xl border border-zinc-800">
        <table className="w-full text-sm">
          <thead className="bg-zinc-900 text-left text-xs uppercase tracking-wide text-zinc-500">
            <tr>
              <th className="px-4 py-2.5">Time</th>
              <th className="px-4 py-2.5">Agent</th>
              <th className="px-4 py-2.5">Duration</th>
              <th className="px-4 py-2.5">Outcome</th>
              <th className="px-4 py-2.5">Sentiment</th>
              <th className="px-4 py-2.5">End reason</th>
              <th className="px-4 py-2.5">Summary</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-800/70">
            {data?.items.map((call) => (
              <tr key={call.id} className="group hover:bg-zinc-900/70">
                <td className="whitespace-nowrap px-4 py-2.5">
                  <Link href={`/calls/${call.id}`} className="text-indigo-400 group-hover:underline">
                    {formatDate(call.started_at)}
                  </Link>
                </td>
                <td className="whitespace-nowrap px-4 py-2.5 text-zinc-400">{call.agent_id}</td>
                <td className="whitespace-nowrap px-4 py-2.5 text-zinc-400">
                  {formatDuration(call.duration_seconds)}
                </td>
                <td className="whitespace-nowrap px-4 py-2.5">
                  {call.analysis_status === "completed" ? (
                    <OutcomeBadge outcome={call.outcome} />
                  ) : (
                    <StatusBadge status={call.analysis_status} />
                  )}
                </td>
                <td className="whitespace-nowrap px-4 py-2.5">
                  <SentimentBadge label={call.sentiment_label} />
                </td>
                <td className="whitespace-nowrap px-4 py-2.5">
                  <div className="flex flex-wrap items-center gap-1">
                    <EndReasonBadge reason={call.end_reason} />
                    <TransferReasonBadge reason={call.transfer_reason} source={call.reason_source} />
                    <NonCompletionReasonBadge
                      reason={call.non_completion_reason}
                      source={call.reason_source}
                    />
                    {/* One placeholder for the whole cell, so the column never looks
                        broken on a call with no reason at all — but never a dash
                        sitting in front of a badge that is present. */}
                    {!call.end_reason &&
                      !call.transfer_reason &&
                      !call.non_completion_reason && (
                        <span className="text-xs text-zinc-500">—</span>
                      )}
                  </div>
                </td>
                <td className="max-w-md truncate px-4 py-2.5 text-zinc-400">
                  {call.summary ?? <span className="text-zinc-600">No summary</span>}
                </td>
              </tr>
            ))}
            {data && data.items.length === 0 && (
              <tr>
                <td colSpan={7} className="px-4 py-10 text-center text-zinc-500">
                  No calls match these filters.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="flex items-center justify-between text-sm text-zinc-500">
        <span>
          Page {page + 1} of {totalPages}
        </span>
        <div className="flex gap-2">
          <button
            disabled={page === 0}
            onClick={() => setPage((p) => p - 1)}
            className="rounded-lg border border-zinc-800 px-3 py-1.5 hover:bg-zinc-900 disabled:opacity-40"
          >
            Previous
          </button>
          <button
            disabled={page + 1 >= totalPages}
            onClick={() => setPage((p) => p + 1)}
            className="rounded-lg border border-zinc-800 px-3 py-1.5 hover:bg-zinc-900 disabled:opacity-40"
          >
            Next
          </button>
        </div>
      </div>
    </div>
  );
}

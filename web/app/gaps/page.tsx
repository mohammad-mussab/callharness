"use client";

import { useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import { fetcher, type KnowledgeGaps, type Overview } from "@/lib/api";
import { formatDate } from "@/lib/format";

const selectClass =
  "rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-1.5 text-sm text-zinc-300";

export default function GapsPage() {
  const [agent, setAgent] = useState("");
  const [days, setDays] = useState("7");
  const [minCount, setMinCount] = useState("1");
  const [copied, setCopied] = useState(false);

  const params = new URLSearchParams({ days, min_count: minCount });
  if (agent) params.set("agent_id", agent);

  const { data, isLoading } = useSWR<KnowledgeGaps>(
    `/api/v1/analytics/knowledge-gaps?${params}`,
    fetcher,
    { keepPreviousData: true }
  );
  const { data: overview } = useSWR<Overview>("/api/v1/analytics/overview", fetcher);

  // The point of this page is the message that gets sent to whoever owns the data,
  // so the report is generated in the exact shape it would be pasted into an email.
  function reportText(d: KnowledgeGaps) {
    const lines = [
      `Missing information — last ${d.window_days} days`,
      "",
      `${d.calls_with_gaps} of ${d.calls_scanned} calls reached a question we could not answer`,
      "because the information is not in the system.",
      "",
      "Adding the following would let the assistant answer these calls itself:",
      "",
    ];
    d.groups.forEach((g, i) => {
      lines.push(`${i + 1}. ${g.question}`);
      lines.push(
        `   asked ${g.count}x · ${g.transferred} transferred to an operator`
      );
      g.examples.forEach((e) =>
        lines.push(`   verify: call ${e.external_id ?? e.call_id} (${formatDate(e.started_at)})`)
      );
      lines.push("");
    });
    return lines.join("\n");
  }

  async function copyReport() {
    if (!data) return;
    await navigator.clipboard.writeText(reportText(data));
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  const pct = data?.gap_call_rate == null ? null : Math.round(data.gap_call_rate * 100);

  return (
    <div className="mx-auto max-w-5xl space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-zinc-100">Missing Information</h1>
        <p className="max-w-3xl text-sm text-zinc-500">
          Questions callers asked that the assistant could not answer because the record
          isn&apos;t in the system — the lookup ran and came back empty. These transfers
          disappear by adding data, not by changing the agent. Technical failures are
          excluded; they belong on the engineering side, not here.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <select value={agent} onChange={(e) => setAgent(e.target.value)} className={selectClass}>
          <option value="">All regions</option>
          {overview?.agents.map((a) => (
            <option key={a} value={a}>{a}</option>
          ))}
        </select>
        <select value={days} onChange={(e) => setDays(e.target.value)} className={selectClass}>
          <option value="1">Last 24 hours</option>
          <option value="7">Last 7 days</option>
          <option value="30">Last 30 days</option>
          <option value="90">Last 90 days</option>
        </select>
        <select value={minCount} onChange={(e) => setMinCount(e.target.value)} className={selectClass}>
          <option value="1">All questions</option>
          <option value="2">Asked 2+ times</option>
          <option value="5">Asked 5+ times</option>
        </select>
        {data && data.groups.length > 0 && (
          <button
            onClick={copyReport}
            className="ml-auto rounded-lg bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500"
          >
            {copied ? "Copied ✓" : "Copy report for email"}
          </button>
        )}
      </div>

      {isLoading && !data && <p className="text-sm text-zinc-500">Loading…</p>}

      {data && data.calls_scanned === 0 && (
        <div className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-8 text-center text-sm text-zinc-500">
          No calls in this window yet.
        </div>
      )}

      {data && data.calls_scanned > 0 && (
        <>
          <div className="grid gap-3 sm:grid-cols-3">
            <div className="rounded-xl border border-amber-900/60 bg-amber-950/20 p-4">
              <div className="text-xs uppercase tracking-wide text-amber-500/80">
                Calls blocked by missing data
              </div>
              <div className="mt-1 text-2xl font-semibold tabular-nums text-amber-300">
                {pct == null ? "—" : `${pct}%`}
              </div>
              <div className="mt-1 text-xs text-zinc-500">
                {data.calls_with_gaps} of {data.calls_scanned} calls
              </div>
            </div>
            <div className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4">
              <div className="text-xs uppercase tracking-wide text-zinc-500">
                Distinct gaps
              </div>
              <div className="mt-1 text-2xl font-semibold tabular-nums text-zinc-100">
                {data.groups.length}
              </div>
              <div className="mt-1 text-xs text-zinc-500">records to add</div>
            </div>
            <div className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4">
              <div className="text-xs uppercase tracking-wide text-zinc-500">
                Transfers caused
              </div>
              <div className="mt-1 text-2xl font-semibold tabular-nums text-zinc-100">
                {data.groups.reduce((n, g) => n + g.transferred, 0)}
              </div>
              <div className="mt-1 text-xs text-zinc-500">avoidable by adding data</div>
            </div>
          </div>

          {data.groups.length === 0 ? (
            <div className="rounded-xl border border-emerald-900/50 bg-emerald-950/20 p-6 text-center text-sm text-emerald-300">
              No missing information found in this window.
            </div>
          ) : (
            <ol className="space-y-2">
              {data.groups.map((g, i) => (
                <li
                  key={`${g.tool}-${g.question}-${i}`}
                  className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4"
                >
                  <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                    <span className="text-xs tabular-nums text-zinc-600">{i + 1}</span>
                    <span className="flex-1 text-sm font-medium text-zinc-100">
                      {g.question}
                    </span>
                    <span className="rounded-full bg-zinc-700/40 px-2 py-0.5 text-xs tabular-nums text-zinc-300">
                      asked {g.count}×
                    </span>
                    {g.transferred > 0 && (
                      <span className="rounded-full bg-violet-500/15 px-2 py-0.5 text-xs tabular-nums text-violet-300">
                        {g.transferred} transferred
                      </span>
                    )}
                  </div>

                  {g.variants.length > 1 && (
                    <div className="mt-2 text-xs text-zinc-500">
                      also asked as:{" "}
                      {g.variants.filter((v) => v !== g.question).map((v, j) => (
                        <span key={j} className="text-zinc-400">
                          {j > 0 && " · "}&ldquo;{v}&rdquo;
                        </span>
                      ))}
                    </div>
                  )}

                  <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-zinc-500">
                    <span>verify in call:</span>
                    {g.examples.map((e) => (
                      <Link
                        key={e.call_id}
                        href={`/calls/${e.call_id}`}
                        className="rounded bg-zinc-800 px-1.5 py-0.5 font-mono text-indigo-400 hover:bg-zinc-700"
                        title={`${formatDate(e.started_at)} · ${e.agent_id}`}
                      >
                        {e.external_id ?? e.call_id.slice(0, 12)}
                      </Link>
                    ))}
                    <span className="text-zinc-600">· lookup: {g.tool}</span>
                  </div>
                </li>
              ))}
            </ol>
          )}
        </>
      )}
    </div>
  );
}

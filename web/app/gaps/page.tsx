"use client";

import { useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import {
  apiSend,
  fetcher,
  type GapGrouping,
  type KnowledgeGap,
  type KnowledgeGaps,
  type Overview,
} from "@/lib/api";
import { formatDate } from "@/lib/format";

const selectClass =
  "rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-1.5 text-sm text-zinc-300";

export default function GapsPage() {
  const [agent, setAgent] = useState("");
  const [days, setDays] = useState("7");
  const [minCount, setMinCount] = useState("1");
  const [copied, setCopied] = useState(false);
  const [grouping, setGrouping] = useState(false);
  const [result, setResult] = useState<GapGrouping | null>(null);
  const [error, setError] = useState<string | null>(null);

  const params = new URLSearchParams({ days, min_count: minCount });
  if (agent) params.set("agent_id", agent);

  const { data: raw, isLoading, mutate } = useSWR<KnowledgeGaps>(
    `/api/v1/analytics/knowledge-gaps?${params}`,
    fetcher,
    { keepPreviousData: true }
  );
  const { data: overview } = useSWR<Overview>("/api/v1/analytics/overview", fetcher);

  // A dashboard built from this checkout is routinely pointed at a backend that has not
  // been redeployed yet — that is the whole point of web/.env.local forwarding to the
  // VM's API, so a UI change can be judged against real calls before it ships. An older
  // backend simply omits the fields added here, so default them rather than letting the
  // page die on `undefined.length` when the two halves are out of step.
  const data: KnowledgeGaps | undefined = raw && {
    ...raw,
    groups: raw.groups ?? [],
    needs_review: raw.needs_review ?? [],
    ungrouped_count: raw.ungrouped_count ?? 0,
  };

  // Only meaningful once something has been merged: before that every row is one call
  // and every count is 1, so the filter would empty the page rather than narrow it.
  const hasGrouping = (data?.groups ?? []).some((g) => g.grouped);

  // The server handles a bounded batch per request — a reasoning model over every
  // ungrouped question at once is slow enough to time out, and a reply that long can come
  // back truncated. So keep pressing on the user's behalf until nothing is left, showing
  // the running total. Progress is reported after each batch rather than at the end,
  // because each one takes a while and a silent button looks broken.
  async function runGrouping() {
    setGrouping(true);
    setError(null);
    setResult(null);
    const total: GapGrouping = {
      considered: 0, grouped: 0, needs_review: 0, new_groups: 0,
      remaining: 0, warnings: [],
    };
    try {
      // Bounded so a server that always reports work remaining cannot spin forever.
      for (let pass = 0; pass < 40; pass++) {
        const params = new URLSearchParams({ days });
        if (agent) params.set("agent_id", agent);
        const res = (await apiSend(
          `/api/v1/analytics/knowledge-gaps/group?${params}`,
          "POST"
        )) as GapGrouping;

        total.considered += res.considered;
        total.grouped += res.grouped;
        total.needs_review += res.needs_review;
        total.new_groups += res.new_groups;
        total.remaining = res.remaining;
        total.warnings = [...total.warnings, ...(res.warnings ?? [])];
        setResult({ ...total });
        await mutate();

        if (res.considered === 0 || res.remaining <= 0) break;
      }
    } catch (e) {
      setError(
        e instanceof Error
          ? `${e.message} — anything already grouped has been saved; press again to continue.`
          : "Grouping failed"
      );
    } finally {
      setGrouping(false);
    }
  }

  async function ungroup(groupId: string) {
    try {
      await apiSend(
        `/api/v1/analytics/knowledge-gaps/group/${encodeURIComponent(groupId)}`,
        "DELETE"
      );
      await mutate();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not ungroup");
    }
  }

  // The point of this page is the message that gets sent to whoever owns the data, so
  // the report is generated in the exact shape it would be pasted into an email. The
  // needs-review set is deliberately absent: nobody can add a record for "curva
  // glicemica" with no attribute, and asking them to wastes their time.
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
      (g.variants ?? [])
        .filter((v) => v !== g.question)
        .forEach((v) => lines.push(`   also asked as: "${v}"`));
      (g.examples ?? []).forEach((e) =>
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
        {hasGrouping && (
          <select
            value={minCount}
            onChange={(e) => setMinCount(e.target.value)}
            className={selectClass}
          >
            <option value="1">All questions</option>
            <option value="2">Asked 2+ times</option>
            <option value="5">Asked 5+ times</option>
          </select>
        )}
        <div className="ml-auto flex items-center gap-2">
          {data && data.ungrouped_count > 0 && (
            <button
              onClick={runGrouping}
              disabled={grouping}
              className="rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm font-medium text-zinc-200 hover:bg-zinc-700 disabled:opacity-50"
              title="Ask the model which of these questions describe the same missing record"
            >
              {grouping
                ? `Grouping… ${result ? `${result.considered} done` : ""}`
                : `Group duplicates (${data.ungrouped_count})`}
            </button>
          )}
          {data && data.groups.length > 0 && (
            <button
              onClick={copyReport}
              className="rounded-lg bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500"
            >
              {copied ? "Copied ✓" : "Copy report for email"}
            </button>
          )}
        </div>
      </div>

      {error && (
        <div className="rounded-lg border border-red-900/60 bg-red-950/30 p-3 text-sm text-red-300">
          {error}
        </div>
      )}

      {result && (
        <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3 text-sm text-zinc-400">
          Looked at {result.considered} question{result.considered === 1 ? "" : "s"}:{" "}
          {result.new_groups} record{result.new_groups === 1 ? "" : "s"} created,{" "}
          {result.grouped} call{result.grouped === 1 ? "" : "s"} merged into a shared
          record, {result.needs_review} sent for human review.
          {result.remaining > 0 && (
            <span className="text-amber-400">
              {" "}
              {result.remaining} left over — press again to continue.
            </span>
          )}
          {result.warnings.length > 0 && (
            <ul className="mt-2 space-y-0.5 text-xs text-amber-400/90">
              {result.warnings.map((w, i) => (
                <li key={i}>· {w}</li>
              ))}
            </ul>
          )}
        </div>
      )}

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
                {hasGrouping ? "Distinct gaps" : "Questions to group"}
              </div>
              <div className="mt-1 text-2xl font-semibold tabular-nums text-zinc-100">
                {data.groups.length}
              </div>
              {/* Before grouping this is one row per call, not a count of records —
                  saying "records to add" here would overstate the work by ~3x. */}
              <div className="mt-1 text-xs text-zinc-500">
                {hasGrouping ? "records to add" : "not merged yet"}
              </div>
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
                <GapRow key={g.group_id ?? g.examples[0]?.call_id ?? i} gap={g} index={i} onUngroup={ungroup} />
              ))}
            </ol>
          )}

          {data.needs_review.length > 0 && (
            <div className="space-y-2 pt-2">
              <div>
                <h2 className="text-sm font-semibold text-zinc-300">
                  Needs human review ({data.needs_review.reduce((n, g) => n + g.count, 0)})
                </h2>
                <p className="max-w-3xl text-xs text-zinc-500">
                  Nobody can add a record for these — the question was mis-heard, names a
                  subject without saying what was wanted about it, or is a search string
                  the software generated rather than something a caller said. They are
                  kept out of the report and its counts; open the calls to see what was
                  really asked.
                </p>
              </div>
              <ul className="space-y-1.5">
                {data.needs_review.map((g, i) => (
                  <li
                    key={g.examples[0]?.call_id ?? i}
                    className="rounded-lg border border-zinc-800/80 bg-zinc-900/40 p-3"
                  >
                    <div className="text-sm text-zinc-400">{g.question}</div>
                    <div className="mt-1.5 flex flex-wrap items-center gap-2 text-xs text-zinc-600">
                      {(g.examples ?? []).map((e) => (
                        <Link
                          key={e.call_id}
                          href={`/calls/${e.call_id}`}
                          className="rounded bg-zinc-800 px-1.5 py-0.5 font-mono text-indigo-400/80 hover:bg-zinc-700"
                          title={`${formatDate(e.started_at)} · ${e.agent_id}`}
                        >
                          {e.external_id ?? e.call_id.slice(0, 12)}
                        </Link>
                      ))}
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function GapRow({
  gap,
  index,
  onUngroup,
}: {
  gap: KnowledgeGap;
  index: number;
  onUngroup: (groupId: string) => void;
}) {
  const others = (gap.variants ?? []).filter((v) => v !== gap.question);
  const examples = gap.examples ?? [];
  return (
    <li className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="text-xs tabular-nums text-zinc-600">{index + 1}</span>
        <span className="flex-1 text-sm font-medium text-zinc-100">{gap.question}</span>
        {gap.count > 1 && (
          <span className="rounded-full bg-zinc-700/40 px-2 py-0.5 text-xs tabular-nums text-zinc-300">
            asked {gap.count}×
          </span>
        )}
        {gap.transferred > 0 && (
          <span className="rounded-full bg-violet-500/15 px-2 py-0.5 text-xs tabular-nums text-violet-300">
            {gap.transferred} transferred
          </span>
        )}
        {/* Grouping never re-judges a call it has already placed, so this is the only
            way to undo a wrong merge. It costs nothing — the calls simply return to the
            ungrouped pool for the next pass. */}
        {gap.grouped && gap.count > 1 && gap.group_id && (
          <button
            onClick={() => onUngroup(gap.group_id!)}
            className="rounded-full border border-zinc-700 px-2 py-0.5 text-xs text-zinc-500 hover:border-zinc-600 hover:text-zinc-300"
            title="Split this back into separate questions"
          >
            ungroup
          </button>
        )}
      </div>

      {others.length > 0 && (
        <div className="mt-2 text-xs text-zinc-500">
          also asked as:{" "}
          {others.map((v, j) => (
            <span key={j} className="text-zinc-400">
              {j > 0 && " · "}&ldquo;{v}&rdquo;
            </span>
          ))}
        </div>
      )}

      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-zinc-500">
        <span>verify in call:</span>
        {examples.map((e) => (
          <Link
            key={e.call_id}
            href={`/calls/${e.call_id}`}
            className="rounded bg-zinc-800 px-1.5 py-0.5 font-mono text-indigo-400 hover:bg-zinc-700"
            title={`${formatDate(e.started_at)} · ${e.agent_id}`}
          >
            {e.external_id ?? e.call_id.slice(0, 12)}
          </Link>
        ))}
        <span className="text-zinc-600">· lookup: {gap.tool}</span>
      </div>
    </li>
  );
}

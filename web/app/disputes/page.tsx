"use client";

import { useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import { fetcher, type Disputes, type Overview } from "@/lib/api";
import { formatDate, formatDuration, titleCase } from "@/lib/format";
import { OutcomeBadge } from "@/components/Badges";

const selectClass =
  "rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-1.5 text-sm text-zinc-300";

function Stat({
  label,
  value,
  hint,
  tone = "normal",
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: "normal" | "bad";
}) {
  return (
    <div
      className={`rounded-xl border p-4 ${
        tone === "bad" ? "border-red-900/60 bg-red-950/20" : "border-zinc-800 bg-zinc-900/60"
      }`}
    >
      <div className="text-xs uppercase tracking-wide text-zinc-500">{label}</div>
      <div
        className={`mt-1 text-2xl font-semibold tabular-nums ${
          tone === "bad" ? "text-red-400" : "text-zinc-100"
        }`}
      >
        {value}
      </div>
      {hint && <div className="mt-1 text-xs text-zinc-500">{hint}</div>}
    </div>
  );
}

export default function DisputesPage() {
  const [agent, setAgent] = useState("");
  const [kind, setKind] = useState("");

  const params = new URLSearchParams();
  if (agent) params.set("agent_id", agent);
  if (kind) params.set("kind", kind);

  const { data, isLoading } = useSWR<Disputes>(
    `/api/v1/analytics/disputes?${params}`,
    fetcher,
    { refreshInterval: 30000, keepPreviousData: true }
  );
  const { data: overview } = useSWR<Overview>("/api/v1/analytics/overview", fetcher);

  const rate = data?.agreement_rate;

  return (
    <div className="mx-auto max-w-6xl space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-zinc-100">Disputed Calls</h1>
        <p className="max-w-3xl text-sm text-zinc-500">
          Calls where your agent&apos;s own verdict and OpenCall&apos;s analysis disagree.
          Your agent judges from the transcript alone; OpenCall also sees the tool calls
          and whether they failed. When the two differ, one of them is wrong — and these
          are the calls worth listening to.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <select
          value={agent}
          onChange={(e) => setAgent(e.target.value)}
          className={selectClass}
        >
          <option value="">All agents</option>
          {overview?.agents.map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </select>
        <select value={kind} onChange={(e) => setKind(e.target.value)} className={selectClass}>
          <option value="">All disagreements</option>
          <option value="outcome">Different outcome</option>
          <option value="reason">Same outcome, different reason</option>
        </select>
      </div>

      {data && data.comparable === 0 ? (
        <div className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-8 text-center">
          <p className="text-sm text-zinc-400">No comparable calls yet.</p>
          <p className="mx-auto mt-2 max-w-lg text-sm text-zinc-500">
            A call can only be compared when the agent sends its own verdict as{" "}
            <code className="text-zinc-400">agent_esito</code> in the call metadata
            <em> and</em> OpenCall has finished analysing it.
          </p>
        </div>
      ) : (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
            <Stat
              label="Comparable"
              value={data ? String(data.comparable) : "—"}
              hint="both judges ruled"
            />
            <Stat
              label="Agreement"
              value={rate == null ? "—" : `${Math.round(rate * 100)}%`}
              hint={data ? `${data.agreed} agreed` : undefined}
            />
            <Stat
              label="Different outcome"
              value={data ? String(data.disputed_outcome) : "—"}
              hint="completed vs not, etc."
            />
            <Stat
              label="Different reason"
              value={data ? String(data.disputed_reason) : "—"}
              hint="same bucket, different why"
            />
            <Stat
              label="Overcounted"
              value={data ? String(data.overcounted) : "—"}
              hint="agent said completed, OpenCall didn't"
              tone={data && data.overcounted > 0 ? "bad" : "normal"}
            />
          </div>

          {data && data.overcounted > 0 && (
            <div className="rounded-xl border border-red-900/60 bg-red-950/20 p-4 text-sm text-red-200">
              <strong className="font-semibold">{data.overcounted}</strong> call
              {data.overcounted === 1 ? " was" : "s were"} reported as completed by the
              agent but judged otherwise by OpenCall. This is the direction that inflates
              your success rate — these calls are never reviewed by anyone today.
            </div>
          )}

          {data && data.matrix.length > 0 && (
            <div className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4">
              <div className="mb-3 text-sm font-medium text-zinc-200">
                Agent verdict → OpenCall verdict
              </div>
              <div className="flex flex-wrap gap-2">
                {data.matrix.map((m) => {
                  const agree = m.agent === m.opencall;
                  return (
                    <div
                      key={`${m.agent}-${m.opencall}`}
                      className={`rounded-lg border px-3 py-2 text-xs ${
                        agree
                          ? "border-zinc-800 bg-zinc-900 text-zinc-400"
                          : "border-amber-900/60 bg-amber-950/20 text-amber-300"
                      }`}
                    >
                      <span className="font-medium">{titleCase(m.agent)}</span>
                      <span className="mx-1.5 text-zinc-600">→</span>
                      <span className="font-medium">{titleCase(m.opencall)}</span>
                      <span className="ml-2 tabular-nums text-zinc-500">{m.count}</span>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          <div className="space-y-2">
            {isLoading && !data && <p className="text-sm text-zinc-500">Loading…</p>}
            {data?.items.map((c) => (
              <div
                key={c.id}
                className={`rounded-xl border p-4 ${
                  c.overcount
                    ? "border-red-900/50 bg-red-950/10"
                    : "border-zinc-800 bg-zinc-900/60"
                }`}
              >
                <div className="flex flex-wrap items-center gap-2 text-xs text-zinc-500">
                  <Link
                    href={`/calls/${c.id}`}
                    className="text-indigo-400 hover:underline"
                  >
                    {formatDate(c.started_at)}
                  </Link>
                  <span>·</span>
                  <span>{c.agent_id}</span>
                  <span>·</span>
                  <span>{formatDuration(c.duration_seconds)}</span>
                  {c.overcount && (
                    <span className="rounded-full bg-red-500/15 px-2 py-0.5 font-medium text-red-400">
                      Overcounted
                    </span>
                  )}
                  <span className="rounded-full bg-zinc-700/40 px-2 py-0.5 text-zinc-300">
                    {c.kind === "outcome" ? "Different outcome" : "Different reason"}
                  </span>
                </div>

                <div className="mt-3 grid gap-3 sm:grid-cols-2">
                  <div className="rounded-lg border border-zinc-800 bg-zinc-950/50 p-3">
                    <div className="text-xs uppercase tracking-wide text-zinc-500">
                      Your agent said
                    </div>
                    <div className="mt-1 text-sm font-medium text-zinc-200">
                      {c.agent_esito ?? "—"}
                    </div>
                    {c.agent_motivazione && (
                      <div className="text-xs text-zinc-400">{c.agent_motivazione}</div>
                    )}
                  </div>
                  <div className="rounded-lg border border-zinc-800 bg-zinc-950/50 p-3">
                    <div className="text-xs uppercase tracking-wide text-zinc-500">
                      OpenCall said
                    </div>
                    <div className="mt-1 flex items-center gap-2">
                      <OutcomeBadge outcome={c.opencall_outcome} />
                      {c.opencall_reason && (
                        <span className="text-xs text-zinc-400">
                          {titleCase(c.opencall_reason)}
                        </span>
                      )}
                    </div>
                  </div>
                </div>

                {c.failed_tool_calls.length > 0 && (
                  <div className="mt-3 flex flex-wrap items-center gap-1.5">
                    <span className="text-xs text-zinc-500">
                      Failed tool calls your agent&apos;s judge never saw:
                    </span>
                    {c.failed_tool_calls.map((name, i) => (
                      <code
                        key={`${name}-${i}`}
                        className="rounded bg-red-500/15 px-1.5 py-0.5 text-xs text-red-300"
                      >
                        {name}
                      </code>
                    ))}
                  </div>
                )}

                {(c.success_rationale || c.summary) && (
                  <p className="mt-3 text-sm text-zinc-400">
                    {c.success_rationale ?? c.summary}
                  </p>
                )}
              </div>
            ))}
            {data && data.items.length === 0 && data.comparable > 0 && (
              <div className="rounded-xl border border-emerald-900/50 bg-emerald-950/20 p-6 text-center text-sm text-emerald-300">
                No disagreements in this selection — both judges reached the same verdict
                on all {data.comparable} comparable calls.
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

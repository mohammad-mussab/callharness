"use client";

import { Suspense, useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import useSWR from "swr";
import { fetcher, type CallList, type Overview } from "@/lib/api";
import { CALLS_LIST_QUERY_KEY } from "@/lib/callsList";
import { formatDate, formatDuration } from "@/lib/format";
import { label } from "@/lib/labels";
import {
  BucketBadge,
  OutcomeBadge,
  SentimentBadge,
  StatusBadge,
} from "@/components/Badges";

const PAGE_SIZE = 25;

// Relative presets are stored by name, not as dates, so a bookmarked
// "last 24 hours" view still means the last 24 hours when reopened tomorrow.
const RANGE_PRESETS: Record<string, number> = {
  "1h": 60 * 60 * 1000,
  "3h": 3 * 60 * 60 * 1000,
  "24h": 24 * 60 * 60 * 1000,
  "7d": 7 * 24 * 60 * 60 * 1000,
  "30d": 30 * 24 * 60 * 60 * 1000,
};

const RANGE_OPTIONS = [
  { value: "", label: "All time" },
  { value: "1h", label: "Last hour" },
  { value: "3h", label: "Last 3 hours" },
  { value: "24h", label: "Last 24 hours" },
  { value: "7d", label: "Last 7 days" },
  { value: "30d", label: "Last 30 days" },
  { value: "custom", label: "Custom dates" },
];
const RANGE_LABELS: Record<string, string> = Object.fromEntries(
  RANGE_OPTIONS.filter((o) => o.value && o.value !== "custom").map((o) => [o.value, o.label]),
);

const OUTCOME_LABELS: Record<string, string> = {
  completed: "Completed",
  transferred: "Transferred",
  non_completed: "Non-completed",
};
const SENTIMENT_LABELS: Record<string, string> = {
  positive: "Positive",
  neutral: "Neutral",
  negative: "Negative",
};

function formatLocal(value: string): string {
  const d = new Date(value);
  return Number.isNaN(d.getTime())
    ? value
    : d.toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

function customRangeText(from: string, to: string): string {
  if (from && to) return `${formatLocal(from)} → ${formatLocal(to)}`;
  if (from) return `after ${formatLocal(from)}`;
  return `before ${formatLocal(to)}`;
}

// <input type="datetime-local"> gives the viewer's local wall-clock time with
// no zone; the API stores naive UTC, so convert before sending.
function localToUtcIso(value: string): string | null {
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

export default function CallsPage() {
  // useSearchParams needs a Suspense boundary under the App Router.
  return (
    <Suspense>
      <CallsList />
    </Suspense>
  );
}

function CallsList() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  // Every filter lives in the URL: it survives opening a call and coming back,
  // the browser's Back button, a reload, and can be shared as a link.
  const agent = searchParams.get("agent") ?? "";
  const outcome = searchParams.get("outcome") ?? "";
  const sentiment = searchParams.get("sentiment") ?? "";
  const bucket = searchParams.get("bucket") ?? "";
  const transferReason = searchParams.get("transfer_reason") ?? "";
  const nonCompletionReason = searchParams.get("non_completion_reason") ?? "";
  const search = searchParams.get("q") ?? "";
  const range = searchParams.get("range") ?? "";
  const customFrom = searchParams.get("from") ?? "";
  const customTo = searchParams.get("to") ?? "";
  const page = Math.max(0, Number(searchParams.get("page") ?? "1") - 1) || 0;

  const [q, setQ] = useState(search);
  useEffect(() => setQ(search), [search]);

  const queryString = searchParams.toString();
  useEffect(() => {
    try {
      sessionStorage.setItem(CALLS_LIST_QUERY_KEY, queryString);
    } catch {
      // Storage blocked: the Back button still works through the URL.
    }
  }, [queryString]);

  // Changing any filter returns to page 1; only the pager passes `page`.
  function update(changes: Record<string, string>) {
    const next = new URLSearchParams(searchParams.toString());
    if (!("page" in changes)) next.delete("page");
    for (const [key, value] of Object.entries(changes)) {
      if (value && !(key === "page" && value === "1")) next.set(key, value);
      else next.delete(key);
    }
    const qs = next.toString();
    router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
  }
  const setPage = (p: number) => update({ page: String(p + 1) });

  const params = new URLSearchParams();
  if (range === "custom") {
    const from = customFrom && localToUtcIso(customFrom);
    const to = customTo && localToUtcIso(customTo);
    if (from) params.set("from", from);
    if (to) params.set("to", to);
  } else if (RANGE_PRESETS[range]) {
    // Rounded to the minute so the SWR key stays stable between renders
    // instead of changing (and refetching) every time.
    const now = Math.floor(Date.now() / 60000) * 60000;
    params.set("from", new Date(now - RANGE_PRESETS[range]).toISOString());
  }
  if (agent) params.set("agent_id", agent);
  if (outcome) params.set("outcome", outcome);
  if (sentiment) params.set("sentiment", sentiment);
  if (bucket) params.set("bucket", bucket);
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

  type ActiveFilter = { key: string; text: string; clear: Record<string, string> };
  const activeFilters = ([
    range === "custom" && (customFrom || customTo)
      ? { key: "range", text: `When: ${customRangeText(customFrom, customTo)}`, clear: { range: "", from: "", to: "" } }
      : RANGE_LABELS[range]
        ? { key: "range", text: `When: ${RANGE_LABELS[range]}`, clear: { range: "", from: "", to: "" } }
        : null,
    search ? { key: "q", text: `Search: “${search}”`, clear: { q: "" } } : null,
    agent ? { key: "agent", text: `Agent: ${agent}`, clear: { agent: "" } } : null,
    outcome ? { key: "outcome", text: `Outcome: ${OUTCOME_LABELS[outcome] ?? outcome}`, clear: { outcome: "" } } : null,
    sentiment ? { key: "sentiment", text: `Sentiment: ${SENTIMENT_LABELS[sentiment] ?? sentiment}`, clear: { sentiment: "" } } : null,
    bucket ? { key: "bucket", text: `What happened: ${label(bucket)}`, clear: { bucket: "" } } : null,
    transferReason ? { key: "tr", text: `Transfer reason: ${label(transferReason)}`, clear: { transfer_reason: "" } } : null,
    nonCompletionReason
      ? { key: "ncr", text: `Non-completion reason: ${label(nonCompletionReason)}`, clear: { non_completion_reason: "" } }
      : null,
  ] as (ActiveFilter | null)[]).filter((f): f is ActiveFilter => f !== null);

  function clearAll() {
    setQ("");
    router.replace(pathname, { scroll: false });
  }

  // A chosen filter gets an indigo border so it stands out from the "any" ones.
  const selectClass = (active: boolean) =>
    `w-full rounded-lg border bg-zinc-950 px-3 py-1.5 text-sm ${
      active ? "border-indigo-500/70 text-zinc-100" : "border-zinc-800 text-zinc-300"
    }`;
  const dateInputClass =
    "rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-1.5 text-sm text-zinc-200 [color-scheme:dark]";
  const sectionLabel = "text-xs font-medium uppercase tracking-wide text-zinc-500";

  return (
    <div className="mx-auto max-w-7xl space-y-4">
      <div>
        <h1 className="text-xl font-semibold text-zinc-100">Calls</h1>
        <p className="text-sm text-zinc-500">
          {data ? `${data.total.toLocaleString()} ${data.total === 1 ? "call" : "calls"}` : "Loading…"}
        </p>
      </div>

      <div className="space-y-4 rounded-xl border border-zinc-800 bg-zinc-900/60 p-4">
        {/* Search */}
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            update({ q: q.trim() });
          }}
        >
          <div className="relative flex-1">
            <svg
              viewBox="0 0 20 20"
              fill="currentColor"
              className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-zinc-500"
              aria-hidden
            >
              <path
                fillRule="evenodd"
                d="M9 3.5a5.5 5.5 0 1 0 3.4 9.83l3.63 3.64a.75.75 0 1 0 1.06-1.06l-3.64-3.63A5.5 5.5 0 0 0 9 3.5ZM5 9a4 4 0 1 1 8 0 4 4 0 0 1-8 0Z"
                clipRule="evenodd"
              />
            </svg>
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Search words said in the call, or paste any call ID…"
              className="w-full rounded-lg border border-zinc-800 bg-zinc-950 py-2 pl-9 pr-3 text-sm text-zinc-200 placeholder:text-zinc-600"
            />
          </div>
          <button
            type="submit"
            className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500"
          >
            Search
          </button>
        </form>

        {/* Time range */}
        <div className="space-y-2">
          <div className={sectionLabel}>When</div>
          <div className="flex flex-wrap gap-1.5" role="group" aria-label="Time range">
            {RANGE_OPTIONS.map((opt) => {
              const selected = range === opt.value;
              return (
                <button
                  key={opt.value || "any"}
                  type="button"
                  aria-pressed={selected}
                  onClick={() =>
                    update(
                      opt.value === "custom"
                        ? { range: "custom" }
                        : { range: opt.value, from: "", to: "" },
                    )
                  }
                  className={`rounded-full border px-3 py-1 text-sm transition-colors ${
                    selected
                      ? "border-indigo-500 bg-indigo-600 text-white"
                      : "border-zinc-700 text-zinc-300 hover:border-zinc-500 hover:bg-zinc-800"
                  }`}
                >
                  {opt.value === "custom" && (
                    <svg viewBox="0 0 20 20" fill="currentColor" className="-ml-0.5 mr-1.5 inline h-3.5 w-3.5 align-[-2px]" aria-hidden>
                      <path
                        fillRule="evenodd"
                        d="M5.75 2a.75.75 0 0 1 .75.75V4h7V2.75a.75.75 0 0 1 1.5 0V4h.25A2.75 2.75 0 0 1 18 6.75v8.5A2.75 2.75 0 0 1 15.25 18H4.75A2.75 2.75 0 0 1 2 15.25v-8.5A2.75 2.75 0 0 1 4.75 4H5V2.75A.75.75 0 0 1 5.75 2Zm-1 5.5c-.69 0-1.25.56-1.25 1.25v6.5c0 .69.56 1.25 1.25 1.25h10.5c.69 0 1.25-.56 1.25-1.25v-6.5c0-.69-.56-1.25-1.25-1.25H4.75Z"
                        clipRule="evenodd"
                      />
                    </svg>
                  )}
                  {opt.label}
                </button>
              );
            })}
          </div>
          {range === "custom" && (
            <div className="flex flex-wrap items-end gap-3 rounded-lg border border-zinc-800 bg-zinc-950/60 p-3">
              <label className="space-y-1">
                <span className="block text-xs text-zinc-500">From</span>
                <input
                  type="datetime-local"
                  value={customFrom}
                  max={customTo || undefined}
                  onChange={(e) => update({ from: e.target.value })}
                  className={dateInputClass}
                />
              </label>
              <span className="pb-2 text-zinc-600">→</span>
              <label className="space-y-1">
                <span className="block text-xs text-zinc-500">To</span>
                <input
                  type="datetime-local"
                  value={customTo}
                  min={customFrom || undefined}
                  onChange={(e) => update({ to: e.target.value })}
                  className={dateInputClass}
                />
              </label>
              <p className="pb-2 text-xs text-zinc-500">
                {customFrom || customTo
                  ? "Times are in your own time zone. Leave one side empty for no limit."
                  : "Pick a start and/or end. Times are in your own time zone."}
              </p>
            </div>
          )}
        </div>

        {/* Dropdown filters */}
        <div className="space-y-2">
          <div className={sectionLabel}>Filter by</div>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
            <label className="space-y-1">
              <span className="block text-xs text-zinc-500">Agent</span>
              <select value={agent} onChange={(e) => update({ agent: e.target.value })} className={selectClass(!!agent)}>
                <option value="">All agents</option>
                {overview?.agents.map((a) => (
                  <option key={a} value={a}>{a}</option>
                ))}
              </select>
            </label>
            <label className="space-y-1">
              <span className="block text-xs text-zinc-500">Outcome</span>
              <select value={outcome} onChange={(e) => update({ outcome: e.target.value })} className={selectClass(!!outcome)}>
                <option value="">Any outcome</option>
                {Object.entries(OUTCOME_LABELS).map(([value, text]) => (
                  <option key={value} value={value}>{text}</option>
                ))}
              </select>
            </label>
            <label className="space-y-1">
              <span className="block text-xs text-zinc-500">Sentiment</span>
              <select value={sentiment} onChange={(e) => update({ sentiment: e.target.value })} className={selectClass(!!sentiment)}>
                <option value="">Any sentiment</option>
                {Object.entries(SENTIMENT_LABELS).map(([value, text]) => (
                  <option key={value} value={value}>{text}</option>
                ))}
              </select>
            </label>
            {(overview?.bucket_breakdown?.length ?? 0) > 0 && (
              <label className="space-y-1">
                <span className="block text-xs text-zinc-500">What happened</span>
                <select value={bucket} onChange={(e) => update({ bucket: e.target.value })} className={selectClass(!!bucket)}>
                  <option value="">Anything</option>
                  {overview!.bucket_breakdown.map((r) => (
                    <option key={r.reason} value={r.reason}>{label(r.reason)} ({r.count})</option>
                  ))}
                </select>
              </label>
            )}
            {(overview?.transfer_reason_breakdown?.length ?? 0) > 0 && (
              <label className="space-y-1">
                <span className="block text-xs text-zinc-500">Transfer reason</span>
                <select
                  value={transferReason}
                  onChange={(e) => update({ transfer_reason: e.target.value })}
                  className={selectClass(!!transferReason)}
                >
                  <option value="">Any</option>
                  {overview!.transfer_reason_breakdown.map((r) => (
                    <option key={r.reason} value={r.reason}>{label(r.reason)} ({r.count})</option>
                  ))}
                </select>
              </label>
            )}
            {(overview?.non_completion_reason_breakdown?.length ?? 0) > 0 && (
              <label className="space-y-1">
                <span className="block text-xs text-zinc-500">Non-completion reason</span>
                <select
                  value={nonCompletionReason}
                  onChange={(e) => update({ non_completion_reason: e.target.value })}
                  className={selectClass(!!nonCompletionReason)}
                >
                  <option value="">Any</option>
                  {overview!.non_completion_reason_breakdown.map((r) => (
                    <option key={r.reason} value={r.reason}>{label(r.reason)} ({r.count})</option>
                  ))}
                </select>
              </label>
            )}
          </div>
        </div>
      </div>

      {/* What is applied right now, each removable on its own */}
      {activeFilters.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="text-zinc-500">Showing only:</span>
          {activeFilters.map((f) => (
            <span
              key={f.key}
              className="inline-flex items-center gap-1 rounded-full border border-indigo-500/40 bg-indigo-500/10 py-0.5 pl-3 pr-1 text-indigo-200"
            >
              {f.text}
              <button
                type="button"
                onClick={() => {
                  if (f.key === "q") setQ("");
                  update(f.clear);
                }}
                className="rounded-full px-1.5 text-indigo-300 hover:bg-indigo-500/20 hover:text-white"
                aria-label={`Remove ${f.text}`}
              >
                ×
              </button>
            </span>
          ))}
          <button type="button" onClick={clearAll} className="ml-1 text-zinc-400 underline-offset-2 hover:text-zinc-200 hover:underline">
            Clear all
          </button>
        </div>
      )}

      <div className="overflow-x-auto rounded-xl border border-zinc-800">
        <table className="w-full text-sm">
          <thead className="bg-zinc-900 text-left text-xs uppercase tracking-wide text-zinc-500">
            <tr>
              <th className="px-4 py-2.5">Time</th>
              <th className="px-4 py-2.5">Agent</th>
              <th className="px-4 py-2.5">Duration</th>
              <th className="px-4 py-2.5">Outcome</th>
              <th className="px-4 py-2.5">What happened</th>
              <th className="px-4 py-2.5">Sentiment</th>
              <th className="px-4 py-2.5">Summary</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-800/70">
            {data?.items.map((call) => (
              <tr key={call.id} className="group align-top hover:bg-zinc-900/70">
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
                  {call.bucket ? (
                    <BucketBadge bucket={call.bucket} note={call.issue_note} />
                  ) : (
                    <span className="text-xs text-zinc-500">—</span>
                  )}
                </td>
                <td className="whitespace-nowrap px-4 py-2.5">
                  <SentimentBadge label={call.sentiment_label} />
                </td>
                <td className="min-w-[24rem] px-4 py-2.5 leading-snug text-zinc-300">
                  {call.summary ?? <span className="text-zinc-600">No summary</span>}
                </td>
              </tr>
            ))}
            {data && data.items.length === 0 && (
              <tr>
                <td colSpan={8} className="px-4 py-10 text-center text-zinc-500">
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
            onClick={() => setPage(page - 1)}
            className="rounded-lg border border-zinc-800 px-3 py-1.5 hover:bg-zinc-900 disabled:opacity-40"
          >
            Previous
          </button>
          <button
            disabled={page + 1 >= totalPages}
            onClick={() => setPage(page + 1)}
            className="rounded-lg border border-zinc-800 px-3 py-1.5 hover:bg-zinc-900 disabled:opacity-40"
          >
            Next
          </button>
        </div>
      </div>
    </div>
  );
}

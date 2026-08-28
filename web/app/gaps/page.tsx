"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import {
  apiSend,
  fetcher,
  type GapGrouping,
  type GapVerification,
  type GapVerifyPlan,
  type GapVerifyRun,
  type KnowledgeGap,
  type KnowledgeGaps,
  type Overview,
} from "@/lib/api";
import { GapStatusBadge } from "@/components/Badges";
import { formatDate } from "@/lib/format";

const selectClass =
  "rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-1.5 text-sm text-zinc-300";

// The working list: records that still need a decision or an action from us.
// Everything else is filed into its own section below, because those need a different
// kind of attention (or none at all).
const OPEN_STATUSES = ["not_verified", "confirmed_missing", "verify_error"];

/** Records that go in the email: proved missing against the real lookup API, and not
 *  already reported. The second half is the whole point of the sent batch — a record the
 *  customer is already working on must not reappear on tomorrow's list. */
function isSendable(g: KnowledgeGap) {
  return g.status === "confirmed_missing" && !g.sent_batch;
}

/** WHAT THE VIEW DROPDOWN DOES, AND WHY IT IS NOT A CLIENT-SIDE FILTER.
 *
 *  `statuses` is sent to the server as `?status=`, so the narrowing happens BEFORE the
 *  row cap. That ordering is the entire fix. The server ranks rows newest-first as its
 *  last tiebreak, and a verified record is by definition an older one, so verified rows
 *  sink to the bottom of the list — measured on the live Lazio database, 133 of 148
 *  records already proved missing sat past rank position 500 and were being thrown away
 *  before the browser ever saw them. Filtering the payload could not have shown them.
 *
 *  The default view sends no filter at all, so the page keeps the layout people already
 *  use: a working list plus the collapsed sections. Every other view is a flat list of
 *  exactly one thing.
 */
const VIEWS: { key: string; label: string; statuses: string[]; blurb?: string }[] = [
  { key: "all", label: "Everything", statuses: [] },
  {
    key: "confirmed_missing",
    label: "Verified missing — ready to report",
    statuses: ["confirmed_missing"],
    blurb:
      "Re-asked against the real lookup API, in several wordings, and nothing came back. These are the only records that may be sent to the customer. Ones already sent carry a batch stamp and stay out of the copied report.",
  },
  {
    key: "not_verified",
    label: "Not checked yet",
    statuses: ["not_verified"],
    blurb:
      "Nobody has re-asked the lookup about these, so they are the judge's word alone. Group them first, then verify — an unverified record is a guess, not a finding.",
  },
  {
    key: "found_in_source",
    label: "Found in source — our lookup failed",
    statuses: ["found_in_source"],
    blurb:
      "The record IS in the database; retrieval missed it during the call. These are engineering bugs on our side, not records for the customer to add.",
  },
  {
    key: "sent",
    label: "Sent — waiting on the customer",
    statuses: ["sent"],
    blurb:
      "Already reported. Re-checking one is how you find out whether it has been added yet: if the record turns up it moves to “Added — confirmed”, and if it is still missing it stays here rather than rejoining the report.",
  },
  {
    key: "added",
    label: "Added",
    statuses: ["added", "added_confirmed"],
    blurb:
      "“Added — confirmed” means we asked the source again and it answered. “Added — not re-checked” means somebody marked it by hand and nothing has been proved yet.",
  },
  {
    key: "verify_error",
    label: "Check failed",
    statuses: ["verify_error"],
    blurb:
      "The lookup never completed — endpoint down, wrong tool name, timeout — so nothing was learned about the customer's data. Fix the probe in Analysis Settings and re-check.",
  },
  {
    key: "bad_question",
    label: "Question not usable",
    statuses: ["bad_question"],
    blurb:
      "Built from mis-heard speech, so it came back empty because it is nonsense rather than because the data is absent. Nobody can add a record for it — open the call and listen.",
  },
];

export default function GapsPage() {
  const [agent, setAgent] = useState("");
  const [days, setDays] = useState("7");
  const [minCount, setMinCount] = useState("1");
  const [copied, setCopied] = useState<string[] | null>(null);
  const [grouping, setGrouping] = useState(false);
  const [result, setResult] = useState<GapGrouping | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [plan, setPlan] = useState<GapVerifyPlan | null>(null);
  const [planning, setPlanning] = useState(false);
  const [verifyingOne, setVerifyingOne] = useState<string | null>(null);
  const [view, setView] = useState("all");

  const activeView = VIEWS.find((v) => v.key === view) ?? VIEWS[0];
  const filtered = activeView.statuses.length > 0;

  // `limit` is sent explicitly. Leaving it to the server's default is what let the cap
  // apply invisibly: the live history is already past 1,000 rows once every grouped
  // record is listed regardless of age, and a record dropped off the end of this list is
  // a record the customer never hears about.
  //
  // 2000, not the server's new 5000 ceiling, ON PURPOSE. This dashboard is routinely run
  // against a backend that has not been redeployed yet (web/.env.local forwards to the
  // VM), and the older endpoint validates `limit` at `le=2000` — asking for more gets a
  // 422 and takes the whole page down over a parameter it does not need. `total_rows`
  // says when 2000 is not enough, which is the honest way to find out.
  const params = new URLSearchParams({ days, min_count: minCount, limit: "2000" });
  if (agent) params.set("agent_id", agent);
  if (filtered) params.set("status", activeView.statuses.join(","));

  const { data: raw, isLoading, mutate } = useSWR<KnowledgeGaps>(
    `/api/v1/analytics/knowledge-gaps?${params}`,
    fetcher,
    { keepPreviousData: true }
  );
  const { data: overview } = useSWR<Overview>("/api/v1/analytics/overview", fetcher);

  // Polled only while a batch is running, so an idle page makes no requests. A run keeps
  // going on the server across reloads, so this also picks up a run somebody else started.
  const { data: run, mutate: mutateRun } = useSWR<GapVerifyRun>(
    "/api/v1/gaps/verify/status",
    fetcher,
    { refreshInterval: (latest) => (latest?.running ? 3000 : 0) }
  );

  // When a run finishes, the verdicts on the page are stale — pull them once.
  const running = run?.running ?? false;
  useEffect(() => {
    if (!running && run?.finished_at) void mutate();
  }, [running, run?.finished_at, mutate]);

  // A dashboard built from this checkout is routinely pointed at a backend that has not
  // been redeployed yet — that is the whole point of web/.env.local forwarding to the
  // VM's API, so a UI change can be judged against real calls before it ships. An older
  // backend simply omits the fields added here, so default them rather than letting the
  // page die on `undefined.length` when the two halves are out of step.
  //
  // Verification specifically has to be told apart from "no sources configured", because
  // the two look identical after defaulting and lead to opposite actions: one is "deploy
  // the server", the other is "fill in Analysis Settings". A row from a backend that has
  // verification always carries `status`; one from an older backend never does.
  const backendHasVerification =
    !raw || (raw.groups ?? []).length === 0 || "status" in raw.groups[0];

  const data: KnowledgeGaps | undefined = raw && {
    ...raw,
    groups: (raw.groups ?? []).map((g) => ({
      ...g,
      status: g.status ?? "not_verified",
      probes_configured: g.probes_configured ?? 0,
      agent_id: g.agent_id ?? null,
    })),
    needs_review: raw.needs_review ?? [],
    ungrouped_count: raw.ungrouped_count ?? 0,
    total_rows: raw.total_rows ?? (raw.groups ?? []).length,
    status_filter: raw.status_filter ?? [],
  };

  // An older backend has no `status` param and no `status_filter`, so it answers a
  // filtered request with the whole unfiltered list. Saying "these are all verified" over
  // a list that is mostly unverified is the worst thing this page could do, so detect the
  // disagreement and say so instead of rendering it.
  const filterIgnored =
    filtered && !!raw && (raw.status_filter ?? []).length === 0;

  // Rows the server matched before `limit` cut them. Shown whenever it disagrees with
  // what arrived: a truncated list that looks complete is exactly how 133 verified
  // records stayed invisible.
  const truncated = !!data && data.total_rows > data.groups.length;

  // Only meaningful once something has been merged: before that every row is one call
  // and every count is 1, so the filter would empty the page rather than narrow it.
  const hasGrouping = (data?.groups ?? []).some((g) => g.grouped);
  const groups = data?.groups ?? [];
  const byStatus = (...statuses: string[]) =>
    groups.filter((g) => statuses.includes(g.status));

  const open = byStatus(...OPEN_STATUSES);
  const sendable = groups.filter(isSendable);
  // Records that HAVE been through grouping and still need a decision. The button is
  // shown whenever any of these exist and disabled with a reason when they cannot
  // actually be checked — hiding it made a missing deployment and a missing setting
  // indistinguishable from "this feature does not exist".
  const verifiable = open.filter((g) => g.grouped && g.status !== "confirmed_missing");
  const checkable = verifiable.filter((g) => g.probes_configured > 0);
  const probesMissing =
    backendHasVerification && groups.some((g) => g.grouped && g.probes_configured === 0);
  const verifyBlocked = !backendHasVerification
    ? "The backend this dashboard is reading does not have verification yet — deploy the server, or point CALLHARNESS_API_URL at one that does."
    : checkable.length === 0 && verifiable.length > 0
      ? "No lookup source is configured for these records' regions. Add one in Analysis Settings."
      : null;

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
      considered: 0, grouped: 0, joined_existing: 0, needs_review: 0,
      new_groups: 0, remaining: 0, warnings: [],
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
        total.joined_existing += res.joined_existing ?? 0;
        total.needs_review += res.needs_review;
        total.new_groups += res.new_groups;
        total.remaining = res.remaining;
        total.warnings = [...total.warnings, ...(res.warnings ?? [])];
        setResult({ ...total });
        await mutate();

        if (res.considered === 0 || res.remaining <= 0) break;
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Grouping failed";
      // 409 means another pass (often one left running by a reload) still holds the lock.
      // That is not a failure and must not read like one, or the natural response is to
      // press again and make it worse.
      setError(
        msg.includes("409")
          ? "A grouping pass is already running — it keeps going even after a reload. Wait for it to finish, then reload this page."
          : `${msg} — everything grouped so far has been saved. Press again to continue.`
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

  /** Ask what a sweep would cost before spending any of it. Every probe lands on the
   *  customer's live service — no rate limiting, no caching, the same instance answering
   *  phone calls — so this is shown and confirmed rather than reported afterwards. */
  async function askPlan() {
    setPlanning(true);
    setError(null);
    try {
      const res = (await apiSend("/api/v1/gaps/verify/plan", "POST", {
        agent_id: agent || null,
        days: Number(days),
        limit: 500,
      })) as GapVerifyPlan;
      setPlan(res);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not work out the cost");
    } finally {
      setPlanning(false);
    }
  }

  async function startRun() {
    setPlan(null);
    setError(null);
    try {
      await apiSend("/api/v1/gaps/verify", "POST", {
        agent_id: agent || null,
        days: Number(days),
        limit: 500,
      });
      await mutateRun();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not start the run");
    }
  }

  async function verifyOne(groupId: string) {
    setVerifyingOne(groupId);
    setError(null);
    try {
      await apiSend(`/api/v1/gaps/${encodeURIComponent(groupId)}/verify`, "POST");
      await mutate();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Verification failed");
    } finally {
      setVerifyingOne(null);
    }
  }

  async function setStatus(groupId: string, status: string) {
    setError(null);
    try {
      await apiSend(`/api/v1/gaps/${encodeURIComponent(groupId)}/status`, "POST", { status });
      await mutate();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not update the record");
    }
  }

  // The point of this page is the message that gets sent to whoever owns the data, so
  // the report is generated in the exact shape it would be pasted into an email.
  //
  // ONLY VERIFIED, UNSENT RECORDS GO IN. A line nobody checked is a guess, and one
  // already sent is a record the customer is working on — repeating it wastes their time
  // and costs us their trust in the list. The needs-review set is absent for a third
  // reason: nobody can add a record for "curva glicemica" with no attribute.
  function reportText(d: KnowledgeGaps, rows: KnowledgeGap[]) {
    const lines = [
      `Missing information — last ${d.window_days} days`,
      "",
      `${d.calls_with_gaps} of ${d.calls_scanned} calls reached a question we could not answer`,
      "because the information is not in the system.",
      "",
      "Each of the following was re-checked against the lookup service before being listed:",
      "the question was asked again, in several wordings, and nothing came back.",
      "",
      "Adding these would let the assistant answer these calls itself:",
      "",
    ];
    rows.forEach((g, i) => {
      lines.push(`${i + 1}. ${g.question}`);
      lines.push(`   asked ${g.count}x · ${g.transferred} transferred to an operator`);
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
    await navigator.clipboard.writeText(reportText(data, sendable));
    // Copying stores nothing on purpose: you can copy the report, read it, and decide not
    // to send it. Marking is a separate press, offered right here so it is hard to forget.
    setCopied(sendable.map((g) => g.group_id!).filter(Boolean));
  }

  async function markSent() {
    if (!copied?.length) return;
    try {
      await apiSend("/api/v1/gaps/mark-sent", "POST", { group_ids: copied });
      setCopied(null);
      setNotice(`Marked ${copied.length} record${copied.length === 1 ? "" : "s"} as sent.`);
      await mutate();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not mark them as sent");
    }
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
        {/* First control on the page, because "where did the verified ones go" is the
            question people arrive with. Filters on the SERVER — see VIEWS. */}
        <select
          value={view}
          onChange={(e) => setView(e.target.value)}
          className={`${selectClass} font-medium text-zinc-200`}
        >
          {VIEWS.map((v) => (
            <option key={v.key} value={v.key}>
              {v.label}
            </option>
          ))}
        </select>
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
          <option value="365">Last year</option>
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
          {verifiable.length > 0 && !filtered && (
            <button
              onClick={askPlan}
              disabled={planning || running || !!verifyBlocked}
              className="rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm font-medium text-zinc-200 hover:bg-zinc-700 disabled:cursor-not-allowed disabled:opacity-50"
              title={
                verifyBlocked ??
                "Re-ask the lookup API about every record nobody has checked"
              }
            >
              {planning
                ? "Checking…"
                : `Verify unchecked (${checkable.length || verifiable.length})`}
            </button>
          )}
          {sendable.length > 0 && (
            <button
              onClick={copyReport}
              className="rounded-lg bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500"
            >
              Copy report ({sendable.length})
            </button>
          )}
        </div>
      </div>

      {error && (
        <div className="rounded-lg border border-red-900/60 bg-red-950/30 p-3 text-sm text-red-300">
          {error}
        </div>
      )}
      {notice && (
        <div className="rounded-lg border border-emerald-900/60 bg-emerald-950/20 p-3 text-sm text-emerald-300">
          {notice}
        </div>
      )}

      {/* Copying stores nothing, so this is the only thing that takes a record off the
          list. Shown right after the copy so it is hard to forget, and never automatic so
          a report you decided not to send does not disappear from the next one. */}
      {copied && copied.length > 0 && (
        <div className="flex flex-wrap items-center gap-3 rounded-lg border border-indigo-900/60 bg-indigo-950/20 p-3 text-sm text-indigo-200">
          <span>
            {copied.length} record{copied.length === 1 ? "" : "s"} copied. Nothing has
            changed yet — mark them as sent once the email has actually gone out, and they
            will stay off every future report.
          </span>
          <button
            onClick={markSent}
            className="ml-auto rounded-lg bg-indigo-600 px-3 py-1 text-sm font-medium text-white hover:bg-indigo-500"
          >
            Mark these {copied.length} as sent
          </button>
          <button
            onClick={() => setCopied(null)}
            className="rounded-lg border border-indigo-800 px-3 py-1 text-sm text-indigo-300 hover:bg-indigo-900/40"
          >
            Not yet
          </button>
        </div>
      )}

      {/* Every one of these requests lands on the customer's own live service, which also
          answers phone calls and has no rate limiting. So the number is shown before it is
          spent, not after. */}
      {plan && (
        <div className="space-y-2 rounded-lg border border-amber-900/60 bg-amber-950/20 p-3 text-sm text-amber-200">
          <div>
            This will re-ask <strong>{plan.groups}</strong> record
            {plan.groups === 1 ? "" : "s"} — about <strong>{plan.requests}</strong> requests
            to {plan.sources.join(" and ") || "the configured sources"}, plus two LLM calls
            per record on our side.
          </div>
          {/* The list above shows every grouped record regardless of age; the RUN is still
              bounded by the date selector, because widening it would spend more of the
              customer's API budget than the button appears to promise. Said out loud so the
              two numbers cannot be mistaken for each other. */}
          <div className="text-xs text-amber-300/80">
            Only records whose calls fall in the selected window (
            {days === "1" ? "last 24 hours" : `last ${days} days`}) are included — the list
            above is not limited by that.
          </div>
          <div className="text-xs text-amber-300/80">
            That service also answers live phone calls and has no rate limiting, so the run
            goes two requests at a time and takes roughly{" "}
            {Math.max(1, Math.round((plan.groups * 45) / 60))} minute
            {Math.round((plan.groups * 45) / 60) === 1 ? "" : "s"}. It keeps running if you
            leave the page.
          </div>
          {Object.keys(plan.unroutable).length > 0 && (
            <div className="text-xs text-amber-400">
              Skipping{" "}
              {Object.entries(plan.unroutable)
                .map(([a, n]) => `${n} in ${a}`)
                .join(", ")}{" "}
              — no lookup source is configured for those regions.
            </div>
          )}
          <div className="flex gap-2 pt-1">
            <button
              onClick={startRun}
              disabled={plan.groups === 0}
              className="rounded-lg bg-amber-600 px-3 py-1 text-sm font-medium text-white hover:bg-amber-500 disabled:opacity-40"
            >
              Run it
            </button>
            <button
              onClick={() => setPlan(null)}
              className="rounded-lg border border-amber-800 px-3 py-1 text-sm text-amber-300 hover:bg-amber-900/30"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {run?.running && (
        <div className="rounded-lg border border-indigo-900/60 bg-indigo-950/20 p-3 text-sm text-indigo-300">
          Verifying {run.done} of {run.total}… This keeps running on the server if you
          leave the page. Only one run happens at a time, to keep the load on the lookup
          API bounded.
          <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-indigo-950">
            <div
              className="h-full bg-indigo-500 transition-all"
              style={{ width: `${run.total ? (run.done / run.total) * 100 : 0}%` }}
            />
          </div>
        </div>
      )}

      {run && !run.running && run.finished_at && Object.keys(run.verdicts).length > 0 && (
        <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3 text-sm text-zinc-400">
          Last run checked {run.done} record{run.done === 1 ? "" : "s"}:{" "}
          {Object.entries(run.verdicts)
            .map(([v, n]) => `${n} ${v.replace(/_/g, " ")}`)
            .join(", ")}
          .{run.error && <span className="text-amber-400"> {run.error}</span>}
        </div>
      )}

      {/* Why the Verify buttons are greyed out. Said in the page rather than in a tooltip:
          the two reasons need opposite actions, and a disabled button with no explanation
          reads as "this feature is not here". */}
      {!backendHasVerification && (
        <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3 text-sm text-zinc-400">
          <strong className="font-medium text-zinc-300">
            Verification is not available on this backend.
          </strong>{" "}
          This dashboard can verify missing records against the lookup API, but the server
          it is reading is an older build that has no{" "}
          <code className="text-zinc-500">/api/v1/gaps</code> endpoints. Deploy the server,
          or point <code className="text-zinc-500">CALLHARNESS_API_URL</code> at one that
          has them. Everything else on this page works as before.
        </div>
      )}

      {probesMissing && (
        <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3 text-sm text-zinc-400">
          <strong className="font-medium text-zinc-300">
            No lookup source is configured
          </strong>{" "}
          for {[...new Set(groups.filter((g) => g.grouped && g.probes_configured === 0).map((g) => g.agent_id ?? "this region"))].join(", ")}
          , so those records cannot be verified — only assumed. There is no default here: a
          knowledge-base URL belongs to one deployment.{" "}
          <Link href="/settings" className="text-indigo-400 hover:text-indigo-300">
            Add one in Analysis Settings →
          </Link>
        </div>
      )}

      {grouping && (
        <div className="rounded-lg border border-indigo-900/60 bg-indigo-950/20 p-3 text-sm text-indigo-300">
          Grouping in batches — each takes a minute or two.{" "}
          <strong className="font-semibold">Don&apos;t reload the page.</strong> The pass
          keeps running on the server if you do, and pressing the button again would send
          the same questions a second time.
        </div>
      )}

      {result && (
        <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3 text-sm text-zinc-400">
          Looked at {result.considered} question{result.considered === 1 ? "" : "s"}:{" "}
          {result.new_groups} record{result.new_groups === 1 ? "" : "s"} created,{" "}
          {result.grouped} merged with each other,{" "}
          {result.joined_existing} added to a record already on the list,{" "}
          {result.needs_review} sent for human review.
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

      {/* The backend is answering the filter as if it were not there. Refusing to render
          the rows is deliberate: a list labelled "verified missing" that is actually the
          whole unfiltered list is a false claim about the customer's database. */}
      {filterIgnored && (
        <div className="rounded-lg border border-amber-900/60 bg-amber-950/20 p-3 text-sm text-amber-200">
          <strong className="font-medium">This backend cannot filter by status.</strong>{" "}
          It ignored <code className="text-amber-300/80">?status=</code> and would return
          every record regardless of the view — so the list is not shown rather than
          mislabelled. Deploy the server, or point{" "}
          <code className="text-amber-300/80">CALLHARNESS_API_URL</code> at one that has
          it. “Everything” still works.
        </div>
      )}

      {/* Rows the server matched but did not send. This is the notice whose absence let
          133 of 148 verified records stay invisible while the page looked complete. */}
      {truncated && !filterIgnored && (
        <div className="rounded-lg border border-amber-900/60 bg-amber-950/20 p-3 text-sm text-amber-200">
          Showing <strong>{data!.groups.length}</strong> of{" "}
          <strong>{data!.total_rows}</strong> matching records — the rest were cut by the
          row cap. Narrow the view or the region to see them; records past the cut are
          also missing from the copied report.
        </div>
      )}

      {isLoading && !data && <p className="text-sm text-zinc-500">Loading…</p>}

      {/* `calls_scanned` counts the WINDOW, but grouped records are listed regardless of
          age — so gating the list on it would hide all 148 verified records the moment the
          window happened to be quiet, which is the same bug in a new place. Both
          conditions have to be empty before the page claims there is nothing here. */}
      {data && data.calls_scanned === 0 && data.groups.length === 0 && (
        <div className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-8 text-center text-sm text-zinc-500">
          {filtered ? "No records have this status." : "No calls in this window yet."}
        </div>
      )}

      {data && (data.calls_scanned > 0 || data.groups.length > 0) && (
        <>
          {/* Tiles only in the unfiltered view. Two of the three count the loaded rows, so
              under a filter "Verified missing: 0" would be an artefact of the dropdown
              rather than a fact about the database — which is the exact confusion this
              whole change exists to remove. */}
          {!filtered && (
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
            {/* Replaces the old "transfers caused" tile: once records can be verified, how
                many are actually PROVEN is the number that decides what gets sent. */}
            <div className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4">
              <div className="text-xs uppercase tracking-wide text-zinc-500">
                Verified missing
              </div>
              <div className="mt-1 text-2xl font-semibold tabular-nums text-zinc-100">
                {sendable.length}
              </div>
              <div className="mt-1 text-xs text-zinc-500">
                ready to report · {byStatus("not_verified").length} unchecked
              </div>
            </div>
          </div>
          )}

          {/* FILTERED: one flat list of exactly what was asked for. The sectioned layout
              below is for the unfiltered view — under a filter it would put a record into
              the main list only if its status happened to be one of OPEN_STATUSES, which
              is how "Found in source" ended up as a collapsed one-line toggle underneath
              500 rows and effectively unreachable. */}
          {filtered && !filterIgnored && (
            <div className="space-y-3">
              <div>
                <h2 className="text-sm font-semibold text-zinc-200">
                  {activeView.label} ({data.total_rows})
                </h2>
                {activeView.blurb && (
                  <p className="max-w-3xl text-xs text-zinc-500">{activeView.blurb}</p>
                )}
              </div>
              {groups.length === 0 ? (
                <div className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-6 text-center text-sm text-zinc-500">
                  No records have this status.
                </div>
              ) : (
                <ol className="space-y-2">
                  {groups.map((g, i) => (
                    <GapRow
                      key={g.group_id ?? g.examples[0]?.call_id ?? i}
                      gap={g}
                      index={i}
                      busy={verifyingOne === g.group_id}
                      disabled={running || verifyingOne !== null}
                      backendReady={backendHasVerification}
                      onUngroup={ungroup}
                      onVerify={verifyOne}
                      onStatus={setStatus}
                    />
                  ))}
                </ol>
              )}
            </div>
          )}

          {!filtered && (open.length === 0 ? (
            <div className="rounded-xl border border-emerald-900/50 bg-emerald-950/20 p-6 text-center text-sm text-emerald-300">
              Nothing left to check or report in this window.
            </div>
          ) : (
            <ol className="space-y-2">
              {open.map((g, i) => (
                <GapRow
                  key={g.group_id ?? g.examples[0]?.call_id ?? i}
                  gap={g}
                  index={i}
                  busy={verifyingOne === g.group_id}
                  disabled={running || verifyingOne !== null}
                  backendReady={backendHasVerification}
                  onUngroup={ungroup}
                  onVerify={verifyOne}
                  onStatus={setStatus}
                />
              ))}
            </ol>
          ))}

          {!filtered && (
          <>
          <GapSection
            title="Found in the source — our lookup failed"
            blurb="The record IS in the database; retrieval missed it on the call. These are engineering bugs on our side, not records for the customer to add, so they are kept out of the report."
            rows={byStatus("found_in_source")}
            onUngroup={ungroup}
            onVerify={verifyOne}
            onStatus={setStatus}
            busy={verifyingOne}
            disabled={running || verifyingOne !== null}
            backendReady={backendHasVerification}
          />

          <GapSection
            title="Sent — waiting on the customer"
            blurb="Already reported. Re-checking one is how you find out whether it has been added yet: if the record turns up, it moves to “Added — confirmed”. If it is still missing it stays here rather than rejoining the report, so the same list is never sent twice."
            rows={byStatus("sent")}
            onUngroup={ungroup}
            onVerify={verifyOne}
            onStatus={setStatus}
            busy={verifyingOne}
            disabled={running || verifyingOne !== null}
            backendReady={backendHasVerification}
          />

          <GapSection
            title="Added"
            blurb="The record is in the database now. “Added — confirmed” means we asked the source again and it answered; “not re-checked” means somebody marked it by hand and nothing has been proved yet."
            rows={byStatus("added", "added_confirmed")}
            onUngroup={ungroup}
            onVerify={verifyOne}
            onStatus={setStatus}
            busy={verifyingOne}
            disabled={running || verifyingOne !== null}
            backendReady={backendHasVerification}
          />

          <GapSection
            title="Question not usable"
            blurb="The question was built from mis-heard speech, so it came back empty because it is nonsense rather than because the data is absent. Nobody can add a record for it — open the call and listen."
            rows={byStatus("bad_question")}
            onUngroup={ungroup}
            onVerify={verifyOne}
            onStatus={setStatus}
            busy={verifyingOne}
            disabled={running || verifyingOne !== null}
            backendReady={backendHasVerification}
          />
          </>
          )}

          {/* Kept in every view. It is not a status — these rows have no GapGroup at all —
              and it is the one section a filter can never surface, so hiding it behind the
              dropdown would recreate the problem in a new place. */}
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
                  kept out of the report and its counts, and cannot be verified; open the
                  calls to see what was really asked.
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

/** A collapsed-by-default group of rows that are no longer part of the working list.
 *  Kept on the page rather than hidden: "we checked and it was there" is a finding, and
 *  "we sent it last Tuesday" is what stops it being sent again. */
function GapSection({
  title,
  blurb,
  rows,
  ...handlers
}: {
  title: string;
  blurb: string;
  rows: KnowledgeGap[];
  busy: string | null;
  disabled: boolean;
  backendReady: boolean;
  onUngroup: (id: string) => void;
  onVerify: (id: string) => void;
  onStatus: (id: string, status: string) => void;
}) {
  const [open, setOpen] = useState(false);
  if (rows.length === 0) return null;
  return (
    <div className="space-y-2 pt-2">
      <button
        onClick={() => setOpen(!open)}
        className="flex w-full items-baseline gap-2 text-left"
      >
        <span className="text-sm font-semibold text-zinc-300">
          {title} ({rows.length})
        </span>
        <span className="text-xs text-zinc-600">{open ? "hide" : "show"}</span>
      </button>
      {open && (
        <>
          <p className="max-w-3xl text-xs text-zinc-500">{blurb}</p>
          <ol className="space-y-2">
            {rows.map((g, i) => (
              <GapRow
                key={g.group_id ?? g.examples[0]?.call_id ?? i}
                gap={g}
                index={i}
                busy={handlers.busy === g.group_id}
                disabled={handlers.disabled}
                backendReady={handlers.backendReady}
                onUngroup={handlers.onUngroup}
                onVerify={handlers.onVerify}
                onStatus={handlers.onStatus}
              />
            ))}
          </ol>
        </>
      )}
    </div>
  );
}

function GapRow({
  gap,
  index,
  busy,
  disabled,
  backendReady,
  onUngroup,
  onVerify,
  onStatus,
}: {
  gap: KnowledgeGap;
  index: number;
  busy: boolean;
  disabled: boolean;
  // False when the API this page is reading predates verification. The button is hidden
  // rather than disabled-with-a-reason, because every reason it could give would name the
  // wrong cause; the page-level banner says what is actually wrong.
  backendReady: boolean;
  onUngroup: (groupId: string) => void;
  onVerify: (groupId: string) => void;
  onStatus: (groupId: string, status: string) => void;
}) {
  const [showEvidence, setShowEvidence] = useState(false);
  const others = (gap.variants ?? []).filter((v) => v !== gap.question);
  const examples = gap.examples ?? [];
  const canVerify = gap.grouped && !!gap.group_id && gap.probes_configured > 0;

  // Fetched only when the panel is opened. The list endpoint already re-renders on every
  // filter change, and an eager fetch per row would multiply that by the page length.
  const { data: history } = useSWR<GapVerification[]>(
    showEvidence && gap.group_id
      ? `/api/v1/gaps/${encodeURIComponent(gap.group_id)}/verifications`
      : null,
    fetcher
  );

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
        <GapStatusBadge status={gap.status} note={gap.status_note} />
      </div>

      {/* WHETHER THIS ROW HAS BEEN THROUGH GROUPING. Without it a group of one and a call
          nobody has grouped look identical — same single question, same single call id —
          and only the first of those is a finished judgement. Verification is deliberately
          limited to grouped records, so the page has to say which is which. */}
      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
        {gap.grouped ? (
          <span className="text-zinc-600" title="The grouping pass has placed this record">
            ✓ grouped
          </span>
        ) : (
          <span className="rounded bg-amber-500/10 px-1.5 py-0.5 text-amber-400/90">
            not grouped yet
          </span>
        )}
        {gap.sent_batch && <span className="text-zinc-600">· {gap.sent_batch}</span>}

        <div className="ml-auto flex flex-wrap items-center gap-2">
          {gap.status !== "not_verified" && gap.group_id && (
            <button
              onClick={() => setShowEvidence(!showEvidence)}
              className="rounded-full border border-zinc-700 px-2 py-0.5 text-zinc-500 hover:border-zinc-600 hover:text-zinc-300"
            >
              {showEvidence ? "hide evidence" : "evidence"}
            </button>
          )}
          {gap.status === "sent" && gap.group_id && (
            <button
              onClick={() => onStatus(gap.group_id!, "added")}
              className="rounded-full border border-lime-800 px-2 py-0.5 text-lime-400/90 hover:bg-lime-950/40"
              title="The customer says they have added it. Verify afterwards to prove it."
            >
              mark added
            </button>
          )}
          {backendReady && (
          <button
            onClick={() => canVerify && onVerify(gap.group_id!)}
            disabled={!canVerify || disabled}
            className={`rounded-full border px-2 py-0.5 disabled:cursor-not-allowed ${
              canVerify
                ? "border-indigo-800 bg-indigo-500/10 text-indigo-300 hover:bg-indigo-500/20 disabled:opacity-40"
                : "border-zinc-800 text-zinc-600 opacity-70"
            }`}
            title={
              !gap.grouped
                ? "Run Group duplicates first: an ungrouped question has not been checked for duplicates, so verifying it may re-ask a record another row already covers."
                : gap.probes_configured === 0
                  ? `No lookup source is configured for ${gap.agent_id ?? "this region"}. Add one in Analysis Settings.`
                  : "Ask the lookup API again, in several wordings"
            }
          >
            {busy
              ? "checking…"
              : !gap.grouped
                ? "verify — group it first"
                : gap.probes_configured === 0
                  ? "verify — no source configured"
                  : gap.status === "not_verified"
                    ? "verify"
                    : "re-check"}
          </button>
          )}
          {/* Grouping never re-judges a call it has already placed, so this is the only
              way to undo a wrong merge. The verdict goes with the record: the members come
              back unverified, and the evidence stays on each call. */}
          {gap.grouped && gap.count > 1 && gap.group_id && (
            <button
              onClick={() => onUngroup(gap.group_id!)}
              className="rounded-full border border-zinc-700 px-2 py-0.5 text-zinc-500 hover:border-zinc-600 hover:text-zinc-300"
              title="Split this back into separate questions. Its verification is discarded with it."
            >
              ungroup
            </button>
          )}
        </div>
      </div>

      {gap.status_note && (
        <p className="mt-2 text-xs text-zinc-400">{gap.status_note}</p>
      )}

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

      {showEvidence && <Evidence history={history} />}
    </li>
  );
}

/** Exactly what was sent and exactly what came back, per attempt.
 *
 *  Shown in full rather than summarised. A verdict about somebody else's database that
 *  cannot be inspected is a verdict nobody should act on — and the common failure here is
 *  a reply that reads confident and answers a different question, which only a human
 *  reading the text can catch. */
function Evidence({ history }: { history: GapVerification[] | undefined }) {
  if (!history) return <p className="mt-3 text-xs text-zinc-600">Loading evidence…</p>;
  if (history.length === 0)
    return <p className="mt-3 text-xs text-zinc-600">No verification runs recorded.</p>;

  return (
    <div className="mt-3 space-y-3 border-t border-zinc-800 pt-3">
      {history.map((run) => (
        <div key={run.id} className="space-y-2">
          <div className="flex flex-wrap items-center gap-2 text-xs text-zinc-500">
            <GapStatusBadge status={run.verdict} />
            <span>{formatDate(run.created_at)}</span>
            {run.date_meant && run.date_probed && run.date_meant !== run.date_probed && (
              <span className="text-amber-400/90">
                asked about {run.date_probed}, caller meant {run.date_meant} (already past)
              </span>
            )}
          </div>
          {run.question_note && <p className="text-xs text-zinc-400">{run.question_note}</p>}
          <div className="space-y-1.5">
            {run.probes.map((p, i) => (
              <div key={i} className="rounded border border-zinc-800 bg-zinc-950/50 p-2 text-xs">
                <div className="flex flex-wrap items-center gap-x-2 text-zinc-500">
                  <span
                    className={
                      p.verdict === "ok"
                        ? "text-emerald-400"
                        : p.verdict === "empty"
                          ? "text-amber-400"
                          : "text-red-400"
                    }
                  >
                    {p.verdict === "ok"
                      ? "answered"
                      : p.verdict === "empty"
                        ? "nothing found"
                        : "failed"}
                  </span>
                  <span>{p.probe_label}</span>
                  <span className="text-zinc-600">{p.variant_kind}</span>
                  {p.http_status != null && <span className="text-zinc-600">HTTP {p.http_status}</span>}
                  {p.ms != null && <span className="text-zinc-600">{p.ms}ms</span>}
                </div>
                <div className="mt-1 text-zinc-300">&ldquo;{p.variant}&rdquo;</div>
                <div className="mt-1 whitespace-pre-wrap break-words text-zinc-500">
                  {p.response}
                </div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

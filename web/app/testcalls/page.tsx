"use client";

import { useState } from "react";
import useSWR from "swr";
import {
  apiSend,
  fetcher,
  type TestCallReadiness,
  type TestRun,
  type TestScenario,
} from "@/lib/api";

const inputClass =
  "w-full rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 placeholder:text-zinc-600";

const ACTIVE = new Set(["queued", "dialing", "talking"]);

export default function TestCallsPage() {
  const { data: readiness } = useSWR<TestCallReadiness>(
    "/api/v1/testcalls/readiness",
    fetcher,
    { refreshInterval: 5000 }
  );
  const { data: scenarios, mutate: mutateScenarios } = useSWR<TestScenario[]>(
    "/api/v1/testcalls/scenarios",
    fetcher
  );
  const { data: runs, mutate: mutateRuns } = useSWR<TestRun[]>(
    "/api/v1/testcalls/runs?limit=25",
    fetcher,
    // Poll hard while a call is in the air — a run moves dialing → talking → completed
    // over a couple of minutes — and back off to a slow refresh once nothing is live.
    { refreshInterval: (data) => (data?.some((r) => ACTIVE.has(r.status)) ? 3000 : 20000) }
  );

  // null = form closed, "new" = creating, a scenario = editing that one.
  const [editing, setEditing] = useState<TestScenario | "new" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  const running = runs?.some((r) => ACTIVE.has(r.status)) ?? false;

  async function runScenario(scenario: TestScenario) {
    setError(null);
    setBusy(scenario.id);
    try {
      await apiSend(`/api/v1/testcalls/scenarios/${scenario.id}/run`, "POST");
      await mutateRuns();
    } catch (e) {
      setError(e instanceof Error ? e.message.replace(/^API error \d+: /, "") : "Call failed.");
    } finally {
      setBusy(null);
    }
  }

  async function cancel(run: TestRun) {
    await apiSend(`/api/v1/testcalls/runs/${run.id}/cancel`, "POST");
    await mutateRuns();
  }

  async function remove(scenario: TestScenario) {
    await apiSend(`/api/v1/testcalls/scenarios/${scenario.id}`, "DELETE");
    await mutateScenarios();
  }

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold text-zinc-100">Test Calls</h1>
          <p className="max-w-2xl text-sm text-zinc-500">
            Ring the agent&apos;s real phone number and have an AI caller talk to it, so you can
            see whether something you just shipped works{" "}
            <span className="text-zinc-300">in production</span> — without waiting for a real
            patient to trigger it. Each call presses its way through the phone menu, holds a
            short conversation, then hangs up.
          </p>
        </div>
        <button
          onClick={() => setEditing((v) => (v ? null : "new"))}
          className="shrink-0 rounded-lg bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500"
        >
          {editing ? "Cancel" : "+ New scenario"}
        </button>
      </div>

      {readiness && !readiness.enabled && (
        <div className="rounded-xl border border-amber-900/60 bg-amber-950/30 p-4 text-sm text-amber-200">
          <div className="font-medium">Test calling is not set up yet</div>
          <p className="mt-1 text-amber-200/80">{readiness.missing}</p>
        </div>
      )}

      {readiness?.enabled && (
        <div className="flex flex-wrap gap-4 rounded-xl border border-zinc-800 bg-zinc-900/40 px-4 py-3 text-xs text-zinc-500">
          <span>
            Caller: <span className="text-zinc-300">{readiness.realtime_model}</span>
          </span>
          <span>
            Hangs up after{" "}
            <span className="text-zinc-300">{readiness.max_duration_seconds}s</span>
          </span>
          <span>
            Test calls are deleted from the call history after{" "}
            <span className="text-zinc-300">{readiness.ttl_hours}h</span>, so they never reach
            the customer&apos;s reports
          </span>
        </div>
      )}

      {error && (
        <div className="rounded-lg border border-red-900/60 bg-red-950/30 px-4 py-3 text-sm text-red-300">
          {error}
        </div>
      )}

      {editing && (
        <ScenarioForm
          // Remounts when you switch between scenarios, so the fields reload rather
          // than keeping the previously edited scenario's values.
          key={editing === "new" ? "new" : editing.id}
          scenario={editing === "new" ? null : editing}
          onCancel={() => setEditing(null)}
          onDone={async () => {
            setEditing(null);
            await mutateScenarios();
          }}
        />
      )}

      <section className="space-y-2">
        <h2 className="text-sm font-medium text-zinc-400">Scenarios</h2>
        {scenarios?.length === 0 && (
          <p className="rounded-xl border border-zinc-800 bg-zinc-900/40 p-4 text-sm text-zinc-500">
            No scenarios yet. A scenario is one rehearsed call: which number to ring, which keys
            to press for the menu, who the caller pretends to be, and what has to be true
            afterwards.
          </p>
        )}
        {scenarios?.map((s) => (
          <div
            key={s.id}
            className="flex items-start justify-between gap-4 rounded-xl border border-zinc-800 bg-zinc-900/60 p-4"
          >
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <span className="font-medium text-zinc-100">{s.name}</span>
                <span className="rounded bg-zinc-800 px-1.5 py-0.5 text-xs text-zinc-400">
                  {s.agent_id}
                </span>
                {!s.enabled && <span className="text-xs text-zinc-600">disabled</span>}
              </div>
              <div className="mt-1 text-xs text-zinc-500">
                {s.to_number}
                {s.dtmf_digits ? ` · presses ${s.dtmf_digits} (${s.dtmf_pause_seconds}s apart)` : " · no menu"}
                {s.criteria.length > 0 ? ` · ${s.criteria.length} criteria` : " · nothing checked"}
              </div>
              <p className="mt-2 line-clamp-2 max-w-2xl text-xs text-zinc-600">{s.persona}</p>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <button
                onClick={() => runScenario(s)}
                disabled={!readiness?.enabled || !s.enabled || running || busy === s.id}
                className="rounded-lg bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-500 disabled:cursor-not-allowed disabled:bg-zinc-800 disabled:text-zinc-500"
                title={running ? "A call is already in progress" : "Place the call now"}
              >
                {busy === s.id ? "Dialling…" : "Call now"}
              </button>
              <button
                onClick={() => {
                  setEditing(s);
                  window.scrollTo({ top: 0, behavior: "smooth" });
                }}
                className="rounded-lg border border-zinc-800 px-2 py-1.5 text-sm text-zinc-400 hover:text-zinc-100"
              >
                Edit
              </button>
              <button
                onClick={() => remove(s)}
                className="rounded-lg border border-zinc-800 px-2 py-1.5 text-sm text-zinc-500 hover:text-zinc-300"
              >
                Delete
              </button>
            </div>
          </div>
        ))}
      </section>

      <section className="space-y-2">
        <h2 className="text-sm font-medium text-zinc-400">Recent runs</h2>
        {runs?.length === 0 && (
          <p className="rounded-xl border border-zinc-800 bg-zinc-900/40 p-4 text-sm text-zinc-500">
            No calls placed yet.
          </p>
        )}
        {runs?.map((run) => (
          <RunRow
            key={run.id}
            run={run}
            expanded={open === run.id}
            onToggle={() => setOpen(open === run.id ? null : run.id)}
            onCancel={() => cancel(run)}
          />
        ))}
      </section>
    </div>
  );
}

function RunRow({
  run,
  expanded,
  onToggle,
  onCancel,
}: {
  run: TestRun;
  expanded: boolean;
  onToggle: () => void;
  onCancel: () => void;
}) {
  const active = ACTIVE.has(run.status);
  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-900/60">
      <button
        onClick={onToggle}
        className="flex w-full items-center justify-between gap-4 p-4 text-left"
      >
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <VerdictBadge run={run} />
            <span className="truncate font-medium text-zinc-200">{run.scenario_name}</span>
            <span className="rounded bg-zinc-800 px-1.5 py-0.5 text-xs text-zinc-400">
              {run.agent_id}
            </span>
          </div>
          <div className="mt-1 text-xs text-zinc-500">
            {new Date(run.started_at).toLocaleString()}
            {run.duration_seconds != null && ` · ${Math.round(run.duration_seconds)}s`}
            {run.ended_on_transfer && " · hung up when a transfer was announced"}
          </div>
        </div>
        <span className="shrink-0 text-xs text-zinc-600">{expanded ? "Hide" : "Details"}</span>
      </button>

      {expanded && (
        <div className="space-y-4 border-t border-zinc-800 p-4 text-sm">
          {run.verdict_reason && <p className="text-zinc-300">{run.verdict_reason}</p>}
          {run.error && <p className="text-amber-400">{run.error}</p>}

          {run.criteria_results && run.criteria_results.length > 0 && (
            <ul className="space-y-1">
              {run.criteria_results.map((c, i) => (
                <li key={i} className="flex gap-2 text-xs">
                  <span className={c.passed ? "text-emerald-400" : "text-red-400"}>
                    {c.passed ? "✓" : "✗"}
                  </span>
                  <span className="text-zinc-400">
                    {c.criterion}
                    {c.note && <span className="text-zinc-600"> — {c.note}</span>}
                  </span>
                </li>
              ))}
            </ul>
          )}

          {run.call_id && !run.call_deleted && (
            <p className="text-xs text-zinc-500">
              The agent&apos;s own record:{" "}
              <a href={`/calls/${run.call_id}`} className="text-indigo-400 hover:underline">
                open the call
              </a>
              {run.call_expires_at &&
                ` — deleted from the call history at ${new Date(
                  run.call_expires_at
                ).toLocaleTimeString()}`}
            </p>
          )}
          {run.call_deleted && (
            <p className="text-xs text-zinc-600">
              The synthetic call has been removed from the call history, as intended. The
              transcript below is kept.
            </p>
          )}
          {!run.call_id && run.status === "completed" && (
            <p className="text-xs text-amber-400">
              The agent never reported this call to CallHarness — which is itself worth
              investigating. Judged on our caller&apos;s transcript alone.
            </p>
          )}

          {run.caller_transcript && run.caller_transcript.length > 0 ? (
            <div className="space-y-1 rounded-lg bg-zinc-950/60 p-3">
              {run.caller_transcript.map((line, i) => (
                <div key={i} className="text-xs">
                  <span
                    className={
                      line.speaker === "agent" ? "text-sky-400" : "text-zinc-500"
                    }
                  >
                    {line.speaker === "agent" ? "Agent" : "Test caller"}:{" "}
                  </span>
                  <span className="text-zinc-300">{line.text}</span>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-xs text-zinc-600">No transcript captured.</p>
          )}

          {active && (
            <button
              onClick={onCancel}
              className="rounded-lg border border-red-900/60 px-3 py-1.5 text-xs text-red-300 hover:bg-red-950/40"
            >
              Hang up now
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function VerdictBadge({ run }: { run: TestRun }) {
  if (ACTIVE.has(run.status)) {
    const label = run.status === "talking" ? "On the call" : "Dialling";
    return (
      <span className="rounded bg-indigo-500/20 px-1.5 py-0.5 text-xs text-indigo-300">
        {label}…
      </span>
    );
  }
  // "error" is kept visually distinct from "fail" on purpose: a test that could not
  // reach a verdict is a broken harness, not a broken agent, and colouring them the
  // same would send someone to debug the wrong system.
  const styles: Record<string, string> = {
    pass: "bg-emerald-500/20 text-emerald-300",
    fail: "bg-red-500/20 text-red-300",
    error: "bg-amber-500/20 text-amber-300",
  };
  const verdict = run.verdict ?? "error";
  const labels: Record<string, string> = {
    pass: "Passed",
    fail: "Failed",
    error: "No verdict",
  };
  return (
    <span className={`rounded px-1.5 py-0.5 text-xs ${styles[verdict] ?? styles.error}`}>
      {labels[verdict] ?? "No verdict"}
    </span>
  );
}

function ScenarioForm({
  scenario,
  onDone,
  onCancel,
}: {
  scenario: TestScenario | null;
  onDone: () => Promise<void>;
  onCancel: () => void;
}) {
  const [name, setName] = useState(scenario?.name ?? "");
  const [agentId, setAgentId] = useState(scenario?.agent_id ?? "");
  const [toNumber, setToNumber] = useState(scenario?.to_number ?? "");
  const [digits, setDigits] = useState(scenario?.dtmf_digits ?? "");
  const [pause, setPause] = useState(String(scenario?.dtmf_pause_seconds ?? 4));
  const [persona, setPersona] = useState(scenario?.persona ?? "");
  const [criteria, setCriteria] = useState((scenario?.criteria ?? []).join("\n"));
  const [maxSeconds, setMaxSeconds] = useState(
    scenario?.max_duration_seconds ? String(scenario.max_duration_seconds) : ""
  );
  const [enabled, setEnabled] = useState(scenario?.enabled ?? true);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    // Every field goes in both directions. A save that omitted one would not leave it
    // alone — the API replaces the whole scenario, so a missing field is a reset.
    const body = {
      name,
      agent_id: agentId,
      to_number: toNumber,
      dtmf_digits: digits || null,
      dtmf_pause_seconds: Number(pause) || 4,
      persona,
      criteria: criteria
        .split("\n")
        .map((c) => c.trim())
        .filter(Boolean),
      max_duration_seconds: maxSeconds ? Number(maxSeconds) : null,
      enabled,
    };
    try {
      if (scenario) {
        await apiSend(`/api/v1/testcalls/scenarios/${scenario.id}`, "PUT", body);
      } else {
        await apiSend("/api/v1/testcalls/scenarios", "POST", body);
      }
      await onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message.replace(/^API error \d+: /, "") : "Could not save.");
    }
  }

  return (
    <form
      onSubmit={submit}
      className="space-y-3 rounded-xl border border-zinc-800 bg-zinc-900/60 p-4"
    >
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-zinc-200">
          {scenario ? `Editing “${scenario.name}”` : "New scenario"}
        </h3>
        {scenario && (
          <label className="flex items-center gap-2 text-xs text-zinc-500">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
              className="accent-indigo-500"
            />
            Enabled
          </label>
        )}
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Name">
          <input
            className={inputClass}
            placeholder="e.g. Opening hours, Milan branch"
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
          />
        </Field>
        <Field label="Region" hint="must match the agent exactly">
          <input
            className={inputClass}
            placeholder="Lazio · Lombardia · Trentino · Piemonte"
            value={agentId}
            onChange={(e) => setAgentId(e.target.value)}
            required
          />
        </Field>
        <Field label="Number to call">
          <input
            className={inputClass}
            placeholder="+3902…"
            value={toNumber}
            onChange={(e) => setToNumber(e.target.value)}
            required
          />
        </Field>
        <div className="grid grid-cols-3 gap-2">
          <Field label="Menu keys">
            <input
              className={inputClass}
              placeholder="2,2"
              value={digits}
              onChange={(e) => setDigits(e.target.value)}
            />
          </Field>
          <Field label="Gap (s)">
            <input
              className={inputClass}
              placeholder="4"
              value={pause}
              onChange={(e) => setPause(e.target.value)}
            />
          </Field>
          <Field label="Max call (s)" hint="blank = default">
            <input
              className={inputClass}
              placeholder="180"
              value={maxSeconds}
              onChange={(e) => setMaxSeconds(e.target.value)}
            />
          </Field>
        </div>
      </div>
      <Field label="Who is calling, and what do they want?" hint="write it in the agent's language">
        <textarea
          className={`${inputClass} h-28`}
          placeholder="Sei un paziente che chiama per sapere gli orari di apertura della sede di via…"
          value={persona}
          onChange={(e) => setPersona(e.target.value)}
          required
        />
      </Field>
      <Field label="What has to be true afterwards?" hint="one per line">
        <textarea
          className={`${inputClass} h-24`}
          placeholder={"The agent gave the opening hours\nThe agent did not transfer the call"}
          value={criteria}
          onChange={(e) => setCriteria(e.target.value)}
        />
      </Field>
      <p className="text-xs text-zinc-600">
        The keys are pressed on a timer before the conversation starts — Twilio cannot send
        them once the call is connected. If the menu&apos;s wording changes, adjust the seconds.
      </p>
      {error && <p className="text-sm text-red-400">{error}</p>}
      <div className="flex items-center gap-2">
        <button
          type="submit"
          className="rounded-lg bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500"
        >
          {scenario ? "Save changes" : "Create scenario"}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-lg border border-zinc-800 px-3 py-1.5 text-sm text-zinc-400 hover:text-zinc-100"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}

/** Label above a control. Placeholders vanish once a field has a value, which makes a
 *  prefilled edit form unreadable — so editing needs real labels, not hints. */
function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block space-y-1">
      <span className="text-xs text-zinc-500">
        {label}
        {hint && <span className="text-zinc-600"> — {hint}</span>}
      </span>
      {children}
    </label>
  );
}

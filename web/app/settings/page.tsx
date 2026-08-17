"use client";

import { useEffect, useState } from "react";
import useSWR from "swr";
import {
  apiSend,
  fetcher,
  type AnalysisConfig,
  type ExtractionField,
  type LookupProbe,
  type Overview,
  type ProbeAttempt,
  type ReasonCategory,
} from "@/lib/api";

const inputClass =
  "w-full rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 placeholder:text-zinc-600";

function Toggle({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <label className="flex cursor-pointer items-center gap-2 text-sm text-zinc-300">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="h-4 w-4 accent-indigo-500"
      />
      {label}
    </label>
  );
}

function TaxonomyEditor({
  title,
  blurb,
  categories,
  onChange,
}: {
  title: string;
  blurb: string;
  categories: ReasonCategory[];
  onChange: (next: ReasonCategory[]) => void;
}) {
  const setRow = (i: number, patch: Partial<ReasonCategory>) =>
    onChange(categories.map((c, j) => (j === i ? { ...c, ...patch } : c)));

  return (
    <div className="space-y-2">
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="text-sm font-medium text-zinc-200">{title}</div>
          <p className="text-sm text-zinc-500">{blurb}</p>
        </div>
        <button
          onClick={() => onChange([...categories, { key: "", description: "" }])}
          className="shrink-0 rounded-lg border border-zinc-700 px-3 py-1 text-sm text-zinc-300 hover:bg-zinc-800"
        >
          + Add category
        </button>
      </div>
      {categories.map((cat, i) => (
        <div key={i} className="grid grid-cols-12 items-start gap-2">
          <input
            value={cat.key}
            onChange={(e) => setRow(i, { key: e.target.value })}
            placeholder="category_key"
            className={`${inputClass} col-span-4 font-mono text-xs`}
          />
          <input
            value={cat.description}
            onChange={(e) => setRow(i, { description: e.target.value })}
            placeholder="When does a call belong in this bucket? The LLM reads this."
            className={`${inputClass} col-span-7`}
          />
          <button
            onClick={() => onChange(categories.filter((_, j) => j !== i))}
            className="col-span-1 rounded-lg px-2 py-2 text-sm text-zinc-500 hover:bg-zinc-800 hover:text-red-400"
            title="Remove category"
          >
            ✕
          </button>
        </div>
      ))}
      {categories.length === 0 && (
        <p className="text-sm text-zinc-500">
          No categories — the built-in defaults will be used.
        </p>
      )}
    </div>
  );
}

/** A blank probe, pre-filled with the VAPI envelope shape so the JSON is not written
 *  from scratch. The URL and the tool name are left empty on purpose — those are the
 *  two fields that must be got right, and a plausible-looking wrong default is worse
 *  than an empty one. */
function blankProbe(): LookupProbe {
  return {
    key: "",
    label: "",
    url: "",
    method: "POST",
    headers: {},
    body_template: JSON.stringify(
      {
        message: {
          toolCallList: [
            {
              id: "callharness-verify",
              function: { name: "", arguments: '{"query": "{{query}}"}' },
            },
          ],
        },
      },
      null,
      2
    ),
    result_path: "results.0.result",
    enabled: true,
    agent_ids: [],
  };
}

function ProbeEditor({
  probes,
  agents,
  onChange,
}: {
  probes: LookupProbe[];
  agents: string[];
  onChange: (next: LookupProbe[]) => void;
}) {
  const [tested, setTested] = useState<Record<number, ProbeAttempt | string>>({});
  const [testing, setTesting] = useState<number | null>(null);
  const [query, setQuery] = useState("");

  const setRow = (i: number, patch: Partial<LookupProbe>) =>
    onChange(probes.map((p, j) => (j === i ? { ...p, ...patch } : p)));

  function toggleAgent(i: number, agent: string) {
    const current = probes[i].agent_ids ?? [];
    setRow(i, {
      agent_ids: current.includes(agent)
        ? current.filter((a) => a !== agent)
        : [...current, agent],
    });
  }

  async function test(i: number) {
    setTesting(i);
    try {
      const res = await apiSend("/api/v1/gaps/probe-test", "POST", {
        probe: probes[i],
        query: query || "orari di apertura",
      });
      setTested({ ...tested, [i]: (res as { attempt: ProbeAttempt }).attempt });
    } catch (e) {
      setTested({ ...tested, [i]: e instanceof Error ? e.message : "Request failed" });
    } finally {
      setTesting(null);
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="text-sm font-medium text-zinc-200">Lookup probes</div>
          <p className="text-sm text-zinc-500">
            Where a missing-record question gets re-asked, to find out whether the record
            is genuinely absent or the lookup simply missed it. List every knowledge source
            the agent uses — each question is sent to all of them for its region, which is
            what settles a call where one tool deferred to another and the agent never
            followed up.
          </p>
        </div>
        <button
          onClick={() => onChange([...probes, blankProbe()])}
          className="shrink-0 rounded-lg border border-zinc-700 px-3 py-1 text-sm text-zinc-300 hover:bg-zinc-800"
        >
          + Add source
        </button>
      </div>

      {probes.length === 0 && (
        <p className="text-sm text-zinc-500">
          None configured. There is no default here — a knowledge-base URL belongs to one
          deployment — so verification stays off and every missing record stays
          &ldquo;Not checked&rdquo; until a source is added.
        </p>
      )}

      {probes.map((probe, i) => {
        const result = tested[i];
        const regions = probe.agent_ids ?? [];
        return (
          <div key={i} className="space-y-2 rounded-lg border border-zinc-800 bg-zinc-950/40 p-3">
            <div className="grid grid-cols-12 items-center gap-2">
              <input
                value={probe.key}
                onChange={(e) => setRow(i, { key: e.target.value })}
                placeholder="rag"
                className={`${inputClass} col-span-2 font-mono text-xs`}
              />
              <input
                value={probe.label}
                onChange={(e) => setRow(i, { label: e.target.value })}
                placeholder="Knowledge base (RAG)"
                className={`${inputClass} col-span-4`}
              />
              <input
                value={probe.url}
                onChange={(e) => setRow(i, { url: e.target.value })}
                placeholder="https://…/lazio/rag_lazio"
                className={`${inputClass} col-span-5 font-mono text-xs`}
              />
              <button
                onClick={() => onChange(probes.filter((_, j) => j !== i))}
                className="col-span-1 rounded-lg px-2 py-2 text-sm text-zinc-500 hover:bg-zinc-800 hover:text-red-400"
                title="Remove source"
              >
                ✕
              </button>
            </div>

            {/* Which regions this source serves. Not cosmetic: these backends dispatch on
                a region-specific tool name and answer an unrecognised one with 200 OK and
                "Tool non supportato" — read as data, that becomes "this record is missing
                from your database" for every gap checked. */}
            <div className="flex flex-wrap items-center gap-1.5 text-xs">
              <span className="text-zinc-500">Regions:</span>
              {agents.map((agent) => (
                <button
                  key={agent}
                  onClick={() => toggleAgent(i, agent)}
                  className={`rounded-full px-2 py-0.5 ${
                    regions.includes(agent)
                      ? "bg-indigo-500/20 text-indigo-300"
                      : "bg-zinc-800 text-zinc-500 hover:text-zinc-300"
                  }`}
                >
                  {agent}
                </button>
              ))}
              <span className="text-zinc-600">
                {regions.length === 0
                  ? "— none selected: this source is used for every region"
                  : ""}
              </span>
            </div>

            <textarea
              value={probe.body_template}
              onChange={(e) => setRow(i, { body_template: e.target.value })}
              rows={7}
              spellCheck={false}
              className={`${inputClass} font-mono text-xs`}
            />

            <div className="grid grid-cols-12 items-center gap-2">
              <input
                value={probe.result_path}
                onChange={(e) => setRow(i, { result_path: e.target.value })}
                placeholder="results.0.result"
                className={`${inputClass} col-span-4 font-mono text-xs`}
              />
              <label className="col-span-3 flex cursor-pointer items-center gap-2 text-sm text-zinc-300">
                <input
                  type="checkbox"
                  checked={probe.enabled}
                  onChange={(e) => setRow(i, { enabled: e.target.checked })}
                  className="h-4 w-4 accent-indigo-500"
                />
                Enabled
              </label>
              <button
                onClick={() => test(i)}
                disabled={testing !== null || !probe.url}
                className="col-span-2 rounded-lg border border-zinc-700 px-3 py-1.5 text-sm text-zinc-300 hover:bg-zinc-800 disabled:opacity-40"
              >
                {testing === i ? "Testing…" : "Test"}
              </button>
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="test question (a real one that should answer)"
                className={`${inputClass} col-span-3 text-xs`}
              />
            </div>

            <p className="text-xs text-zinc-600">
              The body must be valid JSON and contain{" "}
              <code className="text-zinc-400">{"{{query}}"}</code>. Test before saving: a
              wrong URL or tool name still answers 200 OK with a polite sentence, and read
              as data that sentence turns into &ldquo;this record is missing from your
              database&rdquo; for every gap.
            </p>

            {typeof result === "string" && <p className="text-xs text-red-400">{result}</p>}
            {result && typeof result !== "string" && (
              <div className="rounded border border-zinc-800 bg-zinc-900/60 p-2 text-xs">
                <div className="flex flex-wrap gap-x-3 text-zinc-500">
                  <span
                    className={
                      result.verdict === "ok"
                        ? "text-emerald-400"
                        : result.verdict === "empty"
                          ? "text-amber-400"
                          : "text-red-400"
                    }
                  >
                    {result.verdict === "ok"
                      ? "answered"
                      : result.verdict === "empty"
                        ? "nothing found"
                        : "failed / not a lookup"}
                  </span>
                  {result.http_status != null && <span>HTTP {result.http_status}</span>}
                  {result.ms != null && <span>{result.ms}ms</span>}
                </div>
                <div className="mt-1 whitespace-pre-wrap break-words text-zinc-400">
                  {result.response}
                </div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

export default function SettingsPage() {
  const { data, mutate } = useSWR<AnalysisConfig>("/api/v1/config/analysis", fetcher);
  // Only for the region chips on a lookup probe — the list of agents that have actually
  // sent calls is the only honest source of region names.
  const { data: overview } = useSWR<Overview>("/api/v1/analytics/overview", fetcher);
  const [config, setConfig] = useState<AnalysisConfig | null>(null);
  const [saved, setSaved] = useState<"idle" | "saving" | "saved" | "error">("idle");

  useEffect(() => {
    if (data && !config) setConfig(data);
  }, [data, config]);

  if (!config) return <div className="p-8 text-zinc-500">Loading…</div>;

  const set = (patch: Partial<AnalysisConfig>) => {
    setConfig({ ...config, ...patch });
    setSaved("idle");
  };

  const setField = (i: number, patch: Partial<ExtractionField>) => {
    const fields = [...config.extraction_fields];
    fields[i] = { ...fields[i], ...patch };
    set({ extraction_fields: fields });
  };

  async function save() {
    if (!config) return;
    setSaved("saving");
    // Half-filled rows the user added but never named would be rejected by the API,
    // so drop them here rather than surfacing a validation error.
    const payload = {
      ...config,
      buckets: config.buckets.filter((c) => c.key.trim()),
      transfer_reasons: config.transfer_reasons.filter((c) => c.key.trim()),
      non_completion_reasons: config.non_completion_reasons.filter((c) => c.key.trim()),
      // Same reason: a source with no key or no URL cannot be probed, and the API would
      // reject the whole save rather than just that row.
      lookup_probes: (config.lookup_probes ?? []).filter((p) => p.key.trim() && p.url.trim()),
    };
    try {
      const res = await fetch("/api/v1/config/analysis", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error(String(res.status));
      await mutate();
      setSaved("saved");
    } catch {
      setSaved("error");
    }
  }

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-zinc-100">Analysis Settings</h1>
        <p className="text-sm text-zinc-500">
          Configure what the LLM extracts from every call. Changes apply to new and re-analyzed calls.
        </p>
      </div>

      <section className="space-y-3 rounded-xl border border-zinc-800 bg-zinc-900/60 p-4">
        <Toggle label="Generate call summary" checked={config.summary_enabled} onChange={(v) => set({ summary_enabled: v })} />
        {config.summary_enabled && (
          <textarea
            value={config.summary_prompt ?? ""}
            onChange={(e) => set({ summary_prompt: e.target.value || null })}
            placeholder="Optional custom summary instructions (default: 2-3 sentence summary)"
            rows={2}
            className={inputClass}
          />
        )}
      </section>

      <section className="space-y-3 rounded-xl border border-zinc-800 bg-zinc-900/60 p-4">
        <Toggle label="Analyze caller sentiment" checked={config.sentiment_enabled} onChange={(v) => set({ sentiment_enabled: v })} />
      </section>

      <section className="space-y-3 rounded-xl border border-zinc-800 bg-zinc-900/60 p-4">
        <Toggle label="Evaluate call success" checked={config.success_enabled} onChange={(v) => set({ success_enabled: v })} />
        {config.success_enabled && (
          <>
            <textarea
              value={config.success_prompt ?? ""}
              onChange={(e) => set({ success_prompt: e.target.value || null })}
              placeholder="What does a successful call mean for your agent? e.g. 'The call is successful if the caller booked, rescheduled, or cancelled an appointment without being transferred.'"
              rows={3}
              className={inputClass}
            />
            <div className="flex items-center gap-2 text-sm text-zinc-400">
              Rubric:
              <select
                value={config.success_rubric}
                onChange={(e) => set({ success_rubric: e.target.value as AnalysisConfig["success_rubric"] })}
                className="rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-1.5 text-sm text-zinc-300"
              >
                <option value="pass_fail">Pass / Fail</option>
                <option value="numeric_scale">Numeric scale (1-10) + Pass / Fail</option>
              </select>
            </div>
          </>
        )}
      </section>

      <section className="space-y-4 rounded-xl border border-zinc-800 bg-zinc-900/60 p-4">
        <div>
          <Toggle
            label="Sort every call into a bucket"
            checked={config.bucketing_enabled}
            onChange={(v) => set({ bucketing_enabled: v })}
          />
          <p className="mt-1 text-sm text-zinc-500">
            Every analysed call gets exactly one bucket describing what happened, plus a
            one-sentence note about that specific call. Unlike the transfer and
            non-completion reasons below, this applies to <em>every</em> call — including
            successful ones, where a caller who got two of their three answers used to
            leave no trace at all.
          </p>
        </div>
        {config.bucketing_enabled && (
          <>
            <TaxonomyEditor
              title="Buckets"
              blurb="What happened on the call. The description is what the analysis reads to decide — vague descriptions produce vague classification."
              categories={config.buckets}
              onChange={(v) => set({ buckets: v })}
            />
            <p className="text-sm text-zinc-500">
              Order matters: a call can fit several buckets and only stores one, so the
              analysis takes the first match in the built-in severity order. Adding a
              bucket is safe; renaming a key orphans every call already classified under
              it. Review the <code className="text-zinc-400">other</code> bucket on the{" "}
              <a href="/other" className="text-indigo-400 hover:underline">Other</a> page
              and promote anything recurring.
            </p>
          </>
        )}
      </section>

      <section className="space-y-4 rounded-xl border border-zinc-800 bg-zinc-900/60 p-4">
        <div>
          <Toggle
            label="Classify why calls transfer or don't complete (superseded by buckets)"
            checked={config.classification_enabled}
            onChange={(v) => set({ classification_enabled: v })}
          />
          <p className="mt-1 text-sm text-zinc-500">
            The older two-taxonomy scheme. It only ever applied to transferred or
            non-completed calls, so the same root cause filed under two different keys
            depending on how the call ended. Buckets replaced it. Leaving this{" "}
            <strong>off</strong> preserves the labels already on your calls — they stay
            queryable and keep showing on the call pages; turning it back on makes the
            analysis start overwriting them again.
          </p>
        </div>
        {config.classification_enabled && (
          <>
            <TaxonomyEditor
              title="Transfer reasons"
              blurb="Why a call was handed to a human."
              categories={config.transfer_reasons}
              onChange={(v) => set({ transfer_reasons: v })}
            />
            <TaxonomyEditor
              title="Non-completion reasons"
              blurb="Why a call ended without the caller's need being resolved."
              categories={config.non_completion_reasons}
              onChange={(v) => set({ non_completion_reasons: v })}
            />
            <p className="text-sm text-zinc-500">
              Renaming a key leaves calls already classified under the old one behind —
              add a new category instead when you want to split a bucket. Re-analyze a
              call to reclassify it under the updated categories.
            </p>
          </>
        )}
      </section>

      <section className="space-y-3 rounded-xl border border-zinc-800 bg-zinc-900/60 p-4">
        <ProbeEditor
          probes={config.lookup_probes ?? []}
          agents={overview?.agents ?? []}
          onChange={(v) => set({ lookup_probes: v })}
        />
      </section>

      <section className="space-y-3 rounded-xl border border-zinc-800 bg-zinc-900/60 p-4">
        <div className="text-sm font-medium text-zinc-200">Analysis language</div>
        <p className="text-sm text-zinc-500">
          The language summaries, evaluations, and extracted text are written in — even when
          the call itself is in another language (e.g. calls in Italian, analysis in English).
        </p>
        <select
          value={config.output_language ?? "english"}
          onChange={(e) => set({ output_language: e.target.value })}
          className="rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-1.5 text-sm text-zinc-300"
        >
          <option value="english">English</option>
          <option value="italian">Italian</option>
          <option value="spanish">Spanish</option>
          <option value="french">French</option>
          <option value="german">German</option>
          <option value="auto">Same language as the call</option>
        </select>
      </section>

      <section className="space-y-3 rounded-xl border border-zinc-800 bg-zinc-900/60 p-4">
        <div className="flex items-center justify-between">
          <Toggle label="Extract structured data" checked={config.extraction_enabled} onChange={(v) => set({ extraction_enabled: v })} />
          {config.extraction_enabled && (
            <button
              onClick={() =>
                set({
                  extraction_fields: [
                    ...config.extraction_fields,
                    { name: "", type: "text", description: "", choices: null },
                  ],
                })
              }
              className="rounded-lg border border-zinc-700 px-3 py-1 text-sm text-zinc-300 hover:bg-zinc-800"
            >
              + Add field
            </button>
          )}
        </div>
        {config.extraction_enabled &&
          config.extraction_fields.map((field, i) => (
            <div key={i} className="grid grid-cols-12 items-start gap-2 rounded-lg border border-zinc-800 p-3">
              <input
                value={field.name}
                onChange={(e) => setField(i, { name: e.target.value })}
                placeholder="field_name"
                className={`${inputClass} col-span-3`}
              />
              <select
                value={field.type}
                onChange={(e) => setField(i, { type: e.target.value as ExtractionField["type"] })}
                className={`${inputClass} col-span-2`}
              >
                <option value="text">Text</option>
                <option value="boolean">Boolean</option>
                <option value="number">Number</option>
                <option value="enum">Enum</option>
              </select>
              <input
                value={field.description}
                onChange={(e) => setField(i, { description: e.target.value })}
                placeholder="What to extract, e.g. 'The caller's intent'"
                className={`${inputClass} ${field.type === "enum" ? "col-span-3" : "col-span-6"}`}
              />
              {field.type === "enum" && (
                <input
                  value={(field.choices ?? []).join(", ")}
                  onChange={(e) =>
                    setField(i, {
                      choices: e.target.value.split(",").map((s) => s.trim()).filter(Boolean),
                    })
                  }
                  placeholder="choice1, choice2"
                  className={`${inputClass} col-span-3`}
                />
              )}
              <button
                onClick={() =>
                  set({ extraction_fields: config.extraction_fields.filter((_, j) => j !== i) })
                }
                className="col-span-1 rounded-lg px-2 py-2 text-sm text-zinc-500 hover:bg-zinc-800 hover:text-red-400"
                title="Remove field"
              >
                ✕
              </button>
            </div>
          ))}
        {config.extraction_enabled && config.extraction_fields.length === 0 && (
          <p className="text-sm text-zinc-500">
            No fields yet. Add fields like <code className="text-zinc-400">intent</code> (enum),{" "}
            <code className="text-zinc-400">callback_needed</code> (boolean), or{" "}
            <code className="text-zinc-400">order_number</code> (text).
          </p>
        )}
      </section>

      <div className="flex items-center gap-3">
        <button
          onClick={save}
          disabled={saved === "saving"}
          className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
        >
          {saved === "saving" ? "Saving…" : "Save settings"}
        </button>
        {saved === "saved" && <span className="text-sm text-emerald-400">Saved.</span>}
        {saved === "error" && <span className="text-sm text-red-400">Failed to save.</span>}
      </div>
    </div>
  );
}

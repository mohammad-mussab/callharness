"use client";

import { useEffect, useState } from "react";
import useSWR from "swr";
import {
  fetcher,
  type AnalysisConfig,
  type ExtractionField,
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

export default function SettingsPage() {
  const { data, mutate } = useSWR<AnalysisConfig>("/api/v1/config/analysis", fetcher);
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
      transfer_reasons: config.transfer_reasons.filter((c) => c.key.trim()),
      non_completion_reasons: config.non_completion_reasons.filter((c) => c.key.trim()),
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
            label="Classify why calls transfer or don't complete"
            checked={config.classification_enabled}
            onChange={(v) => set({ classification_enabled: v })}
          />
          <p className="mt-1 text-sm text-zinc-500">
            Every transferred or non-completed call is sorted into one of your categories,
            which is what the breakdown charts on the overview page are built from. Edit
            these here — no change to your agent's code. Review the{" "}
            <code className="text-zinc-400">other</code> bucket now and then, and promote
            anything recurring into its own category.
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

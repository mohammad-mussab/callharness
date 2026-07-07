"use client";

import { useEffect, useState } from "react";
import useSWR from "swr";
import { fetcher, type AnalysisConfig, type ExtractionField } from "@/lib/api";

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
    try {
      const res = await fetch("/api/v1/config/analysis", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(config),
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

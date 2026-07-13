"use client";

import { useState } from "react";
import useSWR from "swr";
import { apiSend, fetcher, type Evaluator, type EvaluatorStats } from "@/lib/api";

const inputClass =
  "w-full rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 placeholder:text-zinc-600";

export default function EvaluatorsPage() {
  const { data: evaluators, mutate } = useSWR<EvaluatorStats[]>("/api/v1/evaluators", fetcher, {
    refreshInterval: 15000,
  });
  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function createEvaluator(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await apiSend("/api/v1/evaluators", "POST", { name, prompt, enabled: true });
      setName("");
      setPrompt("");
      setShowForm(false);
      await mutate();
    } catch {
      setError("Failed to create evaluator.");
    }
  }

  async function toggle(stats: EvaluatorStats) {
    const full: Evaluator = await fetcher(`/api/v1/evaluators/${stats.id}`);
    await apiSend(`/api/v1/evaluators/${stats.id}`, "PUT", {
      name: full.name,
      prompt: full.prompt,
      enabled: !full.enabled,
    });
    await mutate();
  }

  async function remove(stats: EvaluatorStats) {
    await apiSend(`/api/v1/evaluators/${stats.id}`, "DELETE");
    await mutate();
  }

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-zinc-100">Custom Checks</h1>
          <p className="max-w-xl text-sm text-zinc-500">
            Yes/no questions that get asked about <span className="text-zinc-300">every call</span>,
            answered automatically by AI. Example: &quot;Did the agent greet the caller and mention the
            company name?&quot; Each call gets a Pass or Fail, and the bar shows how often your agent
            passes — so you can see if a prompt change made things better or worse.
          </p>
        </div>
        <button
          onClick={() => setShowForm((v) => !v)}
          className="rounded-lg bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500"
        >
          {showForm ? "Cancel" : "+ New check"}
        </button>
      </div>

      {showForm && (
        <form onSubmit={createEvaluator} className="space-y-3 rounded-xl border border-zinc-800 bg-zinc-900/60 p-4">
          <input
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Name, e.g. 'Verified caller identity'"
            className={inputClass}
          />
          <textarea
            required
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            rows={3}
            placeholder="Pass criterion, e.g. 'The assistant verified the caller's name and date of birth before sharing any account details.'"
            className={inputClass}
          />
          <div className="flex items-center gap-3">
            <button type="submit" className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500">
              Create check
            </button>
            {error && <span className="text-sm text-red-400">{error}</span>}
          </div>
          <p className="text-xs text-zinc-600">
            New checks run on new calls automatically; use &quot;Re-analyze&quot; on a call to apply them to older calls too.
          </p>
        </form>
      )}

      <div className="space-y-2">
        {evaluators?.map((evaluator) => (
          <div key={evaluator.id} className="rounded-xl border border-zinc-800 bg-zinc-900/60 px-4 py-3">
            <div className="flex flex-wrap items-center gap-3">
              <button
                onClick={() => toggle(evaluator)}
                className={`relative h-5 w-9 rounded-full transition ${evaluator.enabled ? "bg-indigo-500" : "bg-zinc-700"}`}
                title={evaluator.enabled ? "Disable" : "Enable"}
              >
                <span
                  className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all ${evaluator.enabled ? "left-4" : "left-0.5"}`}
                />
              </button>
              <div className="min-w-0 flex-1">
                <div className="text-sm font-medium text-zinc-200">{evaluator.name}</div>
                <div className="text-xs text-zinc-500">
                  {evaluator.total > 0
                    ? `${evaluator.passed}/${evaluator.total} calls passed`
                    : "No results yet"}
                </div>
              </div>
              {evaluator.pass_rate != null && (
                <div className="w-40">
                  <div className="mb-1 text-right text-sm font-medium text-zinc-200">
                    {Math.round(evaluator.pass_rate * 100)}%
                  </div>
                  <div className="h-2 rounded-full bg-zinc-800">
                    <div
                      className={`h-2 rounded-full ${
                        evaluator.pass_rate >= 0.8
                          ? "bg-emerald-500"
                          : evaluator.pass_rate >= 0.5
                            ? "bg-amber-500"
                            : "bg-red-500"
                      }`}
                      style={{ width: `${evaluator.pass_rate * 100}%` }}
                    />
                  </div>
                </div>
              )}
              <button
                onClick={() => remove(evaluator)}
                className="rounded-lg px-2 py-1 text-sm text-zinc-500 hover:bg-zinc-800 hover:text-red-400"
              >
                Delete
              </button>
            </div>
          </div>
        ))}
        {evaluators && evaluators.length === 0 && (
          <p className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-6 text-center text-sm text-zinc-500">
            No checks yet. Try one like: &quot;The agent confirmed the caller&apos;s phone number before ending the call.&quot;
          </p>
        )}
      </div>
    </div>
  );
}

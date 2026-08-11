"use client";

import { useState } from "react";
import useSWR from "swr";
import {
  apiSend,
  fetcher,
  type AlertEvent,
  type AlertRule,
  type AlertTrigger,
} from "@/lib/api";
import { formatDate, titleCase } from "@/lib/format";

const inputClass =
  "rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 placeholder:text-zinc-600";

const TRIGGERS: { value: AlertTrigger; label: string; hint: string; usesThreshold?: string; usesKeyword?: boolean; windowed?: boolean }[] = [
  { value: "negative_sentiment_call", label: "Negative sentiment call", hint: "Fires when a call's sentiment score is at or below the threshold", usesThreshold: "Sentiment score (default -0.5)" },
  { value: "failed_call", label: "Failed call", hint: "Fires when the success evaluation marks a call as failed" },
  { value: "keyword_match", label: "Keyword mention", hint: "Fires when a word or phrase appears in a transcript", usesKeyword: true },
  { value: "high_latency_call", label: "High latency call", hint: "Fires when a call's average response latency exceeds the threshold", usesThreshold: "Latency in ms (default 2000)" },
  { value: "success_rate_window", label: "Success rate drop (windowed)", hint: "Fires when the success rate over the window falls below the threshold", usesThreshold: "Rate 0-1 (default 0.7)", windowed: true },
  { value: "sentiment_window", label: "Sentiment drop (windowed)", hint: "Fires when average sentiment over the window falls below the threshold", usesThreshold: "Score (default -0.2)", windowed: true },
];

const emptyForm = {
  name: "",
  trigger: "negative_sentiment_call" as AlertTrigger,
  threshold: "",
  keyword: "",
  window_minutes: 60,
  min_calls: 5,
  channel: "slack" as "slack" | "webhook" | "email",
  target_url: "",
  cooldown_minutes: 15,
};

export default function AlertsPage() {
  const { data: rules, mutate: mutateRules } = useSWR<AlertRule[]>("/api/v1/alerts/rules", fetcher);
  const { data: events } = useSWR<AlertEvent[]>("/api/v1/alerts/events?limit=25", fetcher, {
    refreshInterval: 15000,
  });
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(emptyForm);
  const [error, setError] = useState<string | null>(null);

  const triggerSpec = TRIGGERS.find((t) => t.value === form.trigger)!;

  async function createRule(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await apiSend("/api/v1/alerts/rules", "POST", {
        name: form.name,
        enabled: true,
        trigger: form.trigger,
        threshold: form.threshold === "" ? null : Number(form.threshold),
        keyword: form.keyword || null,
        window_minutes: form.window_minutes,
        min_calls: form.min_calls,
        channel: form.channel,
        target_url: form.target_url,
        cooldown_minutes: form.cooldown_minutes,
      });
      setForm(emptyForm);
      setShowForm(false);
      await mutateRules();
    } catch {
      setError("Failed to create rule — check the fields.");
    }
  }

  async function toggleRule(rule: AlertRule) {
    await apiSend(`/api/v1/alerts/rules/${rule.id}`, "PUT", { ...rule, enabled: !rule.enabled });
    await mutateRules();
  }

  async function deleteRule(rule: AlertRule) {
    await apiSend(`/api/v1/alerts/rules/${rule.id}`, "DELETE");
    await mutateRules();
  }

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-zinc-100">Alerts</h1>
          <p className="text-sm text-zinc-500">
            Get notified in Slack or via webhook when calls go wrong
          </p>
        </div>
        <button
          onClick={() => setShowForm((v) => !v)}
          className="rounded-lg bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500"
        >
          {showForm ? "Cancel" : "+ New rule"}
        </button>
      </div>

      {showForm && (
        <form onSubmit={createRule} className="space-y-3 rounded-xl border border-zinc-800 bg-zinc-900/60 p-4">
          <div className="grid gap-3 md:grid-cols-2">
            <input
              required
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              placeholder="Rule name, e.g. 'Angry caller alert'"
              className={inputClass}
            />
            <select
              value={form.trigger}
              onChange={(e) => setForm({ ...form, trigger: e.target.value as AlertTrigger })}
              className={inputClass}
            >
              {TRIGGERS.map((t) => (
                <option key={t.value} value={t.value}>{t.label}</option>
              ))}
            </select>
          </div>
          <p className="text-xs text-zinc-500">{triggerSpec.hint}</p>
          <div className="grid gap-3 md:grid-cols-3">
            {triggerSpec.usesThreshold && (
              <input
                value={form.threshold}
                onChange={(e) => setForm({ ...form, threshold: e.target.value })}
                placeholder={triggerSpec.usesThreshold}
                className={inputClass}
              />
            )}
            {triggerSpec.usesKeyword && (
              <input
                required
                value={form.keyword}
                onChange={(e) => setForm({ ...form, keyword: e.target.value })}
                placeholder="Keyword or phrase"
                className={inputClass}
              />
            )}
            {triggerSpec.windowed && (
              <>
                <input
                  type="number"
                  value={form.window_minutes}
                  onChange={(e) => setForm({ ...form, window_minutes: Number(e.target.value) })}
                  placeholder="Window minutes"
                  className={inputClass}
                />
                <input
                  type="number"
                  value={form.min_calls}
                  onChange={(e) => setForm({ ...form, min_calls: Number(e.target.value) })}
                  placeholder="Min calls in window"
                  className={inputClass}
                />
              </>
            )}
          </div>
          <div className="grid gap-3 md:grid-cols-3">
            <select
              value={form.channel}
              onChange={(e) =>
                setForm({ ...form, channel: e.target.value as "slack" | "webhook" | "email" })
              }
              className={inputClass}
            >
              <option value="slack">Slack incoming webhook</option>
              <option value="email">Email</option>
              <option value="webhook">Generic JSON webhook</option>
            </select>
            <input
              required
              value={form.target_url}
              onChange={(e) => setForm({ ...form, target_url: e.target.value })}
              placeholder={
                form.channel === "email"
                  ? "team@company.com, manager@company.com"
                  : "https://hooks.slack.com/services/…"
              }
              className={`${inputClass} md:col-span-2`}
            />
          </div>
          {form.channel === "email" && (
            <p className="text-xs text-zinc-500">
              Email needs SMTP configured on the server once: set CALLHARNESS_SMTP_HOST,
              CALLHARNESS_SMTP_USER, CALLHARNESS_SMTP_PASSWORD and CALLHARNESS_SMTP_FROM in the
              server&apos;s .env (for Gmail: smtp.gmail.com with an app password).
            </p>
          )}
          <div className="flex items-center gap-3">
            <button type="submit" className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500">
              Create rule
            </button>
            {error && <span className="text-sm text-red-400">{error}</span>}
          </div>
        </form>
      )}

      <div className="space-y-2">
        {rules?.map((rule) => {
          const spec = TRIGGERS.find((t) => t.value === rule.trigger);
          return (
            <div key={rule.id} className="flex flex-wrap items-center gap-3 rounded-xl border border-zinc-800 bg-zinc-900/60 px-4 py-3">
              <button
                onClick={() => toggleRule(rule)}
                className={`relative h-5 w-9 rounded-full transition ${rule.enabled ? "bg-indigo-500" : "bg-zinc-700"}`}
                title={rule.enabled ? "Disable" : "Enable"}
              >
                <span
                  className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all ${rule.enabled ? "left-4" : "left-0.5"}`}
                />
              </button>
              <div className="min-w-0 flex-1">
                <div className="text-sm font-medium text-zinc-200">{rule.name}</div>
                <div className="text-xs text-zinc-500">
                  {spec?.label ?? rule.trigger}
                  {rule.keyword ? ` · "${rule.keyword}"` : ""}
                  {rule.threshold != null ? ` · threshold ${rule.threshold}` : ""}
                  {spec?.windowed ? ` · ${rule.window_minutes}m window` : ""}
                  {` · ${rule.channel === "slack" ? "Slack" : rule.channel === "email" ? "Email" : "Webhook"}`}
                  {rule.last_fired_at ? ` · last fired ${formatDate(rule.last_fired_at)}` : ""}
                </div>
              </div>
              <button
                onClick={() => deleteRule(rule)}
                className="rounded-lg px-2 py-1 text-sm text-zinc-500 hover:bg-zinc-800 hover:text-red-400"
              >
                Delete
              </button>
            </div>
          );
        })}
        {rules && rules.length === 0 && (
          <p className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-6 text-center text-sm text-zinc-500">
            No alert rules yet. Create one to get notified about bad calls.
          </p>
        )}
      </div>

      <div>
        <h2 className="mb-2 text-sm font-medium text-zinc-300">Recent alerts</h2>
        <div className="space-y-2">
          {events?.map((event) => (
            <div key={event.id} className="rounded-xl border border-zinc-800 bg-zinc-900/40 px-4 py-3">
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <span className="font-medium text-amber-400">{event.rule_name}</span>
                <span className="text-zinc-600">{formatDate(event.fired_at)}</span>
                {event.delivered ? (
                  <span className="text-emerald-500">delivered</span>
                ) : (
                  <span className="text-red-400" title={event.delivery_error ?? ""}>
                    delivery failed
                  </span>
                )}
                {event.call_id && (
                  <a href={`/calls/${event.call_id}`} className="text-indigo-400 hover:underline">
                    view call →
                  </a>
                )}
              </div>
              <p className="mt-1 text-sm text-zinc-300">{event.message}</p>
            </div>
          ))}
          {events && events.length === 0 && (
            <p className="rounded-xl border border-zinc-800 bg-zinc-900/40 p-6 text-center text-sm text-zinc-500">
              No alerts fired yet.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

import { titleCase } from "@/lib/format";
import { label, labelWithKey } from "@/lib/labels";

const badgeBase =
  "inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium";

export function SentimentBadge({ label }: { label: string | null }) {
  if (!label) return <span className="text-zinc-500 text-xs">—</span>;
  const styles: Record<string, string> = {
    positive: "bg-emerald-500/15 text-emerald-400",
    neutral: "bg-zinc-500/15 text-zinc-300",
    negative: "bg-red-500/15 text-red-400",
  };
  return (
    <span className={`${badgeBase} ${styles[label] ?? styles.neutral}`}>
      {titleCase(label)}
    </span>
  );
}

// analysis_status describes the ANALYSIS pipeline, not the call. Its "completed"
// means "the LLM finished", which is a completely different claim from the outcome
// badge's "Completed" — yet it rendered the same word in a similar colour on every
// analysed call, so a non-completed call showed "Completed  Non-completed" side by
// side. Once analysis has succeeded there is nothing to report: the outcome badge
// is the answer, and its presence already implies analysis ran. So this renders
// only the states that actually need attention.
const STATUS_LABELS: Record<string, string> = {
  pending: "Analysis queued",
  processing: "Analyzing…",
  failed: "Analysis failed",
  skipped: "Not analyzed",
};

export function StatusBadge({ status }: { status: string }) {
  if (status === "completed") return null;
  const styles: Record<string, string> = {
    pending: "bg-amber-500/15 text-amber-400",
    processing: "bg-amber-500/15 text-amber-400",
    failed: "bg-red-500/15 text-red-400",
    skipped: "bg-zinc-500/15 text-zinc-400",
  };
  return (
    <span className={`${badgeBase} ${styles[status] ?? styles.skipped}`}>
      {STATUS_LABELS[status] ?? titleCase(status)}
    </span>
  );
}

// Renders nothing when unset, like its sibling reason badges. It used to emit a "—"
// placeholder, which put a stray dash in front of every non-transferred call's badge
// ("— Hung Up Silently"): agents deliberately leave end_reason null when the call
// wasn't transferred, so CallHarness's own analysis decides the outcome. A group of
// badges needs at most one placeholder, and that belongs to whoever lays the group
// out — see the calls table, which shows a single dash only when the row has none.
export function EndReasonBadge({ reason }: { reason: string | null }) {
  if (!reason) return null;
  return (
    <span className={`${badgeBase} bg-zinc-700/40 text-zinc-300`} title={labelWithKey(reason)}>
      {label(reason)}
    </span>
  );
}

const OUTCOME_STYLES: Record<string, string> = {
  completed: "bg-emerald-500/15 text-emerald-400",
  transferred: "bg-violet-500/15 text-violet-300",
  non_completed: "bg-red-500/15 text-red-400",
};

const OUTCOME_LABELS: Record<string, string> = {
  completed: "Completed",
  transferred: "Transferred",
  non_completed: "Non-completed",
};

export function OutcomeBadge({ outcome }: { outcome: string | null }) {
  if (!outcome) return <span className="text-zinc-500 text-xs">—</span>;
  return (
    <span className={`${badgeBase} ${OUTCOME_STYLES[outcome] ?? "bg-zinc-700/40 text-zinc-300"}`}>
      {OUTCOME_LABELS[outcome] ?? titleCase(outcome)}
    </span>
  );
}

// Shown alongside OutcomeBadge (which already says "Transferred"/"Non-completed"),
// so these only need to add the *reason*, not repeat the outcome itself.
// `source` says who decided: the agent sent it with the call, or CallHarness's
// analysis inferred it. Surfaced on hover rather than as a second badge — it
// matters when a label looks wrong, not on every row.
type ReasonSource = "agent" | "llm" | null;

// Hover shows the stored key alongside the English label, so a badge can always be
// matched against the operational database that uses the original terminology.
function sourceTitle(reason: string, source: ReasonSource) {
  const base = labelWithKey(reason);
  if (source === "agent") return `${base} — classified by the agent at call end`;
  if (source === "llm") return `${base} — inferred by CallHarness's analysis`;
  return base;
}

export function TransferReasonBadge({
  reason,
  source = null,
}: {
  reason: string | null;
  source?: ReasonSource;
}) {
  if (!reason) return null;
  return (
    <span className={`${badgeBase} bg-violet-500/15 text-violet-300`} title={sourceTitle(reason, source)}>
      {label(reason)}
    </span>
  );
}

export function NonCompletionReasonBadge({
  reason,
  source = null,
}: {
  reason: string | null;
  source?: ReasonSource;
}) {
  if (!reason) return null;
  return (
    <span className={`${badgeBase} bg-amber-500/15 text-amber-300`} title={sourceTitle(reason, source)}>
      {label(reason)}
    </span>
  );
}

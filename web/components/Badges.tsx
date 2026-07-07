import { titleCase } from "@/lib/format";

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

export function SuccessBadge({ success }: { success: boolean | null }) {
  if (success === null)
    return <span className="text-zinc-500 text-xs">—</span>;
  return success ? (
    <span className={`${badgeBase} bg-emerald-500/15 text-emerald-400`}>Success</span>
  ) : (
    <span className={`${badgeBase} bg-red-500/15 text-red-400`}>Failed</span>
  );
}

export function StatusBadge({ status }: { status: string }) {
  const styles: Record<string, string> = {
    completed: "bg-indigo-500/15 text-indigo-300",
    pending: "bg-amber-500/15 text-amber-400",
    processing: "bg-amber-500/15 text-amber-400",
    failed: "bg-red-500/15 text-red-400",
    skipped: "bg-zinc-500/15 text-zinc-400",
  };
  return (
    <span className={`${badgeBase} ${styles[status] ?? styles.skipped}`}>
      {titleCase(status)}
    </span>
  );
}

export function EndReasonBadge({ reason }: { reason: string | null }) {
  if (!reason) return <span className="text-zinc-500 text-xs">—</span>;
  return (
    <span className={`${badgeBase} bg-zinc-700/40 text-zinc-300`}>
      {titleCase(reason)}
    </span>
  );
}

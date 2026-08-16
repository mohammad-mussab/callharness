/**
 * English display labels for classification keys.
 *
 * WHY THIS IS DISPLAY-ONLY
 * The stored key is the source of truth and is never changed here. It is what lives
 * on the call row, what the breakdown charts slice on, what the `?transfer_reason=`
 * filter matches, and — for deployments whose agent already classifies its own calls
 * — what joins back to that system's own column. Renaming a key would orphan every
 * call already classified under the old one.
 *
 * So this maps key -> English label at render time only. The key is still shown in
 * brackets so a value on this dashboard can always be matched against the operational
 * database without a lookup table.
 *
 * TODO: these belong in the editable taxonomy (Settings), alongside `key` and
 * `description`, so a deployment in any language can set its own labels without a
 * code change. Hardcoded here for now because the keys are few and stable.
 */

const LABELS: Record<string, string> = {
  // Buckets — what actually happened on the call (server/app/buckets.py)
  answered: "Answered",
  partial_answered: "Partly answered",
  agent_invented_answer: "Invented an answer",
  tool_kept_asking: "Tool kept asking",
  caller_abandoned: "Caller abandoned",
  record_missing: "Record missing",
  lookup_error: "Lookup error",
  needs_human: "Needs a human",
  out_of_scope: "Out of scope",
  no_caller_audio: "No caller audio",

  // Transfer reasons — Italian keys mirror the agent's `motivazione` column
  mancata_comprensione: "Not understood",
  argomento_sconosciuto: "Unknown topic",
  richiesta_paziente: "Caller asked for operator",
  prenotazione: "Booking needs operator",

  // Non-completion reasons
  interrotta_dal_paziente: "Caller hung up",
  fuori_orario: "Outside opening hours",
  problema_tecnico: "Technical problem",

  // Built-in defaults (English deployments / fallback taxonomy)
  knowledge_gap: "Knowledge gap",
  caller_requested_human: "Caller asked for human",
  agent_confusion_loop: "Agent confusion loop",
  technical_error: "Technical error",
  policy_escalation: "Policy escalation",
  caller_hangup_frustrated: "Hung up frustrated",
  caller_hangup_silent: "Hung up silently",
  silence_timeout: "Silence timeout",
  technical_disconnect: "Technical disconnect",
  agent_error: "Agent error",
  other: "Other",

  // Outcomes and end reasons
  completed: "Completed",
  transferred: "Transferred",
  non_completed: "Not completed",
  error: "Error",
  cancelled: "Cancelled",
  unknown: "Unknown",

  // Sentiment
  positive: "Positive",
  neutral: "Neutral",
  negative: "Negative",
};

/** Title-case a raw key when no translation exists, so nothing renders as snake_case. */
function fallback(key: string): string {
  return key
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

/** English label alone — for chart axes and other tight spaces. */
export function label(key: string | null | undefined): string {
  if (!key) return "—";
  return LABELS[key] ?? fallback(key);
}

/**
 * English label with the stored key in brackets, e.g.
 *   "Unknown topic (argomento_sconosciuto)"
 * Use wherever the value may need matching against the operational database.
 * Returns the label alone when the key is already plain English.
 */
export function labelWithKey(key: string | null | undefined): string {
  if (!key) return "—";
  const english = LABELS[key];
  if (!english) return fallback(key);
  const normalized = english.toLowerCase().replace(/[^a-z]/g, "");
  const keyNormalized = key.toLowerCase().replace(/[^a-z]/g, "");
  return normalized === keyNormalized ? english : `${english} (${key})`;
}

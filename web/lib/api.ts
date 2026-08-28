export type ToolCall = {
  name: string;
  arguments: unknown;
  result: unknown;
  success: boolean | null;
};

export type Turn = {
  idx: number;
  role: "user" | "assistant";
  text: string;
  translated_text: string | null;
  start_time: number | null;
  end_time: number | null;
  latency_ms: number | null;
  stt_ms: number | null;
  llm_ttft_ms: number | null;
  tts_ttfb_ms: number | null;
  interrupted: boolean;
  tool_calls: ToolCall[] | null;
};

export type Quality = {
  user_talk_seconds?: number;
  assistant_talk_seconds?: number;
  talk_ratio?: number | null;
  assistant_wpm?: number | null;
  longest_monologue_words?: number;
  interruption_count?: number;
  max_silence_seconds?: number;
  total_silence_seconds?: number;
  long_silence_count?: number;
  turn_count?: number;
};

export type Call = {
  id: string;
  external_id: string | null;
  agent_id: string;
  direction: string;
  from_number: string | null;
  to_number: string | null;
  started_at: string;
  ended_at: string | null;
  duration_seconds: number | null;
  transferred: boolean;
  recording_url: string | null;
  has_recording: boolean;
  has_log: boolean;
  metadata: Record<string, unknown> | null;
  analysis_status: string;
  analysis_error: string | null;
  summary: string | null;
  sentiment_label: string | null;
  sentiment_score: number | null;
  success: boolean | null;
  success_score: number | null;
  success_rationale: string | null;
  structured_data: Record<string, unknown> | null;
  // What happened on this call — one key from the configured taxonomy (buckets.py).
  bucket: string | null;
  issue_note: string | null;
  unanswered_query: string | null;
  // Which missing record this call was merged into, and that record's canonical wording.
  gap_group_id: string | null;
  gap_group_question: string | null;
  // Superseded by `bucket`; historical values only, nothing writes them now.
  transfer_reason: string | null;
  non_completion_reason: string | null;
  reason_source: "agent" | "llm" | null;
  outcome: "transferred" | "completed" | "non_completed";
  quality: Quality | null;
  interruption_count: number;
  language: string | null;
  llm_model: string | null;
  created_at: string;
};

export type EvaluationResult = {
  evaluator_id: number;
  evaluator_name: string;
  passed: boolean | null;
  reason: string | null;
  created_at: string;
};

export type CallDetail = Call & {
  turns: Turn[];
  evaluations: EvaluationResult[];
  // Read from the gap group this call belongs to, not from the call — verification is
  // about the record, and several calls share one.
  gap_status: GapStatus | null;
  gap_status_note: string | null;
};

export type CallList = {
  items: Call[];
  total: number;
  limit: number;
  offset: number;
};

export type Overview = {
  total_calls: number;
  analyzed_calls: number;
  success_rate: number | null;
  transfer_rate: number | null;
  avg_duration_seconds: number | null;
  avg_sentiment_score: number | null;
  sentiment_distribution: Record<string, number>;
  outcome_distribution: Record<string, number>;
  bucket_breakdown: { reason: string; count: number }[];
  raw_answer_rate: number | null;
  addressable_answer_rate: number | null;
  transfer_reason_breakdown: { reason: string; count: number }[];
  non_completion_reason_breakdown: { reason: string; count: number }[];
  agents: string[];
  agent_stats: AgentStats[];
};

export type Buckets = {
  total_calls: number;
  bucketed_calls: number;
  distribution: { bucket: string; count: number }[];
  raw_answer_rate: number | null;
  addressable_answer_rate: number | null;
  agent_stats: {
    agent_id: string;
    calls: number;
    bucketed: number;
    raw_answer_rate: number | null;
    addressable_answer_rate: number | null;
  }[];
};

export type AgentStats = {
  agent_id: string;
  calls: number;
  success_rate: number | null;
  avg_sentiment: number | null;
  avg_duration_seconds: number | null;
  transfer_rate: number | null;
};

export type DisputedCall = {
  id: string;
  started_at: string;
  agent_id: string;
  duration_seconds: number | null;
  kind: "outcome" | "reason";
  overcount: boolean;
  agent_esito: string | null;
  agent_motivazione: string | null;
  callharness_outcome: string;
  callharness_reason: string | null;
  summary: string | null;
  success_rationale: string | null;
  failed_tool_calls: string[];
};

export type Disputes = {
  comparable: number;
  agreed: number;
  disputed_outcome: number;
  disputed_reason: number;
  overcounted: number;
  agreement_rate: number | null;
  matrix: { agent: string; callharness: string; count: number }[];
  items: DisputedCall[];
};

export type GapExample = {
  call_id: string;
  external_id: string | null;
  started_at: string;
  agent_id: string;
  question: string;
  outcome: string;
};

export type KnowledgeGap = {
  question: string;
  tool: string;
  count: number;
  transferred: number;
  variants: string[];
  examples: GapExample[];
  // Null until the grouping pass has placed these calls; present means the row can be
  // ungrouped again. Nothing is merged until someone runs that pass, so before it every
  // row is one call and `count` is 1.
  group_id: string | null;
  grouped: boolean;
  needs_review: boolean;
  // How far this record has got towards being fixed — see gap_verification.py.
  // "not_verified" until somebody re-asks the lookup API about it.
  status: GapStatus;
  status_at: string | null;
  status_note: string | null;
  sent_batch: string | null;
  // The region this record's calls belong to, and how many lookup sources are configured
  // for that region. Zero means Verify cannot run — a source only serves the regions it
  // lists, and probing another region's endpoint answers "Tool non supportato", which
  // read as data becomes "this record is missing".
  agent_id: string | null;
  probes_configured: number;
};

export type GapStatus =
  | "not_verified"
  | "confirmed_missing"
  | "found_in_source"
  | "bad_question"
  | "verify_error"
  | "sent"
  | "added"
  | "added_confirmed";

export type KnowledgeGaps = {
  window_days: number;
  calls_scanned: number;
  calls_with_gaps: number;
  total_gaps: number;
  gap_call_rate: number | null;
  groups: KnowledgeGap[];
  // Questions nobody can add a record for. Deliberately kept out of `groups`, out of the
  // counts, and out of the copied email.
  needs_review: KnowledgeGap[];
  ungrouped_count: number;
  // How many rows matched before `limit` cut the list. When this exceeds groups.length
  // the page is showing a truncated list and has to say so — a silent cut here once hid
  // 133 of 148 already-verified records with nothing on the page to suggest it.
  total_rows: number;
  // The statuses the server actually filtered on. Echoed back so "nothing has this
  // status" can be told apart from "an older backend ignored the parameter".
  status_filter: string[];
};

export type GapGrouping = {
  considered: number;
  grouped: number;
  joined_existing: number;
  needs_review: number;
  new_groups: number;
  remaining: number;
  warnings: string[];
};

/** One request to one source with one wording of the question. Shown in full on the row:
 *  a verdict about somebody's data that cannot be inspected is one nobody should act on. */
export type ProbeAttempt = {
  probe_key: string;
  probe_label: string;
  variant: string;
  variant_kind: "canonical" | "paraphrase" | "dated" | "corrected" | "test" | string;
  url: string | null;
  http_status: number | null;
  ms: number | null;
  response: string | null;
  verdict: "ok" | "empty" | "error";
};

export type GapVerification = {
  id: string;
  created_at: string;
  verdict: GapStatus;
  group_id: string | null;
  call_id: string;
  question_original: string | null;
  question_resolved: string | null;
  // Differ when the caller's day had already passed and a same-weekday substitute was
  // used instead — an empty answer about a day that is over proves nothing.
  date_meant: string | null;
  date_probed: string | null;
  question_note: string | null;
  llm_model: string | null;
  probes: ProbeAttempt[];
};

export type GapGroupStatus = {
  group_id: string;
  status: GapStatus;
  status_at: string | null;
  status_note: string | null;
  sent_batch: string | null;
};

/** What a run would cost, before anything is spent. Every probe lands on the customer's
 *  live service — the same instance answering phone calls — so the page asks first. */
export type GapVerifyPlan = {
  groups: number;
  requests: number;
  sources: string[];
  // Records that cannot be checked because no source is configured for their region.
  unroutable: Record<string, number>;
};

export type GapVerifyRun = {
  running: boolean;
  total: number;
  done: number;
  started_at: string | null;
  finished_at: string | null;
  current_group_id: string | null;
  verdicts: Record<string, number>;
  error: string | null;
};

export type TimeseriesPoint = {
  date: string;
  calls: number;
  success_rate: number | null;
  avg_sentiment: number | null;
  avg_duration_seconds: number | null;
};

export type ExtractionField = {
  name: string;
  type: "text" | "boolean" | "number" | "enum";
  description: string;
  choices: string[] | null;
};

export type ReasonCategory = {
  key: string;
  description: string;
};

export type AnalysisConfig = {
  summary_enabled: boolean;
  summary_prompt: string | null;
  sentiment_enabled: boolean;
  success_enabled: boolean;
  success_prompt: string | null;
  success_rubric: "pass_fail" | "numeric_scale";
  output_language: string;
  extraction_enabled: boolean;
  extraction_fields: ExtractionField[];
  bucketing_enabled: boolean;
  buckets: ReasonCategory[];
  // Superseded by `buckets`. Still round-tripped so an install that has it on keeps
  // its saved taxonomy instead of silently resetting it.
  classification_enabled: boolean;
  transfer_reasons: ReasonCategory[];
  non_completion_reasons: ReasonCategory[];
  // Where a missing-record question gets re-asked. No default — an empty list means
  // verification is off, not "use the built-in one".
  lookup_probes: LookupProbe[];
};

export type LookupProbe = {
  key: string;
  label: string;
  url: string;
  method: "POST" | "GET" | "PUT";
  headers: Record<string, string>;
  // Must be valid JSON and contain {{query}}; both are enforced on save.
  body_template: string;
  result_path: string;
  enabled: boolean;
  // Which regions this source serves; empty means all of them. Not a convenience filter:
  // these backends dispatch on a region-specific tool name and answer an unrecognised one
  // with 200 OK and a polite sentence.
  agent_ids: string[];
};

export type LatencyStats = {
  turn_count: number;
  e2e: { avg: number | null; p50: number | null; p95: number | null; p99: number | null };
  components: Record<string, { avg: number | null; p50: number | null; p95: number | null }>;
  daily: { date: string; p50: number | null; p95: number | null; count: number }[];
  quality: {
    calls: number | null;
    avg_interruptions: number | null;
    pct_calls_with_long_silence: number | null;
    avg_talk_ratio: number | null;
    avg_assistant_wpm: number | null;
  };
};

export type AlertTrigger =
  | "negative_sentiment_call"
  | "failed_call"
  | "keyword_match"
  | "high_latency_call"
  | "success_rate_window"
  | "sentiment_window";

export type AlertRule = {
  id: number;
  name: string;
  enabled: boolean;
  trigger: AlertTrigger;
  threshold: number | null;
  keyword: string | null;
  window_minutes: number;
  min_calls: number;
  channel: "webhook" | "slack" | "email";
  target_url: string;
  cooldown_minutes: number;
  last_fired_at: string | null;
};

export type AlertEvent = {
  id: number;
  rule_id: number | null;
  rule_name: string;
  call_id: string | null;
  message: string;
  fired_at: string;
  delivered: boolean;
  delivery_error: string | null;
};

export type EvaluatorStats = {
  id: number;
  name: string;
  enabled: boolean;
  total: number;
  passed: number;
  pass_rate: number | null;
};

export type Evaluator = {
  id: number;
  name: string;
  prompt: string;
  enabled: boolean;
  created_at: string;
};

// --- Automated test calls ---------------------------------------------------
// A scenario is a rehearsed phone call; a run is one execution of it. Keep in sync
// with server/app/schemas.py by hand — there is no codegen here.

export type TestScenario = {
  id: number;
  name: string;
  agent_id: string;
  to_number: string;
  dtmf_digits: string | null;
  dtmf_pause_seconds: number;
  persona: string;
  criteria: string[];
  max_duration_seconds: number | null;
  enabled: boolean;
  created_at: string;
};

export type TestRun = {
  id: string;
  scenario_id: number | null;
  scenario_name: string;
  agent_id: string;
  to_number: string;
  // queued | dialing | talking | completed | failed
  status: string;
  provider_call_sid: string | null;
  started_at: string;
  ended_at: string | null;
  duration_seconds: number | null;
  // [{ speaker: "agent" | "tester", text }] — "agent" is the production assistant.
  caller_transcript: { speaker: string; text: string }[] | null;
  call_id: string | null;
  call_expires_at: string | null;
  call_deleted: boolean;
  // pass | fail | error. "error" means the test could not reach a verdict, which is
  // a different thing from the agent failing.
  verdict: string | null;
  verdict_reason: string | null;
  criteria_results: { criterion?: string; passed?: boolean; note?: string }[] | null;
  ended_on_transfer: boolean;
  error: string | null;
};

export type TestCallReadiness = {
  enabled: boolean;
  missing: string | null;
  running: boolean;
  max_duration_seconds: number;
  ttl_hours: number;
  realtime_model: string;
};

export const fetcher = async (url: string) => {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`API error ${res.status}`);
  return res.json();
};

// For endpoints that return text rather than JSON — currently just the raw agent log.
// It surfaces the API's `detail` when there is one, so the log panel can tell "no log
// linked to this call" apart from "the log is no longer in Azure".
export const textFetcher = async (url: string) => {
  const res = await fetch(url);
  if (!res.ok) {
    let detail = `API error ${res.status}`;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* not JSON — keep the status message */
    }
    throw new Error(detail);
  }
  return res.text();
};

export async function apiSend(url: string, method: string, body?: unknown) {
  const res = await fetch(url, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    // The status code alone is not enough for the verification endpoints: a 400 there
    // means something specific and fixable ("no lookup source is configured for region
    // Lazio", "only records proved missing can be marked as sent"), and hiding it behind
    // "API error 400" turns a clear instruction into a mystery. The prefix is kept so
    // callers that test for a code — the gaps page checks for 409 — keep working.
    const detail = await res
      .json()
      .then((body) => (typeof body?.detail === "string" ? body.detail : null))
      .catch(() => null);
    throw new Error(detail ? `API error ${res.status}: ${detail}` : `API error ${res.status}`);
  }
  return res.status === 204 ? null : res.json();
}

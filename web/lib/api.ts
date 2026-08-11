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
  end_reason: string | null;
  transferred: boolean;
  recording_url: string | null;
  has_recording: boolean;
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

export type CallDetail = Call & { turns: Turn[]; evaluations: EvaluationResult[] };

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
  end_reason_breakdown: { reason: string; count: number }[];
  transfer_reason_breakdown: { reason: string; count: number }[];
  non_completion_reason_breakdown: { reason: string; count: number }[];
  agents: string[];
  agent_stats: AgentStats[];
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
  opencall_outcome: string;
  opencall_reason: string | null;
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
  matrix: { agent: string; opencall: string; count: number }[];
  items: DisputedCall[];
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
  classification_enabled: boolean;
  transfer_reasons: ReasonCategory[];
  non_completion_reasons: ReasonCategory[];
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

export const fetcher = async (url: string) => {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`API error ${res.status}`);
  return res.json();
};

export async function apiSend(url: string, method: string, body?: unknown) {
  const res = await fetch(url, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`API error ${res.status}`);
  return res.status === 204 ? null : res.json();
}

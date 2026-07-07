export type Turn = {
  idx: number;
  role: "user" | "assistant";
  text: string;
  start_time: number | null;
  end_time: number | null;
  latency_ms: number | null;
  interrupted: boolean;
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
  llm_model: string | null;
  created_at: string;
};

export type CallDetail = Call & { turns: Turn[] };

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
  end_reason_breakdown: { reason: string; count: number }[];
  agents: string[];
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

export type AnalysisConfig = {
  summary_enabled: boolean;
  summary_prompt: string | null;
  sentiment_enabled: boolean;
  success_enabled: boolean;
  success_prompt: string | null;
  success_rubric: "pass_fail" | "numeric_scale";
  extraction_enabled: boolean;
  extraction_fields: ExtractionField[];
};

export const fetcher = async (url: string) => {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`API error ${res.status}`);
  return res.json();
};

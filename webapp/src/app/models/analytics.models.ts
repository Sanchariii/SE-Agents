export type MetricType = 'measured' | 'derived' | 'estimated';
export type MetricFormat = 'number' | 'compact' | 'currency' | 'duration' | 'percent' | 'hours' | 'leverage';
export type AnalyticsProjectSort = 'last_active' | 'turns' | 'cost' | 'tokens' | 'duration';

export interface AnalyticsMetric {
  value: number | null;
  type: MetricType;
  count?: number | null;
  of?: number | null;
  denominator?: number | null;
  note?: string;
}

export interface AnalyticsWindow {
  from: string;
  to: string;
  timezone: string;
}

export interface AnalyticsTrendPoint {
  bucket: string | null;
  workflow_runs: number | null;
  active_users: number | null;
  slides_delivered: number | null;
  cost_usd: number | null;
}

export type TrendMetric = Exclude<keyof AnalyticsTrendPoint, 'bucket'>;

export interface AnalyticsAgent {
  agent_name: string;
  model: string | null;
  runs: number | null;
  success_rate: number | null;
  fallback_rate: number | null;
  median_duration_ms: number | null;
  p95_duration_ms: number | null;
  llm_calls: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cached_input_tokens: number | null;
  cost_usd: number | null;
  tokens_per_run: number | null;
  cost_per_run_usd: number | null;
}

export interface AnalyticsModel {
  model: string | null;
  runs: number | null;
  failure_rate: number | null;
  median_duration_ms: number | null;
  llm_calls: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: number | null;
}

export interface AnalyticsEstimate {
  is_estimated: boolean;
  confidence: string | null;
  baseline_version: string | null;
}

export interface AnalyticsDashboard {
  window: AnalyticsWindow;
  filters?: { agent: string | null; model: string | null };
  overview: Record<'active_users' | 'projects_worked_on' | 'successful_projects' |
    'slides_delivered' | 'workflow_runs' | 'median_ai_turnaround_ms' | 'total_ai_cost_usd', AnalyticsMetric>;
  trends: { bucket: 'day' | 'week'; points: AnalyticsTrendPoint[] };
  adoption: Record<'projects' | 'median_turns_per_project' | 'avg_turns_per_project' |
    'multi_turn_projects' | 'multi_turn_rate', AnalyticsMetric>;
  performance: {
    ai_turnaround_ms: {
      median: number | null;
      p95: number | null;
      total: number | null;
      type: MetricType;
      note: string;
    };
    agent_processing_ms: { total: number | null; type: MetricType; note: string };
  };
  cost: Record<'total_cost_usd' | 'input_tokens' | 'output_tokens' | 'cached_input_tokens' |
    'total_tokens' | 'llm_calls' | 'cost_per_project_usd' | 'cost_per_slide_usd' |
    'cost_per_call_usd' | 'tokens_per_slide' | 'tokens_per_call', AnalyticsMetric>;
  quality: Record<'agent_runs' | 'fallback_rate' | 'agent_failure_rate' | 'rework_rate', AnalyticsMetric> & {
    feedback: Record<'rated_versions' | 'thumbs_up' | 'thumbs_down' | 'thumbs_up_rate' | 'comment_coverage', AnalyticsMetric>;
  };
  efficiency: Record<'estimated_hours_saved' | 'hours_saved_per_dollar' | 'time_leverage', AnalyticsMetric> & {
    estimate: AnalyticsEstimate;
  };
  agents: AnalyticsAgent[];
  models: AnalyticsModel[];
  data_quality: Record<'unpriced_agent_runs' | 'missing_page_counts' | 'dropped_metric_events', number | null>;
}

export interface AnalyticsProject {
  session_id: string;
  user_id: number | string | null;
  owner_name: string | null;
  turns: number | null;
  multi_turn: boolean | null;
  published: boolean | null;
  slides_delivered: number | null;
  ai_turnaround_ms: number | null;
  agent_processing_ms: number | null;
  total_tokens: number | null;
  cost_usd: number | null;
  last_active: string | null;
}

export interface AnalyticsProjectsPage {
  items: AnalyticsProject[];
  pagination: { total: number; limit: number; offset: number };
}

export interface AnalyticsWorkflowRun {
  run_id: string;
  turn_index: number | null;
  intent: string | null;
  started_at: string | null;
  finished_at: string | null;
  wall_ms: number | null;
  status: string | null;
  deck_version_id: number | null;
  slides_created: number | null;
  slides_updated: number | null;
  slides_translated: number | null;
}

export interface AnalyticsAgentRun {
  agent_run_id: string;
  workflow_run_id: string;
  agent_name: string;
  model: string | null;
  started_at: string | null;
  duration_ms: number | null;
  status: string | null;
  llm_calls: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cached_input_tokens: number | null;
  cost_usd: number | null;
  work_units: number | null;
  work_unit_type: string | null;
  pricing_status: string | null;
}

export interface AnalyticsIngestionRun {
  ingestion_run_id: string;
  document_id: string;
  started_at: string | null;
  duration_ms: number | null;
  status: string | null;
  bytes_processed: number | null;
  pages_processed: number | null;
  chunks_created: number | null;
}

export interface AnalyticsProjectDetail {
  session_id: string;
  workflow_runs: AnalyticsWorkflowRun[];
  agent_runs: AnalyticsAgentRun[];
  ingestion_runs: AnalyticsIngestionRun[];
}
export type SessionStatus = 'new' | 'running' | 'answered' | 'done' | 'failed' | 'stopped';

export type RunStatus = 'QUEUED' | 'RUNNING' | 'RECOVERING' | 'CANCELLING' | 'COMPLETED' | 'FAILED' | 'CANCELLED';

export interface StreamEvent {
  type?: string;
  agent?: string;
  message?: string;
  data?: any;
  // Present on persisted/live workflow events; identify the invoke run and its local order.
  run_id?: string | null;
  event_index?: number;
  ts?: string;
  session_id?: string;
  stage?: string;
  attempt?: number;
}

/** Authoritative backend run record (GET /decks/{sid}/runs/...). */
export interface Run {
  run_id: string;
  session_id: string;
  client_request_id: string | null;
  status: RunStatus;
  is_active: boolean;
  current_stage: string | null;
  attempt: number;
  last_event_index: number;
  deck_version: number | null;
  artifact_ready: boolean;
  error_code: string | null;
  error: string | null;
  retryable: boolean;
  cancel_requested: boolean;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
}

export interface RunCurrentResponse {
  session_id: string;
  active: Run | null;
  latest: Run | null;
}

export type StopStatus = 'cancelling' | 'already_cancelling' | 'completing' | 'not_running';

export interface StopResponse {
  session_id: string;
  status: StopStatus;
  run_id: string | null;
  run: Run | null;
}

export interface ChatMessage {
  id?: string | number | null;
  session_id?: string;
  role: 'user' | 'assistant' | 'system' | 'thought';
  content?: string;
  result?: any;
  created_at?: string | null;
  // thought-group entries carry the raw event stream + a title instead of content.
  events?: StreamEvent[];
  title?: string;
  // total wall-clock time (ms) the agent took to build this deck, shown on a finished thought board.
  durationMs?: number | null;
  run_id?: string | null;
  run_status?: RunStatus | null;
}

export type ChatRole = 'user' | 'assistant' | 'system';

/** A durable chat row inside the /messages `timeline`. */
export interface MessageTimelineItem {
  kind: 'message';
  id: number | null;
  run_id: string | null;
  role: ChatRole;
  content: string;
  result: Record<string, unknown> | null;
  ts: string;
  run_status?: RunStatus | null;
}

/** A persisted workflow (agent activity) event inside the /messages `timeline`. */
export interface WorkflowEventTimelineItem {
  kind: 'workflow_event';
  id: number | null;
  run_id: string;
  event_index: number;
  type: string;
  agent: string;
  message: string;
  data: Record<string, unknown>;
  ts: string;
  run_status?: RunStatus | null;
}

export type TimelineItem = MessageTimelineItem | WorkflowEventTimelineItem;

export interface MessagesResponse {
  session_id: string;
  messages: ChatMessage[];
  timeline?: TimelineItem[];
}

export interface SessionDoc {
  session_id?: string;
  doc_id?: string;
  version?: number;
  filename: string;
  mime_type?: string;
  sha256?: string;
  storage_path?: string;
  is_latest?: boolean;
  status?: string;
  error?: string | null;
  uploaded_at?: string | null;
  chunks?: number;
  pct?: number;
  _pid?: string;
  sessionId?: string;
}

export interface Session {
  id: string;
  title: string;
  status: SessionStatus;
  deckVersion: number | null;
  verdict: string | null;
  messages: ChatMessage[];
  docs: SessionDoc[];
  createdAt: string;
  failureConfirmed?: boolean;
}

export interface SessionUser {
  id: any;
  username: string;
  displayName: string;
  email: string | null;
  roles: string[];
  sessionId?: string;
}

export interface AuthError {
  status: number;
  detail: string | null;
  correlationId?: string | null;
}

export type QuestionType = 'single_choice' | 'free_text' | 'scale';

export interface QuestionOption {
  /** Sent back in `answers`; never translated. */
  value: string;
  /** Display text; follows the user's chat language. */
  label: string;
  description: string;
}

/** A structured clarifying question (`questions` SSE event / `result.questions`). */
export interface ClarifyingQuestion {
  /** Stable id (e.g. `creativity_level`); the key used in `answers`. Never key UI on `text`. */
  id: string;
  type: QuestionType;
  text: string;
  options: QuestionOption[];
  default: string;
  required: boolean;
  allow_free_text: boolean;
}

/** `{question id: value}` sent on `/invoke` as `answers`. */
export type QuestionAnswers = Record<string, string>;

/** Single, immutable feedback stored on a deck version (GET/POST /decks/{sid}/versions/{v}/feedback). */
export interface DeckFeedback {
  session_id: string;
  deck_version: number;
  thumbs_up: boolean;
  comment: string | null;
}

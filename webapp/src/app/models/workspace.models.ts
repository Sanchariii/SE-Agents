import { ClarifyingQuestion, QuestionAnswers, StreamEvent } from './models';

export type ViewName = 'login' | 'boot' | 'app';
export type TabName = 'chat' | 'pipeline' | 'approvals' | 'analytics';

export interface SlideInfo {
  index: number;
  total?: number;
  /** progress (writing) | research | written (content ready, awaiting design) | design (being coded)
   *  | retry (visual check / layout gate failed, being redone) | done | failed */
  stage: string;
  detail: string;
  label: string;
  /** Rendered on a template layout after the designer gave up (still a finished slide). */
  fallback?: boolean;
  tier?: number;
  /** Design attempts so far (ppt-code `attempts`); drives the per-slide progress estimate. */
  attempts?: number;
}

export interface FlowState {
  started: boolean;
  stageIdx: number;
  done: boolean;
  error: boolean;
  cancelled: boolean;
  slideTotal: number;
  /** Slides this run actually writes/designs ("Processing N changed slide(s)", "Designing N slide(s)"). */
  slideTarget: number;
  /** Slides the designer will build this run ("Designing N slide(s)"); -1 until known. */
  designTarget: number;
  /** Estimated overall progress 0-100; never moves backwards within a run. */
  pct: number;
  langTotal: number | null;
  slides: Map<number, SlideInfo>;
  langs: Set<string>;
  /** thinking: chat / clarify; planning: curating a plan; build: writing + designing the deck. */
  mode: 'thinking' | 'planning' | 'build';
  /** Plain-language hint for the thinking bubble ("Writing a reply", "Preparing a few questions"). */
  thinkingNote: string;
  /** Plain-language planning steps, oldest first; the last one is in progress while the run is live. */
  activity: string[];
  /** Plain-language description of the latest build activity ("Slide 4 · running a visual check"). */
  now: string;
  /** Slide stage behind `now`, used to colour the "Now" badge. */
  nowStage: string;
}

export interface LiveTurn {
  events: StreamEvent[];
  title: string;
  collapsed: boolean;
  flow: FlowState;
  durationMs?: number | null;
  /** Structured questions streamed by this run (`questions` event); replays overwrite, never append. */
  questions?: ClarifyingQuestion[] | null;
  /** Re-attached after a reload/reconnect and still catching up on events emitted before it. */
  resuming?: boolean;
  /** Event index the replay must reach before the turn is considered caught up (run `last_event_index`). */
  resumeTo?: number;
}

/** The interactive questionnaire for a session; one at a time, keyed by its question signature. */
export interface QuestionFormState {
  signature: string;
  questions: ClarifyingQuestion[];
  /** Picked option values (chips / slider stops), by question id. */
  choices: QuestionAnswers;
  /** Typed free-text answers, by question id; take precedence over `choices`. */
  text: QuestionAnswers;
  submitting: boolean;
  error: string;
}

/** Read-only recap shown under an assistant message whose questions were already answered. */
export interface QuestionSummary {
  entries: { label: string; value: string }[];
}

export interface StudioOutputRow {
  plan_title: string;
  deck_version: number;
  theme: string;
  status: string;
  created_at?: string | null;
  files?: Record<string, any>;
  _sid: string;
  _languages: string[];
}

export const DECK_FEEDBACK_COMMENT_MAX = 1000;

/** Per-version feedback UI state. `thumbsUp` is the draft until `status` is 'saved'. */
export interface DeckFeedbackState {
  status: 'loading' | 'load-error' | 'open' | 'submitting' | 'saved';
  thumbsUp: boolean | null;
  comment: string;
  error: string;
}

export interface ApprovalDecision {
  sessionId: string;
  deckVersion: number;
  action: 'approve' | 'reject';
}

/** A single attached studio consumes these view-only requests. No DOM references cross DI. */
export type WorkspacePresentationEvent =
  | { type: 'scroll-messages' }
  | { type: 'scroll-ingest' }
  | { type: 'resize-composer'; focus: boolean }
  | { type: 'deck-ready' }
  | { type: 'project-created' };

export interface WorkspaceViewPort {
  notify(event: WorkspacePresentationEvent): void;
}
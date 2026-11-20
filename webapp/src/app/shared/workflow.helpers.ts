import type { Run, RunStatus, StreamEvent } from '../models/models';

/** Pure run-state logic for the resilient deck workflow; no Angular, DOM or network access. */

export type ConnectionState = 'idle' | 'connecting' | 'open' | 'reconnecting';

export interface RunTerminal {
  status: RunStatus;
  errorCode: string | null;
  error: string | null;
  retryable: boolean;
}

export interface WorkflowState {
  sessionId: string;
  activeRunId: string | null;
  pendingClientRequestId: string | null;
  runStatus: RunStatus | null;
  connection: ConnectionState;
  lastEventIndex: number;
  recovering: boolean;
  recoveryMessage: string | null;
  stopping: boolean;
  terminal: RunTerminal | null;
}

export interface ApplyResult {
  state: WorkflowState;
  /** True when the event belongs to the tracked run and should be rendered. */
  accepted: boolean;
}

export type StopControl = 'hidden' | 'enabled' | 'stopping' | 'completing';
export type RetryControl = 'none' | 'reconnect' | 'try-again';

export interface RunControls {
  send: boolean;
  stop: StopControl;
  retry: RetryControl;
}

export interface RunUiFlags {
  hasRetryMessage?: boolean;
  stopRequested?: boolean;
  completing?: boolean;
  alreadyRunning?: boolean;
}

export interface RunNotice {
  tone: 'info' | 'warning' | 'danger' | 'muted';
  text: string;
  action: 'reconnect' | 'try-again' | null;
  dismissible: boolean;
}

export interface SseFrame {
  id: number | null;
  data: any | null;
  comment: boolean;
}

const ACTIVE_STATUSES = new Set<string>(['QUEUED', 'RUNNING', 'RECOVERING', 'CANCELLING']);
const TERMINAL_STATUSES = new Set<string>(['COMPLETED', 'FAILED', 'CANCELLED']);
const NON_RETRYABLE_CODES = new Set<string>(['LLM_AUTH_FAILED', 'LLM_REQUEST_INVALID', 'CANCELLED_BY_USER']);
const RETRY_DELAYS_MS = [1000, 2000, 5000, 10000];

export const IDLE_STREAM_TIMEOUT_MS = 45000;
export const DEFAULT_RECOVERING_COPY = 'A temporary issue occurred while preparing the deck. Retrying automatically.';
export const ALREADY_RUNNING_COPY = 'Generation is already running for this project. Showing its progress.';
export const RECONNECTING_COPY = 'Reconnecting to the current progress…';
export const STOPPING_COPY = 'Stopping…';
export const COMPLETING_COPY = 'Almost done. Finishing the deck…';
export const DEFAULT_ERROR_COPY = 'Something went wrong while generating the deck. Please try again.';

export const ERROR_COPY: Readonly<Record<string, string>> = {
  LLM_GATEWAY_UNAVAILABLE: 'The AI service is temporarily unavailable. Please try again in a few minutes.',
  LLM_RATE_LIMITED: 'The AI service is busy right now. Please try again in a few minutes.',
  LLM_TIMEOUT: 'The AI service took too long to respond. Please try again.',
  LLM_OUTPUT_INVALID: 'The AI returned an incomplete answer while preparing the deck. Please try again.',
  LLM_AUTH_FAILED: "The AI service rejected this application's credentials. Please contact support.",
  LLM_REQUEST_INVALID: 'The AI service could not process this request. Please contact support.',
  WORKER_LOST: 'The deck generation was interrupted. Please try again.',
  WORKER_SHUTDOWN: 'The service restarted while generating the deck. Please try again.',
  SERVICE_UNAVAILABLE: 'The service is temporarily unavailable. Please try again shortly.',
  CANCELLED_BY_USER: 'Stopped. Nothing from this request was saved.',
  INTERNAL_ERROR: DEFAULT_ERROR_COPY,
};

export function normalizeRunStatus(value: unknown): RunStatus | null {
  const s = String(value ?? '').trim().toUpperCase();
  return ACTIVE_STATUSES.has(s) || TERMINAL_STATUSES.has(s) ? (s as RunStatus) : null;
}

export function isActiveRunStatus(value: unknown): boolean {
  const s = normalizeRunStatus(value);
  return !!s && ACTIVE_STATUSES.has(s);
}

export function isTerminalRunStatus(value: unknown): boolean {
  const s = normalizeRunStatus(value);
  return !!s && TERMINAL_STATUSES.has(s);
}

export function errorCopy(code?: string | null): string {
  return (code && ERROR_COPY[code]) || DEFAULT_ERROR_COPY;
}

export function newWorkflowState(sessionId: string): WorkflowState {
  return {
    sessionId,
    activeRunId: null,
    pendingClientRequestId: null,
    runStatus: null,
    connection: 'idle',
    lastEventIndex: 0,
    recovering: false,
    recoveryMessage: null,
    stopping: false,
    terminal: null,
  };
}

/** Fresh state attached to a run discovered via REST; replay from index 0 rebuilds its progress. */
export function attachToRun(sessionId: string, run: Pick<Run, 'run_id' | 'status'>): WorkflowState {
  const status = normalizeRunStatus(run.status);
  const live = status && ACTIVE_STATUSES.has(status) ? status : 'RUNNING';
  return {
    ...newWorkflowState(sessionId),
    activeRunId: run.run_id,
    runStatus: live,
    recovering: live === 'RECOVERING',
    recoveryMessage: live === 'RECOVERING' ? DEFAULT_RECOVERING_COPY : null,
    stopping: live === 'CANCELLING',
  };
}

function safeText(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function buildTerminal(status: RunStatus, code: unknown, error: unknown, retryable: unknown): RunTerminal {
  if (status === 'COMPLETED') return { status, errorCode: null, error: null, retryable: false };
  const errorCode = safeText(code) || (status === 'CANCELLED' ? 'CANCELLED_BY_USER' : null);
  return {
    status,
    errorCode,
    error: safeText(error) || errorCopy(errorCode),
    retryable: status === 'CANCELLED' ? false
      : typeof retryable === 'boolean' ? retryable
        : !(errorCode && NON_RETRYABLE_CODES.has(errorCode)),
  };
}

/** Terminal outcome carried by a `done` event; branches on status/error_code, never on text. */
export function terminalFromDone(evt: StreamEvent): RunTerminal {
  const data = evt?.data || {};
  const declared = normalizeRunStatus(data.status);
  const status: RunStatus = declared && TERMINAL_STATUSES.has(declared) ? declared
    : (data.error || data.error_code ? 'FAILED' : 'COMPLETED');
  return buildTerminal(status, data.error_code, data.error, data.retryable);
}

export function terminalFromRun(run: Run): RunTerminal | null {
  const status = normalizeRunStatus(run?.status);
  if (!status || !TERMINAL_STATUSES.has(status)) return null;
  return buildTerminal(status, run.error_code, run.error, run.retryable);
}

/** Folds one stream event into run state: never mixes runs, dedupes by event_index, terminal wins. */
export function applyRunEvent(state: WorkflowState, ev: StreamEvent | null | undefined): ApplyResult {
  const ignored = { state, accepted: false };
  if (!ev || typeof ev !== 'object') return ignored;

  if (ev.type === 'run_started') {
    const runId = ev.run_id ? String(ev.run_id) : null;
    if (!runId) return ignored;
    // A transport retry of the same submission replays the same run; keep its progress.
    if (runId === state.activeRunId) return { state: { ...state, pendingClientRequestId: null }, accepted: false };
    const status = normalizeRunStatus(ev.data?.workflow_status);
    const live = status && ACTIVE_STATUSES.has(status) ? status : 'RUNNING';
    return {
      state: {
        ...state,
        activeRunId: runId,
        pendingClientRequestId: null,
        runStatus: live,
        lastEventIndex: 0,
        recovering: live === 'RECOVERING',
        recoveryMessage: live === 'RECOVERING' ? DEFAULT_RECOVERING_COPY : null,
        stopping: live === 'CANCELLING',
        terminal: null,
      },
      accepted: true,
    };
  }

  if (ev.run_id === undefined || ev.run_id === null || ev.run_id === '') {
    // Run-less `done` before any run attached = admission failure; other run-less events are ingestion.
    if (ev.type === 'done' && !state.activeRunId && !state.terminal) {
      const terminal = terminalFromDone(ev);
      const failed = terminal.status === 'COMPLETED'
        ? buildTerminal('FAILED', 'SERVICE_UNAVAILABLE', null, true) : terminal;
      return {
        state: { ...state, pendingClientRequestId: null, runStatus: failed.status, terminal: failed, recovering: false, recoveryMessage: null, stopping: false },
        accepted: true,
      };
    }
    return ignored;
  }

  if (String(ev.run_id) !== state.activeRunId) return ignored;
  if (state.terminal) return ignored;
  const idx = typeof ev.event_index === 'number' && Number.isFinite(ev.event_index) ? ev.event_index : null;
  if (idx && idx <= state.lastEventIndex) return ignored;

  let next: WorkflowState = { ...state };
  if (idx != null) next.lastEventIndex = Math.max(state.lastEventIndex, idx);
  switch (ev.type) {
    case 'workflow_recovering':
      next.recovering = true;
      next.recoveryMessage = safeText(ev.message) || DEFAULT_RECOVERING_COPY;
      if (next.runStatus !== 'CANCELLING') next.runStatus = 'RECOVERING';
      break;
    case 'workflow_recovered':
      next.recovering = false;
      next.recoveryMessage = null;
      if (next.runStatus === 'RECOVERING') next.runStatus = 'RUNNING';
      break;
    case 'workflow_cancelled':
      next.stopping = true;
      next.runStatus = 'CANCELLING';
      break;
    case 'done': {
      const terminal = terminalFromDone(ev);
      next = { ...next, terminal, runStatus: terminal.status, recovering: false, recoveryMessage: null, stopping: false };
      break;
    }
    default:
      if (!next.runStatus) next.runStatus = 'RUNNING';
  }
  return { state: next, accepted: true };
}

/** Applies an authoritative Run snapshot to the tracked run (ignored for other runs or after terminal). */
export function applyRunSnapshot(state: WorkflowState, run: Run | null | undefined): WorkflowState {
  if (!run || run.run_id !== state.activeRunId || state.terminal) return state;
  const status = normalizeRunStatus(run.status);
  if (!status) return state;
  if (TERMINAL_STATUSES.has(status)) {
    return { ...state, runStatus: status, terminal: terminalFromRun(run), recovering: false, recoveryMessage: null, stopping: false };
  }
  return {
    ...state,
    runStatus: status,
    recovering: status === 'RECOVERING',
    recoveryMessage: status === 'RECOVERING' ? (state.recoveryMessage || DEFAULT_RECOVERING_COPY) : null,
    stopping: status === 'CANCELLING',
  };
}

/** True while a submission or run is in flight, so a new submission must not be sent. */
export function isRunBlocking(state: WorkflowState | null | undefined): boolean {
  if (!state || state.terminal) return false;
  return state.connection === 'connecting'
    || state.connection === 'reconnecting'
    || state.pendingClientRequestId != null
    || isActiveRunStatus(state.runStatus);
}

export function controlsFor(state: WorkflowState | null | undefined, flags: RunUiFlags = {}): RunControls {
  if (!state) return { send: true, stop: 'hidden', retry: 'none' };
  if (state.terminal) {
    const tryAgain = state.terminal.status === 'FAILED' && state.terminal.retryable && !!flags.hasRetryMessage;
    return { send: true, stop: 'hidden', retry: tryAgain ? 'try-again' : 'none' };
  }
  if (!isRunBlocking(state)) return { send: true, stop: 'hidden', retry: 'none' };
  const stop: StopControl = !state.activeRunId ? 'hidden'
    : (state.stopping || state.runStatus === 'CANCELLING' || flags.stopRequested) ? 'stopping'
      : flags.completing ? 'completing' : 'enabled';
  const retry: RetryControl = state.connection === 'reconnecting' && state.activeRunId ? 'reconnect' : 'none';
  return { send: false, stop, retry };
}

export function runNotice(state: WorkflowState | null | undefined, flags: RunUiFlags = {}): RunNotice | null {
  if (!state) return null;
  const t = state.terminal;
  if (t) {
    if (t.status === 'FAILED') {
      return {
        tone: 'danger',
        text: t.error || errorCopy(t.errorCode),
        action: t.retryable && flags.hasRetryMessage ? 'try-again' : null,
        dismissible: true,
      };
    }
    if (t.status === 'CANCELLED') {
      return { tone: 'muted', text: t.error || errorCopy('CANCELLED_BY_USER'), action: null, dismissible: true };
    }
    return null;
  }
  if (!isRunBlocking(state)) return null;
  if (state.connection === 'reconnecting') {
    return state.activeRunId
      ? { tone: 'info', text: RECONNECTING_COPY, action: 'reconnect', dismissible: false }
      : { tone: 'info', text: 'Connecting to the service…', action: null, dismissible: false };
  }
  if (state.stopping || state.runStatus === 'CANCELLING' || flags.stopRequested) {
    return { tone: 'warning', text: STOPPING_COPY, action: null, dismissible: false };
  }
  if (flags.completing) return { tone: 'info', text: COMPLETING_COPY, action: null, dismissible: false };
  if (state.recovering) {
    return { tone: 'warning', text: state.recoveryMessage || DEFAULT_RECOVERING_COPY, action: null, dismissible: false };
  }
  if (flags.alreadyRunning) return { tone: 'info', text: ALREADY_RUNNING_COPY, action: null, dismissible: false };
  return null;
}

/** Backoff for stream reconnects and pre-`run_started` resubmits: 1s, 2s, 5s, then 10s. */
export function retryDelayMs(attempt: number): number {
  const i = Math.max(0, Math.min(Math.floor(attempt) || 0, RETRY_DELAYS_MS.length - 1));
  return RETRY_DELAYS_MS[i];
}

/** Parses one SSE block (`id:` / `data:` lines, `:` comments). `data` is null for comment-only frames. */
export function parseSseFrame(block: string): SseFrame {
  let id: number | null = null;
  let comment = false;
  const dataLines: string[] = [];
  for (const line of String(block || '').split(/\r?\n/)) {
    if (!line) continue;
    if (line.startsWith(':')) { comment = true; continue; }
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'data') dataLines.push(value);
    else if (field === 'id') {
      const n = Number(value);
      id = value !== '' && Number.isFinite(n) ? n : null;
    }
  }
  let data: any = null;
  if (dataLines.length) {
    try {
      const parsed = JSON.parse(dataLines.join('\n'));
      data = parsed && typeof parsed === 'object' ? parsed : null;
    } catch { data = null; }
  }
  if (data && id != null && data.event_index == null) data.event_index = id;
  return { id, data, comment };
}

/** Splits buffered SSE text into complete blocks plus the unterminated remainder. */
export function splitSseBuffer(buffer: string): { blocks: string[]; rest: string } {
  const parts = buffer.split(/\r?\n\r?\n/);
  const rest = parts.pop() as string;
  return { blocks: parts, rest };
}

export function newClientRequestId(): string {
  const c: any = (globalThis as any).crypto;
  if (c && typeof c.randomUUID === 'function') return c.randomUUID();
  const bytes = new Uint8Array(16);
  if (c && typeof c.getRandomValues === 'function') c.getRandomValues(bytes);
  else for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

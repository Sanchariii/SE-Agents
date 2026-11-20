import { Injectable } from '@angular/core';
import { ChatMessage, ClarifyingQuestion, DeckFeedback, QuestionAnswers, Run, RunStatus, Session, SessionDoc, StreamEvent, TimelineItem, WorkflowEventTimelineItem } from '../../models/models';
import {
  DECK_FEEDBACK_COMMENT_MAX, DeckFeedbackState, LiveTurn, QuestionFormState, QuestionSummary, StudioOutputRow, TabName, ViewName,
  WorkspacePresentationEvent, WorkspaceViewPort,
} from '../../models/workspace.models';
import {
  APPROVE_COMMAND, AUTOPILOT_PLAIN_REPLY, answerFor, autopilotSubmission, buildSubmission, describeAnswers, missingRequired,
  newQuestionForm, normalizeQuestions, questionsOf, questionsSignature, stripQuestionList, summarizeAnswers,
} from '../questions.helpers';
import {
  buildMessagesFromTimeline, findRunningRunId, latestTimelineRunStatus, newFlowState, resultDurationMs, timelineEventToStreamEvent,
  updateFlowState,
} from '../../training-studio/flow.helpers';
import { isDocProcessing, isPresignUnavailable } from '../../training-studio/document.helpers';
import {
  ALREADY_RUNNING_COPY, COMPLETING_COPY, RunControls, RunNotice, RunUiFlags, WorkflowState,
  applyRunEvent, applyRunSnapshot, attachToRun, controlsFor, isRunBlocking, isTerminalRunStatus,
  newClientRequestId, newWorkflowState, normalizeRunStatus, retryDelayMs, runNotice, terminalFromDone, terminalFromRun,
} from '../workflow.helpers';
import { ApiService, RequestOpts } from './api.service';
import { ConfigService } from './config.service';
import { DeckTimerRecord, DeckTimerService } from './deck-timer.service';
import { SsoService } from './sso.service';
import { StoreService } from './store.service';
import { UiService } from './ui.service';
import { ThemeService } from './theme.service';

const EIGHT_HOUR_MSG = 'For your security, please sign in again — sessions expire 8 hours after sign-in.';
const SESSION_POLL_MS = 15000;

export const ACCEPTED_UPLOAD_EXTENSIONS = [
  '.pdf', '.docx', '.pptx', '.xlsx', '.csv', '.txt', '.md',
  '.png', '.jpg', '.jpeg', '.webp', '.mp4', '.mov', '.mkv',
];
export const MAX_UPLOAD_BYTES = 200 * 1024 * 1024; // 200 MB

const MAX_SUBMIT_RETRIES = 3;
const PENDING_REQUEST_PREFIX = 'se_pending_req:';
/** Prompts typed while the agent was busy, per project; survives a refresh (cleared on sign-out). */
const QUEUE_STORAGE_KEY = 'se_prompt_queue';
/** Per-project autopilot state; survives sign-out like the theme. */
const AUTOPILOT_STORAGE_KEY = 'se_autopilot';
/** Consecutive autopilot replies allowed before a deck is built; beyond this a human must take over. */
const AUTOPILOT_MAX_REPLIES = 5;
/** Backoff retries (1.5s → 60s, ~4 min total) when an autopilot check fails or the run is still settling. */
const AUTOPILOT_MAX_RETRIES = 8;

interface AutopilotState { on: boolean; replies: number; }

/** A submission whose `run_started` has not arrived yet; survives a refresh so it can be resent idempotently. */
interface PendingRequest { message: string; clientRequestId: string; answers?: QuestionAnswers | null; bubble?: string | null; }

/** Per-session run identity, transport and UI flags. `state` is the backend-authoritative run state. */
interface RunTracker {
  sid: string;
  state: WorkflowState;
  turn: LiveTurn | null;
  /** Message that started the run; null when attached to a run started elsewhere. */
  message: string | null;
  /** Structured answers sent with that message (questionnaire submit), if any. */
  answers: QuestionAnswers | null;
  /** Text of the optimistic local user bubble (differs from `message` for answers-only turns). */
  bubble: string | null;
  /** False when `message` must never be put back in the composer (answers, approve command). */
  restorable: boolean;
  /** The message came from the prompt queue; if the backend refuses it, it goes back to the queue. */
  fromQueue: boolean;
  controller: AbortController | null;
  reconnectAttempt: number;
  reconnectTimer: ReturnType<typeof setTimeout> | null;
  wake: (() => void) | null;
  stopRequested: boolean;
  completing: boolean;
  alreadyRunning: boolean;
  noticeDismissed: boolean;
  disposed: boolean;
  lastPill: string | null;
  /** Bumped whenever the tracker starts/attaches a different run; older async loops bail out. */
  gen: number;
}

function readPendingRequest(sid: string): PendingRequest | null {
  try {
    const parsed = JSON.parse(sessionStorage.getItem(PENDING_REQUEST_PREFIX + sid) || 'null');
    return parsed && typeof parsed.message === 'string' && typeof parsed.clientRequestId === 'string' ? parsed : null;
  } catch { return null; }
}
function writePendingRequest(sid: string, pending: PendingRequest): void {
  try { sessionStorage.setItem(PENDING_REQUEST_PREFIX + sid, JSON.stringify(pending)); } catch { /* storage unavailable */ }
}
function clearPendingRequest(sid: string): void {
  try { sessionStorage.removeItem(PENDING_REQUEST_PREFIX + sid); } catch { /* storage unavailable */ }
}
function newLiveTurn(): LiveTurn {
  return { events: [], title: 'Thought process', collapsed: false, flow: newFlowState() };
}
/** Safety net: a re-attached turn stops showing "Resuming" after this long even if the replay stalls. */
const RESUME_MAX_MS = 15000;

/** Session-scoped orchestration. Main owns the lifetime; persistent tab children share it. */
@Injectable()
export class WorkspaceService {
  view: ViewName = 'boot';
  bootMessage = 'Restoring your session…';
  loginError = '';
  ssoLoading = false;
  currentTab: TabName = 'chat';
  connState: '' | 'ok' | 'bad' = '';
  userChipText = '—';
  userChipTitle = '';

  sessions: Session[] = [];
  currentSessionId: string | null = null;
  currentSessionTitle = '';
  deletingSessionIds = new Set<string>();
  statusPillText = '';
  messagesForCurrent: ChatMessage[] = [];
  messagesLoading = false;
  messagesErrorText = '';
  // Incremental ("lazy") chat history: the newest page loads first; older pages load on scroll-up.
  hasMoreOlderMessages = false;
  loadingOlderMessages = false;
  liveTurn: LiveTurn | null = null;
  /**
   * Shown in place of the live turn while the open project is known to be building but the run has not
   * re-attached yet (e.g. right after a reload): a board rebuilt from the persisted timeline, or a placeholder.
   */
  resumePreview: LiveTurn | null = null;
  readonly resumePlaceholder: LiveTurn = { ...newLiveTurn(), resuming: true };
  /** Persisted events of each session's in-progress run (from the last /messages), used to skip the replay. */
  private runningTimeline = new Map<string, { runId: string; events: StreamEvent[] }>();

  // Structured questionnaire (one per session) and the plan-approval control, resolved per view sync.
  questionForm: QuestionFormState | null = null;
  /** True while the form belongs to the still-running live turn (submit waits for `done`). */
  questionFormLive = false;
  activeQuestionMessage: ChatMessage | null = null;
  approveHostMessage: ChatMessage | null = null;
  questionSummaries = new Map<ChatMessage, QuestionSummary>();
  private assistantDisplayText = new Map<ChatMessage, string>();
  private questionForms = new Map<string, QuestionFormState>();
  /** Answers submitted from this tab, keyed `${sid}|${message key}`, for the read-only recap. */
  private submittedAnswers = new Map<string, QuestionAnswers>();
  private questionViewInFlight = false;
  /** Projects where autopilot switched itself off after hitting the reply limit (shows a notice). */
  autopilotHalted = new Set<string>();
  private autopilotStates: Record<string, AutopilotState> | null = null;
  private autopilotChecking = new Set<string>();
  /** Last assistant message autopilot replied to, per project, so it never replies twice. */
  private autopilotHandled = new Map<string, string>();
  private autopilotRetryTimers = new Map<string, ReturnType<typeof setTimeout>>();
  private autopilotRetryAttempts = new Map<string, number>();
  // Draft state belongs to the workspace, while measuring/rendering it belongs to the studio.
  chatInputValue = '';
  composerBusy = false;
  composerQueued = false;
  composerPlaceholder = 'Ask a question or describe the deck you want';
  composerTitle = 'Send';
  composerExpanded = false;
  composerHeight: number | null = null;
  pendingMessagesForCurrent: string[] = [];
  runControls: RunControls = { send: true, stop: 'hidden', retry: 'none' };
  runNotice: RunNotice | null = null;

  studioInputsRows: (SessionDoc & { _pid?: string })[] = [];
  ingestLogForCurrent: StreamEvent[] = [];
  studioOutputs: StudioOutputRow[] | null = null;
  studioOutputsLoading = false;
  private studioOutputsSid: string | null = null;
  private downloadBusy = new Set<string>();
  private deletingVersions = new Set<string>();
  private deletingDocs = new Set<string>();
  // Keyed `${sid}|${version}` so each deck version keeps independent feedback state.
  private deckFeedback = new Map<string, DeckFeedbackState>();
  /** True only while the project list itself (GET /decks/sessions) is being fetched. */
  sidebarStatusLoading = false;
  /** Projects whose status request failed; shown by name only. */
  sidebarStatusFailed = new Set<string>();
  /** Older projects whose status isn't prefetched on load; shown by date only until opened. */
  sidebarStatusDeferred = new Set<string>();
  /** Only the most recent projects get their status prefetched for the sidebar. */
  private readonly SIDEBAR_STATUS_PREFETCH = 10;
  // Request prioritisation: opening a project (foreground) pauses + preempts background prefetches.
  private foregroundCount = 0;
  private foregroundWaiters: (() => void)[] = [];
  private backgroundRequests = new Set<AbortController>();
  private selectionAbort: AbortController | null = null;
  private readonly INITIAL_MESSAGES_LIMIT = 20;
  private readonly MESSAGES_LIMIT_STEP = 20;
  private messagesLimitForCurrent = 0;

  approvalsRows: any[] = [];
  approvalsLoadingText = '';
  approvalsFilterValue = '';
  approvalsBadgeCount = 0;
  analyticsAccessDenied = false;

  private runs = new Map<string, RunTracker>();
  private hydrateSeq = new Map<string, number>();
  private reconcilingSessions = new Set<string>();
  private liveTurns = new Map<string, LiveTurn>();
  // Build-timer state: in-memory start per session (fast path) + finalized durations for the current session.
  private pendingStarts = new Map<string, number>();
  private deckTimings = new Map<number, number>();
  private uploadingSessions = new Map<string, number>();
  private pendingMessages = new Map<string, string[]>();
  private flushingQueues = new Set<string>();
  private queueRetryTimers = new Map<string, ReturnType<typeof setTimeout>>();
  private queueRetryAttempts = new Map<string, number>();
  private pendingUploads = new Map<string, SessionDoc & { _pid?: string; sessionId?: string }>();
  private ingestLogs = new Map<string, StreamEvent[]>();
  private signingOut = false;
  private timers: ReturnType<typeof setInterval>[] = [];
  private initialized = false;
  private destroyed = false;
  private cancelBootstrapWait?: () => void;
  private streamCleanups = new Set<() => void>();

  // A bounded, single-consumer port also retains requests raised before app-view exists.
  private viewPort?: WorkspaceViewPort;
  private presentationRequests = new Map<WorkspacePresentationEvent['type'], WorkspacePresentationEvent>();
  private readonly authExpiredHandler = () => this.showLogin();
  private readonly offlineHandler = () => this.onNetworkOffline();
  private readonly onlineHandler = () => this.onNetworkOnline();
  private readonly storageHandler = (e: StorageEvent) => {
    if (e.key === AUTOPILOT_STORAGE_KEY || e.key === null) this.autopilotStates = null;
  };
  private readonly authLostHandler = () => { if (!this.destroyed) this.forceSignOut(this.sessionLostMessage()); };

  constructor(
    public api: ApiService,
    public store: StoreService,
    private sso: SsoService,
    private ui: UiService,
    private config: ConfigService,
    private theme: ThemeService,
    private deckTimer: DeckTimerService,
  ) {}

  get isAdmin(): boolean { return this.api.hasRole('admin'); }
  get canViewAnalytics(): boolean { return this.isAdmin && !this.analyticsAccessDenied; }
  get userName(): string { return this.api.user?.displayName || this.api.user?.username || 'User'; }
  get userFirstName(): string {
    const raw = (this.api.user?.displayName || this.api.user?.username || '').trim();
    if (!raw || raw.toLowerCase() === 'user') return 'Amal';
    const first = raw.split(/\s+/)[0];
    return first ? (first.charAt(0).toUpperCase() + first.slice(1)) : 'Amal';
  }
  get userEmail(): string { return this.api.user?.email || ''; }
  get userRoles(): string[] { return this.api.user?.roles || (this.isAdmin ? ['Administrator'] : ['User']); }
  get userInitial(): string { return (this.userName.trim().charAt(0) || 'U').toUpperCase(); }

  initialize(): void {
    if (this.initialized || this.destroyed) return;
    this.initialized = true;
    this.theme.applyStoredTheme();
    this.loadQueues();
    this.api.onAuthLost = this.authLostHandler;
    window.addEventListener('auth-expired', this.authExpiredHandler);
    window.addEventListener('offline', this.offlineHandler);
    window.addEventListener('online', this.onlineHandler);
    window.addEventListener('storage', this.storageHandler);
    this.timers.push(setInterval(() => {
      if (this.view !== 'app' || !this.api.loggedIn) return;
      this.reconcileAllRuns();
    }, 10000));
    this.timers.push(setInterval(() => this.pollSession(), SESSION_POLL_MS));
    this.timers.push(setInterval(() => this.pingHealth(), 15000));
    this.bootstrap();
  }

  destroy(): void {
    this.destroyed = true;
    this.timers.forEach((t) => clearInterval(t));
    this.timers = [];
    this.cancelBootstrapWait?.();
    for (const cleanup of Array.from(this.streamCleanups)) cleanup();
    this.streamCleanups.clear();
    this.disposeAllTrackers();
    this.pendingMessages.clear();
    for (const timer of this.queueRetryTimers.values()) clearTimeout(timer);
    this.queueRetryTimers.clear();
    for (const timer of this.autopilotRetryTimers.values()) clearTimeout(timer);
    this.autopilotRetryTimers.clear();
    this.viewPort = undefined;
    this.presentationRequests.clear();
    window.removeEventListener('auth-expired', this.authExpiredHandler);
    window.removeEventListener('offline', this.offlineHandler);
    window.removeEventListener('online', this.onlineHandler);
    window.removeEventListener('storage', this.storageHandler);
    if (this.api.onAuthLost === this.authLostHandler) this.api.onAuthLost = null;
  }

  attachViewPort(port: WorkspaceViewPort): () => void {
    if (this.destroyed) return () => {};
    this.viewPort = port;
    for (const event of this.presentationRequests.values()) port.notify(event);
    this.presentationRequests.clear();
    return () => { if (this.viewPort === port) this.viewPort = undefined; };
  }

  private notifyPresentation(event: WorkspacePresentationEvent): void {
    if (this.destroyed) return;
    if (this.viewPort) this.viewPort.notify(event);
    else this.presentationRequests.set(event.type, event);
  }

  async onSsoLoginClick(): Promise<void> {
    this.loginError = '';
    this.ssoLoading = true;
    try {
      const tokens = await this.sso.ssoLogin();
      if (this.destroyed) return;
      await this.api.loginSso(tokens);
      try { localStorage.removeItem('se_current'); } catch {}
      this.showApp();
      this.selectSession(null);
    } catch (err: any) {
      if (this.destroyed) return;
      this.loginError = err.message || 'Sign-in failed';
      if (err.correlationId) console.error(`login failed — correlation_id ${err.correlationId}`);
      if (err.status === 403) { try { await this.sso.ssoLogout(); } catch { /* ignore */ } }
    } finally {
      this.ssoLoading = false;
    }
  }

  onLogoutClick(): void { this.forceSignOut(); }

  async onNewSessionClick(): Promise<void> {
    const title = await this.ui.askText('Name this project (e.g. "SMT ppm training"):', 'New project');
    if (title === null) return;
    const s = this.store.create(title || 'Untitled session');
    this.registerProject(s);
    this.refreshSessionListView();
    this.selectSession(s.id);
    this.switchTab('chat');
    this.notifyPresentation({ type: 'project-created' });
  }

  onFilesSelected(event: Event): void {
    const input = event.target as HTMLInputElement;
    const files = Array.from(input.files || []) as File[];
    input.value = '';
    if (!files.length) return;
    const { valid, rejected } = this.validateFiles(files);
    if (rejected.length) {
      this.ui.toast(rejected.length === 1 ? rejected[0] : `${rejected.length} files were rejected: ${rejected.join(' ')}`, 'error');
    }
    if (!valid.length) return;
    const sid = this.ensureSession();
    // Hold chat until ingestion has finished, including legacy and presigned uploads.
    this.markUploadStart(sid);
    this.uploadFiles(sid, valid).finally(() => this.markUploadEnd(sid));
  }

  /** Reject files with an unsupported extension or over the max upload size before they're sent. */
  private validateFiles(files: File[]): { valid: File[]; rejected: string[] } {
    const valid: File[] = [];
    const rejected: string[] = [];
    for (const file of files) {
      const ext = `.${file.name.split('.').pop()?.toLowerCase() || ''}`;
      if (!ACCEPTED_UPLOAD_EXTENSIONS.includes(ext)) {
        rejected.push(`"${file.name}" has an unsupported format.`);
        continue;
      }
      if (file.size > MAX_UPLOAD_BYTES) {
        rejected.push(`"${file.name}" exceeds the 200 MB limit.`);
        continue;
      }
      valid.push(file);
    }
    return { valid, rejected };
  }

  onChatSubmit(): void {
    const message = (this.chatInputValue || '').trim();
    if (!message) return;
    const sid = this.ensureSession();
    this.chatInputValue = '';
    this.resetAutopilotReplies(sid);
    this.notifyPresentation({ type: 'resize-composer', focus: false });
    // Anything in flight (or already queued ahead) means this prompt waits its turn.
    if (this.hasQueuedMessage(sid) || !this.queueReady(sid)) this.queuePendingMessage(sid, message);
    else void this.sendMessage(sid, message);
    this.updateComposerState();
  }

  onOpenSession(sid: string): void {
    this.switchTab('chat');
    this.selectSession(sid);
  }

  onPipelineDownload(s: Session): void {
    if (s.deckVersion == null) return;
    const version = s.deckVersion;
    this.deckFileName(s.id, version)
      .then((name) => this.api.downloadDeck(s.id, version, undefined, name))
      .catch((e: any) => this.ui.toast(e.message, 'error'));
  }

  /**
   * `{plan_title}_{projectTitle}_v{version}[_{language}].pptx`. `plan_title` comes from the
   * ppt_versions row (via GET /decks/{sid}/versions); the project title falls back to the
   * session id when unknown. Characters illegal in filenames are removed.
   */
  private async deckFileName(sid: string, version: number, language?: string): Promise<string> {
    const clean = (s: string) => s.replace(/[\\/:*?"<>|\x00-\x1f]/g, '').replace(/\s+/g, '_').replace(/^[._]+|[._]+$/g, '');
    const planTitle = clean((await this.deckPlanTitle(sid, version)) || '');
    const projectTitle = clean(this.store.get(sid)?.title || '') || clean(sid) || 'deck';
    const parts = [planTitle, projectTitle, `v${version}`];
    if (language) parts.push(clean(language));
    return `${parts.filter(Boolean).join('_')}.pptx`;
  }

  private async deckPlanTitle(sid: string, version: number): Promise<string | null> {
    const cached = this.studioOutputsSid === sid
      ? this.studioOutputs?.find((v) => v.deck_version === version)
      : undefined;
    if (cached?.plan_title) return cached.plan_title;
    try {
      const data = await this.api.getJson(`/decks/${encodeURIComponent(sid)}/versions`);
      const row = (data?.versions || []).find((v: any) => v.deck_version === version);
      return typeof row?.plan_title === 'string' ? row.plan_title : null;
    } catch {
      return null;
    }
  }

  async onPipelineSubmit(s: Session): Promise<void> {
    try {
      await this.api.postJson('/approvals/submit', { session_id: s.id, deck_version: s.deckVersion });
      this.ui.toast('Submitted for approval', 'success');
      this.switchTab('approvals');
    } catch (e: any) {
      this.ui.toast(`Submit failed: ${e.message}`, 'error');
    }
  }

  private showLogin(): void { if (!this.destroyed) this.view = 'login'; }
  private showBoot(message?: string): void {
    if (this.destroyed) return;
    this.view = 'boot';
    if (message) this.bootMessage = message;
  }

  private updateUserChip(): void {
    this.userChipText = `${this.api.user?.displayName || this.api.user?.username || '?'} · ${(this.api.user?.roles || []).join(', ')}`;
    this.userChipTitle = this.api.user?.email || '';
  }

  private showApp(): void {
    if (this.destroyed) return;
    this.view = 'app';
    this.analyticsAccessDenied = false;
    this.updateUserChip();
    this.refreshSessionListView();
    this.refreshApprovalsBadge();
    this.pingHealth();
    this.restoreServerProjects().then(() => { this.flushAllQueues(); this.resumeAutopilotAll(); });
    this.reconcileAllRuns();
    const last = localStorage.getItem('se_current');
    if (last && this.store.get(last)) {
      this.selectSession(last);
    } else {
      this.selectSession(null);
    }
  }

  switchTab(tab: TabName): void {
    if (tab === 'analytics' && !this.canViewAnalytics) return;
    this.currentTab = tab;
    if (tab === 'pipeline') this.refreshSessionListView();
    if (tab === 'approvals') this.loadApprovals();
  }

  hideAnalytics(): void {
    this.analyticsAccessDenied = true;
    if (this.currentTab === 'analytics') this.currentTab = 'chat';
    this.ui.toast('Analytics access is no longer available for this account.', 'error');
  }

  private sessionLostMessage(): string {
    const e = this.api.lastAuthError;
    if (e?.status === 403) return 'Your access has changed or was revoked. Please sign in again or contact your administrator.';
    return 'Your session has ended — it may have expired or you signed in on another device. Please sign in again.';
  }

  private clearBrowserData(): void {
    const theme = localStorage.getItem('se_theme');
    const autopilot = localStorage.getItem(AUTOPILOT_STORAGE_KEY);
    try { localStorage.clear(); } catch { /* ignore */ }
    try { sessionStorage.clear(); } catch { /* ignore */ }
    if (theme) localStorage.setItem('se_theme', theme);
    if (autopilot) localStorage.setItem(AUTOPILOT_STORAGE_KEY, autopilot);
  }

  private async forceSignOut(message?: string): Promise<void> {
    if (this.signingOut || this.destroyed) return;
    this.signingOut = true;
    this.disposeAllTrackers();
    try {
      await this.api.logout();
      await this.sso.ssoLogout();
    } finally {
      if (!this.destroyed) {
        this.clearBrowserData();
        this.showLogin();
        this.loginError = message || '';
      }
      this.signingOut = false;
    }
  }

  private async pollSession(): Promise<void> {
    if (this.destroyed || this.view !== 'app' || !this.api.loggedIn || this.signingOut) return;
    if (this.api.sessionExpired) { this.forceSignOut(EIGHT_HOUR_MSG); return; }
    const { status, body } = await this.api.validate();
    if (this.destroyed) return;
    if (status === 200) {
      if (body?.data?.should_refresh) await this.api.tryRefresh();
      this.updateUserChip();
      return;
    }
    if (status === 401) {
      if (!(await this.api.tryRefresh())) this.forceSignOut(this.sessionLostMessage());
    }
  }

  private async pingHealth(): Promise<void> {
    try {
      const res = await fetch(`${this.config.getApiBaseUrl()}/health`);
      this.connState = res.ok ? 'ok' : 'bad';
    } catch {
      this.connState = 'bad';
    }
  }

  private async restoreServerProjects(): Promise<void> {
    this.sidebarStatusLoading = true;
    try {
      const data = await this.api.listSessions();
      if (this.destroyed) return;
      const addedIds: string[] = [];
      let corrected = false;
      for (const row of data.sessions || []) {
        const local = this.store.get(row.session_id);
        if (local) {
          // user_sessions.created_at is the source of truth; a locally cached value is only a browser-clock guess.
          if (row.created_at && local.createdAt !== row.created_at) {
            this.store.update(row.session_id, { createdAt: row.created_at });
            corrected = true;
          }
          continue;
        }
        this.store.upsert({
          id: row.session_id,
          title: row.title || 'Untitled project',
          createdAt: row.created_at || new Date().toISOString(),
        });
        addedIds.push(row.session_id);
      }
      if (corrected || addedIds.length) this.refreshSessionListView();
      if (addedIds.length) {
        // Don't sweep messages/versions/stream for every restored project on login. A project's
        // status and history are derived lazily when it's opened (selectSession → hydrateSession).
        // Still reconcile so an already-running build resumes even before it's opened.
        this.reconcileAllRuns();
      }
      // Show the project list as soon as names are known; statuses fill in afterwards.
      this.sidebarStatusLoading = false;
      this.refreshSessionListView();
      await this.hydrateSidebarStatuses();
    } catch { /* offline / older backend — keep local-only projects */ }
    finally { this.sidebarStatusLoading = false; }
  }

  /**
   * Fills in status/version for the most recent projects still marked 'new', so the top of the
   * sidebar is accurate before any is opened. Older projects are left for selectSession → hydrateSession.
   */
  private async hydrateSidebarStatuses(): Promise<void> {
    const all = this.store.all();
    const recent = all.slice(0, this.SIDEBAR_STATUS_PREFETCH);
    this.sidebarStatusDeferred = new Set(all.slice(this.SIDEBAR_STATUS_PREFETCH)
      .filter((s) => s.status === 'new' && s.id !== this.currentSessionId).map((s) => s.id));
    const queue = recent.filter((s) => s.status === 'new').map((s) => s.id);
    const worker = async (): Promise<void> => {
      for (let sid = queue.shift(); sid && !this.destroyed; sid = queue.shift()) {
        // Never compete with a project the user is opening; resume once its requests settle.
        await this.whenForegroundIdle();
        if (this.destroyed) return;
        // Opening a project derives its status itself, so skip anything already resolved.
        const before = this.store.get(sid);
        if (!before || before.status !== 'new' || sid === this.currentSessionId) continue;
        const ctrl = new AbortController();
        this.backgroundRequests.add(ctrl);
        let data: any;
        try {
          data = await this.api.getMessages(sid, this.INITIAL_MESSAGES_LIMIT, { signal: ctrl.signal, priority: 'low' });
        } catch {
          // Preempted by a foreground load: requeue and retry after it finishes.
          if (ctrl.signal.aborted && !this.destroyed) queue.unshift(sid);
          else this.sidebarStatusFailed.add(sid);
          continue;
        } finally {
          this.backgroundRequests.delete(ctrl);
        }
        try {
          if (this.destroyed) return;
          const cur = this.store.get(sid);
          if (!cur || cur.status !== 'new') continue;
          const msgs: any[] = data.messages || [];
          const latest = [...msgs].reverse().find((m) => m.role === 'assistant' && m.result);
          const patch: any = {};
          const lastRun = latestTimelineRunStatus(data.timeline);
          if (findRunningRunId(data.timeline)) patch.status = 'running';
          else if (lastRun === 'CANCELLED') patch.status = 'stopped';
          else if (lastRun === 'FAILED') patch.status = 'failed';
          else if (latest?.result?.deck_updated) patch.status = 'done';
          else if (msgs.some((m) => m.role === 'assistant')) patch.status = 'answered';
          if (latest?.result?.deck_version != null) patch.deckVersion = latest.result.deck_version;
          if (latest?.result?.verdict != null) patch.verdict = latest.result.verdict;
          if (Object.keys(patch).length) {
            this.store.update(sid, patch);
            this.refreshSessionListView();
          }
        } catch { this.sidebarStatusFailed.add(sid); }
      }
    };
    await Promise.all([worker(), worker(), worker()]);
  }

  /** Marks a user-initiated load; background prefetches are cancelled and paused until every one ends. */
  private beginForeground(): () => void {
    this.foregroundCount++;
    for (const c of this.backgroundRequests) c.abort();
    let ended = false;
    return () => {
      if (ended) return;
      ended = true;
      if (--this.foregroundCount > 0) return;
      for (const resolve of this.foregroundWaiters.splice(0)) resolve();
    };
  }

  private whenForegroundIdle(): Promise<void> {
    return this.foregroundCount > 0 ? new Promise((resolve) => this.foregroundWaiters.push(resolve)) : Promise.resolve();
  }

  // ---- Run lifecycle: run_id identifies a generation; backend Run.status is the source of truth ----

  private trackerFor(sid: string): RunTracker {
    let t = this.runs.get(sid);
    if (!t) {
      t = {
        sid, state: newWorkflowState(sid), turn: null, message: null, answers: null, bubble: null, restorable: true, fromQueue: false, controller: null,
        reconnectAttempt: 0, reconnectTimer: null, wake: null, stopRequested: false, completing: false,
        alreadyRunning: false, noticeDismissed: false, disposed: false, lastPill: null, gen: 0,
      };
      this.runs.set(sid, t);
    }
    return t;
  }

  private isTrackerBusy(t: RunTracker): boolean { return !!(t.controller || t.reconnectTimer || t.wake); }

  private stopTransport(t: RunTracker): void {
    const controller = t.controller;
    t.controller = null;
    controller?.abort();
    if (t.reconnectTimer) { clearTimeout(t.reconnectTimer); t.reconnectTimer = null; }
    t.wake?.();
  }

  private disposeTracker(sid: string): void {
    const t = this.runs.get(sid);
    if (!t) return;
    t.disposed = true;
    this.stopTransport(t);
    this.runs.delete(sid);
    if (this.liveTurns.delete(sid) && sid === this.currentSessionId) this.syncLiveTurnView();
  }

  private disposeAllTrackers(): void {
    for (const sid of Array.from(this.runs.keys())) this.disposeTracker(sid);
  }

  private runBlocksSession(sid: string | null): boolean {
    return !!sid && isRunBlocking(this.runs.get(sid)?.state);
  }

  private uiFlags(t: RunTracker): RunUiFlags {
    return { hasRetryMessage: !!(t.message || t.answers), stopRequested: t.stopRequested, completing: t.completing, alreadyRunning: t.alreadyRunning };
  }

  private syncRunView(): void {
    const t = this.currentSessionId ? this.runs.get(this.currentSessionId) : undefined;
    const flags = t ? this.uiFlags(t) : {};
    this.runControls = controlsFor(t?.state, flags);
    const notice = t ? runNotice(t.state, flags) : null;
    this.runNotice = notice && !(notice.dismissible && t?.noticeDismissed) ? notice : null;
  }

  private livePill(t: RunTracker): string | null {
    if (!isRunBlocking(t.state)) return null;
    if (t.state.stopping || t.stopRequested) return 'stopping';
    if (t.state.connection === 'reconnecting') return 'reconnecting';
    if (t.state.recovering) return 'recovering';
    return 'running';
  }

  private setRunState(t: RunTracker, state: WorkflowState): void {
    t.state = state;
    this.onRunChanged(t);
  }

  private onRunChanged(t: RunTracker): void {
    if (t.disposed || t.sid !== this.currentSessionId) return;
    this.updateComposerState();
    if (this.runBlocksSession(t.sid) !== this.questionViewInFlight) this.refreshQuestionView();
    const pill = this.livePill(t);
    if (pill !== t.lastPill) {
      t.lastPill = pill;
      this.syncCurrentSessionPill();
    }
  }

  /** Resolves after `ms`, or immediately when the tracker is stopped/disposed. */
  private waitFor(t: RunTracker, ms: number): Promise<void> {
    return new Promise((resolve) => {
      const timer = setTimeout(() => { t.wake = null; resolve(); }, ms);
      t.wake = () => { clearTimeout(timer); t.wake = null; resolve(); };
    });
  }

  dismissRunNotice(): void {
    const t = this.currentSessionId ? this.runs.get(this.currentSessionId) : undefined;
    if (!t) return;
    t.noticeDismissed = true;
    this.syncRunView();
  }

  onReconnectNowClick(): void {
    const t = this.currentSessionId ? this.runs.get(this.currentSessionId) : undefined;
    if (!t || t.state.terminal || !t.state.activeRunId || t.controller) return;
    if (t.reconnectTimer) { clearTimeout(t.reconnectTimer); t.reconnectTimer = null; }
    void this.resumeRun(t);
  }

  /** "Try again" after a retryable failure: a genuinely new submission with a new client_request_id. */
  onTryAgainClick(): void {
    const sid = this.currentSessionId;
    const t = sid ? this.runs.get(sid) : undefined;
    const terminal = t?.state.terminal;
    if (!sid || !t || !(t.message || t.answers) || terminal?.status !== 'FAILED' || !terminal.retryable) return;
    if (this.isSessionBusy(sid)) return;
    void this.sendMessage(sid, t.message || '', { answers: t.answers, bubble: t.bubble, restorable: t.restorable });
  }

  async onStopClick(): Promise<void> {
    const sid = this.currentSessionId;
    const t = sid ? this.runs.get(sid) : undefined;
    const runId = t?.state.activeRunId;
    if (!sid || !t || !runId || t.state.terminal || t.state.stopping || t.stopRequested) return;
    t.stopRequested = true;
    this.onRunChanged(t);
    try {
      const res = await this.api.stopRun(sid, runId);
      if (this.destroyed || t.disposed) return;
      const status = String(res?.status || '').trim().toLowerCase();
      const live = t.state.activeRunId === runId && !t.state.terminal;
      // `done` can beat the stop response, so always tell the user why Stop did not cancel.
      if (status === 'completing') {
        if (live) t.completing = true;
        this.ui.toast(COMPLETING_COPY);
      } else if (!live) {
        return;
      } else if (status === 'cancelling' || status === 'already_cancelling') {
        t.state = { ...t.state, stopping: true, runStatus: 'CANCELLING' };
        this.returnQueueToDraft(sid);
      } else {
        // not_running: our view is stale. Re-read the run and replay its remaining events.
        this.stopTransport(t);
        void this.resumeRun(t);
      }
    } catch (err: any) {
      if (!this.destroyed && !t.disposed) this.ui.toast(`Couldn't stop the generation: ${err?.message || 'request failed'}`, 'error');
    } finally {
      t.stopRequested = false;
      this.onRunChanged(t);
    }
  }

  private onRunEvent(t: RunTracker, evt: StreamEvent): void {
    if (this.destroyed || t.disposed || !evt || typeof evt !== 'object') return;
    const before = t.state;
    const { state, accepted } = applyRunEvent(before, evt);
    if (evt.type === 'run_started') {
      if (!state.activeRunId || state.activeRunId !== String(evt.run_id)) return;
      clearPendingRequest(t.sid);
      t.reconnectAttempt = 0;
      if (accepted && evt.data?.status === 'already_running') this.onAlreadyRunning(t);
      this.setRunState(t, { ...state, connection: 'open' });
      return;
    }
    if (!accepted) return;
    t.reconnectAttempt = 0;
    if (evt.type === 'questions') {
      // Live or replayed: replace (never append) so a reconnect cannot duplicate the form.
      const questions = normalizeQuestions(evt.data?.questions);
      if (t.turn && questions.length) t.turn.questions = questions;
      this.setRunState(t, { ...state, connection: 'open' });
      this.settleResume(t);
      if (t.sid === this.currentSessionId) { this.refreshQuestionView(); this.scheduleMessagesScroll(); }
      return;
    }
    if (t.turn && state.activeRunId) {
      t.turn.events.push(evt);
      updateFlowState(t.turn.flow, evt);
      if (t.sid === this.currentSessionId) this.scheduleMessagesScroll();
    }
    this.setRunState(t, { ...state, connection: 'open' });
    this.settleResume(t);
    if (state.terminal && !before.terminal) this.finishRun(t, evt);
  }

  /** The submitted message was NOT recorded: keep its text for the user and show the active run instead. */
  private onAlreadyRunning(t: RunTracker): void {
    const message = t.message;
    const bubble = t.bubble;
    const restorable = t.restorable;
    const fromQueue = t.fromQueue;
    t.message = null;
    t.answers = null;
    t.bubble = null;
    t.fromQueue = false;
    t.alreadyRunning = true;
    if (bubble) this.removeLocalUserMessage(t.sid, bubble);
    if (message && fromQueue) {
      // Still its turn next: put it back at the head of the queue; the active run's end flushes it.
      this.setQueue(t.sid, [message, ...(this.pendingMessages.get(t.sid) || [])]);
      this.releaseQuestionForm(t.sid, '');
      return;
    }
    if (message && restorable) this.restoreDraft(t.sid, message);
    this.releaseQuestionForm(t.sid, '');
    this.ui.toast(ALREADY_RUNNING_COPY);
  }

  private async sendMessage(
    sid: string, message: string,
    opts: {
      clientRequestId?: string; resend?: boolean; answers?: QuestionAnswers | null; bubble?: string | null; restorable?: boolean;
      fromQueue?: boolean;
    } = {},
  ): Promise<void> {
    if (this.destroyed || !this.store.get(sid)) return;
    const t = this.trackerFor(sid);
    if (isRunBlocking(t.state) || this.isTrackerBusy(t)) {
      // Never drop a queued prompt: keep it at the head so the in-flight run's end sends it.
      if (opts.fromQueue && message) this.setQueue(sid, [message, ...(this.pendingMessages.get(sid) || [])]);
      return;
    }
    const clientRequestId = opts.clientRequestId || newClientRequestId();
    const answers = opts.answers && Object.keys(opts.answers).length ? opts.answers : null;
    const bubble = opts.bubble || message || (answers ? describeAnswers(answers) : '');
    if (!message && !answers) return;
    writePendingRequest(sid, { message, clientRequestId, answers, bubble });
    const gen = ++t.gen;
    t.message = message;
    t.answers = answers;
    t.bubble = bubble;
    t.restorable = opts.restorable ?? (!answers && message.trim() !== APPROVE_COMMAND);
    t.fromQueue = !!opts.fromQueue;
    t.stopRequested = false;
    t.completing = false;
    t.alreadyRunning = false;
    t.noticeDismissed = false;
    t.reconnectAttempt = 0;
    t.state = { ...newWorkflowState(sid), pendingClientRequestId: clientRequestId, connection: 'connecting' };
    if (!opts.resend || this.lastLocalUserMessage(sid) !== bubble) this.store.addMessage(sid, 'user', bubble);
    if (sid === this.currentSessionId) { this.syncMessagesView(); this.scheduleMessagesScroll(); }
    t.turn = newLiveTurn();
    this.liveTurns.set(sid, t.turn);
    // Start the build timer (durable across logout / reload via IndexedDB); finalized in applyDoneToStore.
    const startedAt = Date.now();
    this.pendingStarts.set(sid, startedAt);
    void this.deckTimer.startPending(sid, this.store.get(sid)?.title || 'Untitled project', this.userName, startedAt);
    if (sid === this.currentSessionId) { this.syncLiveTurnView(); this.scheduleMessagesScroll(); }
    this.store.update(sid, { status: 'running' });
    this.refreshSessionListView();
    t.lastPill = null;
    this.onRunChanged(t);
    await this.invokeRun(t, gen, message, clientRequestId, answers);
  }

  /** POST /invoke; transport failures before `run_started` resend with the SAME client_request_id. */
  private async invokeRun(t: RunTracker, gen: number, message: string, clientRequestId: string, answers: QuestionAnswers | null = null): Promise<void> {
    for (let attempt = 0; ; attempt++) {
      if (this.destroyed || t.disposed || t.gen !== gen) return;
      const controller = new AbortController();
      t.controller = controller;
      let error: any = null;
      try {
        await this.api.streamInvoke(t.sid, message, clientRequestId, (evt) => { if (t.gen === gen) this.onRunEvent(t, evt); }, controller.signal, answers);
      } catch (err) { error = err; }
      if (t.controller === controller) t.controller = null;
      if (t.gen === gen && t.state.terminal?.status === 'COMPLETED') this.kickAutopilot(t.sid);
      if (this.destroyed || t.disposed || t.gen !== gen || controller.signal.aborted || t.state.terminal) return;
      if (t.state.activeRunId) { this.scheduleReconnect(t, true); return; }
      const status = Number(error?.status) || 0;
      const retryable = !status || status === 408 || status === 409 || status === 429 || status >= 500;
      if (!retryable || attempt >= MAX_SUBMIT_RETRIES) { this.failBeforeRunStarted(t, error); return; }
      this.setRunState(t, { ...t.state, connection: 'reconnecting' });
      await this.waitFor(t, retryDelayMs(attempt));
    }
  }

  /** No run was created, so nothing was recorded server-side; surface a controlled failure. */
  private failBeforeRunStarted(t: RunTracker, error: any): void {
    const status = Number(error?.status) || 0;
    const rejected = status >= 400 && status < 500 && status !== 408 && status !== 409 && status !== 429;
    const detail = typeof error?.detail === 'string' && error.detail.trim() ? error.detail.trim() : '';
    t.state = {
      ...t.state,
      pendingClientRequestId: null,
      runStatus: 'FAILED',
      terminal: rejected
        ? { status: 'FAILED', errorCode: 'REQUEST_REJECTED', error: detail || 'The request could not be processed. Please edit your message and try again.', retryable: false }
        : { status: 'FAILED', errorCode: 'SERVICE_UNAVAILABLE', error: "Couldn't reach the service. Check your connection and try again.", retryable: true },
    };
    if (rejected && t.message && t.restorable) this.restoreDraft(t.sid, t.message);
    const answersRejected = rejected && !!t.answers;
    // A rejected questionnaire stays on screen with the backend's reason inline (not in the composer).
    if (answersRejected) this.releaseQuestionForm(t.sid, detail || 'Your answers could not be submitted. Please review them and try again.');
    else if (t.answers) this.releaseQuestionForm(t.sid, '');
    this.finishRun(t, { type: 'done', data: { status: 'FAILED', error: t.state.terminal!.error } });
    if (answersRejected && this.store.get(t.sid)) {
      // Nothing was recorded server-side, so the session is still waiting for answers, not failed.
      this.store.update(t.sid, { status: 'answered' });
      t.noticeDismissed = true;
      this.refreshSessionListView();
      if (t.sid === this.currentSessionId) this.syncCurrentSessionPill();
      this.onRunChanged(t);
    }
  }

  private scheduleReconnect(t: RunTracker, immediate = false): void {
    if (this.destroyed || t.disposed || t.state.terminal || !t.state.activeRunId) return;
    if (t.controller || t.reconnectTimer) return;
    if (t.state.connection !== 'reconnecting') this.setRunState(t, { ...t.state, connection: 'reconnecting' });
    const ms = immediate ? 0 : retryDelayMs(t.reconnectAttempt);
    t.reconnectAttempt++;
    t.reconnectTimer = setTimeout(() => { t.reconnectTimer = null; void this.resumeRun(t); }, ms);
  }

  /** Open streams can outlive a dead link (DevTools Offline, sleeping NIC): drop them and show Reconnecting now. */
  private onNetworkOffline(): void {
    if (this.destroyed) return;
    for (const t of this.runs.values()) {
      if (t.disposed || t.state.terminal || !t.state.activeRunId) continue;
      this.stopTransport(t);
      this.scheduleReconnect(t);
    }
  }

  private onNetworkOnline(): void {
    if (this.destroyed) return;
    for (const t of this.runs.values()) {
      if (t.disposed || t.state.terminal || !t.state.activeRunId || t.controller) continue;
      if (t.reconnectTimer) { clearTimeout(t.reconnectTimer); t.reconnectTimer = null; }
      t.reconnectAttempt = 0;
      void this.resumeRun(t);
    }
  }

  /** Reconnect via GET /runs/{id} + /runs/{id}/events?after_event_index=N. Never re-POSTs /invoke. */
  private async resumeRun(t: RunTracker): Promise<void> {
    const runId = t.state.activeRunId;
    if (this.destroyed || t.disposed || !runId || t.state.terminal || t.controller) return;
    const sid = t.sid;
    const gen = t.gen;
    const controller = new AbortController();
    t.controller = controller;
    const stale = () => this.destroyed || t.disposed || t.gen !== gen || controller.signal.aborted || t.state.activeRunId !== runId;
    if (t.state.connection === 'idle') this.setRunState(t, { ...t.state, connection: 'connecting' });
    let snapshot: Run | null = null;
    try {
      snapshot = await this.api.getRun(sid, runId);
      if (stale()) return;
      if (!isTerminalRunStatus(snapshot?.status)) this.setRunState(t, applyRunSnapshot(t.state, snapshot));
      if (t.turn?.resuming && snapshot) {
        t.turn.resumeTo = Math.max(t.turn.resumeTo || 0, Number(snapshot.last_event_index) || 0);
        this.settleResume(t);
      }
      await this.api.streamRunEvents(sid, runId, t.state.lastEventIndex, (evt) => { if (!stale()) this.onRunEvent(t, evt); }, controller.signal,
        () => { if (!stale() && !t.state.terminal) this.setRunState(t, { ...t.state, connection: 'open' }); });
    } catch (err: any) {
      if (!stale() && err?.status === 404) {
        // The run is not part of this session (deleted/foreign); drop it and re-read the session's runs.
        t.controller = null;
        this.forgetRun(t);
        void this.reconcileRun(sid, sid === this.currentSessionId);
        return;
      }
    } finally {
      if (t.controller === controller) t.controller = null;
    }
    if (!stale() && t.state.terminal?.status === 'COMPLETED') this.kickAutopilot(sid);
    if (stale() || t.state.terminal) return;
    if (snapshot && isTerminalRunStatus(snapshot.status)) { this.finishFromSnapshot(t, snapshot); return; }
    this.scheduleReconnect(t);
  }

  private forgetRun(t: RunTracker): void {
    this.stopTransport(t);
    t.gen++;
    t.state = newWorkflowState(t.sid);
    t.turn = null;
    if (this.liveTurns.delete(t.sid) && t.sid === this.currentSessionId) this.syncLiveTurnView();
    if (this.store.get(t.sid)?.status === 'running') this.store.update(t.sid, { status: 'answered' });
    this.refreshSessionListView();
    t.lastPill = null;
    this.onRunChanged(t);
  }

  /** The events stream ended without `done` but the run is terminal: apply the authoritative Run. */
  private finishFromSnapshot(t: RunTracker, run: Run): void {
    const state = applyRunSnapshot(t.state, run);
    const terminal = state.terminal;
    if (!terminal) return;
    const result = terminal.status === 'COMPLETED' && run.artifact_ready && run.deck_version != null
      ? { deck_updated: true, deck_version: run.deck_version } : undefined;
    t.state = state;
    this.finishRun(t, {
      type: 'done', agent: 'supervisor', run_id: run.run_id,
      message: terminal.status === 'COMPLETED' ? 'Finished' : (terminal.error || ''),
      data: { status: terminal.status, error: terminal.error, error_code: terminal.errorCode, retryable: terminal.retryable, synthetic: true, result },
    });
  }

  private finishRun(t: RunTracker, evt: StreamEvent): void {
    const sid = t.sid;
    const terminal = t.state.terminal;
    if (!terminal || t.disposed) return;
    if (t.reconnectTimer) { clearTimeout(t.reconnectTimer); t.reconnectTimer = null; }
    t.wake?.();
    t.stopRequested = false;
    t.completing = false;
    t.alreadyRunning = false;
    t.noticeDismissed = false;
    clearPendingRequest(sid);
    this.releaseQuestionForm(sid, null);
    const turn = t.turn;
    t.turn = null;
    t.state = { ...t.state, connection: 'idle', pendingClientRequestId: null };
    if (!t.state.activeRunId) {
      // Admission failure / request never accepted: no run, message or deck exists server-side.
      if (this.liveTurns.get(sid) === turn) this.liveTurns.delete(sid);
      if (t.bubble) this.removeLocalUserMessage(sid, t.bubble);
      this.pendingStarts.delete(sid);
      void this.deckTimer.discardPending(sid);
      if (this.store.get(sid)) this.store.update(sid, { status: 'failed' });
      if (sid === this.currentSessionId) { this.syncLiveTurnView(); this.syncMessagesView(); }
    } else {
      this.applyDoneToStore(sid, evt, turn);
      if (terminal.status === 'CANCELLED' && t.message && t.restorable) this.restoreDraft(sid, t.message);
      // Completed/stopped runs are persisted (or rolled back) before `done`, so the server view is final.
      if (terminal.status !== 'FAILED' && sid === this.currentSessionId) {
        void this.hydrateSession(sid, this.messagesLimitForCurrent || this.INITIAL_MESSAGES_LIMIT);
      }
      if (this.store.get(sid)) this.refreshDocuments(sid).catch(() => {});
    }
    this.refreshSessionListView();
    t.lastPill = null;
    if (sid === this.currentSessionId) this.syncCurrentSessionPill();
    this.onRunChanged(t);
    // Deferred so the finished stream loop unwinds (and releases its controller) before a queued send.
    setTimeout(() => {
      void this.flushPendingMessages(sid);
      if (terminal.status === 'COMPLETED') this.kickAutopilot(sid);
    }, 0);
  }

  /** Poll/refresh reconciliation: the open project always, background projects only while running. */
  private reconcileAllRuns(): void {
    for (const s of this.store.all()) {
      const current = s.id === this.currentSessionId;
      if (current || s.status === 'running' || this.runBlocksSession(s.id)) void this.reconcileRun(s.id, current);
    }
  }

  /** GET /runs/current: attach to an active run, settle a finished one, or resend a pending submission. */
  private async reconcileRun(sid: string, attach: boolean): Promise<void> {
    if (this.destroyed || !this.api.loggedIn || this.reconcilingSessions.has(sid)) return;
    const existing = this.runs.get(sid);
    if (existing && this.isTrackerBusy(existing)) return;
    this.reconcilingSessions.add(sid);
    try {
      const cur = await this.api.getCurrentRun(sid);
      if (this.destroyed || !this.store.get(sid)) return;
      // Decide on arrival: a reconcile started before this project was opened (boot) must still attach it.
      const open = attach || sid === this.currentSessionId;
      const t = this.runs.get(sid);
      if (t && this.isTrackerBusy(t)) return;
      const pending = readPendingRequest(sid);
      const active = cur.active && cur.active.run_id ? cur.active : null;
      if (active) {
        if (pending) {
          clearPendingRequest(sid);
          // A different run is active, so the backend would not record this message; hand it back.
          if (active.client_request_id !== pending.clientRequestId && !pending.answers
            && pending.message.trim() !== APPROVE_COMMAND) this.restoreDraft(sid, pending.message);
        }
        if (this.store.get(sid)?.status !== 'running') {
          this.store.update(sid, { status: 'running' });
          this.refreshSessionListView();
          if (sid === this.currentSessionId) this.syncCurrentSessionPill();
        }
        if (open || (t && t.state.activeRunId === active.run_id)) this.attachActiveRun(sid, active);
        return;
      }
      if (t && t.state.activeRunId && !t.state.terminal) {
        // Our run ended while we were disconnected: replay its remaining events to `done`.
        void this.resumeRun(t);
        return;
      }
      if (pending) {
        if (cur.latest?.client_request_id === pending.clientRequestId) clearPendingRequest(sid);
        else if (attach) {
          void this.sendMessage(sid, pending.message, {
            clientRequestId: pending.clientRequestId, resend: true, answers: pending.answers, bubble: pending.bubble,
          });
          return;
        }
      }
      this.applyLatestRun(sid, cur.latest, attach);
    } catch { /* offline or transient: the next poll retries */ }
    finally {
      this.reconcilingSessions.delete(sid);
      if (sid === this.currentSessionId) this.updateComposerState();
      void this.flushPendingMessages(sid);
      void this.runAutopilot(sid);
    }
  }

  private attachActiveRun(sid: string, run: Run): void {
    const t = this.trackerFor(sid);
    if (t.state.activeRunId === run.run_id && !t.state.terminal) {
      if (!this.isTrackerBusy(t)) void this.resumeRun(t);
      return;
    }
    this.stopTransport(t);
    t.gen++;
    t.state = attachToRun(sid, run);
    t.message = null;
    t.answers = null;
    t.bubble = null;
    t.restorable = true;
    t.fromQueue = false;
    t.stopRequested = false;
    t.completing = false;
    t.alreadyRunning = false;
    t.noticeDismissed = false;
    t.reconnectAttempt = 0;
    const turn = newLiveTurn();
    turn.resumeTo = Math.max(0, Number(run.last_event_index) || 0);
    turn.resuming = turn.resumeTo > 0;
    t.turn = turn;
    this.liveTurns.set(sid, turn);
    this.dropPersistedThoughtForRun(sid, run.run_id);
    // Events already persisted in /messages need no replay; the stream resumes after them.
    this.seedFromTimeline(t);
    if (turn.resuming) {
      setTimeout(() => {
        if (!turn.resuming) return;
        turn.resuming = false;
        if (sid === this.currentSessionId) this.syncLiveTurnView();
      }, RESUME_MAX_MS);
    }
    if (sid === this.currentSessionId) { this.syncLiveTurnView(); this.scheduleMessagesScroll(); }
    t.lastPill = null;
    this.onRunChanged(t);
    void this.resumeRun(t);
  }

  /** Reflect a finished latest run (e.g. from another tab or before a refresh) without re-streaming it. */
  private applyLatestRun(sid: string, latest: Run | null, attach: boolean): void {
    const s = this.store.get(sid);
    const status = normalizeRunStatus(latest?.status);
    if (!s || !latest || !status || !isTerminalRunStatus(status)) {
      if (s?.status === 'running' && !this.runBlocksSession(sid)) {
        this.store.update(sid, { status: 'answered' });
        this.refreshSessionListView();
        if (sid === this.currentSessionId) this.syncCurrentSessionPill();
      }
      return;
    }
    const wasRunning = s.status === 'running';
    const patch: Partial<Session> = {};
    if (status === 'COMPLETED') {
      const next = latest.artifact_ready ? 'done' : (wasRunning ? 'answered' : s.status);
      if (next !== s.status) patch.status = next;
      if (latest.deck_version != null && latest.deck_version !== s.deckVersion) patch.deckVersion = latest.deck_version;
      if (wasRunning && latest.artifact_ready) this.resetAutopilotReplies(sid);
    } else {
      const next = status === 'FAILED' ? 'failed' : 'stopped';
      if (next !== s.status) patch.status = next;
    }
    if (Object.keys(patch).length) {
      this.store.update(sid, patch);
      this.refreshSessionListView();
      if (sid === this.currentSessionId) this.syncCurrentSessionPill();
    }
    if (!attach) return;
    const t = this.trackerFor(sid);
    if (!isRunBlocking(t.state) && t.state.activeRunId !== latest.run_id) {
      t.state = status === 'FAILED'
        ? { ...newWorkflowState(sid), activeRunId: latest.run_id, runStatus: status, terminal: terminalFromRun(latest) }
        : newWorkflowState(sid);
      t.message = status === 'FAILED' ? this.lastLocalUserMessage(sid) : null;
      t.answers = null;
      t.bubble = t.message;
      t.restorable = !!t.message && t.message.trim() !== APPROVE_COMMAND;
      t.noticeDismissed = false;
      this.onRunChanged(t);
    }
    if (wasRunning && sid === this.currentSessionId) {
      void this.hydrateSession(sid, this.messagesLimitForCurrent || this.INITIAL_MESSAGES_LIMIT);
      this.loadStudioOutputs(sid);
    }
  }

  private liveRunIdFor(sid: string): string | null {
    const t = this.runs.get(sid);
    return t && isRunBlocking(t.state) ? t.state.activeRunId : null;
  }

  private dropPersistedThoughtForRun(sid: string, runId: string): void {
    const s = this.store.get(sid);
    if (!s) return;
    const kept = s.messages.filter((m) => !(m.role === 'thought' && (m.events || []).some((e) => e.run_id === runId)));
    if (kept.length === s.messages.length) return;
    this.store.update(sid, { messages: kept });
    if (sid === this.currentSessionId) this.syncMessagesView();
  }

  private lastLocalUserMessage(sid: string): string | null {
    const msgs = this.store.get(sid)?.messages || [];
    for (let i = msgs.length - 1; i >= 0; i--) if (msgs[i].role === 'user') return msgs[i].content || null;
    return null;
  }

  private removeLocalUserMessage(sid: string, message: string): void {
    const s = this.store.get(sid);
    if (!s) return;
    for (let i = s.messages.length - 1; i >= 0; i--) {
      const m = s.messages[i];
      if (m.role === 'user' && m.id == null && m.content === message) {
        s.messages.splice(i, 1);
        this.store.update(sid, { messages: s.messages });
        if (sid === this.currentSessionId) this.syncMessagesView();
        return;
      }
    }
  }

  private markLastUserMessage(sid: string, runStatus: RunStatus): void {
    const s = this.store.get(sid);
    if (!s) return;
    for (let i = s.messages.length - 1; i >= 0; i--) {
      if (s.messages[i].role !== 'user') continue;
      s.messages[i] = { ...s.messages[i], run_status: runStatus };
      this.store.update(sid, { messages: s.messages });
      if (sid === this.currentSessionId) this.syncMessagesView();
      return;
    }
  }

  /** Put an unsent/stopped message back in the composer without discarding what the user typed since. */
  private restoreDraft(sid: string, message: string): void {
    if (sid !== this.currentSessionId || !message) return;
    const current = (this.chatInputValue || '').trim();
    if (current === message.trim() || current.includes(message.trim())) return;
    this.chatInputValue = current ? `${message}\n\n${current}` : message;
    this.notifyPresentation({ type: 'resize-composer', focus: false });
  }

  private isUploading(sid: string): boolean { return (this.uploadingSessions.get(sid) || 0) > 0; }
  private isSessionBusy(sid: string | null): boolean {
    if (!sid) return false;
    return this.isUploading(sid) || this.runBlocksSession(sid);
  }
  /** True while a deck build is in flight or the server reports the session as running. */
  isGenerating(sid: string | null): boolean {
    if (!sid) return false;
    return this.runBlocksSession(sid) || this.store.get(sid)?.status === 'running';
  }
  private hasQueuedMessage(sid: string | null): boolean { return !!(sid && this.pendingMessages.get(sid)?.length); }

  private markUploadStart(sid: string): void {
    this.uploadingSessions.set(sid, (this.uploadingSessions.get(sid) || 0) + 1);
    if (sid === this.currentSessionId) this.updateComposerState();
  }
  private markUploadEnd(sid: string): void {
    const n = (this.uploadingSessions.get(sid) || 0) - 1;
    if (n > 0) this.uploadingSessions.set(sid, n); else this.uploadingSessions.delete(sid);
    if (sid === this.currentSessionId) this.updateComposerState();
    void this.flushPendingMessages(sid);
    this.kickAutopilot(sid);
  }
  private queuePendingMessage(sid: string, message: string): void {
    this.setQueue(sid, [...(this.pendingMessages.get(sid) || []), message]);
    void this.flushPendingMessages(sid);
  }

  private setQueue(sid: string, q: string[]): void {
    if (q.length) this.pendingMessages.set(sid, q); else this.pendingMessages.delete(sid);
    const stored: Record<string, string[]> = {};
    for (const [id, msgs] of this.pendingMessages) stored[id] = msgs;
    try { sessionStorage.setItem(QUEUE_STORAGE_KEY, JSON.stringify(stored)); } catch { /* storage unavailable */ }
    if (sid === this.currentSessionId) {
      this.syncPendingMessagesView();
      this.updateComposerState();
    }
  }

  private loadQueues(): void {
    try {
      const stored = JSON.parse(sessionStorage.getItem(QUEUE_STORAGE_KEY) || '{}');
      for (const [sid, msgs] of Object.entries(stored || {})) {
        const q = Array.isArray(msgs) ? msgs.filter((m): m is string => typeof m === 'string' && !!m.trim()) : [];
        if (q.length) this.pendingMessages.set(sid, q);
      }
    } catch { /* corrupt or unavailable: start empty */ }
  }

  private flushAllQueues(): void {
    for (const sid of Array.from(this.pendingMessages.keys())) void this.flushPendingMessages(sid);
  }

  /** Nothing local is in flight for this project (run, upload, transport, or an unacknowledged submit). */
  private queueReady(sid: string): boolean {
    const t = this.runs.get(sid);
    return !this.isSessionBusy(sid) && !(t && this.isTrackerBusy(t)) && !readPendingRequest(sid);
  }

  /**
   * Send the next queued prompt once the previous workflow is fully finished. The server is asked
   * first, so `/invoke` is never called while it still reports an active run for this project.
   */
  private async flushPendingMessages(sid: string): Promise<void> {
    if (this.destroyed || this.flushingQueues.has(sid) || !this.hasQueuedMessage(sid)) return;
    if (!this.store.get(sid)) { this.setQueue(sid, []); return; }
    // A live run/upload flushes again when it ends; anything else only needs a short re-check.
    if (!this.queueReady(sid)) { if (!this.isSessionBusy(sid)) this.scheduleQueueRetry(sid); return; }
    this.flushingQueues.add(sid);
    try {
      let active = false;
      try { active = !!(await this.api.getCurrentRun(sid))?.active?.run_id; } catch { this.scheduleQueueRetry(sid); return; }
      if (this.destroyed || !this.store.get(sid)) return;
      if (active || !this.queueReady(sid)) { this.scheduleQueueRetry(sid); return; }
      const q = [...(this.pendingMessages.get(sid) || [])];
      const message = q.shift();
      if (!message) return;
      this.clearQueueRetry(sid);
      this.setQueue(sid, q);
      void this.sendMessage(sid, message, { fromQueue: true });
    } finally {
      this.flushingQueues.delete(sid);
    }
  }

  private scheduleQueueRetry(sid: string): void {
    if (this.destroyed || this.queueRetryTimers.has(sid)) return;
    const attempt = this.queueRetryAttempts.get(sid) || 0;
    this.queueRetryAttempts.set(sid, attempt + 1);
    const timer = setTimeout(() => {
      this.queueRetryTimers.delete(sid);
      void this.flushPendingMessages(sid);
    }, Math.min(15000, 1500 * 2 ** attempt));
    this.queueRetryTimers.set(sid, timer);
  }

  private clearQueueRetry(sid: string): void {
    const timer = this.queueRetryTimers.get(sid);
    if (timer) clearTimeout(timer);
    this.queueRetryTimers.delete(sid);
    this.queueRetryAttempts.delete(sid);
  }

  /** On Stop, queued prompts are handed back to the composer instead of being sent. */
  private returnQueueToDraft(sid: string): void {
    const q = this.pendingMessages.get(sid);
    if (!q?.length) return;
    this.clearQueueRetry(sid);
    this.setQueue(sid, []);
    for (const message of [...q].reverse()) this.restoreDraft(sid, message);
    this.ui.toast(q.length === 1 ? 'Your queued message was moved back to the message box.'
      : `Your ${q.length} queued messages were moved back to the message box.`);
  }

  cancelPendingMessage(sid: string, index: number): void {
    const q = [...(this.pendingMessages.get(sid) || [])];
    if (index < 0 || index >= q.length) return;
    q.splice(index, 1);
    if (!q.length) this.clearQueueRetry(sid);
    this.setQueue(sid, q);
  }
  private syncPendingMessagesView(): void {
    this.pendingMessagesForCurrent = this.currentSessionId ? [...(this.pendingMessages.get(this.currentSessionId) || [])] : [];
  }
  private updateComposerState(): void {
    const sid = this.currentSessionId;
    const uploading = !!sid && this.isUploading(sid);
    const running = this.runBlocksSession(sid);
    const queued = !!(sid && this.pendingMessages.get(sid)?.length);
    this.composerBusy = uploading || running;
    this.composerQueued = queued;
    this.composerTitle = running ? 'Queue \u2014 sends automatically when the agent finishes'
      : uploading ? 'Queue \u2014 sends automatically once the sources finish uploading'
        : queued ? 'Queue \u2014 sends after the messages ahead of it'
          : 'Send';
    this.composerPlaceholder = running
      ? 'Agent is working\u2026 type a message to queue it'
      : uploading
        ? 'Uploading sources\u2026 your message will send once they finish'
        : 'Ask a question or describe the deck you want';
    this.syncRunView();
  }

  private applyDoneToStore(sid: string, evt: StreamEvent, turn?: LiveTurn | null): string {
    const outcome = terminalFromDone(evt);
    const result = outcome.status === 'COMPLETED' ? evt.data?.result : null;
    const hasDeck = !!(result && result.deck_updated);
    const status: Session['status'] = outcome.status === 'FAILED' ? 'failed'
      : outcome.status === 'CANCELLED' ? 'stopped'
        : hasDeck ? 'done' : 'answered';
    const patch: Partial<Session> = { status };
    if (outcome.status === 'FAILED') patch.failureConfirmed = true;
    if (result?.deck_version != null) patch.deckVersion = result.deck_version;
    if (result?.verdict != null) patch.verdict = result.verdict;
    this.store.update(sid, patch);
    if (hasDeck) this.resetAutopilotReplies(sid);
    // Resolve how long this build took. The backend's own `time_taken_minutes` is authoritative; the
    // browser timer (survives logout/login via IndexedDB) is only a fallback for older backends.
    const version = (hasDeck && result?.deck_version != null) ? Number(result.deck_version) : null;
    const serverMs = resultDurationMs(result);
    let durationMs: number | null = null;
    if (version != null) {
      const memStart = this.pendingStarts.get(sid);
      if (serverMs != null) {
        this.pendingStarts.delete(sid);
        durationMs = serverMs;
        this.recordTiming(sid, version, serverMs);
        const parsedEnd = Date.parse(result?.ended_at ?? '');
        const endedAt = Number.isFinite(parsedEnd) ? parsedEnd : Date.now();
        void this.deckTimer.finalizeWith(sid, version, this.store.get(sid)?.title || 'Untitled project', this.userName, endedAt - serverMs, endedAt);
      } else if (memStart != null) {
        this.pendingStarts.delete(sid);
        durationMs = Math.max(0, Date.now() - memStart);
        this.recordTiming(sid, version, durationMs);
        void this.deckTimer.finalizeWith(sid, version, this.store.get(sid)?.title || 'Untitled project', this.userName, memStart);
      } else {
        // Resumed / reopened after the start was lost from memory — recover it from IndexedDB.
        void this.deckTimer.finalizePending(sid, version).then((rec) => {
          if (!this.destroyed && rec?.durationMs != null) this.recordTiming(sid, version, rec.durationMs);
        });
      }
    } else {
      // No deck this turn (questions / answer / failure) — drop the stale start.
      this.pendingStarts.delete(sid);
      void this.deckTimer.discardPending(sid);
    }
    // Persist the streamed "thought process" only when the turn actually built something —
    // skip pure Q&A turns (agent asked questions / answered without creating a deck) and stopped runs.
    const doneTurn = turn || this.liveTurns.get(sid);
    if (outcome.status !== 'CANCELLED' && doneTurn && doneTurn.events.length) {
      const events = doneTurn.events.slice();
      if (!events.some((e) => e.type === 'done')) events.push(evt);
      if (this.thoughtIsMeaningful(events)) {
        this.store.addThought(sid, events, 'Thought process', durationMs);
        if (sid === this.currentSessionId) this.syncMessagesView();
      }
    }
    if (this.liveTurns.has(sid)) {
      this.liveTurns.delete(sid);
      if (sid === this.currentSessionId) this.syncLiveTurnView();
    }
    if (outcome.status !== 'COMPLETED') this.markLastUserMessage(sid, outcome.status);
    if (result?.reply) {
      this.store.addMessage(sid, 'assistant', result.reply, result, { run_id: evt.run_id ?? null });
      if (sid === this.currentSessionId) {
        this.syncMessagesView();
        this.scheduleMessagesScroll();
      }
    } else if (outcome.status === 'FAILED') {
      this.store.addMessage(sid, 'assistant', `⚠ ${outcome.error}`);
      if (sid === this.currentSessionId) {
        this.syncMessagesView();
        this.scheduleMessagesScroll();
      }
    }
    if (sid === this.currentSessionId) { this.syncCurrentSessionPill(); this.updateComposerState(); }
    this.refreshSessionListView();
    if (hasDeck && sid === this.currentSessionId) {
      this.loadStudioOutputs(sid);
      this.notifyPresentation({ type: 'deck-ready' });
    }
    return status;
  }

  private refreshSessionListView(): void { this.sessions = this.store.all(); }

  /** Time taken to build a deck version of the open project, or null when unknown. */
  deckDurationFor(sid: string | null | undefined, version: number | null | undefined): number | null {
    if (!sid || sid !== this.currentSessionId || version == null) return null;
    return this.deckTimings.get(Number(version)) ?? null;
  }

  /** Cache a finalized build duration and stamp any matching thought once it belongs to the open session. */
  private recordTiming(sid: string, version: number, durationMs: number): void {
    if (sid !== this.currentSessionId) return;
    this.deckTimings.set(version, durationMs);
    this.restampThoughts(sid);
  }

  /** Apply cached durations to persisted thought groups that don't yet carry one. */
  private restampThoughts(sid: string): void {
    const session = this.store.get(sid);
    if (!session) return;
    let changed = false;
    for (const m of session.messages) {
      if (m.role !== 'thought' || m.durationMs != null) continue;
      const version = this.deckVersionOfThought(m);
      if (version == null) continue;
      const durationMs = this.deckTimings.get(version);
      if (durationMs == null) continue;
      m.durationMs = durationMs;
      changed = true;
    }
    if (changed) {
      this.store.update(sid, { messages: session.messages });
      if (sid === this.currentSessionId) this.syncMessagesView();
    }
  }

  /** The deck version a finished thought group produced, read from its embedded done event. */
  private deckVersionOfThought(m: ChatMessage): number | null {
    for (const e of (m.events || [])) {
      const v = (e as any)?.data?.result?.deck_version;
      if (v != null) return Number(v);
    }
    return null;
  }

  private syncCurrentSessionPill(): void {
    const sid = this.currentSessionId;
    const s = sid ? this.store.get(sid) : null;
    this.currentSessionTitle = s ? s.title : '';
    const t = sid ? this.runs.get(sid) : undefined;
    const live = t ? this.livePill(t) : null;
    if (live) { this.statusPillText = live; return; }
    if (!s || s.status === 'new') { this.statusPillText = ''; return; }
    this.statusPillText = s.status;
  }
  private syncMessagesView(): void {
    if (!this.currentSessionId) { this.messagesForCurrent = []; this.refreshQuestionView(); return; }
    const s = this.store.get(this.currentSessionId);
    this.messagesForCurrent = s ? [...s.messages] : [];
    this.refreshQuestionView();
  }
  private syncLiveTurnView(): void {
    this.liveTurn = this.currentSessionId ? this.liveTurns.get(this.currentSessionId) || null : null;
    this.syncResumePreview();
    this.refreshQuestionView();
  }

  /** True while the open project is building but its live turn has not re-attached yet. */
  get resumingRun(): boolean {
    const id = this.currentSessionId;
    return !!id && !this.liveTurn && this.store.get(id)?.status === 'running';
  }

  private syncResumePreview(): void {
    const id = this.currentSessionId;
    const cached = id ? this.runningTimeline.get(id) : null;
    if (!id || this.liveTurn || !cached?.events.length) { this.resumePreview = null; return; }
    if (this.resumePreview?.events === cached.events) return;
    const flow = newFlowState();
    for (const e of cached.events) updateFlowState(flow, e);
    this.resumePreview = { events: cached.events, title: 'Thought process', collapsed: false, flow, resuming: true };
  }

  /** Folds the persisted events of the tracked run into its live turn so the stream only has to send what's new. */
  private seedFromTimeline(t: RunTracker): void {
    const cached = this.runningTimeline.get(t.sid);
    if (!t.turn || t.state.terminal || !cached || cached.runId !== t.state.activeRunId) return;
    for (const evt of cached.events) {
      if (typeof evt.event_index === 'number' && evt.event_index > t.state.lastEventIndex) this.onRunEvent(t, evt);
    }
  }

  /** Clears the resuming state once the live turn has caught up with the run's events. */
  private settleResume(t: RunTracker): void {
    const turn = t.turn;
    if (!turn?.resuming || t.state.lastEventIndex < (turn.resumeTo || 0)) return;
    turn.resuming = false;
  }

  // ---- Structured questionnaire + plan approval ----

  /**
   * A server snapshot can predate the turn this tab just sent (the backend records it only once the
   * run starts). While that turn is in flight, keep its local bubble so the chat never loses it.
   */
  private keepInFlightTurn(sid: string, messages: ChatMessage[]): ChatMessage[] {
    const t = this.runs.get(sid);
    if (!t || !t.bubble || !isRunBlocking(t.state)) return messages;
    const runId = t.state.activeRunId;
    if (runId && messages.some((m) => m.role === 'user' && m.run_id === runId)) return messages;
    let lastReply = -1;
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === 'assistant' || messages[i].role === 'system') { lastReply = i; break; }
    }
    const sent = [t.bubble, t.message].filter((x): x is string => !!x).map((x) => x.trim());
    if (messages.slice(lastReply + 1).some((m) => m.role === 'user' && sent.includes((m.content || '').trim()))) return messages;
    const local = (this.store.get(sid)?.messages || []).filter((m) => m.role === 'user' && m.id == null).pop();
    return [...messages, {
      id: null, session_id: sid, role: 'user', content: t.bubble, result: null,
      created_at: local?.created_at ?? new Date().toISOString(), run_id: runId,
    }];
  }
  private messageKey(m: ChatMessage): string {
    return m.run_id ? `run:${m.run_id}` : m.id != null ? `id:${m.id}` : `at:${m.created_at || ''}`;
  }

  /** Reply text to render: without the numbered question list when a form or recap replaces it. */
  displayContentFor(m: ChatMessage): string {
    return this.assistantDisplayText.get(m) ?? (m.content || '');
  }

  private formFor(sid: string, questions: ClarifyingQuestion[]): QuestionFormState {
    let form = this.questionForms.get(sid);
    if (!form || form.signature !== questionsSignature(questions)) {
      form = newQuestionForm(questions);
      this.questionForms.set(sid, form);
    }
    return form;
  }

  private releaseQuestionForm(sid: string, error: string | null): void {
    const form = this.questionForms.get(sid);
    if (!form) return;
    form.submitting = false;
    if (error !== null) form.error = error;
  }

  /**
   * Resolve which message hosts the live questionnaire (the latest reply still awaiting answers,
   * or the running turn's `questions` event), which ones show a read-only recap, and where the
   * "Approve plan" control goes. Runs on every message/live-turn view sync.
   */
  private refreshQuestionView(): void {
    const sid = this.currentSessionId;
    const msgs = this.messagesForCurrent;
    this.activeQuestionMessage = null;
    this.approveHostMessage = null;
    this.questionFormLive = false;
    this.questionSummaries = new Map();
    this.assistantDisplayText = new Map();
    this.questionForm = null;
    if (!sid) return;
    // A stopped/failed user turn recorded nothing usable, so it does not count as an answer.
    const isAnswer = (m: ChatMessage) => m.role === 'user' && m.run_status !== 'CANCELLED' && m.run_status !== 'FAILED';
    let lastAssistant = -1;
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role === 'assistant' || msgs[i].role === 'system') { lastAssistant = i; break; }
    }
    const answeredAfter = (i: number) => msgs.slice(i + 1).some(isAnswer);
    // Once a turn is in flight the questions are no longer awaiting input, even before its bubble lands.
    const inFlight = this.runBlocksSession(sid);
    this.questionViewInFlight = inFlight;
    let activeQuestions: ClarifyingQuestion[] = [];
    msgs.forEach((m, i) => {
      const questions = questionsOf(m);
      if (!questions.length) return;
      if (i === lastAssistant && !answeredAfter(i) && !inFlight) {
        this.activeQuestionMessage = m;
        activeQuestions = questions;
      } else {
        const next = msgs.slice(i + 1).find(isAnswer);
        const answers = this.submittedAnswers.get(`${sid}|${this.messageKey(m)}`) || next?.result?.answers;
        const summary = answers && typeof answers === 'object' ? summarizeAnswers(answers, questions) : null;
        // Answered by typing in chat: keep the full numbered list so the question context stays readable.
        if (!summary) return;
        this.questionSummaries.set(m, summary);
      }
      this.assistantDisplayText.set(m, stripQuestionList(m.content) ?? 'A few quick questions before I plan your deck:');
    });
    const liveQuestions = this.liveTurn?.questions;
    if (liveQuestions?.length) {
      this.questionForm = this.formFor(sid, liveQuestions);
      this.questionFormLive = true;
    } else if (this.activeQuestionMessage) {
      this.questionForm = this.formFor(sid, activeQuestions);
    }
    if (lastAssistant !== -1 && !this.activeQuestionMessage && !this.liveTurn && !answeredAfter(lastAssistant)) {
      const r = msgs[lastAssistant].result;
      if (r?.awaiting_approval === true && !r?.awaiting_answers) this.approveHostMessage = msgs[lastAssistant];
    }
  }

  /** Submit the questionnaire as structured `answers` on `/invoke` (no typed message needed). */
  submitQuestionAnswers(): void {
    const sid = this.currentSessionId;
    const form = this.questionForm;
    const host = this.activeQuestionMessage;
    if (!sid || !form || !host || this.questionFormLive || form.submitting || this.isSessionBusy(sid)) return;
    const missing = missingRequired(form);
    if (missing.length) {
      form.error = missing.length === 1 ? `Please answer: ${missing[0].text}` : `Please answer the ${missing.length} required questions.`;
      return;
    }
    const { message, answers } = buildSubmission(form);
    if (!message && !Object.keys(answers).length) { form.error = 'Please answer at least one question.'; return; }
    const picked: QuestionAnswers = {};
    for (const q of form.questions) {
      const v = answerFor(form, q);
      if (v) picked[q.id] = v;
    }
    this.submittedAnswers.set(`${sid}|${this.messageKey(host)}`, picked);
    form.error = '';
    form.submitting = true;
    this.resetAutopilotReplies(sid);
    void this.sendMessage(sid, message, { answers });
    this.updateComposerState();
  }

  /** The "Approve plan" button sends the backend's fixed approve command. */
  approvePlan(): void {
    const sid = this.currentSessionId;
    if (!sid || !this.approveHostMessage || this.isSessionBusy(sid) || this.hasQueuedMessage(sid)) return;
    this.resetAutopilotReplies(sid);
    void this.sendMessage(sid, APPROVE_COMMAND, { restorable: false });
    this.updateComposerState();
  }

  // ---- Autopilot: auto-answer questions and auto-approve plans for opted-in projects ----

  private get autopilotMap(): Record<string, AutopilotState> {
    if (!this.autopilotStates) this.autopilotStates = this.readAutopilotStorage();
    return this.autopilotStates;
  }

  private readAutopilotStorage(): Record<string, AutopilotState> {
    try {
      const parsed = JSON.parse(localStorage.getItem(AUTOPILOT_STORAGE_KEY) || '{}');
      return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
    } catch { return {}; }
  }

  /** Re-reads storage before writing so another tab's changes to other projects are never clobbered. */
  private setAutopilotEntry(sid: string, state: AutopilotState | null): void {
    const map = this.readAutopilotStorage();
    if (state) map[sid] = state; else delete map[sid];
    this.autopilotStates = map;
    try { localStorage.setItem(AUTOPILOT_STORAGE_KEY, JSON.stringify(map)); } catch { /* storage unavailable */ }
  }

  isAutopilotOn(sid: string | null): boolean {
    return !!sid && this.autopilotMap[sid]?.on === true;
  }

  get autopilotForCurrent(): boolean { return this.isAutopilotOn(this.currentSessionId); }
  get autopilotHaltedForCurrent(): boolean { return !!this.currentSessionId && this.autopilotHalted.has(this.currentSessionId); }

  toggleAutopilot(): void {
    const sid = this.currentSessionId;
    if (!sid) return;
    const on = !this.isAutopilotOn(sid);
    this.setAutopilotEntry(sid, { on, replies: 0 });
    this.autopilotHalted.delete(sid);
    this.autopilotHandled.delete(sid);
    this.clearAutopilotRetry(sid);
    this.ui.toast(on ? 'Autopilot on: questions and plans will be handled automatically.' : 'Autopilot off.', 'info');
    if (on) void this.runAutopilot(sid);
  }

  dismissAutopilotNotice(): void {
    if (this.currentSessionId) this.autopilotHalted.delete(this.currentSessionId);
  }

  private resetAutopilotReplies(sid: string): void {
    const state = this.readAutopilotStorage()[sid];
    if (!state || !state.replies) return;
    this.setAutopilotEntry(sid, { ...state, replies: 0 });
  }

  private forgetAutopilot(sid: string): void {
    this.autopilotHalted.delete(sid);
    this.autopilotHandled.delete(sid);
    this.clearAutopilotRetry(sid);
    if (sid in this.readAutopilotStorage()) this.setAutopilotEntry(sid, null);
  }

  /** Too many automatic replies without a deck: switch off and hand the project back to the user. */
  private haltAutopilot(sid: string): void {
    this.setAutopilotEntry(sid, { on: false, replies: 0 });
    this.clearAutopilotRetry(sid);
    this.autopilotHalted.add(sid);
    const title = this.store.get(sid)?.title || 'this project';
    this.ui.toast(`Autopilot stopped for "${title}" — the assistant keeps asking. Please review and reply yourself.`, 'error');
  }

  private resumeAutopilotAll(): void {
    for (const s of this.store.all()) if (this.isAutopilotOn(s.id)) void this.runAutopilot(s.id);
  }

  /** A run or upload just ended: check now with a fresh retry budget. */
  private kickAutopilot(sid: string): void {
    this.autopilotRetryAttempts.delete(sid);
    void this.runAutopilot(sid);
  }

  /** Background projects are not reconciled once idle, so a failed/inconclusive check retries itself. */
  private scheduleAutopilotRetry(sid: string): void {
    if (this.destroyed || this.autopilotRetryTimers.has(sid)) return;
    const attempt = this.autopilotRetryAttempts.get(sid) || 0;
    if (attempt >= AUTOPILOT_MAX_RETRIES) return;
    this.autopilotRetryAttempts.set(sid, attempt + 1);
    const timer = setTimeout(() => {
      this.autopilotRetryTimers.delete(sid);
      void this.runAutopilot(sid);
    }, Math.min(60000, 1500 * 2 ** attempt));
    this.autopilotRetryTimers.set(sid, timer);
  }

  private clearAutopilotRetry(sid: string): void {
    const timer = this.autopilotRetryTimers.get(sid);
    if (timer) clearTimeout(timer);
    this.autopilotRetryTimers.delete(sid);
    this.autopilotRetryAttempts.delete(sid);
  }

  /** Reconcile polls are read-only and back off once a run starts, so they don't block autopilot. */
  private autopilotIdle(sid: string): boolean {
    const t = this.runs.get(sid);
    return !this.isSessionBusy(sid) && !this.hasQueuedMessage(sid) && !(t && this.isTrackerBusy(t)) && !readPendingRequest(sid);
  }

  /**
   * If the project's latest server message is waiting on the user, reply on their behalf: structured
   * questions get autopilot answers, plain questions a generic reply, a plan gets approved. Safe to
   * call often — it is a no-op unless autopilot is on, the project is idle and the message is new.
   */
  private async runAutopilot(sid: string): Promise<void> {
    if (this.destroyed || !this.api.loggedIn || !this.isAutopilotOn(sid) || this.autopilotChecking.has(sid)) return;
    if (!this.store.get(sid)) return;
    // Real runs/uploads re-trigger autopilot when they end; only transient busy states need a retry.
    if (!this.autopilotIdle(sid)) { if (!this.isSessionBusy(sid)) this.scheduleAutopilotRetry(sid); return; }
    this.autopilotChecking.add(sid);
    let retry = false;
    try {
      const [cur, data] = await Promise.all([this.api.getCurrentRun(sid), this.api.getMessages(sid, 6)]);
      if (cur?.active?.run_id) { retry = true; return; }
      if (this.destroyed || !this.store.get(sid) || !this.isAutopilotOn(sid)) return;
      if (!this.autopilotIdle(sid)) { retry = true; return; }
      const msgs: ChatMessage[] = (data?.messages || [])
        .filter((m: any) => m.role === 'user' || m.role === 'assistant' || m.role === 'system');
      const last = msgs[msgs.length - 1];
      if (!last || last.role === 'user') return;
      const r = last.result;
      const awaitingAnswers = !!r?.awaiting_answers;
      if (!awaitingAnswers && r?.awaiting_approval !== true) return;
      const key = String(last.id ?? last.run_id ?? last.created_at ?? '');
      if (this.autopilotHandled.get(sid) === key) return;
      const state = this.readAutopilotStorage()[sid] || { on: true, replies: 0 };
      if (state.replies >= AUTOPILOT_MAX_REPLIES) { this.haltAutopilot(sid); return; }
      this.autopilotHandled.set(sid, key);
      this.setAutopilotEntry(sid, { ...state, replies: state.replies + 1 });
      if (awaitingAnswers) {
        const questions = questionsOf(last);
        if (questions.length) {
          const { message, answers, picked } = autopilotSubmission(questions);
          if (last.run_id) this.submittedAnswers.set(`${sid}|run:${last.run_id}`, picked);
          if (last.id != null) this.submittedAnswers.set(`${sid}|id:${last.id}`, picked);
          void this.sendMessage(sid, message, { answers, restorable: false });
        } else {
          void this.sendMessage(sid, AUTOPILOT_PLAIN_REPLY, { restorable: false });
        }
      } else {
        void this.sendMessage(sid, APPROVE_COMMAND, { restorable: false });
      }
      if (sid === this.currentSessionId) this.updateComposerState();
    } catch { retry = true; }
    finally {
      this.autopilotChecking.delete(sid);
      if (retry) this.scheduleAutopilotRetry(sid);
      else this.autopilotRetryAttempts.delete(sid);
    }
  }

  /**
   * Rebuild the chat list with a "thought process" board before each deck turn.
   * Prefers a client-streamed thought (live continuity); otherwise synthesizes one
   * from server data (deck versions + reply) so the board survives reload / logout and
   * also appears for projects generated before thoughts were persisted.
   */
  private buildMessagesWithThoughts(localMsgs: ChatMessage[], fetched: ChatMessage[], versions: any[]): ChatMessage[] {
    const clientThoughtByOrdinal = new Map<number, ChatMessage>();
    let ordinal = 0;
    for (const m of localMsgs) {
      if (m.role === 'thought') clientThoughtByOrdinal.set(ordinal, m);
      else if (m.role === 'assistant' || m.role === 'system') ordinal++;
    }
    const langsByVersion = new Map<number, string[]>();
    for (const v of (versions || [])) {
      if (v && v.deck_version != null) langsByVersion.set(v.deck_version, Object.keys(v.files || {}));
    }
    const latestLangs = versions?.length ? Object.keys(versions[versions.length - 1].files || {}) : [];
    const merged: ChatMessage[] = [];
    let ao = 0;
    for (const fm of fetched) {
      if (fm.role === 'assistant' || fm.role === 'system') {
        const client = clientThoughtByOrdinal.get(ao);
        if (client && this.thoughtIsMeaningful(client.events || [])) merged.push(client);
        else {
          const synth = this.synthesizeThought(fm, langsByVersion, latestLangs);
          if (synth) merged.push(synth);
        }
        ao++;
      }
      merged.push(fm);
    }
    const trailing = clientThoughtByOrdinal.get(ao);
    if (trailing && this.thoughtIsMeaningful(trailing.events || [])) merged.push(trailing);
    // Stamp any known build durations onto their thought boards (recovered from IndexedDB in hydrate).
    for (const m of merged) {
      if (m.role !== 'thought' || m.durationMs != null) continue;
      const v = this.deckVersionOfThought(m);
      if (v != null && this.deckTimings.has(v)) m.durationMs = this.deckTimings.get(v)!;
    }
    return merged;
  }

  /** A thought is worth showing only when the turn built something concrete (slides, languages or a deck). */
  private thoughtIsMeaningful(events: StreamEvent[]): boolean {
    if (!events?.length) return false;
    const flow = newFlowState();
    for (const e of events) updateFlowState(flow, e);
    if (flow.slides.size > 0 || flow.langs.size > 0) return true;
    return events.some((e) => e.type === 'done' && e.data?.result?.deck_updated);
  }

  /** Reconstruct a final progress board (steps/slides/languages) for a completed deck turn from its persisted result. */
  private synthesizeThought(m: ChatMessage, langsByVersion: Map<number, string[]>, fallbackLangs: string[]): ChatMessage | null {
    const result = m.result;
    if (!result || !result.deck_updated) return null;
    const reply = `${result.reply || m.content || ''}`;
    const sm = reply.match(/(\d+)\s*slide/i);
    const slideTotal = sm ? Number.parseInt(sm[1], 10) : 0;
    const dv = result.deck_version;
    const langs = (dv != null && langsByVersion.get(dv)) || fallbackLangs || [];
    if (!slideTotal && !langs.length) return null;
    const events: StreamEvent[] = [];
    for (let i = 1; i <= slideTotal; i++) {
      events.push({ type: 'agent_step', agent: 'slide', message: `Slide ${i}/${slideTotal} complete`, data: { slide_number: i, total: slideTotal, phase: 'done' } });
    }
    for (const lang of langs) {
      events.push({ type: 'agent_step', agent: 'deck', message: `Built ${lang} deck` });
    }
    events.push({ type: 'done', agent: 'supervisor', message: 'Deck ready', data: { result } });
    return {
      id: null,
      session_id: m.session_id,
      role: 'thought',
      content: '',
      events,
      title: 'Thought process',
      created_at: m.created_at ?? null,
      durationMs: resultDurationMs(result),
    };
  }

  async renameProject(id: string, currentTitle: string): Promise<void> {
    const title = (await this.ui.askText('Rename project:', currentTitle))?.trim();
    if (!title || title === currentTitle) return;
    try {
      const res = await this.api.renameSession(id, title);
      this.store.update(id, { title: res?.title || title });
      if (this.currentSessionId === id) this.currentSessionTitle = res?.title || title;
      this.refreshSessionListView();
      this.ui.toast('Project renamed', 'success');
    } catch (err: any) {
      this.ui.toast(`Rename failed: ${err.message}`, 'error');
    }
  }

  async deleteProject(id: string, title: string): Promise<boolean> {
    if (this.deletingSessionIds.has(id)) return false;
    const ok = await this.ui.confirmDialog(`Delete project "${title}"? This also deletes its files, decks and chat history on the server.`);
    if (!ok) return false;
    this.deletingSessionIds.add(id);
    this.refreshSessionListView();
    try {
      await this.api.deleteSession(id);
    } catch (err: any) {
      this.deletingSessionIds.delete(id);
      this.refreshSessionListView();
      this.ui.toast(`Server delete failed: ${err.message}`, 'error');
      return false;
    }
    this.deletingSessionIds.delete(id);
    this.disposeTracker(id);
    clearPendingRequest(id);
    this.clearQueueRetry(id);
    this.setQueue(id, []);
    this.store.remove(id);
    this.forgetAutopilot(id);
    this.pendingStarts.delete(id);
    void this.deckTimer.clearForSession(id);
    if (this.currentSessionId === id) {
      const remaining = this.store.all();
      if (remaining.length) {
        this.selectSession(remaining[0].id);
      } else {
        this.ensureSession();
      }
    }
    this.refreshSessionListView();
    this.ui.toast('Project deleted', 'success');
    return true;
  }

  selectSession(id: string | null): void {
    if (id === this.currentSessionId) { this.refreshSessionListView(); return; }
    const leaving = this.currentSessionId ? this.runs.get(this.currentSessionId) : undefined;
    if (leaving?.state.terminal?.status === 'CANCELLED') leaving.noticeDismissed = true;
    this.currentSessionId = id;
    // The composer is shared across projects; don't leak the previous project's draft or restored message.
    if (this.chatInputValue) {
      this.chatInputValue = '';
      this.notifyPresentation({ type: 'resize-composer', focus: false });
    }
    try { if (id) localStorage.setItem('se_current', id); else localStorage.removeItem('se_current'); } catch { /* ignore */ }
    const s = id ? this.store.get(id) : null;
    this.refreshSessionListView();
    this.rebuildStudioInputs();
    this.ingestLogForCurrent = id ? [...(this.ingestLogs.get(id) || [])] : [];
    this.messagesLoading = !!id;
    this.messagesErrorText = '';
    this.hasMoreOlderMessages = false;
    this.loadingOlderMessages = false;
    this.messagesLimitForCurrent = this.INITIAL_MESSAGES_LIMIT;
    this.messagesForCurrent = id ? [...(s?.messages || [])] : [];
    this.currentSessionTitle = s ? s.title : '';
    this.syncCurrentSessionPill();
    this.syncLiveTurnView();
    this.studioOutputs = null;
    this.studioOutputsLoading = false;
    this.studioOutputsSid = null;
    this.deckTimings.clear();
    // Requests still loading the project we're leaving are no longer useful.
    this.selectionAbort?.abort();
    this.selectionAbort = null;
    if (id) {
      const ctrl = new AbortController();
      this.selectionAbort = ctrl;
      const opts: RequestOpts = { signal: ctrl.signal, priority: 'high' };
      const endForeground = this.beginForeground();
      // One /versions request feeds both the Outputs panel and the progress-board rebuild.
      const versions = this.api.getJson(`/decks/${id}/versions`, opts);
      versions.catch(() => { /* handled by each consumer */ });
      void Promise.allSettled([
        this.loadStudioOutputs(id, { opts, versions }),
        this.hydrateSession(id, this.INITIAL_MESSAGES_LIMIT, false, { opts, versions }),
        this.reconcileRun(id, true),
      ]).finally(() => {
        endForeground();
        if (this.selectionAbort === ctrl) this.selectionAbort = null;
      });
    }
    this.syncPendingMessagesView();
    this.updateComposerState();
    this.scheduleMessagesScroll();
  }

  private async refreshDocuments(id: string, opts: RequestOpts = {}): Promise<any[]> {
    const data = await this.api.getDocuments(id, true, opts);
    const documents = (data.documents || []).map((d: any) => ({
      session_id: d.session_id || id,
      doc_id: d.doc_id,
      version: d.version,
      filename: d.filename,
      mime_type: d.mime_type || '',
      sha256: d.sha256 || '',
      storage_path: d.storage_path || '',
      is_latest: d.is_latest !== false,
      status: d.status,
      error: d.error ?? null,
      uploaded_at: d.uploaded_at || null,
      chunks: d.chunks,
    }));
    if (this.destroyed) return documents;
    this.store.setDocs(id, documents);
    if (id === this.currentSessionId) this.rebuildStudioInputs();
    return documents;
  }

  /**
   * Resolves false only when the fetch failed, so callers can report a failed older-page load.
   * `fg` carries a project-open's cancel signal / priority and an already-started /versions request.
   */
  private async hydrateSession(
    id: string, limit?: number, isLoadOlder = false, fg: { opts?: RequestOpts; versions?: Promise<any> } = {},
  ): Promise<boolean> {
    const s = this.store.get(id);
    if (!s) return true;
    const opts = fg.opts || {};
    // Only the newest hydrate for a session may apply; an older response would roll back newer local turns.
    const seq = (this.hydrateSeq.get(id) || 0) + 1;
    this.hydrateSeq.set(id, seq);
    const isStale = () => this.hydrateSeq.get(id) !== seq || !!opts.signal?.aborted;
    const deriveStatus = s.status === 'new';
    let fetchedMsgs: any[] | null = null;
    let timeline: TimelineItem[] | null = null;
    let versions: any[] | null = null;
    let timings: Map<number, DeckTimerRecord> | null = null;
    let returnedChatCount = 0;
    const tasks: Promise<any>[] = [];
    tasks.push(this.api.getMessages(id, limit, opts).then((data: any) => {
      returnedChatCount = (data.messages || []).length;
      fetchedMsgs = (data.messages || [])
        .filter((m: any) => m.role === 'user' || m.role === 'assistant' || m.role === 'system')
        .map((m: any) => ({
          id: m.id ?? null,
          session_id: m.session_id || id,
          role: m.role,
          content: m.content || '',
          result: m.result ?? null,
          created_at: m.created_at ?? null,
        }));
      // Persisted agent-activity timeline (newer backend). Absent on older/staggered deploys.
      timeline = Array.isArray(data.timeline) ? (data.timeline as TimelineItem[]) : null;
    }));
    tasks.push(this.refreshDocuments(id, opts));
    // Always fetch versions so we can reconstruct the final progress board (languages live here).
    tasks.push((fg.versions || this.api.getJson(`/decks/${id}/versions`, opts))
      .then((data: any) => { versions = data.versions || []; })
      .catch(() => { /* leave undetermined */ }));
    // Durable build timings (survive logout / login); empty for old or cache-cleared projects.
    tasks.push(this.deckTimer.listForSession(id).then((map) => { timings = map; }));
    try {
      await Promise.all(tasks);
    } catch (err: any) {
      if (this.destroyed || isStale()) return true;
      // An older-page fetch must never wipe what's already on screen; just stop and let the user retry.
      if (isLoadOlder) return false;
      if (err.status === 404) {
        this.store.update(id, { messages: [] });
        if (id === this.currentSessionId) {
          this.messagesLoading = false;
          this.messagesErrorText = 'Session not found.';
        }
        return false;
      }
      if (id === this.currentSessionId) {
        this.messagesLoading = false;
        this.messagesErrorText = 'Unable to load this session.';
      }
      return false;
    }
    if (this.destroyed || isStale()) return true;
    // Status may have changed while the fetch was in flight (e.g. a new message started a run).
    const curStatus = this.store.get(id)?.status ?? s.status;
    // Fold recovered timings into the current-session cache before rebuilding boards (keep any fresher in-memory value).
    if (id === this.currentSessionId && timings) {
      for (const [ver, rec] of timings) {
        if (rec?.durationMs != null && !this.deckTimings.has(ver)) this.deckTimings.set(ver, rec.durationMs);
      }
    }
    // Backend-reported build times (persisted on each deck turn's result) override the browser timer.
    if (id === this.currentSessionId) {
      const results: any[] = (fetchedMsgs || []).map((m: any) => m.result);
      for (const t of (timeline || [])) {
        if (t.kind === 'message') results.push(t.result);
        else if (t.type === 'done') results.push((t.data as any)?.result);
      }
      for (const r of results) {
        const ms = r?.deck_updated && r.deck_version != null ? resultDurationMs(r) : null;
        if (ms != null) this.deckTimings.set(Number(r.deck_version), ms);
      }
    }
    const patch: any = {};
    // The in-progress run is rendered by the live stream, so keep it out of the persisted list.
    const runningRunId = findRunningRunId(timeline);
    const liveRunId = this.liveRunIdFor(id);
    // ...but its persisted events let the live turn (or a pre-attach preview) skip most of the replay.
    if (runningRunId && timeline) {
      const events = (timeline as TimelineItem[])
        .filter((item) => item.kind === 'workflow_event' && item.run_id === runningRunId)
        .map((item) => timelineEventToStreamEvent(item as WorkflowEventTimelineItem));
      this.runningTimeline.set(id, { runId: runningRunId, events });
    } else {
      this.runningTimeline.delete(id);
    }
    // Prefer the persisted agent-activity timeline (real logs). Fall back to synthesizing boards
    // from deck versions when an older backend omits `timeline`, preserving backward compatibility.
    patch.messages = this.keepInFlightTurn(id, (timeline && timeline.length)
      ? buildMessagesFromTimeline(timeline, id, liveRunId || runningRunId)
      : this.buildMessagesWithThoughts(this.store.get(id)?.messages || [], fetchedMsgs || [], versions || []));
    const latestResult = [...(fetchedMsgs || [])].reverse().find((m: any) => m.role === 'assistant' && m.result);
    if (latestResult?.result?.deck_version != null) patch.deckVersion = latestResult.result.deck_version;
    if (latestResult?.result?.verdict != null) patch.verdict = latestResult.result.verdict;
    if (curStatus !== 'running' && latestResult?.result?.deck_updated) patch.status = 'done';
    else if (curStatus !== 'running' && latestResult?.role === 'assistant') patch.status = 'answered';
    if (deriveStatus && curStatus === 'new') {
      if (latestResult?.result?.deck_updated || (versions && versions.length)) {
        patch.status = 'done';
        if (versions?.length && patch.deckVersion == null) patch.deckVersion = versions[versions.length - 1].deck_version;
      } else if ((fetchedMsgs || []).some((m: any) => m.role === 'assistant')) patch.status = 'answered';
    }
    if (runningRunId || this.runBlocksSession(id)) patch.status = 'running';
    else if (!liveRunId) {
      const lastRun = latestTimelineRunStatus(timeline);
      if (lastRun === 'CANCELLED') patch.status = 'stopped';
      else if (lastRun === 'FAILED') patch.status = 'failed';
    }
    if (Object.keys(patch).length) this.store.update(id, patch);
    this.sidebarStatusDeferred.delete(id);
    this.restampThoughts(id);
    this.refreshSessionListView();
    const tracked = this.runs.get(id);
    if (runningRunId && tracked?.turn && tracked.state.activeRunId === runningRunId) this.seedFromTimeline(tracked);
    if (runningRunId && !liveRunId && id === this.currentSessionId) void this.reconcileRun(id, true);
    if (id === this.currentSessionId) {
      this.syncResumePreview();
      this.messagesLoading = false;
      this.messagesErrorText = '';
      // More older messages likely exist when the server filled the whole requested page.
      this.hasMoreOlderMessages = limit != null && returnedChatCount >= limit;
      this.syncMessagesView();
      this.rebuildStudioInputs();
      this.syncCurrentSessionPill();
      this.updateComposerState();
      // Loading older history must preserve the reading position; only jump to newest on first load.
      if (!isLoadOlder) this.scheduleMessagesScroll();
    }
    return true;
  }

  ensureSession(): string {
    if (this.currentSessionId && this.store.get(this.currentSessionId)) return this.currentSessionId;
    const existingEmpty = this.store.all().find(
      (s) => (s.status === 'new' || !s.status)
        && s.title === 'New project'
        && (!s.messages || !s.messages.length)
        && (!s.docs || !s.docs.length)
    );
    if (existingEmpty) {
      this.selectSession(existingEmpty.id);
      return existingEmpty.id;
    }
    const s = this.store.create('New project');
    this.registerProject(s);
    this.refreshSessionListView();
    this.selectSession(s.id);
    return s.id;
  }
  private registerProject(session: any): void {
    this.api.registerSession(session.id, session.title).catch(() => { /* keep local-only if offline */ });
  }

  private pushIngestEvent(sid: string, evt: StreamEvent): void {
    const log = this.ingestLogs.get(sid) || [];
    log.push(evt);
    this.ingestLogs.set(sid, log);
    if (sid === this.currentSessionId) {
      this.ingestLogForCurrent = [...log];
      this.notifyPresentation({ type: 'scroll-ingest' });
    }
  }

  private async uploadFiles(sid: string, files: File[]): Promise<void> {
    if (this.destroyed) return;
    const pending = files.map((file) => {
      const pid = `pending-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
      this.pendingUploads.set(pid, { sessionId: sid, filename: file.name, status: 'uploading', pct: 0 } as any);
      return { pid, file };
    });
    const pendingIds = pending.map((p) => p.pid);
    if (sid === this.currentSessionId) this.rebuildStudioInputs();
    const controller = new AbortController();
    const waiting = new Set<string>();
    const finished = new Map<string, any>();
    let started = false;
    let resolveDone: any;
    const allDone = new Promise((r) => { resolveDone = r; });
    let ingestionTimer: ReturnType<typeof setTimeout> | undefined;
    const stop = () => { controller.abort(); resolveDone(); };
    this.streamCleanups.add(stop);
    const maybeDone = () => { if (started && waiting.size === 0) resolveDone(); };
    const applyTerminal = (data: any) => {
      if (this.destroyed || !data || !data.doc_id) return;
      const docs = (this.store.get(sid)?.docs || []).map((d: any) =>
        d.doc_id === data.doc_id
          ? { ...d, status: data.status || d.status, chunks: data.chunks ?? d.chunks, error: data.error ?? d.error }
          : d);
      this.store.setDocs(sid, docs);
      if (sid === this.currentSessionId) this.rebuildStudioInputs();
      waiting.delete(data.doc_id);
      maybeDone();
    };
    const trackDoc = (doc: any) => {
      if (this.destroyed) return;
      const docs = [...(this.store.get(sid)?.docs || []), doc];
      this.store.setDocs(sid, docs);
      if (sid === this.currentSessionId) this.rebuildStudioInputs();
      if (finished.has(doc.doc_id)) applyTerminal(finished.get(doc.doc_id));
      else waiting.add(doc.doc_id);
    };
    // Subscribe BEFORE uploading so no ingest event is missed.
    const onIngestEvent = (evt: any) => {
      if (this.destroyed || !evt.type || !evt.type.startsWith('ingest')) return;
      this.pushIngestEvent(sid, evt);
      if (evt.type === 'ingest_done' || evt.type === 'ingest_error') {
        const data = evt.type === 'ingest_error'
          ? { ...(evt.data || {}), status: evt.data?.status || 'failed', error: evt.data?.error || evt.message }
          : evt.data;
        if (data?.doc_id) finished.set(data.doc_id, data);
        applyTerminal(data);
      }
    };
    // The session stream can close (e.g. when a run finishes) or go silent; resubscribe and
    // catch up from the documents API so terminal ingestion is never missed.
    const streamDone = (async () => {
      while (!controller.signal.aborted && !this.destroyed) {
        await this.api.streamSession(sid, onIngestEvent, controller.signal).catch(() => {});
        if (controller.signal.aborted || this.destroyed) return;
        try {
          const docs = await this.refreshDocuments(sid);
          for (const d of docs) {
            if (d.doc_id && waiting.has(d.doc_id) && !isDocProcessing(d)) {
              applyTerminal({ doc_id: d.doc_id, status: d.status, chunks: d.chunks, error: d.error });
            }
          }
        } catch { /* retry on the next pass */ }
        if (controller.signal.aborted || this.destroyed) return;
        await new Promise<void>((resolve) => {
          const timer = setTimeout(resolve, 1000);
          controller.signal.addEventListener('abort', () => { clearTimeout(timer); resolve(); }, { once: true });
        });
      }
    })();
    try {
      let uploads: any[] | null = null;
      try {
        const reg = await this.api.registerDocuments(sid, files);
        if (this.destroyed) {
          // Registration may finish after the shell has been torn down.
          await Promise.allSettled((reg.uploads || []).map((upload: { doc_id: string; version: number }) =>
            this.api.abortUpload(sid, upload.doc_id, upload.version)));
          return;
        }
        uploads = reg.uploads || [];
      } catch (err: any) {
        if (this.destroyed) return;
        if (!isPresignUnavailable(err)) throw err;
        const result = await this.api.uploadDocuments(sid, files);
        if (this.destroyed) return;
        pendingIds.forEach((pid) => this.pendingUploads.delete(pid));
        (result.documents || []).forEach(trackDoc);
      }
      if (uploads) {
        await Promise.allSettled(pending.map((p, i) => {
          const upload = uploads![i]?.filename === p.file.name ? uploads![i] : uploads!.find((u: any) => u.filename === p.file.name);
          return this.uploadToBlobAndComplete(sid, p, upload, controller.signal, trackDoc);
        }));
      }
      if (this.destroyed) return;
      started = true;
      maybeDone();
      if (waiting.size) {
        await Promise.race([allDone, new Promise((r) => { ingestionTimer = setTimeout(r, 20 * 60 * 1000); })]);
      }
      if (this.destroyed) return;
      controller.abort();
      await streamDone;
      if (this.destroyed) return;
      await this.refreshDocuments(sid);
      pendingIds.forEach((pid) => {
        if (this.pendingUploads.get(pid)?.status !== 'failed') this.pendingUploads.delete(pid);
      });
      this.ingestLogs.delete(sid);
      if (sid === this.currentSessionId) {
        this.rebuildStudioInputs();
        this.ingestLogForCurrent = [];
      }
    } catch (err: any) {
      controller.abort();
      if (this.destroyed) return;
      this.refreshDocuments(sid).catch(() => {});
      for (const pid of pendingIds) {
        if (this.pendingUploads.get(pid)?.status === 'uploading') this.failPending(sid, pid, err.message);
      }
      if (sid === this.currentSessionId) this.rebuildStudioInputs();
      this.ui.toast(`Upload failed: ${err.message}`, 'error');
    } finally {
      if (ingestionTimer !== undefined) clearTimeout(ingestionTimer);
      this.streamCleanups.delete(stop);
      controller.abort();
    }
  }

  private async uploadToBlobAndComplete(
    sid: string,
    { pid, file }: { pid: string; file: File },
    upload: any,
    signal: AbortSignal,
    trackDoc: (doc: any) => void,
  ): Promise<void> {
    if (!upload?.upload_url) {
      if (upload?.doc_id) this.api.abortUpload(sid, upload.doc_id, upload.version).catch(() => {});
      this.failPending(sid, pid, 'no upload URL returned');
      this.ui.toast(`Upload failed for ${file.name}: no upload URL returned`, 'error');
      return;
    }
    try {
      await this.putToBlobWithRetry(upload.upload_url, file, (pct: number) => this.setUploadPct(sid, pid, pct), signal);
    } catch (err: any) {
      this.api.abortUpload(sid, upload.doc_id, upload.version).catch(() => {});
      if (err.name === 'AbortError') return;
      this.failPending(sid, pid, err.message);
      this.ui.toast(`Upload failed for ${file.name}: ${err.message}`, 'error');
      return;
    }
    if (this.destroyed) {
      // The blob landed, but completion/ingestion has not been requested yet.
      await this.api.abortUpload(sid, upload.doc_id, upload.version).catch(() => {});
      return;
    }
    try {
      const res = await this.api.completeDocuments(sid, [{ doc_id: upload.doc_id, version: upload.version }]);
      if (this.destroyed) return;
      const doc = (res.documents || [])[0]
        || { doc_id: upload.doc_id, version: upload.version, filename: file.name, status: 'uploaded' };
      this.pendingUploads.delete(pid);
      trackDoc(doc);
    } catch (err: any) {
      this.failPending(sid, pid, err.message);
      this.ui.toast(`Processing failed for ${file.name}: ${err.message}`, 'error');
    }
  }

  private async putToBlobWithRetry(url: string, file: File, onPct: (pct: number) => void, signal: AbortSignal, attempts = 2): Promise<void> {
    for (let attempt = 1; ; attempt++) {
      try {
        await this.api.uploadToBlob(url, file, onPct, signal);
        onPct(100);
        return;
      } catch (err: any) {
        if (err.name === 'AbortError' || attempt >= attempts) throw err;
        onPct(0);
      }
    }
  }
  private failPending(sid: string, pid: string, message: string): void {
    const p = this.pendingUploads.get(pid);
    if (!p) return;
    p.status = 'failed';
    p.error = message;
    p.pct = undefined;
    if (sid === this.currentSessionId) this.rebuildStudioInputs();
  }
  private setUploadPct(sid: string, pid: string, pct: number): void {
    const p = this.pendingUploads.get(pid);
    if (!p || p.status !== 'uploading') return;
    p.pct = pct;
    if (sid === this.currentSessionId) this.rebuildStudioInputs();
  }
  private rebuildStudioInputs(): void {
    const sid = this.currentSessionId;
    const docs = (sid && this.store.get(sid)?.docs) || [];
    const names = new Set(docs.map((d: any) => d.filename));
    const pending: any[] = [];
    for (const [pid, p] of this.pendingUploads) {
      if (p.sessionId === sid && !names.has(p.filename)) pending.push({ ...p, _pid: pid });
    }
    this.studioInputsRows = [...docs, ...pending];
  }

  isDownloadBusy(sid: string, version: number, language?: string): boolean {
    return this.downloadBusy.has(`${sid}|${version}|${language || ''}`);
  }

  isVersionDeleting(sid: string, version: number): boolean {
    return this.deletingVersions.has(`${sid}|${version}`);
  }

  isDocDeleting(d: SessionDoc): boolean {
    return !!d.doc_id && this.deletingDocs.has(`${d.session_id || this.currentSessionId}|${d.doc_id}`);
  }

  async deleteSourceDoc(d: SessionDoc): Promise<void> {
    const sid = d.session_id || this.currentSessionId;
    if (!sid || !d.doc_id) return;
    if (this.isGenerating(sid)) {
      this.ui.toast('Sources can be deleted once deck creation is complete.', 'info');
      return;
    }
    const key = `${sid}|${d.doc_id}`;
    if (this.deletingDocs.has(key)) return;
    const ok = await this.ui.confirmDialog(`Delete source "${d.filename}"? This permanently removes the file and all its versions.`);
    if (!ok) return;
    this.deletingDocs.add(key);
    try {
      await this.api.deleteDocument(sid, d.doc_id);
    } catch (e: any) {
      this.ui.toast(`Delete failed: ${e.message}`, 'error');
      return;
    } finally {
      this.deletingDocs.delete(key);
    }
    const cur = this.store.get(sid);
    if (cur) this.store.setDocs(sid, (cur.docs || []).filter((x: SessionDoc) => x.doc_id !== d.doc_id));
    if (sid === this.currentSessionId) this.rebuildStudioInputs();
    this.ui.toast(`Deleted ${d.filename}.`, 'success');
  }

  async deleteDeckVersion(v: StudioOutputRow): Promise<void> {
    const key = `${v._sid}|${v.deck_version}`;
    if (this.deletingVersions.has(key)) return;
    const ok = await this.ui.confirmDialog(`Delete version v${v.deck_version} of "${v.plan_title || 'Training deck'}"? This permanently deletes its generated files.`);
    if (!ok) return;
    this.deletingVersions.add(key);
    try {
      await this.api.deleteVersion(v._sid, v.deck_version);
    } catch (e: any) {
      this.ui.toast(`Delete failed: ${e.message}`, 'error');
      return;
    } finally {
      this.deletingVersions.delete(key);
    }
    this.deckFeedback.delete(key);
    if (this.currentSessionId === v._sid && this.studioOutputs) {
      this.studioOutputs = this.studioOutputs.filter((o) => o.deck_version !== v.deck_version);
    }
    const s = this.store.get(v._sid);
    if (s && s.deckVersion === v.deck_version) {
      const latest = (this.currentSessionId === v._sid ? this.studioOutputs : null)?.[0]?.deck_version ?? null;
      this.store.update(v._sid, { deckVersion: latest });
      this.refreshSessionListView();
    }
    this.ui.toast(`Deleted version v${v.deck_version}.`, 'success');
  }
  async downloadDeckVersion(sid: string, version: number, language?: string): Promise<void> {
    const key = `${sid}|${version}|${language || ''}`;
    if (this.downloadBusy.has(key)) return;
    this.downloadBusy.add(key);
    try {
      await this.api.downloadDeck(sid, version, language, await this.deckFileName(sid, version, language));
    } catch (e: any) {
      this.ui.toast(`Download failed: ${e.message}`, 'error');
    } finally {
      this.downloadBusy.delete(key);
    }
  }
  private async loadStudioOutputs(sid: string, fg: { opts?: RequestOpts; versions?: Promise<any> } = {}): Promise<void> {
    if (!sid) {
      this.studioOutputs = null;
      this.studioOutputsLoading = false;
      return;
    }
    const cancelled = () => !!fg.opts?.signal?.aborted;
    this.studioOutputsLoading = true;
    this.studioOutputs = null;
    this.studioOutputsSid = sid;
    try {
      const data = await (fg.versions || this.api.getJson(`/decks/${sid}/versions`, fg.opts));
      if (sid !== this.currentSessionId || cancelled()) return;
      const versions = (data.versions || []).slice().reverse().map((v: any) => ({
        plan_title: v.plan_title,
        deck_version: v.deck_version,
        theme: v.theme || '',
        status: v.status,
        created_at: v.created_at ?? null,
        files: v.files || {},
        _sid: sid,
        _languages: Object.keys(v.files || {}),
      })) as StudioOutputRow[];
      this.studioOutputs = versions;
      for (const v of versions) void this.loadDeckFeedback(v._sid, v.deck_version);
    } catch {
      if (sid === this.currentSessionId && !cancelled()) this.studioOutputs = [];
    } finally {
      if (sid === this.currentSessionId && !cancelled()) this.studioOutputsLoading = false;
    }
  }

  private scheduleMessagesScroll(): void { this.notifyPresentation({ type: 'scroll-messages' }); }

  // ---- Deck version feedback (one immutable submission per version) ----

  feedbackFor(sid: string, version: number): DeckFeedbackState | undefined {
    return this.deckFeedback.get(`${sid}|${version}`);
  }

  setFeedbackRating(sid: string, version: number, thumbsUp: boolean): void {
    const fb = this.feedbackFor(sid, version);
    if (!fb || fb.status !== 'open') return;
    fb.thumbsUp = thumbsUp;
    fb.error = '';
  }

  /** Drop an unsubmitted draft (rating + comment). */
  clearFeedbackDraft(sid: string, version: number): void {
    const fb = this.feedbackFor(sid, version);
    if (!fb || fb.status !== 'open') return;
    fb.thumbsUp = null;
    fb.comment = '';
    fb.error = '';
  }

  retryDeckFeedback(sid: string, version: number): void {
    void this.loadDeckFeedback(sid, version, true);
  }

  async submitDeckFeedback(sid: string, version: number): Promise<void> {
    const fb = this.feedbackFor(sid, version);
    if (!fb || fb.status !== 'open' || fb.thumbsUp === null) return;
    const comment = fb.comment.trim();
    if (comment.length > DECK_FEEDBACK_COMMENT_MAX) {
      fb.error = `Comment must be ${DECK_FEEDBACK_COMMENT_MAX} characters or fewer.`;
      return;
    }
    fb.status = 'submitting';
    fb.error = '';
    try {
      this.applySavedFeedback(fb, await this.api.submitDeckFeedback(sid, version, fb.thumbsUp, comment || undefined));
      this.ui.toast('Thanks for your feedback!', 'success');
    } catch (e: any) {
      if (e?.status === 409) {
        // Stored by another request first: show that record rather than retrying the POST.
        await this.loadDeckFeedback(sid, version, true);
        this.ui.toast('Feedback for this version was already submitted.', 'info');
        return;
      }
      fb.status = 'open';
      fb.error = e?.status === 404 ? 'This deck version is no longer available.' : (e?.message || 'Could not submit feedback.');
    }
  }

  private async loadDeckFeedback(sid: string, version: number, force = false): Promise<void> {
    const key = `${sid}|${version}`;
    const existing = this.deckFeedback.get(key);
    if (!force && existing && existing.status !== 'load-error') return;
    const fb: DeckFeedbackState = existing || { status: 'loading', thumbsUp: null, comment: '', error: '' };
    fb.status = 'loading';
    fb.error = '';
    this.deckFeedback.set(key, fb);
    try {
      const saved = await this.api.getDeckFeedback(sid, version);
      if (saved) this.applySavedFeedback(fb, saved);
      else fb.status = 'open';
    } catch (e: any) {
      fb.status = 'load-error';
      fb.error = e?.message || 'Could not load feedback.';
    }
  }

  private applySavedFeedback(fb: DeckFeedbackState, saved: DeckFeedback): void {
    fb.status = 'saved';
    fb.thumbsUp = saved.thumbs_up;
    fb.comment = saved.comment || '';
    fb.error = '';
  }

  /** Pull the next older page of chat history for the open session (see hydrateSession). */
  loadOlderMessages(): void {
    const id = this.currentSessionId;
    if (!id || this.loadingOlderMessages || !this.hasMoreOlderMessages) return;
    this.loadingOlderMessages = true;
    this.messagesLimitForCurrent += this.MESSAGES_LIMIT_STEP;
    this.hydrateSession(id, this.messagesLimitForCurrent, true)
      .then((ok) => {
        if (ok || id !== this.currentSessionId) return;
        this.messagesLimitForCurrent -= this.MESSAGES_LIMIT_STEP;
        this.ui.toast("Couldn't load earlier messages. Please try again.", 'error');
      })
      .finally(() => { this.loadingOlderMessages = false; });
  }

  async loadApprovals(): Promise<void> {
    const status = this.approvalsFilterValue;
    this.approvalsRows = [];
    this.approvalsLoadingText = 'Loading…';
    try {
      const data: any = await this.api.getJson(`/approvals${status ? `?status=${status}` : ''}`);
      const rows = Array.isArray(data) ? data : (data?.rows || []);
      this.approvalsRows = rows || [];
      this.approvalsLoadingText = '';
    } catch (err: any) {
      this.approvalsRows = [];
      this.approvalsLoadingText = `Failed to load: ${err.message}`;
    }
  }
  async decide(sessionId: string, version: number, action: string): Promise<void> {
    const note = await this.ui.askText(`Note for ${action} (optional):`, '');
    if (note === null) return;
    try {
      await this.api.postJson(`/approvals/${sessionId}/${version}/${action}`, { note: note || undefined });
      this.ui.toast(`Deck ${action}d`, 'success');
      await this.loadApprovals();
      await this.refreshApprovalsBadge();
    } catch (err: any) {
      this.ui.toast(`${action} failed: ${err.message}`, 'error');
    }
  }
  private async refreshApprovalsBadge(): Promise<void> {
    try {
      const pending: any = await this.api.getJson('/approvals?status=pending');
      const count = Array.isArray(pending) ? pending.length : (pending?.length ?? 0);
      this.approvalsBadgeCount = count;
    } catch { /* non-fatal, e.g. not logged in yet */ }
  }
  private waitForBootstrapRetry(): Promise<void> {
    return new Promise((resolve) => {
      const finish = () => { clearTimeout(timer); this.cancelBootstrapWait = undefined; resolve(); };
      const timer = setTimeout(finish, 4000);
      this.cancelBootstrapWait = finish;
    });
  }
  private async bootstrap(): Promise<void> {
    if (!this.api.loggedIn) { this.showLogin(); return; }
    if (this.api.sessionExpired) { await this.forceSignOut(EIGHT_HOUR_MSG); return; }
    this.showBoot('Restoring your session…');
    while (!this.destroyed) {
      if (this.api.sessionExpired) { await this.forceSignOut(EIGHT_HOUR_MSG); return; }
      const { status } = await this.api.validate();
      if (this.destroyed) return;
      if (status === 200) { this.showApp(); return; }
      if (status === 401) {
        if (await this.api.tryRefresh()) { this.showApp(); return; }
        await this.forceSignOut(this.sessionLostMessage());
        return;
      }
      this.showBoot('Can’t reach the server — reconnecting…');
      await this.waitForBootstrapRetry();
    }
  }
}
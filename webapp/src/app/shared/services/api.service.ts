import { Injectable } from '@angular/core';

import { AnalyticsDashboard, AnalyticsProjectDetail, AnalyticsProjectsPage } from '../../models/analytics.models';
import { Config } from '../../../config';
import { DeckFeedback, QuestionAnswers, Run, RunCurrentResponse, StopResponse } from '../../models/models';
import { IDLE_STREAM_TIMEOUT_MS, parseSseFrame, splitSseBuffer } from '../workflow.helpers';
import { ConfigService } from './config.service';

export interface SseReadResult {
  sawDone: boolean;
  aborted: boolean;
}

/** Per-request options: `signal` cancels it; `priority` is a browser fetch-priority hint. */
export interface RequestOpts {
  signal?: AbortSignal;
  priority?: 'high' | 'low' | 'auto';
}

async function httpError(res: Response, label: string): Promise<Error> {
  const data = await res.json().catch(() => ({}));
  const detail = typeof data?.detail === 'string' ? data.detail : data?.detail ? JSON.stringify(data.detail) : '';
  return Object.assign(new Error(detail || `${label} -> ${res.status}`), { status: res.status, detail });
}

@Injectable({ providedIn: 'root' })
export class ApiService {
  base: string;
  access: string | null;
  refresh: string | null;
  user: any;
  deadline: number | null;
  lastAuthError: any = null;
  onAuthLost: (() => void) | null = null;
  private _refreshing: Promise<boolean> | null = null;

  constructor(private config: ConfigService) {
    this.base = this.config.getApiBaseUrl();
    this.access = localStorage.getItem('se_access') || null;
    this.refresh = localStorage.getItem('se_refresh') || null;
    this.user = JSON.parse(localStorage.getItem('se_user') || 'null');
    this.deadline = Number(localStorage.getItem('se_deadline')) || null;
  }

  get roles(): string[] {
    return (this.user?.roles || []).map(String);
  }

  hasRole(...names: string[]): boolean {
    const want = names.map((n) => String(n).toLowerCase());
    return this.roles.some((r) => want.includes(r.toLowerCase()));
  }

  get loggedIn(): boolean {
    return !!this.access;
  }

  get sessionExpired(): boolean {
    return this.deadline != null && Date.now() >= this.deadline;
  }

  _persist(): void {
    if (this.access) localStorage.setItem('se_access', this.access); else localStorage.removeItem('se_access');
    if (this.refresh) localStorage.setItem('se_refresh', this.refresh); else localStorage.removeItem('se_refresh');
    if (this.user) localStorage.setItem('se_user', JSON.stringify(this.user)); else localStorage.removeItem('se_user');
    if (this.deadline) localStorage.setItem('se_deadline', String(this.deadline)); else localStorage.removeItem('se_deadline');
  }

  /** Maps a SessionUser block to our stored user; roles are cumulative (gate on these). */
  _applyUser(p: any = {}): void {
    this.user = {
      id: p.user_id,
      username: p.username,
      displayName: p.display_name || p.username,
      email: p.email || null,
      roles: (Array.isArray(p.roles) && p.roles.length) ? p.roles : (p.role ? [p.role] : []),
      sessionId: p.session_id,
    };
  }

  /**
   * Applies an AuthResponse — the shape shared by /auth/login and /auth/refresh:
   * stores the opaque bearer `token`, the single-use `refresh_token`, and the user.
   * `resetDeadline` (login only) starts the absolute 8-hour re-login clock.
   */
  _applyAuth(data: any, { resetDeadline = false }: { resetDeadline?: boolean } = {}): void {
    this.access = data.token || null;
    this.refresh = data.refresh_token || null;
    this._applyUser(data.data || {});
    if (resetDeadline) {
      const secs = Number(data.refresh_expires_in) || 8 * 3600;
      this.deadline = Date.now() + secs * 1000;
    }
    this.lastAuthError = null;
    this._persist();
  }

  /** Parses the uniform error shape `{detail, correlation_id}` and logs the id. */
  async _readError(res: Response): Promise<any> {
    const body = await res.json().catch(() => ({}));
    const correlationId = body.correlation_id || res.headers.get('x-correlation-id') || null;
    const detail = typeof body.detail === 'string' ? body.detail : (body.message || null);
    if (correlationId) console.error(`[api] ${res.status} ${detail || 'error'} — correlation_id ${correlationId}`);
    return { status: res.status, detail, correlationId };
  }

  /**
   * Exchanges the Entra (Azure AD) SSO tokens for an application session via
   * POST /auth/login. Throws on any non-2xx with a clear, status-specific message
   * (403 = not entitled: don't retry) so the caller keeps the user on sign-in.
   */
  async loginSso({ accessToken, idToken }: { accessToken: string; idToken: string }): Promise<any> {
    const res = await fetch(`${this.base}${Config.API.AUTH_LOGIN}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ access_token: accessToken, id_token: idToken }),
    });
    if (!res.ok) {
      const e = await this._readError(res);
      const err: any = new Error(this._authErrorMessage(e.status, e.detail));
      err.status = e.status;
      err.correlationId = e.correlationId;
      throw err;
    }
    const data = await res.json();
    this._applyAuth(data, { resetDeadline: true });
    if (!this.access) throw new Error('Sign-in response did not include a session token.');
    return data;
  }

  _authErrorMessage(status: number, detail: string | null): string {
    switch (status) {
      case 403: return detail || 'Your account is not entitled to use this app (no mapped access group). '
        + 'Please contact your administrator — retrying will not help.';
      case 401: return detail || 'Sign-in failed: your Microsoft identity could not be verified. Please try again.';
      case 422: return detail || 'Sign-in failed: the Microsoft tokens were missing or invalid. Please try again.';
      case 502: return detail || 'Microsoft sign-in is temporarily unavailable (could not reach Entra). Please try again shortly.';
      default:  return detail || `Sign-in failed (${status}).`;
    }
  }

  /**
   * Cheap session liveness check (GET /auth/validate) for the background poll.
   * On 200 it refreshes the cached user so roles stay current. Returns the raw
   * status + body WITHOUT auto-refreshing, so the caller decides what to do.
   */
  async validate(): Promise<{ status: number; body: any }> {
    try {
      const res = await fetch(`${this.base}${Config.API.AUTH_VALIDATE}`, { headers: this._headers() });
      const body = await res.json().catch(() => ({}));
      if (res.status === 200 && body?.data?.user) { this._applyUser(body.data.user); this._persist(); }
      return { status: res.status, body };
    } catch {
      return { status: 0, body: null };   // network blip — treat as transient
    }
  }

  /**
   * Rotates the session with its single-use refresh handle (POST /auth/refresh),
   * replacing BOTH tokens on success. Single-flight: concurrent callers share one
   * in-flight refresh so the handle is never replayed (a double-submit trips
   * reuse-detection and kills the session). Records the failure reason on error.
   */
  async tryRefresh(): Promise<boolean> {
    if (!this.refresh) { this.lastAuthError = { status: 0, detail: null }; return false; }
    if (this._refreshing) return this._refreshing;
    this._refreshing = (async () => {
      try {
        const res = await fetch(`${this.base}${Config.API.AUTH_REFRESH}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ refresh_token: this.refresh }),
        });
        if (!res.ok) { this.lastAuthError = await this._readError(res); return false; }
        this._applyAuth(await res.json());
        return true;
      } catch {
        this.lastAuthError = { status: 0, detail: null };
        return false;
      } finally {
        this._refreshing = null;
      }
    })();
    return this._refreshing;
  }

  /** The caller's stored profile (GET /auth/me). */
  async me(): Promise<any> {
    const res = await this._fetch(Config.API.AUTH_ME);
    if (!res.ok) throw new Error(`profile -> ${res.status}`);
    return res.json();
  }

  async logout(): Promise<void> {
    // Ask the server to end the session everywhere (idempotent; bearer, no body).
    if (this.access) {
      try {
        await fetch(`${this.base}${Config.API.AUTH_LOGOUT}`, { method: 'POST', headers: this._headers() });
      } catch { /* best-effort — always reach a signed-out state locally */ }
    }
    this.access = null; this.refresh = null; this.user = null; this.deadline = null; this.lastAuthError = null;
    this._persist();
  }

  _headers(extra: any = {}): any {
    return this.access ? { Authorization: `Bearer ${this.access}`, ...extra } : extra;
  }

  async _fetch(path: string, opts: any = {}, retry = true): Promise<Response> {
    const res = await fetch(`${this.base}${path}`, { ...opts, headers: this._headers(opts.headers) });
    if (res.status === 401 && retry) {
      // Token expired mid-session: rotate once and replay the request. If it can't
      // be refreshed the session is truly gone (e.g. signed in on another device).
      if (this.refresh && await this.tryRefresh()) return this._fetch(path, opts, false);
      this.onAuthLost?.();
    }
    if (res.status === 401) window.dispatchEvent(new CustomEvent('auth-expired'));
    return res;
  }

  async getJson(path: string, opts: RequestOpts = {}): Promise<any> {
    const res = await this._fetch(path, opts);
    if (!res.ok) throw new Error(`${path} -> ${res.status}`);
    return res.json();
  }

  getAnalyticsDashboard(filters: Record<string, string | number | undefined>, signal?: AbortSignal): Promise<AnalyticsDashboard> {
    return this.analyticsRequest(Config.API.ANALYTICS_DASHBOARD, filters, signal);
  }

  getAnalyticsProjects(filters: Record<string, string | number | undefined>, signal?: AbortSignal): Promise<AnalyticsProjectsPage> {
    return this.analyticsRequest(Config.API.ANALYTICS_PROJECTS, filters, signal);
  }

  getAnalyticsProject(sessionId: string, signal?: AbortSignal): Promise<AnalyticsProjectDetail> {
    return this.analyticsRequest(Config.API.ANALYTICS_PROJECT(sessionId), {}, signal);
  }

  private async analyticsRequest<Result>(
    path: string,
    filters: Record<string, string | number | undefined>,
    signal?: AbortSignal,
  ): Promise<Result> {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value !== undefined && value !== '') query.set(key, String(value));
    }
    const search = query.toString();
    const response = await this._fetch(`${path}${search ? `?${search}` : ''}`, { signal });
    if (!response.ok) {
      const error = await this._readError(response);
      throw Object.assign(new Error(error.detail || `Analytics request failed (${response.status}).`), error);
    }
    return response.json();
  }

  async postJson(path: string, body: any): Promise<any> {
    const res = await this._fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail ? JSON.stringify(data.detail) : `${path} -> ${res.status}`);
    return data;
  }

  /**
   * Legacy multipart upload — POSTs the file bytes THROUGH the API. Still used as a
   * fallback for small files when the backend reports direct-to-blob presigning is
   * unavailable; large files 413 at the gateway (that's why the register→PUT→complete
   * flow below exists).
   */
  async uploadDocuments(sessionId: string, files: File[]): Promise<any> {
    const form = new FormData();
    for (const f of files) form.append('files', f);
    const res = await this._fetch(Config.API.DECK_DOCUMENTS(sessionId), { method: 'POST', body: form });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail ? JSON.stringify(data.detail) : `upload -> ${res.status}`);
    return data;
  }

  /**
   * Step 1 of the direct-to-blob flow: register the selected files so the backend
   * returns one short-lived SAS `upload_url` per file. On error the thrown Error
   * carries `.status`/`.detail` so the caller can detect the "presigning unavailable"
   * 400 and fall back to {@link uploadDocuments}.
   */
  async registerDocuments(sessionId: string, files: File[]): Promise<any> {
    const body = { files: files.map((f) => ({ filename: f.name, size: f.size })) };
    const res = await this._fetch(Config.API.DECK_DOCUMENTS_REGISTER(sessionId), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err: any = new Error(data.detail
        ? (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail))
        : `register -> ${res.status}`);
      err.status = res.status;
      err.detail = data.detail;
      throw err;
    }
    return data;
  }

  /**
   * Step 2: PUT the raw bytes straight to Azure Blob Storage using the presigned SAS
   * URL — bypassing the API body-size limit. The SAS token in the URL authorizes the
   * request, so we deliberately send NO Authorization header (this call skips `_fetch`
   * and its auth/refresh interceptors). Uses XMLHttpRequest for real upload progress;
   * a single Put Blob covers the backend's 200 MB per-file cap.
   */
  uploadToBlob(uploadUrl: string, file: File, onProgress?: (pct: number) => void, signal?: AbortSignal): Promise<void> {
    return new Promise((resolve, reject) => {
      if (signal?.aborted) { reject(new DOMException('aborted', 'AbortError')); return; }
      const xhr = new XMLHttpRequest();
      let aborted = false;
      xhr.open('PUT', uploadUrl, true);
      xhr.setRequestHeader('x-ms-blob-type', 'BlockBlob');
      if (file.type) xhr.setRequestHeader('Content-Type', file.type);
      const onAbort = () => { aborted = true; xhr.abort(); };
      xhr.upload.onprogress = (e) => {
        if (onProgress && e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
      };
      // Settle on `onloadend` (fires for EVERY terminal outcome: load, error, timeout, abort)
      // instead of only `onload`/`onerror`. A CORS or upload-phase failure often errors solely
      // on `xhr.upload` and never on `xhr`, which would otherwise leave this promise unsettled
      // and the file row stuck showing "uploading" forever.
      xhr.onloadend = () => {
        signal?.removeEventListener('abort', onAbort);
        if (aborted) { reject(new DOMException('aborted', 'AbortError')); return; }
        if (xhr.status >= 200 && xhr.status < 300) { resolve(); return; }
        // status 0 = the request was blocked before any response was read — a network drop or,
        // most commonly for browser->Blob, a missing Storage CORS rule allowing PUT/OPTIONS +
        // x-ms-blob-type for this origin.
        reject(new Error(xhr.status === 0
          ? 'network or CORS error (is Storage CORS configured?)'
          : `blob upload -> ${xhr.status}`));
      };
      signal?.addEventListener('abort', onAbort);
      xhr.send(file);
    });
  }

  /**
   * Aborts a registered-but-not-completed upload so the backend deletes the half-registered
   * file (call after a failed/cancelled browser->Blob PUT so nothing is left orphaned).
   * Idempotent: a 404 ("no in-progress upload found") is treated as success. Omit `version`
   * to abort every in-progress version of `docId`. Do NOT call after `completeDocuments`
   * succeeded — that's a real document; use {@link deleteDocument} instead.
   */
  async abortUpload(sessionId: string, docId: string, version?: number): Promise<any> {
    const q = version != null ? `?version=${version}` : '';
    const res = await this._fetch(`${Config.API.DECK_DOCUMENT_ABORT(sessionId, docId)}${q}`, { method: 'POST' });
    if (!res.ok && res.status !== 404) throw new Error(`abort upload -> ${res.status}`);
    return res.json().catch(() => ({}));
  }

  /**
   * Step 3: tell the backend which blobs landed so it verifies them and starts
   * ingestion. Response shape matches the legacy upload response (a `documents` array).
   * Accepts a subset, so files can be completed individually as each PUT finishes.
   */
  async completeDocuments(sessionId: string, documents: any[]): Promise<any> {
    const res = await this._fetch(Config.API.DECK_DOCUMENTS_COMPLETE(sessionId), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ documents }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail ? JSON.stringify(data.detail) : `complete -> ${res.status}`);
    return data;
  }

  /** Short-lived read-only SAS URL for a deck file. Never log or persist it. */
  private async deckDownloadUrl(sessionId: string, version: number, language?: string): Promise<string> {
    const q = language ? `?language=${encodeURIComponent(language)}` : '';
    const res = await this._fetch(`${Config.API.DECK_DOWNLOAD_URL(sessionId, version)}${q}`);
    if (!res.ok) {
      let detail: any = '';
      try { detail = (await res.json()).detail; } catch { /* response body isn't JSON */ }
      throw new Error(detail ? (typeof detail === 'string' ? detail : JSON.stringify(detail)) : `download failed (${res.status})`);
    }
    const { url } = await res.json();
    if (typeof url !== 'string' || !/^https?:\/\//i.test(url)) throw new Error('download failed (no download URL returned)');
    return url;
  }

  /**
   * Fetch a rendered deck version as a raw .pptx {@link Blob}: a SAS URL from the API (bearer auth),
   * then the bytes straight from Blob Storage (no bearer). Shared by {@link downloadDeck} and the
   * in-app slide preview, which needs the bytes — hence fetch-as-blob rather than navigating to the URL.
   */
  async fetchDeckBlob(sessionId: string, version: number, language?: string): Promise<Blob> {
    const url = await this.deckDownloadUrl(sessionId, version, language);
    let res: Response;
    try {
      // Plain fetch, not _fetch: the SAS query authorizes the read, so the API token never reaches Blob.
      res = await fetch(url, { credentials: 'omit', cache: 'no-store', referrerPolicy: 'no-referrer' });
    } catch {
      throw new Error('download failed (network or CORS error — is Storage CORS configured for GET?)');
    }
    if (!res.ok) throw new Error(`download failed (${res.status})`);
    return res.blob();
  }

  async downloadDeck(sessionId: string, version: number, language?: string, filename?: string): Promise<void> {
    const blob = await this.fetchDeckBlob(sessionId, version, language);
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    const suffix = language ? `_${language}` : '';
    a.href = url; a.download = filename || `${sessionId}_v${version}${suffix}.pptx`; // callers normally pass {plan_title}_{project}_v{n}[_{lang}]
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  }

  async deleteDocument(sessionId: string, docId: string, version?: number): Promise<any> {
    const q = version != null ? `?version=${version}` : '';
    const res = await this._fetch(`${Config.API.DECK_DOCUMENT(sessionId, docId)}${q}`, { method: 'DELETE' });
    if (!res.ok) throw new Error(`delete document -> ${res.status}`);
    return res.json();
  }

  async deleteVersion(sessionId: string, deckVersion: number): Promise<any> {
    const res = await this._fetch(Config.API.DECK_VERSION(sessionId, deckVersion), { method: 'DELETE' });
    if (!res.ok) throw new Error(`delete version -> ${res.status}`);
    return res.json();
  }

  /** Stored feedback for a deck version, or `null` when none has been submitted (404). */
  async getDeckFeedback(sessionId: string, deckVersion: number): Promise<DeckFeedback | null> {
    const res = await this._fetch(this.feedbackPath(sessionId, deckVersion));
    if (res.status === 404) return null;
    if (!res.ok) throw await this.feedbackError(res);
    return res.json();
  }

  /** One-time feedback submission; errors carry `.status` (409 = already submitted, 422 = invalid). */
  async submitDeckFeedback(sessionId: string, deckVersion: number, thumbsUp: boolean, comment?: string): Promise<DeckFeedback> {
    const body: { thumbs_up: boolean; comment?: string } = { thumbs_up: thumbsUp };
    if (comment) body.comment = comment;
    const res = await this._fetch(this.feedbackPath(sessionId, deckVersion), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw await this.feedbackError(res);
    return res.json();
  }

  private feedbackPath(sessionId: string, deckVersion: number): string {
    return Config.API.DECK_FEEDBACK(sessionId, deckVersion);
  }

  private async feedbackError(res: Response): Promise<Error> {
    if (res.status !== 422) {
      const e = await this._readError(res);
      return Object.assign(new Error(e.detail || `Feedback request failed (${res.status}).`), e);
    }
    // FastAPI validation errors: `detail` is a list of `{loc, msg}`.
    const body = await res.json().catch(() => ({}));
    const msgs = Array.isArray(body.detail) ? body.detail.map((d: any) => d?.msg).filter(Boolean) : [];
    const detail = msgs.join('; ') || (typeof body.detail === 'string' ? body.detail : 'Invalid feedback.');
    return Object.assign(new Error(detail), { status: 422, detail, correlationId: body.correlation_id || null });
  }

  async deleteSession(sessionId: string): Promise<any> {
    const res = await this._fetch(Config.API.DECK(sessionId), { method: 'DELETE' });
    if (!res.ok) throw new Error(`delete session -> ${res.status}`);
    return res.json();
  }

  async renameSession(sessionId: string, title: string): Promise<any> {
    const res = await this._fetch(Config.API.DECK_TITLE(sessionId), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `rename session -> ${res.status}`);
    return data;
  }

  /** Server-persisted project list for the current user (survives a browser cache clear). */
  async listSessions(): Promise<any> {
    const res = await this._fetch(Config.API.DECK_SESSIONS);
    if (!res.ok) throw new Error(`list sessions -> ${res.status}`);
    return res.json();
  }

  /** Persist a project (id + title) server-side so it isn't lost when the cache is cleared. */
  async registerSession(sessionId: string, title: string): Promise<any> {
    return this.postJson(Config.API.DECK_SESSIONS, { session_id: sessionId, title });
  }

  /** Server-persisted chat history for a session (Postgres is the source of truth). */
  async getMessages(sessionId: string, limit?: number, opts: RequestOpts = {}): Promise<any> {
    const query = limit != null ? `?limit=${encodeURIComponent(limit)}` : '';
    const res = await this._fetch(`${Config.API.DECK_MESSAGES(sessionId)}${query}`, opts);
    if (!res.ok) {
      const err: any = new Error(`messages -> ${res.status}`);
      err.status = res.status;
      throw err;
    }
    return res.json();
  }

  /** Server-persisted source documents; latest_only defaults to true on the API. */
  async getDocuments(sessionId: string, latestOnly = true, opts: RequestOpts = {}): Promise<any> {
    const query = latestOnly === false ? '?latest_only=false' : '';
    const res = await this._fetch(`${Config.API.DECK_DOCUMENTS(sessionId)}${query}`, opts);
    if (!res.ok) {
      const err: any = new Error(`documents -> ${res.status}`);
      err.status = res.status;
      throw err;
    }
    return res.json();
  }

  /**
   * Follows a session's live event stream until aborted via `signal`, the server closes it, or it
   * goes silent past the keepalive window. Only used for run-less (ingestion) events.
   */
  async streamSession(sessionId: string, onEvent: (evt: any) => void, signal?: AbortSignal, retry = true): Promise<void> {
    let res: Response;
    try {
      res = await this._fetch(Config.API.DECK_STREAM(sessionId), { signal });
    } catch (err: any) {
      if (err.name === 'AbortError') return;
      throw err;
    }
    if (res.status === 401 && retry) {
      // Rotate once and reconnect; if the session is truly gone, hand off to sign-out.
      if (this.refresh && await this.tryRefresh()) return this.streamSession(sessionId, onEvent, signal, false);
      this.onAuthLost?.();
      return;
    }
    if (!res.ok || !res.body) return;
    await this.readSse(res, onEvent, { signal, stopOnDone: false });
  }

  /**
   * POSTs a user message and streams the resulting run. Resolves when the stream ends (with or
   * without a terminal `done`); throws only when the request fails before streaming starts.
   */
  async streamInvoke(
    sessionId: string, message: string, clientRequestId: string,
    onEvent: (evt: any) => void, signal?: AbortSignal, answers?: QuestionAnswers | null, retry = true,
  ): Promise<SseReadResult> {
    const body: Record<string, unknown> = { message, client_request_id: clientRequestId };
    // Older backends never see the field unless the user actually answered something.
    if (answers && Object.keys(answers).length) body['answers'] = answers;
    const res = await fetch(`${this.base}${Config.API.DECK_INVOKE(sessionId)}`, {
      method: 'POST',
      headers: this._headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body),
      signal,
    });
    if (res.status === 401 && retry) {
      // Rotate once and retry; if the session is truly gone, hand off to sign-out.
      if (this.refresh && await this.tryRefresh()) {
        return this.streamInvoke(sessionId, message, clientRequestId, onEvent, signal, answers, false);
      }
      this.onAuthLost?.();
      throw Object.assign(new Error('Your session has ended. Please sign in again.'), { status: 401 });
    }
    if (!res.ok || !res.body) throw await httpError(res, 'invoke');
    return this.readSse(res, onEvent, { signal, stopOnDone: true });
  }

  /** Replays a run's events after `afterEventIndex`, then follows it live until its terminal `done`. */
  async streamRunEvents(
    sessionId: string, runId: string, afterEventIndex: number,
    onEvent: (evt: any) => void, signal?: AbortSignal, onOpen?: () => void,
  ): Promise<SseReadResult> {
    const after = Math.max(0, Math.floor(afterEventIndex) || 0);
    const res = await this._fetch(Config.API.DECK_RUN_EVENTS(sessionId, runId, after), {
      signal, headers: { Accept: 'text/event-stream' },
    });
    if (!res.ok || !res.body) throw await httpError(res, 'run events');
    onOpen?.();
    return this.readSse(res, onEvent, { signal, stopOnDone: true });
  }

  /** Active and latest run for a session; an unknown session/older backend reads as "no runs". */
  async getCurrentRun(sessionId: string): Promise<RunCurrentResponse> {
    const res = await this._fetch(Config.API.DECK_RUN_CURRENT(sessionId));
    if (res.status === 404) return { session_id: sessionId, active: null, latest: null };
    if (!res.ok) throw await httpError(res, 'current run');
    const data = await res.json();
    return { session_id: sessionId, active: data?.active || null, latest: data?.latest || null };
  }

  async getRun(sessionId: string, runId: string): Promise<Run> {
    const res = await this._fetch(Config.API.DECK_RUN(sessionId, runId));
    if (!res.ok) throw await httpError(res, 'run');
    return res.json();
  }

  async stopRun(sessionId: string, runId: string | null): Promise<StopResponse> {
    const res = await this._fetch(Config.API.DECK_STOP(sessionId), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(runId ? { run_id: runId } : {}),
    });
    if (!res.ok) throw await httpError(res, 'stop');
    return res.json();
  }

  /**
   * Reads SSE frames until `done` (when `stopOnDone`), close, abort, or idle timeout. Keepalive
   * comments count as activity. A mid-stream network drop resolves (never throws) so callers reconnect.
   */
  private async readSse(
    res: Response, onEvent: (evt: any) => void,
    opts: { signal?: AbortSignal; stopOnDone: boolean; idleMs?: number },
  ): Promise<SseReadResult> {
    const reader = res.body!.getReader();
    const decoder = new TextDecoder();
    const idleMs = opts.idleMs ?? IDLE_STREAM_TIMEOUT_MS;
    let buf = '';
    let idleTimer: ReturnType<typeof setTimeout> | undefined;
    const arm = () => {
      clearTimeout(idleTimer);
      if (idleMs > 0) idleTimer = setTimeout(() => { reader.cancel().catch(() => {}); }, idleMs);
    };
    arm();
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        arm();
        buf += decoder.decode(value, { stream: true });
        const { blocks, rest } = splitSseBuffer(buf);
        buf = rest;
        for (const block of blocks) {
          const frame = parseSseFrame(block);
          if (!frame.data) continue;
          try { onEvent(frame.data); } catch { /* a handler error must not end the stream */ }
          if (opts.stopOnDone && frame.data.type === 'done') {
            reader.cancel().catch(() => {});
            return { sawDone: true, aborted: false };
          }
        }
      }
      // A server that closes right after its last frame may omit the blank-line terminator;
      // dispatch that trailing frame instead of dropping a terminal `done`.
      buf += decoder.decode();
      if (buf.trim()) {
        const frame = parseSseFrame(buf);
        if (frame.data) {
          try { onEvent(frame.data); } catch { /* a handler error must not end the stream */ }
          if (frame.data.type === 'done') return { sawDone: true, aborted: false };
        }
      }
    } catch (err: any) {
      return { sawDone: false, aborted: err?.name === 'AbortError' || !!opts.signal?.aborted };
    } finally {
      clearTimeout(idleTimer);
    }
    return { sawDone: false, aborted: !!opts.signal?.aborted };
  }
}

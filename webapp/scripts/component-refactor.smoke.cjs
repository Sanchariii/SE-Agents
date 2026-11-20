#!/usr/bin/env node
'use strict';

/**
 * Standalone, UI-only component-refactor regression smoke test. No @playwright/test.
 * Run ONLY inside mcr.microsoft.com/playwright:v1.51.1-noble, with playwright@1.51.1
 * installed under /tmp/pw and NODE_PATH=/tmp/pw/node_modules. Mount this workspace
 * read-only at /work; the already-built frontend must be running independently.
 * BASE_URL defaults to http://host.docker.internal:8080. No host Node/npm needed.
 * SMOKE_SCOPE=thought-process runs only the Thought Process header/preview checks.
 * SMOKE_SCOPE=deck-feedback runs only the Outputs deck-version feedback checks.
 * SMOKE_SCOPE=messages runs only the chat history timeline / lazy-loading checks.
 * SMOKE_SCOPE=deck-download runs only the SAS (download-url -> Blob GET) deck download checks.
 * Optional ARTIFACT_DIR (default /tmp/component-refactor-smoke) receives failure
 * screenshots/HTML/JSON. Mount a separate artifact volume if these must survive --rm.
 *
 * All API, blob, and SSE traffic is synthetic. Only frontend static GETs may leave
 * the browser. Unknown requests fail closed (including auth/destructive endpoints).
 * Google Fonts CSS is intentionally stubbed; the app's system-font fallback is used.
 * Real SSO, approvals decisions, deletion, and actual PPTX validity are NOT tested.
 * The hidden Approvals navigation and commented-out Submit action are not exercised.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { setTimeout: delay } = require('node:timers/promises');

assert(process.platform === 'linux' && fs.existsSync('/.dockerenv'),
  'Docker only: run this script in the documented Playwright image, not on the host.');
const { chromium } = process.env.SMOKE_SCOPE === 'analytics-helpers' ? {} : require('playwright');

const BASE_URL = new URL(process.env.BASE_URL || 'http://host.docker.internal:8080');
assert(['http:', 'https:'].includes(BASE_URL.protocol), 'BASE_URL must be HTTP(S)');
assert(!BASE_URL.username && !BASE_URL.password, 'BASE_URL must not contain credentials');
const API_ORIGIN = 'https://component-smoke-api.invalid';
const BLOB_ORIGIN = 'https://component-smoke-blob.invalid';
const ARTIFACT_DIR = process.env.ARTIFACT_DIR || '/tmp/component-refactor-smoke';
const THOUGHT_PROCESS_ONLY = process.env.SMOKE_SCOPE === 'thought-process';
const ANALYTICS_ONLY = process.env.SMOKE_SCOPE === 'analytics';
const DECK_FEEDBACK_ONLY = process.env.SMOKE_SCOPE === 'deck-feedback';
const MESSAGES_ONLY = process.env.SMOKE_SCOPE === 'messages';
const DECK_DOWNLOAD_ONLY = process.env.SMOKE_SCOPE === 'deck-download';
const PPTX_MIME = 'application/vnd.openxmlformats-officedocument.presentationml.presentation';
const SAS_QUERY = '?sig=synthetic-read-sas';
const TOKEN = 'synthetic-smoke-access-not-a-real-token';
const USER_ID = 'component-smoke-user';
const RESTORED = 'smoke-restored';
const EMPTY = 'smoke-empty';
const NOW = '2026-09-11T10:00:00.000Z';
const NEW_TITLE = 'Smoke modal project';
const QUEUED_MESSAGE = 'Create a training deck from both uploaded smoke sources.';
const DOWNLOAD_BYTES = Buffer.from('synthetic-pptx-download-fixture\n');
const UPLOAD_FILES = [
  { name: 'queue source α.txt', mimeType: 'text/plain', buffer: Buffer.from('Synthetic source one.\nTwo lines.\n') },
  { name: 'queue source two.txt', mimeType: 'text/plain', buffer: Buffer.from('Synthetic source two, with different bytes.\n') },
];
const PROFILE = {
  user_id: USER_ID, username: 'smoke.admin', display_name: 'Smoke Admin',
  email: 'smoke@example.invalid', roles: ['admin'], session_id: 'synthetic-auth-session',
};
const MARKDOWN = [
  '## Restored training summary', '', '**Verified** and *readable* with `safe_code`.', '',
  '- Source reviewed', '- Deck prepared', '', '> Fixture quotation', '',
  '```text', '<fixture> & code', '```', '',
  '[Reference](https://example.invalid/smoke-reference)',
].join('\n');
const warnings = new Set();
let currentStep = 'startup';

function log(message) { console.log(`[smoke ${new Date().toISOString()}] ${message}`); }
function warn(message) {
  if (!warnings.has(message)) { warnings.add(message); console.warn(`[smoke WARNING] ${message}`); }
}
async function eventually(label, check, timeout = 12000) {
  const deadline = Date.now() + timeout;
  let lastError;
  do {
    try { await check(); return; } catch (error) { lastError = error; }
    await delay(50);
  } while (Date.now() < deadline);
  throw new Error(`${label}: ${lastError?.message || 'timed out'}`, { cause: lastError });
}
async function textIs(locator, expected) {
  await eventually(`text ${expected}`, async () => assert.equal((await locator.innerText()).trim(), expected));
}
async function textIncludes(locator, expected) {
  await eventually(`contains ${expected}`, async () => assert((await locator.innerText()).includes(expected)));
}
function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}
function fixtureSession(id, title, status = 'new') {
  return { id, title, status, deckVersion: status === 'done' ? 1 : null, verdict: null,
    messages: [], docs: [], createdAt: NOW };
}
function fixtureVersion(version, title) {
  return { deck_version: version, plan_title: title, theme: 'Smoke theme', status: 'done',
    files: { English: 'synthetic-english.pptx', French: 'synthetic-french.pptx' } };
}

/** 23 user/assistant turns; turns 3 and 23 built decks and carry persisted workflow events. */
function historyFixture(turns = 23) {
  const rows = [];
  let id = 0;
  for (let turn = 1; turn <= turns; turn++) {
    const runId = `run-${turn}`;
    const deck = turn === 3 || turn === turns;
    const result = deck ? { deck_updated: true, deck_version: turn === 3 ? 1 : 2, verdict: 'approve' } : null;
    const events = deck ? [
      { type: 'agent_step', agent: 'slide', message: 'Slide 1/1 complete', data: { slide_number: 1, total: 1, phase: 'done' } },
      { type: 'done', agent: 'supervisor', message: 'Deck ready', data: { result } },
    ] : [];
    rows.push({ id: ++id, run_id: runId, role: 'user', content: `Question ${turn}`, result: null, events: [] });
    rows.push({ id: ++id, run_id: runId, role: 'assistant', content: `Answer ${turn}`, result, events });
  }
  return rows;
}

/** Mirrors GET /decks/{sid}/messages: last `limit` rows plus a timeline of only those runs' events. */
function messagesResponse(sid, rows, limit) {
  const page = limit ? rows.slice(-limit) : rows;
  const timeline = [];
  for (const row of page) {
    row.events.forEach((event, index) => timeline.push({ kind: 'workflow_event', id: null, run_id: row.run_id,
      event_index: index, ts: NOW, ...event }));
    timeline.push({ kind: 'message', id: row.id, run_id: row.run_id, role: row.role, content: row.content,
      result: row.result, ts: NOW });
  }
  return { session_id: sid, timeline, messages: page.map((row) => ({ id: row.id, session_id: sid, role: row.role,
    content: row.content, result: row.result, created_at: NOW })) };
}

function analyticsFixture(params, mode = 'data') {
  const measured = (value) => ({ value, type: 'measured' });
  const derived = (value, count, total) => ({ value, type: 'derived', count, of: total });
  const unit = (value, denominator) => ({ value, type: 'derived', denominator });
  const estimated = (value) => ({ value, type: 'estimated' });
  const agents = [
    { agent_name: 'planner', model: 'claude-sonnet-4-6', runs: 120, success_rate: .98, fallback_rate: .01,
      median_duration_ms: 5400, p95_duration_ms: 9800, llm_calls: 240, input_tokens: 180000,
      output_tokens: 21000, cached_input_tokens: 34000, cost_usd: 4.12, tokens_per_run: 1675, cost_per_run_usd: .0343 },
    { agent_name: 'slide_content', model: 'claude-opus-5', runs: 85, success_rate: .96, fallback_rate: .02,
      median_duration_ms: 6100, p95_duration_ms: 15400, llm_calls: 120, input_tokens: 95000,
      output_tokens: 21000, cached_input_tokens: 11000, cost_usd: 6.52, tokens_per_run: 1364.7, cost_per_run_usd: .0767 },
    { agent_name: 'planner', model: 'enterprise-model-with-a-very-long-version-name-for-responsive-checks-2026-10',
      runs: 15, success_rate: .93, fallback_rate: 0, median_duration_ms: 3900, p95_duration_ms: 9700,
      llm_calls: 16, input_tokens: 63000, output_tokens: 12000, cached_input_tokens: 1200,
      cost_usd: .0042, tokens_per_run: 5000, cost_per_run_usd: .00028 },
    { agent_name: 'layout', model: null, runs: 0, success_rate: null, fallback_rate: null,
      median_duration_ms: null, p95_duration_ms: null, llm_calls: 0, input_tokens: null,
      output_tokens: null, cached_input_tokens: null, cost_usd: null, tokens_per_run: null, cost_per_run_usd: null },
  ].filter((agent) => (!params.get('agent') || agent.agent_name === params.get('agent'))
    && (!params.get('model') || agent.model === params.get('model')));
  const data = {
    window: { from: params.get('from'), to: params.get('to'), timezone: params.get('tz') || 'UTC' },
    filters: { agent: params.get('agent'), model: params.get('model') },
    overview: { active_users: measured(42), projects_worked_on: measured(205), successful_projects: measured(61),
      slides_delivered: measured(540), workflow_runs: measured(210), median_ai_turnaround_ms: measured(18450), total_ai_cost_usd: measured(12.87) },
    trends: { bucket: 'day', points: Array.from({ length: 7 }, (_, index) => ({
      bucket: new Date(Date.parse(params.get('from')) + index * 86400000).toISOString(),
      workflow_runs: 12 + index * 3, active_users: 5 + index, slides_delivered: 34 + index * 5,
      cost_usd: index === 2 ? null : .91 + index * .15,
    })) },
    adoption: { projects: measured(205), median_turns_per_project: measured(2), avg_turns_per_project: { value: 2.38, type: 'derived' },
      multi_turn_projects: measured(76), multi_turn_rate: derived(.38, 76, 200) },
    performance: { ai_turnaround_ms: { median: 18450, p95: 72000, total: 3874500, type: 'measured', note: 'User-visible wait; agents may run in parallel' },
      agent_processing_ms: { total: 5820000, type: 'measured', note: 'Summed agent-active time; can exceed wall time' } },
    cost: { total_cost_usd: measured(12.87), input_tokens: measured(338000), output_tokens: measured(54000), cached_input_tokens: measured(46200),
      total_tokens: measured(392000), llm_calls: measured(376), cost_per_project_usd: unit(.211, 61), cost_per_slide_usd: unit(.0238, 540),
      cost_per_call_usd: unit(.0342, 376), tokens_per_slide: unit(725.93, 540), tokens_per_call: unit(1042.55, 376) },
    quality: { agent_runs: measured(220), fallback_rate: derived(.01, 2, 220), agent_failure_rate: derived(.04, 9, 220), rework_rate: derived(.18, 11, 61),
      feedback: { rated_versions: measured(50), thumbs_up: measured(37), thumbs_down: measured(13), thumbs_up_rate: derived(.74, 37, 50), comment_coverage: derived(.4, 20, 50) } },
    efficiency: { estimated_hours_saved: estimated(54.3), hours_saved_per_dollar: estimated(4.22), time_leverage: estimated(18.7),
      estimate: { is_estimated: true, confidence: 'low', baseline_version: 'v1' } },
    agents,
    models: agents.map((agent) => ({ model: agent.model, runs: agent.runs, failure_rate: agent.runs ? .02 : null,
      median_duration_ms: agent.median_duration_ms, llm_calls: agent.llm_calls, input_tokens: agent.input_tokens,
      output_tokens: agent.output_tokens, cost_usd: agent.cost_usd })),
    data_quality: { unpriced_agent_runs: 1, missing_page_counts: 3, dropped_metric_events: 0 },
  };
  if (mode === 'null' || mode === 'empty') {
    for (const metric of Object.values(data.overview)) metric.value = null;
    data.overview.slides_delivered.value = 0;
    for (const metric of Object.values(data.cost)) {
      metric.value = null;
      if ('denominator' in metric) metric.denominator = 0;
    }
    for (const metric of [data.adoption.multi_turn_rate, data.quality.rework_rate, data.quality.fallback_rate,
      data.quality.agent_failure_rate, data.quality.feedback.thumbs_up_rate, data.quality.feedback.comment_coverage]) {
      metric.value = .5;
      metric.count = 0;
      metric.of = 0;
    }
    data.efficiency.estimated_hours_saved.value = null;
    data.efficiency.hours_saved_per_dollar.value = null;
    data.efficiency.time_leverage.value = null;
    data.trends.points = [];
    data.agents = [];
    data.models = [];
  }
  if (mode === 'stale') data.overview.active_users.value = 999;
  return data;
}

function analyticsProjectFixtures() {
  return Array.from({ length: 205 }, (_, index) => ({
    session_id: index === 0 ? '=FORMULA("fixture")' : `analytics-project-${String(index).padStart(3, '0')}`,
    owner_name: index % 19 === 0 ? null : `Owner ${index % 4 + 1}`, turns: 1 + index % 5, multi_turn: index % 5 > 0,
    published: index % 4 !== 0, slides_delivered: index % 4 !== 0 ? 12 : 0,
    ai_turnaround_ms: index === 0 ? null : 42000 + index * 1000, agent_processing_ms: 68000 + index * 1000,
    total_tokens: index === 0 ? null : 154000 + index, cost_usd: index === 0 ? null : index / 100,
    last_active: new Date(Date.parse(NOW) + index * 60000).toISOString(),
  }));
}

function analyticsDetailFixture(sessionId) {
  return {
    session_id: sessionId,
    workflow_runs: [
      { run_id: 'workflow-1', turn_index: 1, intent: 'create', started_at: NOW, finished_at: '2026-09-11T10:00:18.450Z', wall_ms: 18450,
        status: 'succeeded', deck_version_id: 2, slides_created: 8, slides_updated: 0, slides_translated: 0 },
      { run_id: 'workflow-2', turn_index: 2, intent: 'edit', started_at: '2026-09-11T10:05:00Z', finished_at: null, wall_ms: null,
        status: 'failed', deck_version_id: null, slides_created: 0, slides_updated: 0, slides_translated: 0 },
    ],
    agent_runs: [
      { agent_run_id: 'node-1', workflow_run_id: 'workflow-1', agent_name: 'slide_content', model: 'claude-opus-5', started_at: NOW,
        duration_ms: 6100, status: 'succeeded', llm_calls: 3, input_tokens: 13000, output_tokens: 4100, cached_input_tokens: 0,
        cost_usd: .09, work_units: 1, work_unit_type: 'slide', pricing_status: 'priced' },
      { agent_run_id: 'node-2', workflow_run_id: 'workflow-2', agent_name: 'planner', model: null, started_at: NOW,
        duration_ms: null, status: 'fallback', llm_calls: 1, input_tokens: null, output_tokens: null, cached_input_tokens: null,
        cost_usd: null, work_units: null, work_unit_type: null, pricing_status: 'unpriced' },
    ],
    ingestion_runs: [{ ingestion_run_id: 'ingestion-1', document_id: 'source-handbook', started_at: NOW, duration_ms: 1800,
      status: 'succeeded', bytes_processed: 16384, pages_processed: null, chunks_created: 12 }],
  };
}

/** Runs before app bootstrap. Only intercepts transport; never accesses Angular. */
function browserInit({ authenticated, origin, apiOrigin, token, userId, restored, seedSession, profile }) {
  if (location.origin !== origin) return;
  if (!localStorage.getItem('component_smoke_seeded')) {
    localStorage.clear();
    localStorage.setItem('component_smoke_seeded', '1');
    localStorage.setItem('se_theme', 'dark');
    localStorage.setItem('se_backend_url', apiOrigin);
    if (authenticated) {
      localStorage.setItem('se_access', token);
      localStorage.setItem('se_refresh', 'synthetic-smoke-refresh-not-a-real-token');
      localStorage.setItem('se_deadline', String(Date.now() + 8 * 60 * 60 * 1000));
      localStorage.setItem('se_user', JSON.stringify({ id: userId, username: profile.username,
        displayName: profile.display_name, email: profile.email, roles: profile.roles,
        sessionId: profile.session_id }));
      localStorage.setItem(`se_sessions:${userId}`, JSON.stringify([seedSession]));
      localStorage.setItem('se_current', restored);
    }
  }
  const nativeFetch = window.fetch.bind(window);
  const requests = [];
  const channels = [];
  const faults = [];
  const terminalDocs = [];
  let sequence = 0;
  const snapshot = () => ({ requests, faults, terminalDocs,
    channels: channels.map(({ controller, signal, onAbort, ...record }) => record) });
  window.__componentSmoke = {
    snapshot,
    emit(kind, sid, event, { close = false, fragment = false } = {}) {
      const targets = channels.filter((c) => c.kind === kind && c.sid === sid && c.active);
      if (!targets.length) throw new Error(`No active ${kind} SSE channel for ${sid}`);
      if (event.type === 'ingest_done') terminalDocs.push(event.data.doc_id);
      const encoder = new TextEncoder();
      for (const c of targets) {
        // Invoke streams are run-scoped: announce the run once, then tag run events with run_id/event_index.
        let payload = event;
        if (kind === 'invoke' && !String(event.type || '').startsWith('ingest')) {
          if (!c.runId) {
            c.runId = `smoke-run-${++sequence}`;
            c.nextIndex = 0;
            c.controller.enqueue(encoder.encode(`data: ${JSON.stringify({ type: 'run_started', agent: 'supervisor',
              message: 'Starting', run_id: c.runId, data: { status: 'started', workflow_status: 'RUNNING' } })}\n\n`));
          }
          payload = { ...event, run_id: c.runId, event_index: ++c.nextIndex };
        }
        const bytes = encoder.encode(`data: ${JSON.stringify(payload)}\n\n`);
        c.events.push({ sequence: ++sequence, type: event.type, docId: event.data?.doc_id });
        // Split framing too: parser must retain incomplete JSON and a trailing newline.
        if (fragment) {
          c.controller.enqueue(bytes.slice(0, 7));
          c.controller.enqueue(bytes.slice(7, bytes.length - 1));
          c.controller.enqueue(bytes.slice(bytes.length - 1));
        } else c.controller.enqueue(bytes);
        if (close) {
          c.active = false;
          c.closed = true;
          c.signal?.removeEventListener('abort', c.onAbort);
          c.controller.close();
        }
      }
      return targets.length;
    },
  };
  window.fetch = function smokeFetch(input, options = {}) {
    const url = new URL(typeof input === 'string' || input instanceof URL ? String(input) : input.url, location.href);
    const match = url.pathname.match(/^\/api\/decks\/([^/]+)\/(stream|invoke)$/);
    if (!match) return nativeFetch(input, options);
    const sid = decodeURIComponent(match[1]);
    const kind = match[2];
    const method = (options.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();
    const headers = Object.fromEntries(new Headers(options.headers || (input instanceof Request ? input.headers : {})));
    const signal = options.signal || (input instanceof Request ? input.signal : undefined);
    let body = null;
    try { body = options.body ? JSON.parse(options.body) : null; } catch { faults.push('Invalid SSE request JSON'); }
    const record = { sequence: ++sequence, kind, sid, method, url: url.href, headers, body,
      terminalDocsAtRequest: [...terminalDocs], abortSupported: !!signal };
    requests.push(record);
    if (url.origin !== apiOrigin || url.search || !authenticated
      || headers.authorization !== `Bearer ${token}`
      || method !== (kind === 'invoke' ? 'POST' : 'GET')
      || !['smoke-restored', 'smoke-empty'].includes(sid)
      || (kind === 'invoke' && (!body || typeof body.message !== 'string' || !/^[0-9a-f-]{36}$/.test(body.client_request_id || '')))) {
      faults.push(`Unexpected SSE request: ${method} ${url.href}`);
      return Promise.reject(new Error('Blocked unexpected smoke SSE request'));
    }
    if (signal?.aborted) return Promise.reject(new DOMException('Aborted', 'AbortError'));
    const channel = { kind, sid, active: true, aborted: false, closed: false, events: [], signal };
    const stream = new ReadableStream({
      start(controller) {
        channel.controller = controller;
        channel.onAbort = () => {
          if (!channel.active) return;
          channel.active = false;
          channel.aborted = true;
          signal.removeEventListener('abort', channel.onAbort);
          controller.error(new DOMException('Aborted', 'AbortError'));
        };
        signal?.addEventListener('abort', channel.onAbort, { once: true });
      },
      cancel() {
        channel.active = false;
        channel.closed = true;
        signal?.removeEventListener('abort', channel.onAbort);
      },
    });
    channels.push(channel);
    return Promise.resolve(new Response(stream, {
      status: 200, headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
    }));
  };
}

async function makeHarness(browser, authenticated, roles = PROFILE.roles) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 },
    serviceWorkers: 'block', acceptDownloads: true, colorScheme: 'dark' });
  const state = {
    label: authenticated ? 'authenticated' : 'login', authenticated,
    profile: { ...PROFILE, roles },
    analytics: { mode: 'data', dashboardStatus: 200, projectsStatus: 200, detailStatus: 200,
      dashboardGate: null, detailGate: null, emptyProjects: false, allowedErrors: new Set(), rows: analyticsProjectFixtures() },
    errors: [], pageErrors: [], consoleErrors: [], api: [], blob: [], downloads: [],
    registrations: [], completes: [], created: [], blobGate: deferred(),
    projects: new Map([
      [RESTORED, fixtureSession(RESTORED, 'Restored smoke project', 'done')],
      [EMPTY, fixtureSession(EMPTY, 'Empty smoke project')],
    ]),
    docs: new Map([[RESTORED, [{ session_id: RESTORED, doc_id: 'restored-source', version: 1,
      filename: 'Restored source handbook.pdf', mime_type: 'application/pdf', status: 'indexed',
      chunks: 7, uploaded_at: NOW, is_latest: true }]], [EMPTY, []]]),
    messages: new Map([[RESTORED, [
      { id: 1, session_id: RESTORED, role: 'user', content: 'Review my training source.', created_at: NOW },
      { id: 2, session_id: RESTORED, role: 'assistant', content: MARKDOWN, created_at: NOW,
        result: { deck_updated: true, deck_version: 1, verdict: 'approve' } },
    ]], [EMPTY, []]]),
    versions: new Map([[RESTORED, [fixtureVersion(1, 'Restored bilingual deck')]], [EMPTY, []]]),
    feedback: new Map(), feedbackPosts: [], feedbackGates: new Map(),
    history: historyFixture(), messageLimits: [], failNextMessages: false,
    sasRequests: [], blobCors: true, allowedConsole: [],
  };
  const cors = {
    'access-control-allow-origin': BASE_URL.origin,
    'access-control-allow-methods': 'GET, POST, PUT, OPTIONS',
    'access-control-allow-headers': 'authorization, content-type, x-ms-blob-type',
  };
  async function json(route, body, status = 200) {
    await route.fulfill({ status, contentType: 'application/json', headers: cors, body: JSON.stringify(body) });
  }
  function guarded(handler) {
    return async (route) => {
      try { await handler(route); } catch (error) {
        state.errors.push(`${route.request().method()} ${route.request().url()}: ${error.stack || error}`);
        try { await json(route, { detail: 'Smoke mock rejected request' }, 599); } catch { /* context closing */ }
      }
    };
  }
  // Routes are LIFO: specific API/blob routes below win over this static-only firewall.
  await context.route('**/*', guarded(async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin === 'https://fonts.googleapis.com' && request.resourceType() === 'stylesheet') {
      warn('Google Fonts stylesheet mocked; theme/layout tests use the system-font fallback.');
      return route.fulfill({ status: 200, contentType: 'text/css', body: '/* offline smoke fixture */' });
    }
    const staticType = ['script', 'stylesheet', 'image', 'font', 'manifest'].includes(request.resourceType());
    const entry = request.resourceType() === 'document' && url.pathname === BASE_URL.pathname;
    assert(request.method() === 'GET' && url.origin === BASE_URL.origin && (staticType || entry),
      `Unexpected network request blocked: ${request.resourceType()} ${request.method()} ${url.href}`);
    assert(!url.pathname.startsWith('/api'), 'API must never pass through static firewall');
    await route.continue();
  }));
  await context.route('**/api/**', guarded(async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    state.api.push({ method, path: url.pathname + url.search });
    assert.equal(url.origin, API_ORIGIN, 'All app API calls must use the synthetic backend');
    if (method === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
    const pathname = url.pathname.slice(4);
    if (method === 'GET' && pathname === '/health') return json(route, { status: 'ok' });
    assert(authenticated, 'Signed-out page must not attempt authentication/backend operations');
    if (ANALYTICS_ONLY && method === 'POST' && pathname === '/auth/refresh') {
      assert.equal(request.headers().authorization, undefined);
      assert.deepEqual(request.postDataJSON(), { refresh_token: 'synthetic-smoke-refresh-not-a-real-token' });
      return json(route, { detail: 'Fixture expired session' }, 401);
    }
    assert.equal(request.headers().authorization, `Bearer ${TOKEN}`);
    if (method === 'GET' && pathname === '/auth/validate') {
      return json(route, { data: { user: state.profile, should_refresh: false } });
    }
    if (method === 'GET' && pathname === '/auth/me') return json(route, { data: state.profile });
    if (ANALYTICS_ONLY && method === 'POST' && pathname === '/auth/logout') return json(route, { status: 'ok' });
    if (method === 'GET' && pathname === '/approvals') {
      assert(['', '?status=pending'].includes(url.search), 'Unexpected approvals filter');
      return json(route, []);
    }
    if (method === 'GET' && pathname === '/analytics/dashboard') {
      assert(Date.parse(url.searchParams.get('from')) < Date.parse(url.searchParams.get('to')), 'Dashboard requires a valid window');
      const status = state.analytics.dashboardStatus;
      const body = status === 200 ? analyticsFixture(url.searchParams, state.analytics.mode) : { detail: 'Fixture analytics validation or access error' };
      const gate = state.analytics.dashboardGate;
      state.analytics.dashboardGate = null;
      if (gate) { gate.arrived.resolve(); await gate.release.promise; }
      return json(route, body, status);
    }
    if (method === 'GET' && pathname === '/analytics/projects') {
      assert(!url.searchParams.has('agent') && !url.searchParams.has('model') && !url.searchParams.has('tz'), 'Breakdown filters cannot leak into projects');
      if (state.analytics.projectsStatus !== 200) return json(route, { detail: 'Fixture projects error' }, state.analytics.projectsStatus);
      const limit = Number(url.searchParams.get('limit'));
      const offset = Number(url.searchParams.get('offset'));
      assert(limit > 0 && limit <= 200 && offset >= 0);
      assert(['last_active', 'turns', 'cost', 'tokens', 'duration'].includes(url.searchParams.get('sort')));
      assert(['asc', 'desc'].includes(url.searchParams.get('order')));
      const sortKey = { last_active: 'last_active', turns: 'turns', cost: 'cost_usd', tokens: 'total_tokens', duration: 'ai_turnaround_ms' }[url.searchParams.get('sort')];
      const direction = url.searchParams.get('order') === 'asc' ? 1 : -1;
      const rows = state.analytics.emptyProjects ? [] : [...state.analytics.rows].sort((first, second) => {
        if (first[sortKey] == null) return second[sortKey] == null ? 0 : 1;
        if (second[sortKey] == null) return -1;
        return (first[sortKey] < second[sortKey] ? -1 : first[sortKey] > second[sortKey] ? 1 : 0) * direction;
      });
      return json(route, { items: rows.slice(offset, offset + limit), pagination: { total: rows.length, limit, offset } });
    }
    if (method === 'GET' && pathname.startsWith('/analytics/projects/')) {
      assert.equal(url.search, '', 'Project detail is all-time, not date-window filtered');
      if (state.analytics.detailStatus !== 200) return json(route, { detail: 'Fixture project detail error' }, state.analytics.detailStatus);
      const sessionId = decodeURIComponent(pathname.slice('/analytics/projects/'.length));
      assert(state.analytics.rows.some((project) => project.session_id === sessionId), 'Unknown project detail');
      const gate = state.analytics.detailGate;
      state.analytics.detailGate = null;
      if (gate) { gate.arrived.resolve(); await gate.release.promise; }
      return json(route, analyticsDetailFixture(sessionId));
    }
    if (method === 'GET' && pathname === '/decks/sessions') {
      return json(route, { sessions: [...state.projects.values()].map((s) => ({
        session_id: s.id, title: s.title, created_at: s.createdAt,
      })) });
    }
    if (method === 'POST' && pathname === '/decks/sessions') {
      const body = request.postDataJSON();
      assert.deepEqual(Object.keys(body).sort(), ['session_id', 'title']);
      assert.match(body.session_id, /^web-[a-z0-9]+$/);
      assert.equal(body.title, NEW_TITLE);
      assert.equal(state.created.length, 0, 'Only the explicitly confirmed project may be registered');
      state.created.push(body);
      state.projects.set(body.session_id, fixtureSession(body.session_id, body.title));
      state.docs.set(body.session_id, []);
      state.messages.set(body.session_id, []);
      state.versions.set(body.session_id, []);
      return json(route, body);
    }
    const match = pathname.match(/^\/decks\/([^/]+)\/(.+)$/);
    assert(match, `Unmocked/destructive API blocked: ${method} ${pathname}`);
    const [, sid, endpoint] = match;
    assert(state.projects.has(sid), `Unknown fixture session: ${sid}`);
    if (MESSAGES_ONLY && method === 'GET' && endpoint === 'messages') {
      const limit = url.searchParams.get('limit');
      state.messageLimits.push({ sid, limit });
      if (sid === EMPTY) return json(route, { session_id: sid, messages: state.messages.get(sid) }); // legacy: no timeline
      if (state.failNextMessages) {
        state.failNextMessages = false;
        return json(route, { detail: 'Fixture history outage' }, 500);
      }
      return json(route, messagesResponse(sid, state.history, Number(limit) || 0));
    }
    if (method === 'GET' && endpoint === 'runs/current') return json(route, { session_id: sid, active: null, latest: null });
    if (method === 'GET' && endpoint === 'messages') return json(route, { messages: state.messages.get(sid) });
    if (method === 'GET' && endpoint === 'documents') return json(route, { documents: state.docs.get(sid) });
    if (method === 'GET' && endpoint === 'versions') return json(route, { versions: state.versions.get(sid) });
    const downloadUrl = endpoint.match(/^versions\/(\d+)\/download-url$/);
    if (method === 'GET' && downloadUrl) {
      const version = Number(downloadUrl[1]);
      const language = url.searchParams.get('language');
      assert.equal(sid, RESTORED);
      assert(version === 1 ? language === 'French'
        : THOUGHT_PROCESS_ONLY && version === 2 && ['English', 'French'].includes(language), 'Unexpected deck download');
      state.sasRequests.push({ sid, version, language });
      return json(route, { session_id: sid, deck_version: version, language, expires_in: 900,
        url: `${BLOB_ORIGIN}/decks/${encodeURIComponent(sid)}/v${version}/${encodeURIComponent(language)}.pptx${SAS_QUERY}` });
    }
    if (method === 'POST' && endpoint === 'documents/register') {
      assert.equal(sid, EMPTY);
      assert.equal(state.registrations.length, 0, 'Duplicate upload registration');
      const body = request.postDataJSON();
      assert.deepEqual(body, { files: UPLOAD_FILES.map((f) => ({ filename: f.name, size: f.buffer.length })) });
      state.registrations.push(body);
      return json(route, { uploads: UPLOAD_FILES.map((f, i) => ({
        filename: f.name, doc_id: `uploaded-source-${i + 1}`, version: 1,
        upload_url: `${BLOB_ORIGIN}/upload/${i + 1}?sig=synthetic-only`,
      })) });
    }
    if (method === 'POST' && endpoint === 'documents/complete') {
      assert.equal(sid, EMPTY);
      const body = request.postDataJSON();
      assert.equal(body.documents?.length, 1, 'Complete is called separately for each uploaded blob');
      const docId = body.documents[0].doc_id;
      const index = ['uploaded-source-1', 'uploaded-source-2'].indexOf(docId);
      assert(index >= 0, 'Unknown completed document');
      assert.deepEqual(body, { documents: [{ doc_id: docId, version: 1 }] });
      assert(!state.completes.some((c) => c.documents[0].doc_id === docId), 'Duplicate complete');
      assert(state.blob.some((b) => b.index === index && b.released), 'Complete occurred before blob success');
      state.completes.push(body);
      const doc = { session_id: sid, doc_id: docId, version: 1, filename: UPLOAD_FILES[index].name,
        status: 'uploaded', mime_type: UPLOAD_FILES[index].mimeType, is_latest: true, uploaded_at: NOW };
      state.docs.get(sid).push(doc);
      return json(route, { documents: [doc] });
    }
    const feedbackMatch = endpoint.match(/^versions\/(\d+)\/feedback$/);
    if (feedbackMatch) {
      const version = Number(feedbackMatch[1]);
      const key = `${sid}|${version}`;
      if (method === 'GET') {
        const gate = state.feedbackGates.get(key);
        state.feedbackGates.delete(key);
        if (gate) { gate.arrived.resolve(); await gate.release.promise; }
        // Other scopes get a stored rating so the unsubmitted 404 never reaches their console checks.
        const stored = state.feedback.get(key)
          || (DECK_FEEDBACK_ONLY ? null : { session_id: sid, deck_version: version, thumbs_up: true, comment: null });
        return stored ? json(route, stored) : json(route, { detail: 'deck feedback not found' }, 404);
      }
      if (method === 'POST') {
        assert(DECK_FEEDBACK_ONLY, 'Feedback is only submitted by the deck-feedback scope');
        assert.equal(request.headers()['content-type'], 'application/json');
        const body = request.postDataJSON();
        state.feedbackPosts.push({ key, body });
        if (state.feedback.has(key)) return json(route, { detail: 'feedback already submitted for deck version' }, 409);
        const stored = { session_id: sid, deck_version: version, thumbs_up: body.thumbs_up, comment: body.comment ?? null };
        state.feedback.set(key, stored);
        return json(route, stored, 201);
      }
    }
    assert.fail(`Unmocked/destructive API blocked: ${method} ${pathname}`);
  }));
  await context.route(`${BLOB_ORIGIN}/**`, guarded(async (route) => {
    const request = route.request();
    if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
    const url = new URL(request.url());
    if (request.method() === 'GET') {
      assert.equal(request.headers().authorization, undefined, 'SAS download must NEVER carry bearer authorization');
      assert.equal(url.search, SAS_QUERY);
      const file = url.pathname.match(/^\/decks\/([^/]+)\/v(\d+)\/([^/]+)\.pptx$/);
      assert(file, 'Unknown blob download URL');
      state.downloads.push({ sid: decodeURIComponent(file[1]), language: decodeURIComponent(file[3]), version: Number(file[2]) });
      return route.fulfill({ status: 200, contentType: PPTX_MIME, body: DOWNLOAD_BYTES,
        headers: state.blobCors ? cors : {} });
    }
    assert.equal(request.method(), 'PUT');
    assert.equal(url.search, '?sig=synthetic-only');
    const index = ['/upload/1', '/upload/2'].indexOf(url.pathname);
    assert(index >= 0, 'Unknown blob URL');
    assert(!state.blob.some((b) => b.index === index), 'Unexpected blob retry');
    assert.equal(request.headers().authorization, undefined, 'SAS upload must NEVER carry bearer authorization');
    assert.equal(request.headers()['x-ms-blob-type'], 'BlockBlob');
    assert.equal(request.headers()['content-type'], UPLOAD_FILES[index].mimeType);
    assert.deepEqual(request.postDataBuffer(), UPLOAD_FILES[index].buffer, 'Blob bytes changed');
    const record = { index, headers: request.headers(), released: false };
    state.blob.push(record);
    // The test queues a chat message while both actual XHR PUT responses are held.
    await state.blobGate.promise;
    record.released = true;
    await route.fulfill({ status: 201, headers: cors, body: '' });
  }));
  await context.routeWebSocket('**/*', (socket) => {
    state.errors.push(`Unexpected WebSocket blocked: ${socket.url()}`);
    socket.close();
  });
  await context.addInitScript(browserInit, { authenticated, origin: BASE_URL.origin,
    apiOrigin: API_ORIGIN, token: TOKEN, userId: USER_ID, restored: RESTORED, profile: state.profile,
    seedSession: fixtureSession(RESTORED, 'Restored smoke project', 'done') });
  context.on('page', (p) => {
    p.on('pageerror', (error) => state.pageErrors.push(error.stack || error.message));
    p.on('console', (message) => {
      const expectedStatus = message.text().match(/Failed to load resource: the server responded with a status of (\d+)/);
      if (message.type() === 'error' && !(expectedStatus && state.analytics.allowedErrors.has(Number(expectedStatus[1])))
        && !state.allowedConsole.some((pattern) => pattern.test(message.text()))) state.consoleErrors.push(message.text());
      if (message.type() === 'warning') warn(`Browser: ${message.text()}`);
    });
    p.on('dialog', async (dialog) => {
      state.errors.push(`Unexpected native dialog: ${dialog.type()} ${dialog.message()}`);
      await dialog.dismiss();
    });
  });
  const page = await context.newPage();
  page.setDefaultTimeout(12000);
  page.setDefaultNavigationTimeout(30000);
  return { context, page, state };
}

async function snapshot(h) { return h.page.evaluate(() => window.__componentSmoke?.snapshot()); }
async function clean(h) {
  assert.deepEqual(h.state.errors, [], 'Unexpected API/network or mock assertion failures');
  assert.deepEqual(h.state.pageErrors, [], 'Uncaught browser page errors');
  assert.deepEqual(h.state.consoleErrors, [], 'Browser console errors');
  const transport = await snapshot(h);
  assert(transport, 'Browser transport fixture was not installed');
  assert.deepEqual(transport.faults, [], 'SSE transport contract failures');
}
async function step(h, name, action) {
  currentStep = `${h.state.label}: ${name}`;
  log(`START ${currentStep}`);
  await action();
  await clean(h);
  log(`PASS  ${currentStep}`);
}
async function go(h) {
  const url = new URL(BASE_URL);
  // Explicit override also protects against a production backend baked into the bundle.
  url.searchParams.set('backend', API_ORIGIN);
  const response = await h.page.goto(url.href, { waitUntil: 'domcontentloaded' });
  assert.equal(response.status(), 200, 'Frontend must already be served');
}
async function theme(h, value, button) {
  const { page } = h;
  const before = await page.locator('html').getAttribute('data-theme');
  if ((before === 'light' ? 'light' : 'dark') !== value) await page.locator(button).click();
  await eventually(`theme ${value}`, async () => {
    assert.equal(await page.locator('html').getAttribute('data-theme'), value);
    assert.equal(await page.evaluate(() => localStorage.getItem('se_theme')), value);
  });
  // Wait out CSS transitions by checking the computed colors against theme tokens.
  await page.waitForFunction(() => {
    const root = getComputedStyle(document.documentElement);
    const body = getComputedStyle(document.body);
    const rgb = (hex) => {
      const n = parseInt(hex.trim().replace('#', ''), 16);
      return `rgb(${n >> 16}, ${(n >> 8) & 255}, ${n & 255})`;
    };
    return body.color === rgb(root.getPropertyValue('--text'))
      && body.backgroundColor === rgb(root.getPropertyValue('--bg'));
  });
  const colors = await page.locator('body').evaluate((el) => {
    const style = getComputedStyle(el);
    return [style.color, style.backgroundColor];
  });
  function luminance(color) {
    const components = color.match(/[\d.]+/g).slice(0, 3).map(Number).map((v) => v / 255)
      .map((v) => v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
    return components.reduce((sum, c, i) => sum + c * [0.2126, 0.7152, 0.0722][i], 0);
  }
  const luminances = colors.map(luminance).sort((a, b) => a - b);
  assert((luminances[1] + 0.05) / (luminances[0] + 0.05) >= 4.5, 'Body text contrast must be readable');
  return colors;
}
async function tab(page, name) { await page.locator('.topbar .tabs').getByRole('button', { name, exact: true }).click(); }
function sessionRow(page, title) { return page.locator('#session-list .session-item').filter({ hasText: title }); }
function pipelineRow(page, title) { return page.locator('#pipeline-body tr').filter({ hasText: title }); }
function sourceRow(page, filename) { return page.locator('#input-list .doc-row').filter({ hasText: filename }); }
async function emit(h, kind, sid, event, options = {}) {
  const delivered = await h.page.evaluate(({ kind, sid, event, options }) =>
    window.__componentSmoke.emit(kind, sid, event, options), { kind, sid, event, options });
  assert.equal(delivered, 1, 'Exactly one UI-owned stream subscription should receive each fixture event');
}
async function invokeCount(h, expected) {
  const data = await snapshot(h);
  assert.equal(data.requests.filter((r) => r.kind === 'invoke').length, expected);
}
async function fileChooser(page, button) {
  const [chooser] = await Promise.all([page.waitForEvent('filechooser'), button.click()]);
  assert.equal(await chooser.element().getAttribute('id'), 'file-input');
  assert.equal(await chooser.element().evaluate((el) => el.hidden && el.type === 'file'), true);
  assert(chooser.isMultiple(), 'Shared hidden input must allow multiple files');
  return chooser;
}

async function testLogin(h) {
  const { page } = h;
  await step(h, 'login renders without initiating real authentication', async () => {
    await go(h);
    await page.locator('#login-view').waitFor({ state: 'visible' });
    await textIs(page.locator('.login-title'), 'Welcome back');
    assert(await page.locator('#sso-login-btn').isEnabled());
    assert.equal(await page.locator('#app-view').count(), 0);
  });
  await step(h, 'login light/dark theme and reload persistence', async () => {
    const dark = await theme(h, 'dark', '#theme-toggle-login');
    const light = await theme(h, 'light', '#theme-toggle-login');
    assert.notDeepEqual(dark, light);
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.locator('#login-view').waitFor({ state: 'visible' });
    assert.equal(await page.locator('html').getAttribute('data-theme'), 'light');
    await theme(h, 'dark', '#theme-toggle-login');
    assert.equal(h.state.api.filter((r) => r.path.startsWith('/api/auth/')).length, 0);
  });
}

async function testAuthenticated(h) {
  const { page, state } = h;
  await step(h, 'bootstrap restores project, server chat, sources and outputs', async () => {
    await go(h);
    await page.locator('#app-view').waitFor({ state: 'visible' });
    await textIncludes(page.locator('#user-chip'), 'Smoke Admin · admin');
    await page.locator('#conn-dot.ok').waitFor();
    await textIncludes(sessionRow(page, 'Restored smoke project'), 'done · v1');
    await sessionRow(page, 'Empty smoke project').waitFor(); // This project comes only from /sessions.
    await textIs(sourceRow(page, 'Restored source handbook.pdf').locator('.doc-status'), 'indexed');
    assert.match(await sourceRow(page, 'Restored source handbook.pdf').locator('span').first().getAttribute('title'),
      /^Restored source handbook\.pdf\nUploaded /);
    await textIncludes(page.locator('#output-list'), 'Restored bilingual deck');
    assert(await page.locator('#chat-send-btn').isEnabled());
    assert(state.api.some((r) => r.path === '/api/auth/validate'));
  });
  await step(h, 'markdown DOM and both authenticated themes', async () => {
    const message = page.locator('#messages .msg-assistant').first();
    await textIs(message.locator('h4'), 'Restored training summary');
    await textIs(message.locator('strong'), 'Verified');
    await textIs(message.locator('em'), 'readable');
    await textIs(message.locator('.md-code'), 'safe_code');
    assert.equal(await message.locator('ul li').count(), 2);
    await textIs(message.locator('blockquote'), 'Fixture quotation');
    await textIs(message.locator('pre code'), '<fixture> & code');
    assert.equal(await message.locator('pre fixture').count(), 0, 'Code block HTML must remain escaped');
    const link = message.getByRole('link', { name: 'Reference' });
    assert.equal(await link.getAttribute('href'), 'https://example.invalid/smoke-reference');
    assert.equal(await link.getAttribute('rel'), 'noopener noreferrer');
    const dark = await theme(h, 'dark', '#theme-toggle-app');
    const light = await theme(h, 'light', '#theme-toggle-app');
    assert.notDeepEqual(dark, light);
    assert(await sourceRow(page, 'Restored source handbook.pdf').isVisible());
    assert(await message.isVisible());
    await theme(h, 'dark', '#theme-toggle-app');
  });
  await step(h, 'language-specific output download stays intercepted', async () => {
    const [download] = await Promise.all([
      page.waitForEvent('download'),
      page.locator('#output-list').getByRole('button', { name: 'French', exact: true }).click(),
    ]);
    assert.equal(download.suggestedFilename(), `${RESTORED}_v1_French.pptx`);
    assert.equal(await download.failure(), null);
    assert.deepEqual(fs.readFileSync(await download.path()), DOWNLOAD_BYTES);
    assert.deepEqual(state.downloads, [{ sid: RESTORED, language: 'French', version: 1 }]);
  });
  await step(h, 'persistent tab mounting, draft text/height, Pipeline Open and Analytics', async () => {
    const composer = page.locator('#chat-input');
    const draft = 'Persistent draft\nSecond line\nThird line\nFourth line\nFifth line';
    await composer.fill(draft);
    const oldElement = await composer.elementHandle();
    await eventually('expanded composer', async () => assert((await composer.boundingBox()).height > 44));
    const height = (await composer.boundingBox()).height;
    const styleHeight = await composer.evaluate((el) => el.style.height);
    await tab(page, 'Pipeline');
    await page.locator('#tab-chat').waitFor({ state: 'hidden' });
    await page.locator('#tab-pipeline').waitFor({ state: 'visible' });
    const row = pipelineRow(page, 'Restored smoke project');
    await textIs(row.locator('.status-pill'), 'Done');
    assert(await row.getByRole('button', { name: 'Download', exact: true }).isEnabled());
    await row.getByRole('button', { name: 'Open', exact: true }).click();
    await page.locator('#tab-chat').waitFor({ state: 'visible' });
    await page.locator('#tab-pipeline').waitFor({ state: 'hidden' });
    await tab(page, 'Analytics');
    await page.locator('#tab-chat').waitFor({ state: 'hidden' });
    for (const [metric, value] of Object.entries({ 'active-users': '42', projects: '205',
      'successful-projects': '61', slides: '540', turnaround: '18.5 s', 'total-cost': '$12.87' })) {
      await textIs(page.locator(`[data-metric="${metric}"] .metric-value`), value);
    }
    await tab(page, 'Generate');
    await page.locator('#tab-analytics').waitFor({ state: 'hidden' });
    await page.locator('#tab-chat').waitFor({ state: 'visible' });
    assert.equal(await composer.inputValue(), draft);
    assert.equal(await composer.evaluate((el, old) => el === old, oldElement), true, 'Tab switch recreated the composer');
    assert.equal(await composer.evaluate((el) => el.style.height), styleHeight);
    assert(Math.abs((await composer.boundingBox()).height - height) <= 1, 'Draft height was lost');
    await oldElement.dispose();
  });
  await step(h, 'empty project suggestion and sidebar/composer share one hidden input', async () => {
    await tab(page, 'Pipeline');
    await pipelineRow(page, 'Empty smoke project').getByRole('button', { name: 'Open', exact: true }).click();
    await page.locator('.chat-welcome').waitFor({ state: 'visible' });
    await textIncludes(page.locator('#input-list'), 'No sources yet.');
    assert.equal(await page.evaluate(() => localStorage.getItem('se_current')), EMPTY);
    assert(await page.locator('#chat-send-btn').isDisabled());
    await page.getByRole('button', { name: 'Technical Content Deck' }).click();
    const composer = page.locator('#chat-input');
    await eventually('suggestion populates and focuses expanded composer', async () => {
      assert.match(await composer.inputValue(), /^Turn the provided technical documentation into a professional PowerPoint deck/);
      assert((await composer.inputValue()).includes('presentation-ready.'));
      assert(await composer.evaluate((el) => el === document.activeElement));
      assert((await composer.boundingBox()).height > 44);
    });
    await composer.press('Enter');
    await invokeCount(h, 0); // Keyboard submit is also gated until a source exists.
    const sidebarChooser = await fileChooser(page, page.locator('#add-source-link'));
    const sharedInput = sidebarChooser.element();
    await sidebarChooser.setFiles([]); // Cancel-equivalent: no upload and no project creation.
    const plusChooser = await fileChooser(page, page.getByRole('button', { name: 'Add a source', exact: true }));
    assert.equal(await plusChooser.element().evaluate((el, other) => el === other, sharedInput), true);
    assert.equal(await page.locator('input[type=file]').count(), 1);
    await plusChooser.setFiles(UPLOAD_FILES);
    await sharedInput.dispose();
  });
  await step(h, 'queue once while blob PUT is held; validate register and bearer-free uploads', async () => {
    await eventually('two intercepted blob PUTs', async () => {
      assert.equal(state.blob.length, 2);
      await clean(h);
    });
    assert.equal(state.registrations.length, 1);
    assert.equal(state.completes.length, 0);
    const transport = await snapshot(h);
    assert.equal(transport.requests.filter((r) => r.kind === 'stream').length, 1);
    assert(transport.requests.find((r) => r.kind === 'stream').abortSupported);
    for (const file of UPLOAD_FILES) await textIncludes(sourceRow(page, file.name), 'uploading');
    await page.locator('#chat-input').fill(QUEUED_MESSAGE);
    assert(await page.locator('#chat-send-btn').isEnabled(), 'Busy composer must accept one queued message');
    await page.locator('#chat-send-btn').click();
    await textIs(page.locator('#pending-bar .pending-msg-text'), QUEUED_MESSAGE);
    assert(await page.locator('#chat-send-btn').isDisabled());
    assert.equal(await page.locator('#chat-input').inputValue(), '');
    await invokeCount(h, 0);
    // Exercise keyboard double submission: it must not replace or duplicate the queue.
    await page.locator('#chat-input').fill('Do not send this duplicate');
    await page.locator('#chat-input').press('Enter');
    await textIncludes(page.locator('#toast-host'), 'A message is already queued');
    assert.equal(await page.locator('#pending-bar .pending-msg').count(), 1);
    await page.locator('#chat-input').fill('');
    await tab(page, 'Pipeline');
    await tab(page, 'Generate');
    await textIs(page.locator('#pending-bar .pending-msg-text'), QUEUED_MESSAGE);
    await invokeCount(h, 0);
    state.blobGate.resolve();
  });
  await step(h, 'complete payloads and ingestion terminal events gate exactly one invoke', async () => {
    await eventually('both complete calls', async () => assert.equal(state.completes.length, 2));
    for (const file of UPLOAD_FILES) await textIs(sourceRow(page, file.name).locator('.doc-status'), 'processing');
    await invokeCount(h, 0); // Successful PUT/complete alone must NOT flush chat.
    await emit(h, 'stream', EMPTY, { type: 'ingest_step', agent: 'ingest', message: 'Indexing smoke sources' }, { fragment: true });
    await textIncludes(page.locator('#input-status'), 'Indexing smoke sources');
    const first = state.docs.get(EMPTY).find((d) => d.doc_id === 'uploaded-source-1');
    Object.assign(first, { status: 'indexed', chunks: 3 });
    await emit(h, 'stream', EMPTY, { type: 'ingest_done', agent: 'ingest', message: 'First source indexed',
      data: { doc_id: first.doc_id, status: 'indexed', chunks: 3 } }, { fragment: true });
    await textIs(sourceRow(page, UPLOAD_FILES[0].name).locator('.doc-status'), 'indexed');
    await textIs(sourceRow(page, UPLOAD_FILES[1].name).locator('.doc-status'), 'processing');
    await invokeCount(h, 0); // Still waiting on the second document.
    assert(await page.locator('#pending-bar').isVisible());
    const second = state.docs.get(EMPTY).find((d) => d.doc_id === 'uploaded-source-2');
    Object.assign(second, { status: 'indexed', chunks: 4 });
    await emit(h, 'stream', EMPTY, { type: 'ingest_done', agent: 'ingest', message: 'Second source indexed',
      data: { doc_id: second.doc_id, status: 'indexed', chunks: 4 } });
    await eventually('one invoke after terminal ingestion', () => invokeCount(h, 1));
    const transport = await snapshot(h);
    const invoke = transport.requests.find((r) => r.kind === 'invoke');
    assert.equal(invoke.sid, EMPTY);
    assert.equal(invoke.body.message, QUEUED_MESSAGE);
    assert.match(invoke.body.client_request_id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    assert.deepEqual(invoke.terminalDocsAtRequest, ['uploaded-source-1', 'uploaded-source-2']);
    assert.equal(transport.channels.find((c) => c.kind === 'stream').aborted, true,
      'Ingestion stream must settle on AbortSignal, not hang after terminal events');
    assert.equal(transport.channels.filter((c) => c.kind === 'stream' && c.active).length, 0);
    await page.locator('#pending-bar').waitFor({ state: 'hidden' });
    await textIs(page.locator('#messages .msg-user').last(), QUEUED_MESSAGE);
    for (const file of UPLOAD_FILES) await textIs(sourceRow(page, file.name).locator('.doc-status'), 'indexed');
  });
  await step(h, 'live SSE survives tabs and actual done.data.result renders assistant/deck', async () => {
    await emit(h, 'invoke', EMPTY, { type: 'agent_start', agent: 'planner', message: 'Planning smoke deck' }, { fragment: true });
    await textIncludes(page.locator('.thought-group'), 'Planning smoke deck');
    await emit(h, 'invoke', EMPTY, { type: 'agent_step', agent: 'slide', message: 'Slide 1/1 complete',
      data: { slide_number: 1, total: 1, phase: 'done' } });
    await page.locator('.slide-chip.stage-done[data-index="1"]').waitFor();
    await tab(page, 'Pipeline');
    await textIs(pipelineRow(page, 'Empty smoke project').locator('.status-pill'), 'Running');
    await tab(page, 'Generate');
    await page.locator('.slide-chip.stage-done[data-index="1"]').waitFor();
    const result = { reply: '## Generated smoke reply\n\n**Both sources indexed** — deck ready.',
      deck_updated: true, deck_version: 2, verdict: 'approve' };
    state.versions.set(EMPTY, [fixtureVersion(2, 'Generated smoke deck')]);
    state.messages.set(EMPTY, [
      { id: 3, session_id: EMPTY, role: 'user', content: QUEUED_MESSAGE, created_at: NOW },
      { id: 4, session_id: EMPTY, role: 'assistant', content: result.reply, result, created_at: NOW },
    ]);
    await emit(h, 'invoke', EMPTY, { type: 'done', agent: 'supervisor', message: 'Finished', data: { result } },
      { close: true, fragment: true });
    await textIs(page.locator('#messages .msg-assistant h4'), 'Generated smoke reply');
    await textIs(page.locator('#messages .msg-assistant strong'), 'Both sources indexed');
    await textIncludes(sessionRow(page, 'Empty smoke project'), 'done · v2');
    await textIncludes(page.locator('#output-list'), 'Generated smoke deck');
    await page.locator('.thought-group').waitFor({ state: 'hidden' });
    await eventually('composer leaves running state', async () => {
      assert.equal(await page.locator('#chat-send-btn').getAttribute('aria-busy'), 'false');
      assert(await page.locator('#chat-send-btn').isEnabled());
    });
    await tab(page, 'Pipeline');
    await textIs(pipelineRow(page, 'Empty smoke project').locator('.status-pill'), 'Done');
    await pipelineRow(page, 'Empty smoke project').getByRole('button', { name: 'Open', exact: true }).click();
    await invokeCount(h, 1);
    assert.equal(await page.locator('#messages .msg-assistant').count(), 1, 'Duplicate terminal reply');
  });
  await step(h, 'create modal cancel/confirm and cancel project deletion from both views', async () => {
    await page.locator('#new-session-btn').click();
    await page.locator('.modal-overlay .modal-input').fill('Cancelled project');
    await page.locator('.modal-overlay [data-act=cancel]').click();
    assert.equal(state.created.length, 0);
    await page.locator('#new-session-btn').click();
    await textIncludes(page.locator('.modal-msg'), 'Name this project');
    await page.locator('.modal-input').fill(NEW_TITLE);
    await page.locator('.modal-overlay [data-act=ok]').click();
    await eventually('one registered project', async () => assert.equal(state.created.length, 1));
    await sessionRow(page, NEW_TITLE).waitFor();
    await page.locator('.chat-welcome').waitFor({ state: 'visible' });
    assert.equal(await page.evaluate(() => localStorage.getItem('se_current')), state.created[0].session_id);
    // Its accessible name is the visible ×; "Delete project" is only its title.
    await sessionRow(page, NEW_TITLE).locator('.session-delete-btn[title="Delete project"]').click();
    await textIncludes(page.locator('.modal-msg'), `Delete project "${NEW_TITLE}"?`);
    await page.locator('.modal-overlay [data-act=cancel]').click();
    await page.locator('.modal-overlay').waitFor({ state: 'hidden' });
    assert(await sessionRow(page, NEW_TITLE).isVisible());
    await tab(page, 'Pipeline');
    await pipelineRow(page, NEW_TITLE).getByRole('button', { name: 'Delete', exact: true }).click();
    await page.locator('.modal-overlay [data-act=cancel]').click();
    await page.locator('.modal-overlay').waitFor({ state: 'hidden' });
    assert(await pipelineRow(page, NEW_TITLE).isVisible());
    assert.equal(state.api.filter((r) => r.method === 'DELETE').length, 0);
    await invokeCount(h, 1);
    assert.equal((await snapshot(h)).channels.filter((c) => c.active).length, 0, 'Leaked SSE channel');
  });
}

async function testThoughtProcess(harness) {
  const { page, state } = harness;
  const thought = page.locator('.thought-turn-content').last();
  const header = thought.locator('.thought-group-header');
  const stage = header.locator('.thought-stage-status');
  const duration = header.locator('.thought-duration');
  const preview = thought.locator('.thought-preview-card');
  const startedAt = Date.now();
  const title = 'Industrial automation training: commissioning and operational readiness';

  async function checkLayouts(label) {
    fs.mkdirSync(ARTIFACT_DIR, { recursive: true });
    for (const width of [1440, 390, 320]) {
      await page.setViewportSize({ width, height: width < 600 ? 844 : 1000 });
      for (const mode of ['dark', 'light']) {
        await theme(harness, mode, '#theme-toggle-app');
        await thought.scrollIntoViewIfNeeded();
        const layout = await thought.evaluate((element) => {
          const bounds = element.getBoundingClientRect();
          const statusStyle = getComputedStyle(element.querySelector('.thought-stage-status'));
          const durationStyle = getComputedStyle(element.querySelector('.thought-duration'));
          const successStyle = getComputedStyle(element.querySelector('.lang-chip.stage-done'));
          const selectors = ['.thought-group-header', '.thought-stage-status', '.thought-duration',
            '.thought-preview-card', '.thought-preview-copy', '.thought-preview-action'];
          return {
            plainStatus: statusStyle.backgroundColor === 'rgba(0, 0, 0, 0)' && statusStyle.borderTopWidth === '0px',
            greenDuration: ['color', 'backgroundColor', 'borderTopColor']
              .every((property) => durationStyle[property] === successStyle[property]),
            greenTimerIcon: getComputedStyle(element.querySelector('.thought-duration svg')).color === successStyle.color,
            withinViewport: bounds.left >= 0 && bounds.right <= innerWidth + 1,
            overflowing: selectors.filter((selector) => {
              const child = element.querySelector(selector);
              if (!child) return false;
              const rect = child.getBoundingClientRect();
              return rect.left < bounds.left - 1 || rect.right > bounds.right + 1
                || child.scrollWidth > child.clientWidth + 1;
            }),
            headerOverlap: (() => {
              const boxes = ['.thought-group-title', '.thought-stage-status', '.thought-duration', '.thought-chevron']
                .map((selector) => element.querySelector(selector)?.getBoundingClientRect()).filter(Boolean);
              return boxes.some((box, index) => boxes.slice(index + 1).some((other) =>
                Math.min(box.right, other.right) - Math.max(box.left, other.left) > 1
                && Math.min(box.bottom, other.bottom) - Math.max(box.top, other.top) > 1));
            })(),
          };
        });
        assert(layout.plainStatus, `${label}/${mode}/${width}: status must be plain text, not a badge`);
        assert(layout.greenDuration, `${label}/${mode}/${width}: timer must use the completed green palette`);
        assert(layout.greenTimerIcon, `${label}/${mode}/${width}: timer icon must match the green duration`);
        assert(layout.withinViewport, `${label}/${mode}/${width}: thought panel outside viewport`);
        assert.deepEqual(layout.overflowing, [], `${label}/${mode}/${width}: content overflow`);
        assert.equal(layout.headerOverlap, false, `${label}/${mode}/${width}: header overlap`);
        if (width !== 320) await page.screenshot({ path: path.join(ARTIFACT_DIR, `thought-${label}-${mode}-${width}.png`) });
      }
    }
    await page.setViewportSize({ width: 1440, height: 1000 });
    await theme(harness, 'dark', '#theme-toggle-app');
  }

  await step(harness, 'live header timing and keyboard collapse', async () => {
    await go(harness);
    await page.locator('#app-view').waitFor({ state: 'visible' });
    await eventually('composer ready', async () => assert(await page.locator('#chat-send-btn').isEnabled()));
    await page.clock.setFixedTime(startedAt);
    await page.locator('#chat-input').fill('Create a three-slide training deck.');
    await page.locator('#chat-send-btn').click();
    await eventually('one generation request', () => invokeCount(harness, 1));
    await emit(harness, 'invoke', RESTORED, { type: 'agent_start', agent: 'planner', message: 'Planning the training deck' });
    await textIs(stage.locator('.thought-stage-copy'), 'Planning your deck');
    await textIs(duration, 'Calculating');
    assert.equal(await preview.count(), 0);
    await header.focus();
    await header.press('Enter');
    assert.equal(await header.getAttribute('aria-expanded'), 'false');
    assert.equal(await thought.locator('.thought-group-body').isVisible(), false);
    assert(await stage.isVisible());
    assert(await duration.isVisible());
    assert.notEqual(await stage.locator('.thought-stage-ellipsis span').first()
      .evaluate((element) => getComputedStyle(element).animationName), 'none');
    await page.emulateMedia({ reducedMotion: 'reduce' });
    assert.equal(await duration.locator('svg').evaluate((element) => getComputedStyle(element).animationName), 'none');
    assert.equal(await stage.locator('.thought-stage-ellipsis span').first()
      .evaluate((element) => getComputedStyle(element).animationName), 'none');
    await page.emulateMedia({ reducedMotion: 'no-preference' });
  });
  await step(harness, 'all in-progress stages remain visible while collapsed', async () => {
    for (const slideNumber of [1, 2, 3]) {
      await emit(harness, 'invoke', RESTORED, { type: 'agent_step', agent: 'slide',
        message: `Slide ${slideNumber}/3 complete`, data: { slide_number: slideNumber, total: 3, phase: 'done' } });
      await textIs(stage.locator('.thought-stage-copy'), 'Creating your slides');
    }
    await emit(harness, 'invoke', RESTORED, { type: 'agent_start', agent: 'deck', message: 'Assembling the deck' });
    await textIs(stage.locator('.thought-stage-copy'), 'Assembling your deck');
    await emit(harness, 'invoke', RESTORED, { type: 'agent_step', agent: 'deck', message: 'Built English deck' });
    await textIs(stage.locator('.thought-stage-copy'), 'Preparing language versions');
    await textIs(duration, 'Calculating');
    assert.equal(await header.getAttribute('aria-expanded'), 'false');
    await checkLayouts('running-collapsed');
  });
  await step(harness, 'ready duration and standalone preview precede final reply', async () => {
    const result = { reply: '## Header layout smoke reply', deck_updated: true, deck_version: 2,
      plan_title: 'Fallback title', files: { English: 'english.pptx' }, verdict: 'approve' };
    state.versions.set(RESTORED, [fixtureVersion(2, title), fixtureVersion(1, 'Restored bilingual deck')]);
    state.messages.get(RESTORED).push(
      { id: 3, session_id: RESTORED, role: 'user', content: 'Create a three-slide training deck.', created_at: NOW },
      { id: 4, session_id: RESTORED, role: 'assistant', content: result.reply, result, created_at: NOW });
    await page.clock.setFixedTime(startedAt + 645000);
    await emit(harness, 'invoke', RESTORED, { type: 'done', agent: 'supervisor', message: 'Deck ready', data: { result } }, { close: true });
    await textIs(stage, 'Your deck is ready');
    await textIs(duration, 'Deck created in 10m 45s');
    await textIs(preview.locator('.output-title'), title);
    await textIncludes(preview.locator('.output-meta'), 'v2');
    await textIncludes(preview.locator('.output-meta'), 'English, French');
    assert.equal(await header.locator('.is-running, .thought-stage-ellipsis').count(), 0);
    assert.equal(await thought.locator('.thought-group-body .flow-board-duration, .thought-group-body .preview-deck-btn').count(), 0);
    const placement = await thought.evaluate((element) => {
      const group = element.querySelector('.thought-group');
      const card = element.querySelector('.thought-preview-card');
      const reply = element.closest('.chat-row').nextElementSibling;
      return !group.contains(card) && group.nextElementSibling === card && !!reply?.querySelector('.msg-assistant');
    });
    assert(placement, 'Preview must sit outside Thought Process and before the assistant reply');
    await header.click();
    assert.equal(await header.getAttribute('aria-expanded'), 'false');
    assert(await duration.isVisible());
    assert(await preview.isVisible());
    await header.press('Space');
    assert.equal(await header.getAttribute('aria-expanded'), 'true');
    await checkLayouts('ready-expanded');
  });
  await step(harness, 'preview tile opens correct deck and keeps language choices', async () => {
    await preview.focus();
    await preview.press('Enter');
    await page.getByRole('dialog', { name: 'Deck preview', exact: true }).waitFor();
    await textIs(page.locator('.preview-title'), title);
    assert.equal(await page.locator('.preview-lang-pill').count(), 2);
    await eventually('preview fetch', async () => assert.deepEqual(state.downloads, [{ sid: RESTORED, language: 'English', version: 2 }]));
    await page.getByRole('button', { name: 'Close preview', exact: true }).click();
    assert(await preview.isVisible());
  });
}

async function testMessages(harness) {
  const { page, state } = harness;
  const messages = page.locator('#messages');
  const users = messages.locator('.msg-user');
  const loadOlder = messages.locator('.load-older-btn');
  const restoredLimits = () => state.messageLimits.filter((r) => r.sid === RESTORED).map((r) => r.limit);
  const anchorTop = (text) => messages.evaluate((box, wanted) => {
    const el = [...box.querySelectorAll('.msg-user')].find((node) => node.textContent.trim() === wanted);
    return el.getBoundingClientRect().top - box.getBoundingClientRect().top;
  }, text);
  state.analytics.allowedErrors.add(500);
  state.messages.set(EMPTY, [
    { id: 1, session_id: EMPTY, role: 'user', content: 'Legacy question', created_at: NOW },
    { id: 2, session_id: EMPTY, role: 'assistant', content: 'Legacy answer', result: null, created_at: NOW },
  ]);

  await step(harness, 'first page uses limit and renders timeline thought boards', async () => {
    await go(harness);
    await page.locator('#app-view').waitFor({ state: 'visible' });
    await eventually('first page', async () => assert.equal(await users.count(), 15));
    assert.deepEqual(restoredLimits(), ['30']);
    await textIs(users.first(), 'Question 9');
    await textIs(users.last(), 'Question 23');
    await textIs(messages.locator('.msg-assistant').last(), 'Answer 23');
    assert.equal(await messages.locator('.thought-turn-content').count(), 1, 'Only the in-window run has a board');
    const board = await messages.evaluate((box) => {
      const rows = [...box.querySelectorAll('.msg-user, .thought-turn-content, .msg-assistant')];
      const i = rows.findIndex((el) => el.classList.contains('thought-turn-content'));
      return { before: rows[i - 1]?.textContent.trim(), after: rows[i + 1]?.textContent.trim() };
    });
    assert.deepEqual(board, { before: 'Question 23', after: 'Answer 23' }, 'Board sits between its run\'s question and reply');
    await textIs(loadOlder, 'Load earlier messages');
    await eventually('opened at newest', async () => assert(await messages.evaluate((box) =>
      box.scrollHeight - box.scrollTop - box.clientHeight < 4)));
  });
  await step(harness, 'failed older page keeps history and reports the error', async () => {
    state.failNextMessages = true;
    await messages.evaluate((box) => { box.scrollTop = 0; });
    await textIncludes(page.locator('#toast-host'), "Couldn't load earlier messages");
    await eventually('retry available', async () => assert(await loadOlder.isEnabled()));
    assert.deepEqual(restoredLimits(), ['30', '60']);
    assert.equal(await users.count(), 15, 'A failed page must not wipe the visible history');
  });
  await step(harness, 'button loads older page without duplicates or scroll jump', async () => {
    const before = await anchorTop('Question 9');
    await loadOlder.click();
    await eventually('older page', async () => assert.equal(await users.count(), 23));
    assert.deepEqual(restoredLimits(), ['30', '60', '60'], 'Failed attempt must not inflate the limit');
    const texts = await users.allInnerTexts();
    assert.equal(new Set(texts).size, texts.length, 'No duplicate messages');
    await textIs(users.first(), 'Question 1');
    assert.equal(await messages.locator('.thought-turn-content').count(), 2);
    assert.equal(await loadOlder.count(), 0, 'No more history once the page is not full');
    await eventually('position preserved', async () => assert(Math.abs((await anchorTop('Question 9')) - before) <= 2,
      `Question 9 moved from ${before}px`));
  });
  await step(harness, 'legacy response without timeline still renders', async () => {
    await page.locator('.project-accordion-row').click();
    await sessionRow(page, 'Empty smoke project').locator('.session-item-info').click();
    await textIs(users.first(), 'Legacy question');
    await textIs(messages.locator('.msg-assistant').first(), 'Legacy answer');
    assert.equal(await users.count(), 1);
    assert.equal(await messages.locator('.thought-turn-content, .load-older-btn').count(), 0);
    assert.deepEqual(state.messageLimits.filter((r) => r.sid === EMPTY).map((r) => r.limit), ['30']);
  });
}

async function testDeckDownload(harness) {
  const { page, state } = harness;
  const chip = page.locator('#output-list .lang-menu-btn').filter({ hasText: 'French' });
  const openMenuAndDownload = async () => {
    await chip.click();
    await page.getByRole('menuitem', { name: 'Download', exact: true }).click();
  };

  await step(harness, 'SAS download keeps filename/bytes and never sends the API token to Blob', async () => {
    await go(harness);
    await page.locator('#app-view').waitFor({ state: 'visible' });
    await page.locator('.outputs-section .studio-section-toggle').click();
    const [download] = await Promise.all([page.waitForEvent('download'), openMenuAndDownload()]);
    assert.equal(download.suggestedFilename(), `${RESTORED}_v1_French.pptx`);
    assert.equal(await download.failure(), null);
    assert.deepEqual(fs.readFileSync(await download.path()), DOWNLOAD_BYTES);
    assert.deepEqual(state.sasRequests, [{ sid: RESTORED, version: 1, language: 'French' }]);
    assert.deepEqual(state.downloads, [{ sid: RESTORED, language: 'French', version: 1 }]);
    assert(!state.api.some((r) => /\/versions\/\d+\/download(\?|$)/.test(r.path)), 'Legacy /download must not be called');
    const stored = await page.evaluate(() => JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage }));
    assert(!stored.includes(SAS_QUERY.slice(1)), 'SAS URL must not be persisted');
    await eventually('busy state cleared', async () => assert(await chip.isEnabled()));
  });
  await step(harness, 'Blob without CORS shows the existing download error toast', async () => {
    state.blobCors = false;
    state.allowedConsole.push(/blocked by CORS policy/, /net::ERR_FAILED/);
    await openMenuAndDownload();
    await textIncludes(page.locator('#toast-host'), 'Download failed: download failed (network or CORS error');
    await eventually('busy state cleared', async () => assert(await chip.isEnabled()));
    const toast = await page.locator('#toast-host').innerText();
    assert(!toast.includes(SAS_QUERY.slice(1)), 'SAS URL must not appear in UI errors');
    state.blobCors = true;
  });
}

async function testDeckFeedback(harness) {
  const { page, state } = harness;
  const comment = 'The deck needs more detail on the final recommendation.';
  const gate = { arrived: deferred(), release: deferred() };
  state.analytics.allowedErrors.add(404).add(409);
  state.versions.set(RESTORED, [fixtureVersion(1, 'Restored bilingual deck'), fixtureVersion(2, 'Second smoke deck')]);
  state.feedbackGates.set(`${RESTORED}|2`, gate);
  const card = (title) => page.locator('#output-list .output-card').filter({ hasText: title });
  const v1 = card('Restored bilingual deck');
  const v2 = card('Second smoke deck');
  const saved = (c) => eventually('feedback saved', async () => assert.equal(await c.locator('.feedback-saved').count(), 1));

  await step(harness, 'no editable controls while feedback loads', async () => {
    await go(harness);
    await page.locator('#app-view').waitFor({ state: 'visible' });
    await page.locator('.outputs-section .studio-section-toggle').click();
    await gate.arrived.promise;
    await textIncludes(v2.locator('.output-feedback'), 'Loading feedback');
    assert.equal(await v2.locator('.feedback-thumb').count(), 0, 'No rating controls during the initial GET');
    gate.release.resolve();
    await eventually('v2 editable after 404', async () => assert.equal(await v2.locator('.feedback-thumb').count(), 2));
    assert.equal(await v2.locator('.feedback-comment').count(), 0, 'Comment/submit hidden until a rating is chosen');
  });
  await step(harness, 'thumbs-down submits exact payload then locks read-only', async () => {
    const down = v2.getByRole('button', { name: 'Thumbs down' });
    await down.click();
    assert.equal(await down.getAttribute('aria-pressed'), 'true');
    assert.equal(await v2.getByRole('button', { name: 'Thumbs up' }).getAttribute('aria-pressed'), 'false');
    const input = v2.locator('.feedback-comment');
    assert.equal(await input.getAttribute('maxlength'), '1000');
    await input.fill(comment);
    await textIs(v2.locator('.feedback-counter'), `${comment.length} / 1000`);
    await v2.getByRole('button', { name: 'Submit' }).click();
    await saved(v2);
    assert.deepEqual(state.feedbackPosts, [{ key: `${RESTORED}|2`, body: { thumbs_up: false, comment } }]);
    await textIncludes(v2.locator('.feedback-saved-rating'), 'not helpful');
    await textIs(v2.locator('.feedback-saved-comment'), comment);
    assert.equal(await v2.locator('.feedback-thumb, .feedback-comment').count(), 0, 'Saved feedback is read-only');
  });
  await step(harness, '409 re-fetches stored feedback; versions stay independent', async () => {
    assert.equal(await v1.locator('.feedback-thumb').count(), 2, 'v1 keeps its own unsubmitted state');
    await v1.getByRole('button', { name: 'Thumbs up' }).click();
    state.feedback.set(`${RESTORED}|1`, { session_id: RESTORED, deck_version: 1, thumbs_up: false, comment: 'Stored by another tab' });
    await v1.getByRole('button', { name: 'Submit' }).click();
    await saved(v1);
    assert.equal(state.feedbackPosts.length, 2, 'A 409 must not retry the POST');
    assert.deepEqual(state.feedbackPosts[1].body, { thumbs_up: true }, 'Blank comment is omitted');
    await textIncludes(v1.locator('.feedback-saved-rating'), 'not helpful');
    await textIs(v1.locator('.feedback-saved-comment'), 'Stored by another tab');
  });
  await step(harness, 'readable in both themes', async () => {
    fs.mkdirSync(ARTIFACT_DIR, { recursive: true });
    for (const mode of ['dark', 'light']) {
      await theme(harness, mode, '#theme-toggle-app');
      await page.locator('#output-list').screenshot({ path: path.join(ARTIFACT_DIR, `deck-feedback-${mode}.png`) });
    }
    await theme(harness, 'dark', '#theme-toggle-app');
  });
}

async function testAnalytics(harness) {
  const { page, state } = harness;
  const dashboardRequests = () => state.api.filter((request) => request.path.startsWith('/api/analytics/dashboard?'));
  const projectRequests = () => state.api.filter((request) => request.path.startsWith('/api/analytics/projects?'));
  const lastProjectQuery = () => new URL(projectRequests().at(-1).path, API_ORIGIN).searchParams;
  const value = (key) => page.locator(`[data-metric="${key}"] .metric-value`);
  const waitProjects = async (count) => eventually('project rows settled', async () => {
    assert.equal(await page.locator('.project-table tbody tr').count(), count);
    assert.equal(await page.locator('.projects-section .loading-heading').count(), 0);
  });
  await step(harness, 'analytics lazy loading, measured metrics, ratio bases and estimate metadata', async () => {
    await go(harness);
    await page.locator('#app-view').waitFor();
    assert.equal(dashboardRequests().length, 0, 'Hidden analytics must not fetch');
    await tab(page, 'Analytics');
    await textIs(value('active-users'), '42');
    await textIs(value('turnaround'), '18.5 s');
    await textIs(value('total-cost'), '$12.87');
    await textIs(value('multi-turn-rate'), '38%');
    await textIncludes(page.locator('[data-metric="multi-turn-rate"]'), '76 of 200');
    await textIncludes(page.locator('[data-metric="cost-slide"]'), '540 slides');
    await textIncludes(page.locator('[data-metric="hours-saved"]'), 'low confidence / baseline v1');
    assert.equal(await page.locator('[data-metric="hours-saved"] .estimated').count(), 1);
    await waitProjects(50);
    assert.equal(dashboardRequests().length, 1);
    assert.equal(projectRequests().length, 1, 'The table and usage chart must share one project request');
    const query = new URL(dashboardRequests()[0].path, API_ORIGIN).searchParams;
    assert.equal(Date.parse(query.get('to')) - Date.parse(query.get('from')), 30 * 86400000);
    assert.equal(query.get('tz'), 'UTC');
    assert.equal(lastProjectQuery().get('from'), query.get('from'));
    assert.equal(lastProjectQuery().get('to'), query.get('to'));
    assert(!state.api.some((request) => request.path.includes('/analytics/summary')));
  });
  await step(harness, 'project usage chart reuses table data and switches all six views without requests', async () => {
    const metricControls = page.getByRole('group', { name: 'AI usage metric', exact: true });
    const groupControls = page.getByRole('group', { name: 'AI usage grouping', exact: true });
    const ids = (await page.locator('.project-link').allTextContents()).map((text) => text.trim());
    const sourceRows = ids.map((id) => state.analytics.rows.find((row) => row.session_id === id));
    const totalTokens = sourceRows.reduce((total, row) => total + row.total_tokens, 0);
    const totalCost = sourceRows.reduce((total, row) => total + row.cost_usd, 0);
    await textIs(value('project-usage-tokens'), new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 2 }).format(totalTokens));
    await textIs(value('project-usage-cost'), new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 4 }).format(totalCost));
    await textIncludes(page.locator('#project-usage-scope'), 'Current page / 50 of 50 projects with usage / 205 matching projects');
    assert(await page.locator('.project-usage').evaluate((element) =>
      !!(element.compareDocumentPosition(document.querySelector('.project-table')) & Node.DOCUMENT_POSITION_FOLLOWING)));
    const before = state.api.filter((request) => request.path.startsWith('/api/analytics/')).length;
    const pictures = new Set();
    for (const [groupLabel, group] of [['By project', 'project'], ['By owner', 'owner']]) {
      await groupControls.getByRole('button', { name: groupLabel, exact: true }).click();
      assert.equal(await groupControls.getByRole('button', { name: groupLabel, exact: true }).getAttribute('aria-pressed'), 'true');
      for (const [metricLabel, field] of [['Tokens', 'total_tokens'], ['AI cost', 'cost_usd'], ['Time', 'agent_processing_ms']]) {
        await metricControls.getByRole('button', { name: metricLabel, exact: true }).click();
        assert.equal(await metricControls.getByRole('button', { name: metricLabel, exact: true }).getAttribute('aria-pressed'), 'true');
        const labels = await page.locator('.project-usage-values li').allTextContents();
        if (group === 'project') {
          const sorted = [...sourceRows].sort((first, second) => second[field] - first[field]);
          assert.equal(labels.length, sorted.length);
          assert(labels.every((label, index) => label.startsWith(sorted[index].session_id)));
        } else {
          const owners = new Map();
          for (const row of sourceRows) {
            const owner = row.owner_name ?? 'Unknown owner';
            const aggregate = owners.get(owner) || { owner, count: 0, value: 0 };
            aggregate.count++;
            aggregate.value += row[field];
            owners.set(owner, aggregate);
          }
          const sorted = [...owners.values()].sort((first, second) => second.value - first.value);
          assert.equal(labels.length, sorted.length);
          assert(labels.every((label, index) => label.startsWith(`${sorted[index].owner} (${sorted[index].count})`)));
        }
        if (metricLabel === 'Time') {
          await textIncludes(page.locator('.project-usage-legend'), 'Agent processing');
          await textIncludes(page.locator('.project-usage-legend'), 'AI turnaround');
          assert.equal(await page.locator('.usage-swatch.outlined').count(), 1);
          assert(labels.every((label) => /Agent processing: [\d,.]+ (ms|s|min|h)/.test(label)));
        }
        pictures.add(await page.locator('.project-usage-chart canvas').evaluate((canvas) => canvas.toDataURL()));
      }
    }
    assert.equal(pictures.size, 6, 'Every usage mode and grouping must redraw the chart');
    await groupControls.getByRole('button', { name: 'By project', exact: true }).click();
    await metricControls.getByRole('button', { name: 'Tokens', exact: true }).focus();
    await page.keyboard.press('Enter');
    assert.equal(await metricControls.getByRole('button', { name: 'Tokens', exact: true }).getAttribute('aria-pressed'), 'true');
    assert.equal(state.api.filter((request) => request.path.startsWith('/api/analytics/')).length, before);
    assert.deepEqual((await page.locator('.project-link').allTextContents()).map((text) => text.trim()), ids, 'Chart controls must not reorder the table');
  });
  await step(harness, 'sample bar colors and row-correct tooltips remain visible when scrolling', async () => {
    fs.mkdirSync(ARTIFACT_DIR, { recursive: true });
    const requestsBefore = state.api.filter((request) => request.path.startsWith('/api/analytics/')).length;
    const metricControls = page.getByRole('group', { name: 'AI usage metric', exact: true });
    const scroller = page.locator('.project-usage-chart-scroll');
    await page.evaluate(() => {
      const fillText = CanvasRenderingContext2D.prototype.fillText;
      window.__analyticsTooltipText = [];
      CanvasRenderingContext2D.prototype.fillText = function(text, positionX, positionY, ...rest) {
        if (this.canvas.closest('.project-usage-chart')) {
          const metrics = this.measureText(text);
          const transform = this.getTransform();
          const rect = this.canvas.getBoundingClientRect();
          const left = transform.transformPoint(new DOMPoint(positionX - metrics.actualBoundingBoxLeft, positionY - metrics.actualBoundingBoxAscent));
          const right = transform.transformPoint(new DOMPoint(positionX + metrics.actualBoundingBoxRight, positionY + metrics.actualBoundingBoxDescent));
          window.__analyticsTooltipText.push({ text, font: this.font,
            left: rect.left + left.x * rect.width / this.canvas.width, top: rect.top + left.y * rect.height / this.canvas.height,
            right: rect.left + right.x * rect.width / this.canvas.width, bottom: rect.top + right.y * rect.height / this.canvas.height });
        }
        return fillText.call(this, text, positionX, positionY, ...rest);
      };
      window.__restoreAnalyticsText = () => { CanvasRenderingContext2D.prototype.fillText = fillText; };
    });
    try {
      for (const mode of ['dark', 'light']) {
        for (const width of [1440, 320]) {
          await page.setViewportSize({ width, height: 1000 });
          await theme(harness, mode, '#theme-toggle-app');
          for (const [metric, rgb, label] of [
            ['Tokens', [57, 135, 229], 'Tokens used'], ['AI cost', [25, 158, 112], 'AI cost (USD)'],
            ['Time', [57, 135, 229], 'Agent processing'],
          ]) {
            await metricControls.getByRole('button', { name: metric, exact: true }).click();
            await page.mouse.move(1, 1);
            await scroller.scrollIntoViewIfNeeded();
            const bands = await page.locator('.project-usage-chart canvas').evaluate((canvas, expected) => {
              const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
              const rows = [];
              let band;
              for (let vertical = 0; vertical < canvas.height; vertical++) {
                let start = -1;
                let end = -1;
                for (let horizontal = 0; horizontal < canvas.width; horizontal++) {
                  const offset = (vertical * canvas.width + horizontal) * 4;
                  if (pixels[offset + 3] > 200 && expected.every((channel, index) => pixels[offset + index] === channel)) {
                    if (start < 0) start = horizontal;
                    end = horizontal;
                  }
                }
                if (end - start > 8) {
                  if (!band) { band = { first: vertical, last: vertical, start, end }; rows.push(band); }
                  band.last = vertical;
                } else band = undefined;
              }
              return rows.map((row) => ({ x: (row.start + Math.min(24, (row.end - row.start) / 2)) * canvas.clientWidth / canvas.width,
                y: (row.first + row.last) / 2 * canvas.clientHeight / canvas.height }));
            }, rgb);
            const expectedRows = (await page.locator('.project-usage-values li').allTextContents()).map((text) => text.split(' / ')[0]);
            assert.equal(bands.length, expectedRows.length, `Exact sample bar color ${metric}/${mode}/${width}`);
            const swatches = await page.locator('.usage-swatch').evaluateAll((elements) => elements.map((element) => ({
              fill: getComputedStyle(element).backgroundColor, border: getComputedStyle(element).borderColor,
            })));
            assert.equal(swatches[0].fill, `rgb(${rgb.join(', ')})`);
            if (metric === 'Time') assert.equal(swatches[1].border, 'rgb(217, 89, 38)');
            for (const index of [0, Math.floor(bands.length / 2), bands.length - 1]) {
              await page.mouse.move(1, 1);
              await scroller.evaluate((element, target) => {
                element.scrollTop = target.last ? element.scrollHeight : Math.max(0, target.y - 12);
              }, { y: bands[index].y, last: index === bands.length - 1 });
              const rect = await page.locator('.project-usage-chart canvas').boundingBox();
              await page.evaluate(() => { window.__analyticsTooltipText = []; });
              await page.mouse.move(rect.x + bands[index].x, rect.y + bands[index].y);
              await eventually(`tooltip ${metric}/${mode}/${width}/${index}`, async () => {
                const draws = await page.evaluate(() => window.__analyticsTooltipText);
                const bodyIndex = draws.findIndex((draw) => draw.text.startsWith(`${label}:`));
                assert(bodyIndex > 0, 'A tooltip must be drawn');
                const title = draws.slice(0, bodyIndex).filter((draw) => draw.font.includes('bold')).at(-1);
                assert.equal(title?.text, expectedRows[index], 'Tooltip must match the hovered row');
                const clip = await scroller.evaluate((element) => {
                  const bounds = element.getBoundingClientRect();
                  return { left: bounds.left, right: bounds.right, top: Math.max(0, bounds.top), bottom: Math.min(innerHeight, bounds.bottom) };
                });
                const text = [title, ...draws.slice(bodyIndex)];
                assert(text.every((draw) => draw.left >= clip.left - 1 && draw.right <= clip.right + 1
                  && draw.top >= clip.top - 1 && draw.bottom <= clip.bottom + 1),
                `Clipped tooltip ${metric}/${mode}/${width}/${index}: ${JSON.stringify({ clip, text })}`);
                if (metric === 'Time') assert(text.some((draw) => draw.text.startsWith('AI turnaround:')));
                if (metric === 'AI cost') assert(text.some((draw) => draw.text.includes('per 1M tokens')));
              });
              if (index === Math.floor(bands.length / 2)) {
                await page.screenshot({ path: path.join(ARTIFACT_DIR, `analytics-tooltip-${metric.replace(' ', '-')}-${mode}-${width}.png`) });
              }
            }
          }
        }
      }
    } finally { await page.evaluate(() => window.__restoreAnalyticsText()); }
    assert.equal(state.api.filter((request) => request.path.startsWith('/api/analytics/')).length, requestsBefore);
    await page.setViewportSize({ width: 1440, height: 1000 });
    await metricControls.getByRole('button', { name: 'Tokens', exact: true }).click();
  });
  await step(harness, 'responsive light/dark layouts and chart pixels at 320-1440px', async () => {
    fs.mkdirSync(ARTIFACT_DIR, { recursive: true });
    for (const mode of ['dark', 'light']) {
      for (const width of [1440, 1024, 768, 390, 320]) {
        await page.setViewportSize({ width, height: width < 600 ? 844 : 1000 });
        await theme(harness, mode, '#theme-toggle-app');
        await page.locator('#tab-analytics').evaluate((element) => { element.scrollTop = 0; });
        await page.waitForFunction(() => [...document.querySelectorAll('app-analytics-chart canvas')]
          .every((canvas) => canvas.width > 0 && canvas.getBoundingClientRect().width > 30));
        const layout = await page.evaluate(() => {
          const panel = document.querySelector('#tab-analytics');
          const root = getComputedStyle(document.documentElement);
          const palette = ['--analytics-chart-blue', '--analytics-chart-red', '--analytics-chart-gold', '--analytics-usage-blue'].map((token) => {
            const hex = root.getPropertyValue(token).trim().replace('#', '');
            return [parseInt(hex.slice(0, 2), 16), parseInt(hex.slice(2, 4), 16), parseInt(hex.slice(4, 6), 16)];
          });
          const overflow = [...panel.querySelectorAll('.metric-value, .metric-label, .metric-context, .window-filters, .view-tabs, .project-usage-controls, .usage-segments, .project-usage-heading')]
            .filter((element) => element.getClientRects().length && element.scrollWidth > element.clientWidth + 2)
            .map((element) => element.className + ': ' + element.textContent.trim());
          const chartPixels = [...panel.querySelectorAll('canvas')].map((canvas) => {
            const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
            let colored = 0;
            for (let offset = 0; offset < pixels.length; offset += 4) {
              if (pixels[offset + 3] > 200 && palette.some((rgb) => rgb[0] === pixels[offset]
                && rgb[1] === pixels[offset + 1] && rgb[2] === pixels[offset + 2])) colored++;
            }
            return { width: canvas.width, colored };
          });
          return { overflow, chartPixels, documentWidth: document.documentElement.scrollWidth,
            panelWidth: panel.clientWidth, panelScrollWidth: panel.scrollWidth, viewport: innerWidth };
        });
        assert(layout.documentWidth <= width + 1, `Page overflow ${mode}/${width}: ${JSON.stringify(layout)}`);
        assert(layout.panelScrollWidth <= layout.panelWidth + 1, `Analytics overflow ${mode}/${width}`);
        assert.deepEqual(layout.overflow, [], `Text overflow ${mode}/${width}`);
        assert.equal(layout.chartPixels.length, 3);
        assert(layout.chartPixels.every((chart) => chart.colored > 30), `Blank chart ${mode}/${width}: ${JSON.stringify(layout.chartPixels)}`);
        if ([1440, 390].includes(width)) {
          await page.screenshot({ path: path.join(ARTIFACT_DIR, `analytics-${mode}-${width}.png`) });
          await page.locator('#usage-title').scrollIntoViewIfNeeded();
          await page.screenshot({ path: path.join(ARTIFACT_DIR, `analytics-usage-${mode}-${width}.png`) });
          await page.locator('.usage-grid').screenshot({ path: path.join(ARTIFACT_DIR, `analytics-charts-${mode}-${width}.png`) });
          await page.locator('.efficiency-section').screenshot({ path: path.join(ARTIFACT_DIR, `analytics-value-${mode}-${width}.png`) });
          await page.locator('.technical-section').screenshot({ path: path.join(ARTIFACT_DIR, `analytics-technical-${mode}-${width}.png`) });
          await page.locator('.project-usage').screenshot({ path: path.join(ARTIFACT_DIR, `analytics-project-usage-${mode}-${width}.png`) });
          await page.getByRole('group', { name: 'AI usage grouping', exact: true }).getByRole('button', { name: 'By owner', exact: true }).click();
          await page.getByRole('group', { name: 'AI usage metric', exact: true }).getByRole('button', { name: 'Time', exact: true }).click();
          await page.locator('.project-usage').screenshot({ path: path.join(ARTIFACT_DIR, `analytics-owner-time-${mode}-${width}.png`) });
          await page.getByRole('group', { name: 'AI usage grouping', exact: true }).getByRole('button', { name: 'By project', exact: true }).click();
          await page.getByRole('group', { name: 'AI usage metric', exact: true }).getByRole('button', { name: 'Tokens', exact: true }).click();
        }
      }
    }
    await page.setViewportSize({ width: 1440, height: 1000 });
  });
  await step(harness, 'trend selection is local; breakdown filters do not affect projects', async () => {
    const before = dashboardRequests().length;
    const pixels = await page.locator('.trend-chart canvas').evaluate((canvas) => canvas.toDataURL());
    await page.selectOption('select[name="trend"]', 'cost_usd');
    await eventually('trend chart changes', async () => assert.notEqual(await page.locator('.trend-chart canvas').evaluate((canvas) => canvas.toDataURL()), pixels));
    assert.equal(dashboardRequests().length, before);
    const projectsBefore = projectRequests().length;
    await page.selectOption('select[name="agent"]', 'planner');
    await eventually('agent filter', async () => assert.equal(await page.locator('#agents-panel tbody tr').count(), 2));
    assert.equal(new URL(dashboardRequests().at(-1).path, API_ORIGIN).searchParams.get('agent'), 'planner');
    await textIs(value('active-users'), '42');
    assert.equal(projectRequests().length, projectsBefore);
    await page.selectOption('select[name="model"]', 'claude-sonnet-4-6');
    await eventually('model filter', async () => assert.equal(await page.locator('#agents-panel tbody tr').count(), 1));
    await page.locator('#agents-tab').focus();
    await page.keyboard.press('ArrowRight');
    assert.equal(await page.locator('#models-tab').getAttribute('aria-selected'), 'true');
    assert(await page.locator('#models-panel').isVisible());
    await page.selectOption('select[name="agent"]', '');
    await eventually('agent filter cleared', async () => assert.equal(new URL(dashboardRequests().at(-1).path, API_ORIGIN).searchParams.get('agent'), null));
    await page.selectOption('select[name="model"]', '');
    await eventually('breakdowns restored', async () => assert.equal(await page.locator('#models-panel tbody tr').count(), 4));
  });
  await step(harness, 'server sorting, page sizes, next and previous pages', async () => {
    await page.selectOption('select[name="projectSort"]', 'cost');
    await waitProjects(50);
    assert.equal(lastProjectQuery().get('sort'), 'cost');
    assert.equal(lastProjectQuery().get('order'), 'desc');
    await textIncludes(page.locator('.project-link').first(), 'analytics-project-204');
    await page.selectOption('select[name="pageSize"]', { label: '25' });
    await waitProjects(25);
    await page.getByRole('button', { name: 'Next project page' }).click();
    await waitProjects(25);
    assert.equal(lastProjectQuery().get('offset'), '25');
    const pageIds = (await page.locator('.project-link').allTextContents()).map((text) => text.trim());
    const chartRows = await page.locator('.project-usage-values li').allTextContents();
    assert.equal(chartRows.length, 25);
    assert(chartRows.every((text) => pageIds.some((id) => text.startsWith(id))), 'Chart must follow the new table page');
    await page.getByRole('button', { name: 'Previous project page' }).click();
    await waitProjects(25);
    assert.equal(lastProjectQuery().get('offset'), '0');
    for (const sort of ['turns', 'tokens', 'duration', 'last_active']) {
      await page.selectOption('select[name="projectSort"]', sort);
      await waitProjects(25);
      assert.equal(lastProjectQuery().get('sort'), sort);
    }
    await page.getByRole('button', { name: 'Sort ascending', exact: true }).click();
    await waitProjects(25);
    assert.equal(lastProjectQuery().get('order'), 'asc');
    await textIncludes(page.locator('.project-link').first(), '=FORMULA("fixture")');
    assert.equal(await page.locator('.project-usage-values li').count(), 24);
    await textIncludes(page.locator('.project-usage-note'), '=FORMULA("fixture")');
    await textIncludes(page.locator('#project-usage-scope'), '24 of 25 projects');
  });
  await step(harness, 'all-results CSV export, nullable cells and formula escaping', async () => {
    const before = state.api.length;
    const [download] = await Promise.all([page.waitForEvent('download'), page.getByRole('button', { name: 'Export CSV', exact: true }).click()]);
    assert.equal(await download.failure(), null);
    const csv = fs.readFileSync(await download.path(), 'utf8');
    assert(csv.startsWith('\ufeffsession_id,owner_name,turns,multi_turn,published,slides_delivered,ai_turnaround_ms,agent_processing_ms,total_tokens,cost_usd,last_active'));
    assert.equal(csv.split('\r\n').length, 206, 'Export must include all 205 projects, not 25 visible rows');
    assert(csv.includes(`"'=FORMULA(""fixture"")","",`), 'CSV must escape formulas and preserve null as blank');
    assert(!csv.includes('undefined') && !csv.includes('null'));
    const requests = state.api.slice(before).filter((request) => request.path.startsWith('/api/analytics/projects?'));
    assert.equal(requests.length, 2);
    assert.deepEqual(requests.map((request) => new URL(request.path, API_ORIGIN).searchParams.get('offset')), ['0', '200']);
    assert(requests.every((request) => new URL(request.path, API_ORIGIN).searchParams.get('limit') === '200'));
  });
  await step(harness, 'project detail timeline, agent nodes, ingestion, focus and responsive drawer', async () => {
    const link = page.locator('.project-link').first();
    await link.click();
    const drawer = page.locator('.project-drawer');
    await drawer.waitFor();
    await eventually('workflow detail', async () => assert.equal(await drawer.locator('.workflow-timeline li').count(), 2));
    assert(state.api.some((request) => request.path === `/api/analytics/projects/${encodeURIComponent('=FORMULA("fixture")')}`));
    assert((await drawer.textContent()).includes('all recorded activity'));
    await textIncludes(drawer.locator('.workflow-timeline'), '18.5 s');
    await page.locator('#detail-agents-tab').click();
    assert.equal(await drawer.locator('.detail-agent-table tbody tr').count(), 2);
    assert((await drawer.locator('.detail-agent-table').textContent()).includes('unpriced'));
    await page.selectOption('select[name="detailRun"]', 'workflow-2');
    assert.equal(await drawer.locator('.detail-agent-table tbody tr').count(), 1);
    await textIncludes(drawer.locator('.detail-agent-table tbody'), 'No data');
    await page.locator('#ingestion-tab').click();
    await textIncludes(drawer.locator('.ingestion-table'), '16 KB');
    await textIs(drawer.locator('.ingestion-table tbody tr td').nth(4), 'No data');
    for (const width of [1440, 390, 320]) {
      await page.setViewportSize({ width, height: 844 });
      await page.locator('#workflow-tab').click();
      const bounds = await drawer.boundingBox();
      assert(bounds.x >= -1 && bounds.x + bounds.width <= width + 1);
      const overflow = await drawer.evaluate((element) => element.scrollWidth > element.clientWidth + 1);
      assert.equal(overflow, false, `Drawer overflow at ${width}`);
      for (let count = 0; count < 8; count++) await page.keyboard.press('Tab');
      assert(await page.evaluate(() => !!document.activeElement.closest('dialog')), 'Dialog focus must stay trapped');
      if (width !== 320) await page.screenshot({ path: path.join(ARTIFACT_DIR, `analytics-detail-${width}.png`) });
    }
    await page.keyboard.press('Escape');
    await drawer.waitFor({ state: 'hidden' });
    assert(await link.evaluate((element) => document.activeElement === element), 'Closing detail must restore focus');
    await page.setViewportSize({ width: 1440, height: 1000 });
  });
  await step(harness, 'custom inclusive end date, timezone, and local date validation', async () => {
    await page.locator('input[name="from"]').fill('2026-09-01');
    await page.locator('input[name="to"]').fill('2026-09-30');
    await page.selectOption('select[name="timezone"]', 'Asia/Kolkata');
    await page.getByRole('button', { name: 'Apply', exact: true }).click();
    await textIs(value('active-users'), '42');
    await waitProjects(25);
    const query = new URL(dashboardRequests().at(-1).path, API_ORIGIN).searchParams;
    assert.equal(query.get('from'), '2026-09-01T00:00:00.000Z');
    assert.equal(query.get('to'), '2026-10-01T00:00:00.000Z');
    assert.equal(query.get('tz'), 'Asia/Kolkata');
    assert.equal(lastProjectQuery().get('from'), query.get('from'));
    const before = dashboardRequests().length;
    await page.locator('input[name="from"]').fill('2026-10-02');
    await page.getByRole('button', { name: 'Apply', exact: true }).click();
    await textIncludes(page.locator('.analytics-page > .error-text'), 'start date must be on or before');
    assert.equal(dashboardRequests().length, before);
    await page.locator('input[name="from"]').fill('2024-01-01');
    await page.getByRole('button', { name: 'Apply', exact: true }).click();
    await textIncludes(page.locator('.analytics-page > .error-text'), '366 days');
    assert.equal(dashboardRequests().length, before);
    await page.selectOption('select[name="range"]', '30');
  });
  await step(harness, 'HTTP 400/500 messages, retries and independent project loading', async () => {
    state.analytics.allowedErrors.add(400);
    state.analytics.allowedErrors.add(500);
    state.analytics.dashboardStatus = 400;
    await page.getByRole('button', { name: 'Apply', exact: true }).click();
    await textIncludes(page.locator('.analytics-page > .request-error'), 'Fixture analytics validation or access error');
    await waitProjects(25);
    state.analytics.dashboardStatus = 200;
    await page.getByRole('button', { name: 'Retry', exact: true }).click();
    await textIs(value('active-users'), '42');
    state.analytics.projectsStatus = 500;
    await page.selectOption('select[name="projectSort"]', 'cost');
    await textIncludes(page.locator('.projects-section .request-error'), 'Fixture projects error');
    assert.equal(await page.locator('.project-usage').count(), 0, 'A failed project request must not leave stale usage visible');
    await textIs(value('active-users'), '42');
    state.analytics.projectsStatus = 200;
    await page.getByRole('button', { name: 'Retry projects', exact: true }).click();
    await waitProjects(25);
    state.analytics.detailStatus = 500;
    await page.locator('.project-link').first().click();
    await textIncludes(page.locator('.project-drawer .request-error'), 'Fixture project detail error');
    state.analytics.detailStatus = 200;
    await page.getByRole('button', { name: 'Retry detail', exact: true }).click();
    await eventually('detail retry', async () => assert.equal(await page.locator('.workflow-timeline li').count(), 2));
    await page.getByRole('button', { name: 'Close project detail', exact: true }).click();
  });
  await step(harness, 'missing project usage keeps no-data KPIs and an empty chart state', async () => {
    const originalRows = state.analytics.rows;
    state.analytics.rows = [originalRows[0]];
    await page.getByRole('button', { name: 'Refresh analytics', exact: true }).click();
    await waitProjects(1);
    await textIs(value('project-usage-tokens'), 'No data');
    await textIs(value('project-usage-cost'), 'No data');
    await textIs(value('project-usage-time'), 'No data');
    await textIs(value('project-usage-unit-cost'), 'No data');
    assert.equal(await page.locator('.project-usage canvas').count(), 0);
    await textIncludes(page.locator('.project-usage'), 'No chartable usage on this page');
    state.analytics.rows = originalRows;
  });
  await step(harness, 'null is no data; zero remains zero; zero-denominator ratios are hidden', async () => {
    state.analytics.mode = 'null';
    state.analytics.emptyProjects = true;
    await page.getByRole('button', { name: 'Refresh analytics', exact: true }).click();
    await textIs(value('active-users'), 'No data');
    await textIs(value('total-cost'), 'No data');
    await textIs(value('slides'), '0');
    await textIs(value('hours-saved'), 'No data');
    for (const key of ['cost-project', 'cost-slide', 'cost-call', 'tokens-slide', 'tokens-call', 'multi-turn-rate', 'rework-rate', 'thumbs-up-rate', 'comments']) {
      assert.equal(await page.locator(`[data-metric="${key}"]`).count(), 0, `${key} has zero denominator`);
    }
    await textIncludes(page.locator('.outcomes-grid'), 'No trend data');
    await textIncludes(page.locator('.projects-section'), 'No projects in this window');
    assert(await page.getByRole('button', { name: 'Export CSV', exact: true }).isDisabled());
    state.analytics.emptyProjects = false;
    state.analytics.mode = 'data';
  });
  await step(harness, 'superseded dashboard responses and closed detail requests cannot restore stale data', async () => {
    const gate = { arrived: deferred(), release: deferred() };
    state.analytics.mode = 'stale';
    state.analytics.dashboardGate = gate;
    await page.getByRole('button', { name: 'Apply', exact: true }).click();
    await gate.arrived.promise;
    state.analytics.mode = 'data';
    await page.selectOption('select[name="range"]', '7');
    await page.getByRole('button', { name: 'Apply', exact: true }).click();
    await textIs(value('active-users'), '42');
    gate.release.resolve();
    await waitProjects(25);
    await textIs(value('active-users'), '42');
    const detailGate = { arrived: deferred(), release: deferred() };
    state.analytics.detailGate = detailGate;
    await page.locator('.project-link').first().click();
    await detailGate.arrived.promise;
    await page.getByRole('button', { name: 'Close project detail', exact: true }).click();
    detailGate.release.resolve();
    await page.locator('.project-drawer').waitFor({ state: 'hidden' });
    assert.equal(await page.locator('.workflow-timeline li').count(), 0);
  });
}

async function testAnalyticsAccess(browser) {
  for (const scenario of ['non-admin', 'dashboard-403', 'projects-403', 'detail-403', 'dashboard-401']) {
    const harness = await makeHarness(browser, true, scenario === 'non-admin' ? ['author'] : ['admin']);
    try {
      await step(harness, `analytics access control: ${scenario}`, async () => {
        await go(harness);
        await harness.page.locator('#app-view').waitFor();
        if (scenario === 'non-admin') {
          assert.equal(await harness.page.getByRole('button', { name: 'Analytics', exact: true }).count(), 0);
          assert(!harness.state.api.some((request) => request.path.includes('/analytics/')));
          return;
        }
        harness.state.analytics.allowedErrors.add(403);
        harness.state.analytics.allowedErrors.add(401);
        if (scenario.startsWith('dashboard')) harness.state.analytics.dashboardStatus = scenario.endsWith('401') ? 401 : 403;
        if (scenario === 'projects-403') harness.state.analytics.projectsStatus = 403;
        if (scenario === 'detail-403') harness.state.analytics.detailStatus = 403;
        await tab(harness.page, 'Analytics');
        if (scenario === 'detail-403') await harness.page.locator('.project-link').first().click();
        await eventually('analytics removed after denied request', async () => {
          assert.equal(await harness.page.getByRole('button', { name: 'Analytics', exact: true }).count(), 0);
          assert.equal(await harness.page.locator('.metric-value').count(), 0);
          assert.equal(await harness.page.locator('.project-drawer[open]').count(), 0);
        });
      });
    } catch (error) {
      await diagnostics(harness);
      throw error;
    } finally { await harness.context.close(); }
  }
}

function testAnalyticsHelpers() {
  const typescript = require('typescript');
  const Module = require('node:module');
  const helperPath = path.resolve(__dirname, '../src/app/analytics/analytics.helpers.ts');
  const compiled = typescript.transpileModule(fs.readFileSync(helperPath, 'utf8'), {
    compilerOptions: { module: typescript.ModuleKind.CommonJS, target: typescript.ScriptTarget.ES2022 },
  });
  const helperModule = new Module(helperPath, module);
  helperModule._compile(compiled.outputText, helperPath);
  const { formatAnalyticsValue, metricHasDenominator, analyticsWindow, projectsCsv, buildProjectUsage } = helperModule.exports;
  for (const format of ['number', 'compact', 'currency', 'duration', 'percent', 'hours', 'leverage']) {
    assert.equal(formatAnalyticsValue(null, format), 'No data');
    assert.equal(formatAnalyticsValue(undefined, format), 'No data');
    assert.equal(formatAnalyticsValue(NaN, format), 'No data');
  }
  assert.equal(formatAnalyticsValue(0), '0');
  assert.equal(formatAnalyticsValue(3370472, 'compact'), '3.37M');
  assert.equal(formatAnalyticsValue(0, 'compact'), '0');
  assert.equal(formatAnalyticsValue(0, 'currency'), '$0.00');
  assert.equal(formatAnalyticsValue(.0042, 'currency'), '$0.0042');
  assert.equal(formatAnalyticsValue(18450, 'duration'), '18.5 s');
  assert.equal(formatAnalyticsValue(.38, 'percent'), '38%');
  assert.equal(metricHasDenominator({ value: .5, type: 'derived', of: 0 }), false);
  assert.equal(metricHasDenominator({ value: 0, type: 'derived', denominator: 0 }), false);
  assert.equal(metricHasDenominator({ value: null, type: 'measured' }), true);
  const now = new Date('2026-10-01T12:30:00Z');
  assert.deepEqual(analyticsWindow('30', '', '', 'UTC', now), { from: '2026-09-01T12:30:00.000Z', to: '2026-10-01T12:30:00.000Z', timezone: 'UTC' });
  assert.deepEqual(analyticsWindow('custom', '2026-09-01', '2026-09-30', 'Asia/Kolkata'), { from: '2026-09-01T00:00:00.000Z', to: '2026-10-01T00:00:00.000Z', timezone: 'Asia/Kolkata' });
  assert.throws(() => analyticsWindow('custom', '2026-10-02', '2026-10-01', 'UTC'), /start date/);
  assert.throws(() => analyticsWindow('custom', '2025-01-01', '2026-01-02', 'UTC'), /366 days/);
  assert.throws(() => analyticsWindow('custom', '2026-02-30', '2026-03-02', 'UTC'), /valid/);
  const csv = projectsCsv([{ ...analyticsProjectFixtures()[0], session_id: '=SUM(1,2)\n"quoted"' }]);
  assert(csv.includes(`"'=SUM(1,2)\n""quoted"""`));
  assert(csv.includes(',"",') && !csv.includes('null'));
  const baseProject = analyticsProjectFixtures()[1];
  const usageProjects = [
    { ...baseProject, session_id: 'first', owner_name: 'Owner Seven', total_tokens: 1000000, cost_usd: 2, agent_processing_ms: 120000, ai_turnaround_ms: 60000, slides_delivered: 10 },
    { ...baseProject, session_id: 'second', owner_name: 'Owner Seven', total_tokens: 2000000, cost_usd: 1, agent_processing_ms: 60000, ai_turnaround_ms: 30000, slides_delivered: 5 },
    { ...baseProject, session_id: 'third', owner_name: null, total_tokens: 1, cost_usd: 0, agent_processing_ms: 0, ai_turnaround_ms: null, slides_delivered: 0 },
    { ...baseProject, session_id: 'unpriced', cost_usd: null },
    { ...baseProject, session_id: 'no-time', agent_processing_ms: null },
    { ...baseProject, session_id: 'no-tokens', total_tokens: 0 },
  ];
  const originalUsage = JSON.stringify(usageProjects);
  const usage = buildProjectUsage(usageProjects);
  assert.deepEqual(usage.rows.map((row) => row.label), ['second', 'first', 'third']);
  assert.deepEqual(usage.excluded, ['unpriced', 'no-time', 'no-tokens']);
  assert.equal(usage.totals.total_tokens, 3000001);
  assert.equal(usage.totals.cost_usd, 3);
  assert.equal(usage.totals.agent_processing_ms, 180000);
  assert.equal(usage.totals.ai_turnaround_ms, null, 'Unknown turnaround cannot be silently counted as zero');
  assert.equal(usage.averageCost, 1);
  assert.equal(usage.costPerSlide, .2);
  assert.equal(usage.costPerMillionTokens, 3 / 3000001 * 1000000);
  assert.deepEqual(buildProjectUsage(usageProjects, 'cost').rows.map((row) => row.label), ['first', 'second', 'third']);
  const owners = buildProjectUsage(usageProjects, 'time', 'owner');
  assert.deepEqual(owners.rows.map((row) => row.label), ['Owner Seven (2)', 'Unknown owner (1)']);
  assert.equal(owners.rows[0].agent_processing_ms, 180000);
  assert.equal(owners.rows[0].ai_turnaround_ms, 90000);
  assert.equal(owners.rows[1].ai_turnaround_ms, null);
  assert.equal(JSON.stringify(usageProjects), originalUsage, 'Chart grouping/sorting cannot mutate table rows');
  const emptyUsage = buildProjectUsage([]);
  assert.equal(emptyUsage.totals.total_tokens, null);
  assert.equal(emptyUsage.averageTokens, null);
  assert.equal(emptyUsage.costPerMillionTokens, null);
  assert.equal(emptyUsage.costPerSlide, null);
  assert.equal(buildProjectUsage([{ ...baseProject, total_tokens: 1, slides_delivered: 0 }]).costPerSlide, null);
  log('PASS: analytics formatting, null/zero semantics, window boundaries, and CSV escaping');
  log('PASS: project usage totals, owner grouping, metric sorting, exclusions and input immutability');
}

async function diagnostics(h) {
  const data = { step: currentStep, ...h.state, projects: [...h.state.projects.values()],
    docs: [...h.state.docs], messages: [...h.state.messages], versions: [...h.state.versions] };
  data.analytics = { ...h.state.analytics, rows: h.state.analytics.rows.length };
  delete data.blobGate;
  try { data.transport = await snapshot(h); } catch { /* page unavailable */ }
  console.error('[smoke DIAGNOSTICS]', JSON.stringify(data, null, 2));
  try {
    fs.mkdirSync(ARTIFACT_DIR, { recursive: true });
    const prefix = path.join(ARTIFACT_DIR, h.state.label);
    fs.writeFileSync(`${prefix}.json`, JSON.stringify(data, null, 2));
    fs.writeFileSync(`${prefix}.html`, await h.page.content());
    await h.page.screenshot({ path: `${prefix}.png`, fullPage: true, timeout: 5000 });
    log(`Failure artifacts: ${prefix}.{json,html,png}`);
  } catch (error) { console.error(`[smoke] Could not save all artifacts: ${error.message}`); }
}

async function main() {
  log(`Frontend: ${BASE_URL.href}; backend/blob requests are mocked, never forwarded`);
  warn('Real SSO, approval mutations, confirmed deletion and PPTX validity are deliberately excluded.');
  const browser = await chromium.launch({ headless: true, args: ['--disable-background-networking', '--dns-prefetch-disable'] });
  try {
    for (const authenticated of (THOUGHT_PROCESS_ONLY || ANALYTICS_ONLY || DECK_FEEDBACK_ONLY || MESSAGES_ONLY || DECK_DOWNLOAD_ONLY ? [true] : [false, true])) {
      const h = await makeHarness(browser, authenticated);
      try {
        if (ANALYTICS_ONLY) await testAnalytics(h);
        else if (THOUGHT_PROCESS_ONLY) await testThoughtProcess(h);
        else if (DECK_FEEDBACK_ONLY) await testDeckFeedback(h);
        else if (MESSAGES_ONLY) await testMessages(h);
        else if (DECK_DOWNLOAD_ONLY) await testDeckDownload(h);
        else if (authenticated) await testAuthenticated(h); else await testLogin(h);
        await clean(h);
        const transport = await snapshot(h);
        log(`SUMMARY ${h.state.label}: API=${h.state.api.length}, blob PUTs=${h.state.blob.length}, `
          + `SSE=${transport.requests.length}, page errors=${h.state.pageErrors.length}, unexpected=${h.state.errors.length}`);
        log(`API audit ${h.state.label}: ${JSON.stringify(h.state.api)}`);
      } catch (error) {
        console.error(`[smoke FAIL] ${currentStep}: ${error.stack || error}`);
        await diagnostics(h);
        throw error;
      } finally {
        h.state.blobGate.resolve();
        await h.context.close();
      }
    }
    if (ANALYTICS_ONLY) await testAnalyticsAccess(browser);
    log(`PASS: all component-refactor smoke checks completed (${warnings.size} documented warning(s))`);
  } finally { await browser.close(); }
}

if (process.env.SMOKE_SCOPE === 'analytics-helpers') testAnalyticsHelpers();
else main().catch((error) => { console.error(`[smoke] ${error.message}`); process.exitCode = 1; });
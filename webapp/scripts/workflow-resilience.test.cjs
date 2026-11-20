#!/usr/bin/env node
'use strict';

/**
 * Workflow resilience tests (frontend spec §7). Runs the real workflow helpers, ApiService and
 * WorkspaceService against an in-process mocked backend with controllable SSE streams.
 * No browser or network needed:  node scripts/workflow-resilience.test.cjs
 * Requires `typescript` (local node_modules, or NODE_PATH inside the Docker build image).
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const ts = require('typescript');

const SRC = path.resolve(__dirname, '../src');
const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

// ---- Minimal browser environment for the services ----
class MemoryStorage {
  constructor() { this.map = new Map(); }
  getItem(k) { return this.map.has(k) ? this.map.get(k) : null; }
  setItem(k, v) { this.map.set(k, String(v)); }
  removeItem(k) { this.map.delete(k); }
  clear() { this.map.clear(); }
  key(i) { return [...this.map.keys()][i] ?? null; }
  get length() { return this.map.size; }
}
globalThis.localStorage = new MemoryStorage();
globalThis.sessionStorage = new MemoryStorage();
globalThis.window = { addEventListener() {}, removeEventListener() {}, dispatchEvent() { return true; } };

// ---- Load TypeScript sources directly (Angular's decorator is a no-op here) ----
const originalLoad = Module._load;
Module._load = function load(request, ...rest) {
  if (request === '@angular/core') return { Injectable: () => () => undefined };
  return originalLoad.call(this, request, ...rest);
};
require.extensions['.ts'] = (module, filename) => {
  const { outputText } = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    fileName: filename,
    compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
      experimentalDecorators: true, useDefineForClassFields: false,
    },
  });
  module._compile(outputText, filename);
};
const H = require(path.join(SRC, 'app/shared/workflow.helpers.ts'));
const { ApiService } = require(path.join(SRC, 'app/shared/services/api.service.ts'));
const { StoreService } = require(path.join(SRC, 'app/shared/services/store.service.ts'));
const { WorkspaceService } = require(path.join(SRC, 'app/shared/services/workspace.service.ts'));

// ---- Test utilities ----
const results = [];
async function test(name, fn) {
  try { await fn(); results.push([true, name]); console.log(`  ok   ${name}`); }
  catch (err) { results.push([false, name]); console.error(`  FAIL ${name}\n${err.stack || err}`); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function eventually(check, label = 'condition', timeout = 4000) {
  const deadline = Date.now() + timeout;
  let last;
  while (Date.now() < deadline) {
    try { const ok = await check(); if (ok !== false) return; last = new Error(`${label} not met`); }
    catch (err) { last = err; }
    await sleep(5);
  }
  throw last || new Error(`${label} timed out`);
}

const NOW = '2026-10-09T08:00:00.000Z';
function runRecord(runId, status, extra = {}) {
  const active = ['RUNNING', 'RECOVERING', 'CANCELLING'].includes(status);
  return {
    run_id: runId, session_id: 'web-test', client_request_id: null, status, is_active: active,
    current_stage: null, attempt: 1, last_event_index: 0, deck_version: null, artifact_ready: false,
    error_code: null, error: null, retryable: false, cancel_requested: false,
    created_at: NOW, updated_at: NOW, finished_at: active ? null : NOW, ...extra,
  };
}
const runStarted = (runId, status = 'started') => ({
  type: 'run_started', agent: 'supervisor', message: 'Starting', session_id: 'web-test', run_id: runId,
  data: { status, workflow_status: 'RUNNING', stream_resume_supported: true },
});
const ev = (runId, index, type, agent, message, data = {}) => ({
  type, agent, message, data, session_id: 'web-test', run_id: runId, event_index: index, ts: NOW,
});
const done = (runId, index, data) => ({ ...ev(runId, index, 'done', 'supervisor', 'Finished', data) });

function sseChannel(signal) {
  const enc = new TextEncoder();
  let controller;
  let open = true;
  const stream = new ReadableStream({ start(c) { controller = c; }, cancel() { open = false; } });
  signal?.addEventListener('abort', () => {
    if (!open) return;
    open = false;
    try { controller.error(new DOMException('Aborted', 'AbortError')); } catch { /* already closed */ }
  }, { once: true });
  return {
    stream,
    get open() { return open; },
    send(evt) {
      assert(open, `SSE channel closed before sending ${evt.type}`);
      const id = evt.event_index != null ? `id: ${evt.event_index}\n` : '';
      controller.enqueue(enc.encode(`${id}data: ${JSON.stringify(evt)}\n\n`));
    },
    keepalive() { controller.enqueue(enc.encode(': keepalive\n\n')); },
    close() { if (open) { open = false; controller.close(); } },
  };
}

function makeBackend() {
  const b = {
    requests: [], channels: [], runs: new Map(),
    current: { active: null, latest: null },
    stopResponse: { status: 'not_running', run_id: null, run: null },
    invokeFailures: 0, runFailures: 0, runGate: null, onEvents: null,
    messages: { messages: [], timeline: [] },
    docs: [{ session_id: 'web-test', doc_id: 'doc-1', version: 1, filename: 'source.pdf', status: 'indexed', is_latest: true }],
    versions: [],
  };
  const json = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
  const sse = (ch) => new Response(ch.stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  b.fetch = async (input, opts = {}) => {
    const url = new URL(String(input));
    const method = (opts.method || 'GET').toUpperCase();
    const body = opts.body ? JSON.parse(opts.body) : null;
    const p = url.pathname.replace(/^\/api/, '');
    b.requests.push({ method, path: p, search: url.search, body });
    let m;
    if (method === 'POST' && /^\/decks\/[^/]+\/invoke$/.test(p)) {
      if (b.invokeFailures > 0) { b.invokeFailures--; throw new TypeError('Failed to fetch'); }
      const ch = sseChannel(opts.signal);
      b.channels.push({ kind: 'invoke', body, ch });
      return sse(ch);
    }
    if (/^\/decks\/[^/]+\/runs\/current$/.test(p)) return json({ session_id: 'web-test', ...b.current });
    if ((m = p.match(/^\/decks\/[^/]+\/runs\/([^/]+)\/events$/))) {
      const ch = sseChannel(opts.signal);
      const channel = { kind: 'events', runId: decodeURIComponent(m[1]), after: Number(url.searchParams.get('after_event_index')), ch };
      b.channels.push(channel);
      if (b.onEvents) b.onEvents(channel);
      return sse(ch);
    }
    if ((m = p.match(/^\/decks\/[^/]+\/runs\/([^/]+)$/))) {
      if (b.runGate) await b.runGate;
      if (b.runFailures > 0) { b.runFailures--; throw new TypeError('Failed to fetch'); }
      const run = b.runs.get(decodeURIComponent(m[1]));
      return run ? json(run) : json({ detail: 'run not found' }, 404);
    }
    if (method === 'POST' && /^\/decks\/[^/]+\/stop$/.test(p)) return json({ session_id: 'web-test', ...b.stopResponse });
    if (p.endsWith('/messages')) return json(b.messages);
    if (p.endsWith('/documents')) return json({ documents: b.docs });
    if (p.endsWith('/versions')) return json({ versions: b.versions });
    if (p.endsWith('/stream')) { const ch = sseChannel(opts.signal); b.channels.push({ kind: 'stream', ch }); return sse(ch); }
    return json({ detail: `unmocked ${method} ${p}` }, 404);
  };
  b.count = (method, re) => b.requests.filter((r) => r.method === method && re.test(r.path)).length;
  b.invokes = () => b.channels.filter((c) => c.kind === 'invoke');
  b.lastInvoke = () => b.invokes().at(-1);
  b.events = (runId) => b.channels.filter((c) => c.kind === 'events' && (!runId || c.runId === runId));
  return b;
}

const live = [];
function makeWorkspace(backend, { resetStorage = true } = {}) {
  if (resetStorage) { localStorage.clear(); sessionStorage.clear(); }
  globalThis.fetch = backend.fetch;
  localStorage.setItem('se_access', 'synthetic-token');
  localStorage.setItem('se_user', JSON.stringify({ id: 'u1', username: 'tester', displayName: 'Tester', roles: [] }));
  const config = { getApiBaseUrl: () => 'http://backend.test/api' };
  const api = new ApiService(config);
  const store = new StoreService();
  const toasts = [];
  const ui = { toast: (m, k) => toasts.push({ m, k }), confirmDialog: async () => true, askText: async () => null };
  const deckTimer = {
    startPending: async () => {}, discardPending: async () => {}, finalizeWith: async () => {},
    finalizePending: async () => null, listForSession: async () => new Map(), clearForSession: async () => {},
  };
  const ws = new WorkspaceService(api, store, {}, ui, config, { applyStoredTheme() {} }, deckTimer);
  ws.view = 'app';
  const sid = 'web-test';
  if (!store.get(sid)) store.upsert({ id: sid, title: 'Test project', createdAt: NOW });
  live.push(ws);
  return { ws, store, toasts, sid };
}
async function open(ctx) {
  ctx.ws.selectSession(ctx.sid);
  await eventually(() => ctx.ws.hasSources && !ctx.ws.messagesLoading, 'session hydrated');
}
async function submit(ctx, backend, text) {
  const before = backend.invokes().length;
  ctx.ws.chatInputValue = text;
  ctx.ws.onChatSubmit();
  await eventually(() => backend.invokes().length > before, 'invoke POST');
  return backend.lastInvoke();
}
const tracker = (ctx) => ctx.ws.runs.get(ctx.sid);

async function main() {
  console.log('Pure workflow helpers');

  await test('reducer ignores other runs, duplicates and late events after terminal', () => {
    let s = H.newWorkflowState('web-test');
    s = H.applyRunEvent(s, runStarted('A')).state;
    assert.equal(s.activeRunId, 'A');
    let r = H.applyRunEvent(s, ev('B', 1, 'agent_step', 'slide', 'other run'));
    assert.equal(r.accepted, false);
    assert.equal(r.state, s);
    r = H.applyRunEvent(s, ev('A', 1, 'agent_step', 'slide', 'one'));
    assert(r.accepted);
    s = r.state;
    assert.equal(s.lastEventIndex, 1);
    assert.equal(H.applyRunEvent(s, ev('A', 1, 'agent_step', 'slide', 'dup')).accepted, false);
    s = H.applyRunEvent(s, done('A', 2, { status: 'COMPLETED', result: {} })).state;
    assert.equal(s.terminal.status, 'COMPLETED');
    const late = H.applyRunEvent(s, ev('A', 3, 'agent_step', 'slide', 'late'));
    assert.equal(late.accepted, false);
    assert.equal(late.state.terminal.status, 'COMPLETED');
    assert.equal(H.applyRunEvent(s, done('A', 4, { status: 'FAILED', error: 'x' })).state.terminal.status, 'COMPLETED');
  });

  await test('duplicate_request run_started for the same run keeps progress; a new run resets it', () => {
    let s = H.applyRunEvent(H.newWorkflowState('w'), runStarted('A')).state;
    s = H.applyRunEvent(s, ev('A', 5, 'agent_step', 'slide', 'x')).state;
    const again = H.applyRunEvent(s, runStarted('A', 'duplicate_request'));
    assert.equal(again.accepted, false);
    assert.equal(again.state.lastEventIndex, 5);
    const other = H.applyRunEvent(s, runStarted('B', 'already_running')).state;
    assert.equal(other.activeRunId, 'B');
    assert.equal(other.lastEventIndex, 0);
  });

  await test('recovering / recovered / cancelled transitions', () => {
    let s = H.applyRunEvent(H.newWorkflowState('w'), runStarted('A')).state;
    s = H.applyRunEvent(s, ev('A', 1, 'workflow_recovering', 'supervisor', 'Retrying automatically.', { attempt: 2 })).state;
    assert.equal(s.runStatus, 'RECOVERING');
    assert.equal(H.runNotice(s).text, 'Retrying automatically.');
    s = H.applyRunEvent(s, ev('A', 2, 'workflow_recovered', 'supervisor', 'Recovered')).state;
    assert.equal(s.runStatus, 'RUNNING');
    assert.equal(H.runNotice(s), null);
    s = H.applyRunEvent(s, ev('A', 3, 'workflow_cancelled', 'supervisor', 'Stopping', { error_code: 'CANCELLED_BY_USER' })).state;
    assert.equal(s.runStatus, 'CANCELLING');
    assert.equal(H.controlsFor(s).stop, 'stopping');
  });

  await test('admission failure (run-less done) is a retryable terminal with no run', () => {
    const s0 = { ...H.newWorkflowState('w'), pendingClientRequestId: 'id', connection: 'connecting' };
    const { state, accepted } = H.applyRunEvent(s0, { type: 'done', run_id: null,
      data: { status: 'FAILED', error_code: 'SERVICE_UNAVAILABLE', retryable: true } });
    assert(accepted);
    assert.equal(state.activeRunId, null);
    assert.equal(state.terminal.errorCode, 'SERVICE_UNAVAILABLE');
    assert.equal(state.terminal.error, H.ERROR_COPY.SERVICE_UNAVAILABLE);
    assert.deepEqual(H.controlsFor(state, { hasRetryMessage: true }), { send: true, stop: 'hidden', retry: 'try-again' });
    assert.equal(H.applyRunEvent(s0, { type: 'ingest_step', message: 'indexing' }).accepted, false);
  });

  await test('terminal outcome branches on status / error_code, not on text', () => {
    assert.deepEqual(H.terminalFromDone({ type: 'done', data: { error: 'legacy raw' } }),
      { status: 'FAILED', errorCode: null, error: 'legacy raw', retryable: true });
    assert.deepEqual(H.terminalFromDone({ type: 'done', data: { status: 'FAILED', error_code: 'LLM_AUTH_FAILED' } }),
      { status: 'FAILED', errorCode: 'LLM_AUTH_FAILED', error: H.ERROR_COPY.LLM_AUTH_FAILED, retryable: false });
    const cancelled = H.terminalFromDone({ type: 'done', data: { status: 'CANCELLED' } });
    assert.equal(cancelled.errorCode, 'CANCELLED_BY_USER');
    assert.equal(cancelled.retryable, false);
    assert.equal(H.terminalFromDone({ type: 'done', data: { status: 'COMPLETED', result: {} } }).error, null);
  });

  await test('controls table (spec §5.7)', () => {
    const base = H.newWorkflowState('w');
    const running = { ...base, activeRunId: 'A', runStatus: 'RUNNING', connection: 'open' };
    assert.deepEqual(H.controlsFor(running), { send: false, stop: 'enabled', retry: 'none' });
    assert.deepEqual(H.controlsFor({ ...running, runStatus: 'RECOVERING', recovering: true }), { send: false, stop: 'enabled', retry: 'none' });
    assert.deepEqual(H.controlsFor({ ...running, runStatus: 'CANCELLING', stopping: true }), { send: false, stop: 'stopping', retry: 'none' });
    assert.deepEqual(H.controlsFor({ ...running, connection: 'reconnecting' }), { send: false, stop: 'enabled', retry: 'reconnect' });
    const failed = (retryable) => ({ ...running, connection: 'idle', runStatus: 'FAILED', terminal: { status: 'FAILED', errorCode: 'X', error: 'e', retryable } });
    assert.deepEqual(H.controlsFor(failed(true), { hasRetryMessage: true }), { send: true, stop: 'hidden', retry: 'try-again' });
    assert.deepEqual(H.controlsFor(failed(false), { hasRetryMessage: true }), { send: true, stop: 'hidden', retry: 'none' });
    assert.deepEqual(H.controlsFor(base), { send: true, stop: 'hidden', retry: 'none' });
    assert.deepEqual(H.controlsFor({ ...base, connection: 'connecting', pendingClientRequestId: 'x' }), { send: false, stop: 'hidden', retry: 'none' });
  });

  await test('SSE parser: id lines, keepalive comments and split frames', () => {
    const keepalive = H.parseSseFrame(': keepalive');
    assert.deepEqual(keepalive, { id: null, data: null, comment: true });
    const frame = H.parseSseFrame('id: 126\ndata: {"type":"agent_step","run_id":"A"}');
    assert.equal(frame.id, 126);
    assert.equal(frame.data.event_index, 126);
    const { blocks, rest } = H.splitSseBuffer('data: {"a":1}\n\n: keepalive\n\ndata: {"b"');
    assert.equal(blocks.length, 2);
    assert.equal(rest, 'data: {"b"');
    assert.equal(H.parseSseFrame('data: not json').data, null);
  });

  await test('retry backoff, uuid v4 and status helpers', () => {
    assert.deepEqual([0, 1, 2, 3, 4, 9].map(H.retryDelayMs), [1000, 2000, 5000, 10000, 10000, 10000]);
    assert.match(H.newClientRequestId(), UUID_V4);
    const savedCrypto = Object.getOwnPropertyDescriptor(globalThis, 'crypto');
    Object.defineProperty(globalThis, 'crypto', { value: undefined, configurable: true });
    try { assert.match(H.newClientRequestId(), UUID_V4); }
    finally { Object.defineProperty(globalThis, 'crypto', savedCrypto); }
    assert(H.isActiveRunStatus('recovering'));
    assert(!H.isActiveRunStatus('COMPLETED'));
    assert(H.isTerminalRunStatus('CANCELLED'));
  });

  console.log('\nWorkspace run lifecycle (mocked backend + SSE)');

  await test('keepalive comments reset the dead-connection timer; silence ends the stream as a disconnect', async () => {
    const api = new ApiService({ getApiBaseUrl: () => 'http://backend.test/api' });
    const ch = sseChannel();
    const events = [];
    const started = Date.now();
    const reading = api.readSse(new Response(ch.stream), (e) => events.push(e), { stopOnDone: true, idleMs: 80 });
    for (let i = 0; i < 6; i++) { await sleep(40); ch.keepalive(); }
    ch.send(ev('A', 1, 'agent_step', 'slide', 'still alive'));
    const result = await reading;
    assert(Date.now() - started >= 280, 'keepalives kept the stream open past the idle window');
    assert.deepEqual(result, { sawDone: false, aborted: false });
    assert.equal(events.length, 1, 'comment frames are not delivered as events');
  });

  await test('submit sends client_request_id, filters mixed/duplicate events and auto-loads the completed deck', async () => {
    const backend = makeBackend();
    const ctx = makeWorkspace(backend);
    await open(ctx);
    const inv = await submit(ctx, backend, 'Build a deck');
    assert.equal(inv.body.message, 'Build a deck');
    assert.match(inv.body.client_request_id, UUID_V4);
    assert.equal(JSON.parse(sessionStorage.getItem('se_pending_req:web-test')).clientRequestId, inv.body.client_request_id);
    assert.equal(ctx.ws.runControls.send, false, 'Send disabled while submitting');
    inv.ch.send(runStarted('run-A'));
    await eventually(() => ctx.ws.runControls.stop === 'enabled', 'stop visible');
    assert.equal(sessionStorage.getItem('se_pending_req:web-test'), null, 'pending id cleared after run_started');
    inv.ch.send(ev('run-A', 1, 'agent_start', 'planner', 'Planning'));
    inv.ch.send(ev('run-A', 2, 'agent_step', 'slide', 'Slide 1/1 complete', { slide_number: 1, total: 1, phase: 'done' }));
    inv.ch.send(ev('run-B', 3, 'agent_step', 'slide', 'Other run'));
    inv.ch.send(ev('run-A', 2, 'agent_step', 'slide', 'Duplicate'));
    inv.ch.keepalive();
    inv.ch.send({ type: 'ingest_step', agent: 'ingest', message: 'run-less ingest' });
    await eventually(() => ctx.ws.liveTurn?.events.length === 2, 'two events applied');
    assert(!ctx.ws.liveTurn.events.some((e) => /Other run|Duplicate|ingest/.test(e.message)));
    assert.equal(ctx.ws.statusPillText, 'running');
    const result = { reply: 'Your deck is ready', deck_updated: true, deck_version: 1 };
    backend.versions = [{ deck_version: 1, plan_title: 'Deck', files: { English: 'en.pptx' } }];
    backend.messages = { messages: [], timeline: [
      { kind: 'message', id: 1, run_id: 'run-A', role: 'user', content: 'Build a deck', result: null, ts: NOW, run_status: 'COMPLETED' },
      { kind: 'message', id: 2, run_id: 'run-A', role: 'assistant', content: 'Your deck is ready', result, ts: NOW, run_status: 'COMPLETED' },
    ] };
    const messageLoads = backend.count('GET', /\/messages$/);
    inv.ch.send(done('run-A', 3, { status: 'COMPLETED', result }));
    await eventually(() => ctx.store.get(ctx.sid).status === 'done' && ctx.ws.runControls.send, 'completed');
    await eventually(() => backend.count('GET', /\/messages$/) > messageLoads, 'messages refetched');
    await eventually(() => backend.requests.some((r) => r.path.endsWith('/versions')), 'versions refetched');
    assert.equal(ctx.store.get(ctx.sid).deckVersion, 1);
    assert.equal(ctx.ws.liveTurn, null);
    assert.equal(ctx.ws.runNotice, null);
    assert.equal(backend.invokes().length, 1);
  });

  await test('SSE disconnect shows Reconnecting (not Failed) and resumes after lastEventIndex without re-POSTing', async () => {
    const backend = makeBackend();
    const ctx = makeWorkspace(backend);
    await open(ctx);
    const inv = await submit(ctx, backend, 'Long deck');
    inv.ch.send(runStarted('run-C'));
    for (let i = 1; i <= 3; i++) inv.ch.send(ev('run-C', i, 'agent_step', 'slide', `Slide ${i}/5 complete`, { slide_number: i, total: 5, phase: 'done' }));
    await eventually(() => ctx.ws.liveTurn?.events.length === 3);
    backend.runs.set('run-C', runRecord('run-C', 'RUNNING', { last_event_index: 3 }));
    let release;
    backend.runGate = new Promise((r) => { release = r; });
    inv.ch.close();
    await eventually(() => ctx.ws.runNotice?.text === H.RECONNECTING_COPY, 'reconnecting notice');
    assert.equal(ctx.ws.statusPillText, 'reconnecting');
    assert.notEqual(ctx.store.get(ctx.sid).status, 'failed');
    assert(!ctx.store.get(ctx.sid).messages.some((m) => m.content?.startsWith('⚠')));
    assert.equal(ctx.ws.runControls.retry, 'reconnect');
    assert.equal(ctx.ws.runControls.send, false);
    backend.runGate = null;
    release();
    await eventually(() => backend.events('run-C').length === 1, 'events stream opened');
    assert.equal(backend.events('run-C')[0].after, 3);
    const stream = backend.events('run-C')[0].ch;
    stream.send(ev('run-C', 3, 'agent_step', 'slide', 'Replayed duplicate'));
    stream.send(ev('run-C', 4, 'agent_step', 'slide', 'Slide 4/5 complete', { slide_number: 4, total: 5, phase: 'done' }));
    await eventually(() => ctx.ws.liveTurn?.events.length === 4);
    await eventually(() => ctx.ws.runNotice === null && ctx.ws.statusPillText === 'running', 'connection open again');
    stream.send(done('run-C', 5, { status: 'COMPLETED', result: { reply: 'Done', deck_updated: true, deck_version: 2 } }));
    await eventually(() => ctx.store.get(ctx.sid).status === 'done');
    assert.equal(backend.invokes().length, 1, 'reconnect never re-POSTs /invoke');
  });

  await test('Reconnect now retries immediately after a failed reconnect attempt', async () => {
    const backend = makeBackend();
    const ctx = makeWorkspace(backend);
    await open(ctx);
    const inv = await submit(ctx, backend, 'Flaky network');
    inv.ch.send(runStarted('run-R'));
    inv.ch.send(ev('run-R', 1, 'agent_step', 'planner', 'Planning'));
    await eventually(() => ctx.ws.liveTurn?.events.length === 1);
    backend.runs.set('run-R', runRecord('run-R', 'RUNNING'));
    backend.runFailures = 1;
    inv.ch.close();
    await eventually(() => backend.count('GET', /\/runs\/run-R$/) === 1 && !tracker(ctx).controller, 'first reconnect failed');
    assert.equal(ctx.ws.runControls.retry, 'reconnect');
    ctx.ws.onReconnectNowClick();
    await eventually(() => backend.events('run-R').length === 1, 'manual reconnect', 500);
    backend.events('run-R')[0].ch.send(done('run-R', 2, { status: 'COMPLETED', result: { reply: 'ok' } }));
    await eventually(() => ctx.store.get(ctx.sid).status === 'answered');
    assert.equal(backend.invokes().length, 1);
  });

  await test('refresh during generation attaches to the active run (full replay), shows recovery, and completes from snapshot after a drop', async () => {
    const backend = makeBackend();
    const first = makeWorkspace(backend);
    await open(first);
    first.ws.destroy();
    const active = runRecord('run-D', 'RUNNING', { last_event_index: 2 });
    backend.current = { active, latest: active };
    backend.runs.set('run-D', active);
    backend.requests.length = 0;
    const ctx = makeWorkspace(backend, { resetStorage: false });
    await open(ctx);
    await eventually(() => backend.events('run-D').length === 1, 'attached');
    assert.equal(backend.events('run-D')[0].after, 0);
    assert.equal(backend.count('POST', /\/invoke$/), 0, 'refresh never POSTs /invoke');
    const stream = backend.events('run-D')[0].ch;
    stream.send(ev('run-D', 1, 'agent_start', 'planner', 'Planning'));
    stream.send(ev('run-D', 2, 'agent_step', 'slide', 'Slide 1/2 complete', { slide_number: 1, total: 2, phase: 'done' }));
    await eventually(() => ctx.ws.liveTurn?.events.length === 2 && ctx.ws.runControls.stop === 'enabled');
    stream.send(ev('run-D', 3, 'workflow_recovering', 'supervisor',
      'A temporary issue occurred while preparing the deck plan. Retrying automatically.', { stage: 'planning', attempt: 2, max_attempts: 3 }));
    await eventually(() => ctx.ws.runNotice?.tone === 'warning' && ctx.ws.statusPillText === 'recovering');
    assert.match(ctx.ws.runNotice.text, /Retrying automatically/);
    assert.equal(ctx.ws.liveTurn.events.length, 3, 'progress preserved while recovering');
    stream.send(ev('run-D', 4, 'workflow_recovered', 'supervisor', 'Recovered successfully. Continuing deck generation.'));
    await eventually(() => ctx.ws.runNotice === null && ctx.ws.statusPillText === 'running');
    backend.runs.set('run-D', runRecord('run-D', 'COMPLETED', { artifact_ready: true, deck_version: 2, last_event_index: 9 }));
    backend.current = { active: null, latest: backend.runs.get('run-D') };
    backend.onEvents = (channel) => channel.ch.close();
    const loads = backend.count('GET', /\/messages$/);
    stream.close();
    await eventually(() => ctx.store.get(ctx.sid).status === 'done', 'completed from snapshot');
    assert.equal(ctx.store.get(ctx.sid).deckVersion, 2);
    await eventually(() => backend.count('GET', /\/messages$/) > loads, 'deck/messages reloaded');
    assert.equal(ctx.ws.liveTurn, null);
    assert.equal(backend.count('POST', /\/invoke$/), 0);
  });

  await test('already_running attaches to the returned run and keeps the user text', async () => {
    const backend = makeBackend();
    const ctx = makeWorkspace(backend);
    await open(ctx);
    const inv = await submit(ctx, backend, 'My new message');
    assert.equal(ctx.ws.chatInputValue, '');
    inv.ch.send(runStarted('run-E', 'already_running'));
    await eventually(() => ctx.ws.chatInputValue === 'My new message', 'text restored');
    assert(!ctx.ws.messagesForCurrent.some((m) => m.role === 'user' && m.content === 'My new message'));
    assert(ctx.toasts.some((t) => t.m === H.ALREADY_RUNNING_COPY));
    assert.equal(ctx.ws.runNotice?.text, H.ALREADY_RUNNING_COPY);
    assert.equal(tracker(ctx).state.activeRunId, 'run-E');
    inv.ch.send(ev('run-E', 7, 'agent_step', 'slide', 'Other tab progress'));
    await eventually(() => ctx.ws.liveTurn?.events.length === 1);
    inv.ch.send(done('run-E', 8, { status: 'COMPLETED', result: { reply: 'other tab done' } }));
    await eventually(() => ctx.ws.runControls.send && ctx.ws.runNotice === null);
  });

  await test('failure before run_started resends the SAME client_request_id; Try again uses a NEW one', async () => {
    const backend = makeBackend();
    const ctx = makeWorkspace(backend);
    await open(ctx);
    backend.invokeFailures = 1;
    ctx.ws.chatInputValue = 'Retry me';
    ctx.ws.onChatSubmit();
    await eventually(() => ctx.ws.runNotice?.text === 'Connecting to the service…', 'connecting notice');
    await eventually(() => backend.invokes().length === 1, 'resent after backoff', 4000);
    const posts = backend.requests.filter((r) => r.method === 'POST' && r.path.endsWith('/invoke'));
    assert.equal(posts.length, 2);
    assert.equal(posts[0].body.client_request_id, posts[1].body.client_request_id);
    const inv = backend.lastInvoke();
    inv.ch.send(runStarted('run-F'));
    inv.ch.send(done('run-F', 1, { status: 'FAILED', error: H.ERROR_COPY.LLM_TIMEOUT, error_code: 'LLM_TIMEOUT', retryable: true }));
    await eventually(() => ctx.ws.runNotice?.tone === 'danger', 'failure notice');
    assert.equal(ctx.ws.runNotice.text, H.ERROR_COPY.LLM_TIMEOUT);
    assert.equal(ctx.ws.runNotice.action, 'try-again');
    assert.equal(ctx.ws.runControls.retry, 'try-again');
    assert.equal(ctx.ws.runControls.send, true);
    assert.equal(ctx.store.get(ctx.sid).status, 'failed');
    assert.equal(ctx.ws.messagesForCurrent.filter((m) => m.role === 'user').at(-1).run_status, 'FAILED');
    ctx.ws.onTryAgainClick();
    await eventually(() => backend.invokes().length === 2, 'try again POST');
    const retry = backend.lastInvoke();
    assert.equal(retry.body.message, 'Retry me');
    assert.notEqual(retry.body.client_request_id, posts[0].body.client_request_id);
    retry.ch.send({ type: 'done', agent: 'supervisor', message: 'Unavailable', run_id: null,
      data: { status: 'FAILED', error: H.ERROR_COPY.SERVICE_UNAVAILABLE, error_code: 'SERVICE_UNAVAILABLE', retryable: true } });
    await eventually(() => ctx.ws.runNotice?.text === H.ERROR_COPY.SERVICE_UNAVAILABLE, 'admission failure');
    assert.equal(tracker(ctx).state.activeRunId, null);
    assert.equal(ctx.ws.runControls.retry, 'try-again');
    assert.equal(ctx.ws.liveTurn, null);
  });

  await test('non-retryable failure shows safe copy without Try again', async () => {
    const backend = makeBackend();
    const ctx = makeWorkspace(backend);
    await open(ctx);
    const inv = await submit(ctx, backend, 'Auth broken');
    inv.ch.send(runStarted('run-G'));
    inv.ch.send(done('run-G', 1, { status: 'FAILED', error: H.ERROR_COPY.LLM_AUTH_FAILED, error_code: 'LLM_AUTH_FAILED', retryable: false }));
    await eventually(() => ctx.ws.runNotice?.tone === 'danger');
    assert.equal(ctx.ws.runNotice.text, H.ERROR_COPY.LLM_AUTH_FAILED);
    assert.equal(ctx.ws.runNotice.action, null);
    assert.equal(ctx.ws.runControls.retry, 'none');
    ctx.ws.dismissRunNotice();
    assert.equal(ctx.ws.runNotice, null);
  });

  await test('Stop: POST /stop with run_id → Stopping… until done(CANCELLED) → stopped turn, text restored', async () => {
    const backend = makeBackend();
    const ctx = makeWorkspace(backend);
    await open(ctx);
    const inv = await submit(ctx, backend, 'Stop me');
    inv.ch.send(runStarted('run-S'));
    inv.ch.send(ev('run-S', 1, 'agent_step', 'planner', 'Planning'));
    await eventually(() => ctx.ws.runControls.stop === 'enabled');
    backend.stopResponse = { status: 'cancelling', run_id: 'run-S', run: runRecord('run-S', 'CANCELLING') };
    await ctx.ws.onStopClick();
    const stopReq = backend.requests.find((r) => r.method === 'POST' && r.path.endsWith('/stop'));
    assert.deepEqual(stopReq.body, { run_id: 'run-S' });
    assert.equal(ctx.ws.runControls.stop, 'stopping');
    assert.equal(ctx.ws.runNotice?.text, H.STOPPING_COPY);
    assert.equal(ctx.ws.statusPillText, 'stopping');
    assert.equal(ctx.ws.runControls.send, false);
    backend.messages = { messages: [], timeline: [
      { kind: 'message', id: 11, run_id: 'run-S', role: 'user', content: 'Stop me', result: null, ts: NOW, run_status: 'CANCELLED' },
    ] };
    inv.ch.send(ev('run-S', 2, 'workflow_cancelled', 'supervisor', 'Stopping', { error_code: 'CANCELLED_BY_USER' }));
    inv.ch.send(done('run-S', 3, { status: 'CANCELLED', error: H.ERROR_COPY.CANCELLED_BY_USER, error_code: 'CANCELLED_BY_USER', retryable: false }));
    await eventually(() => ctx.store.get(ctx.sid).status === 'stopped', 'stopped');
    assert.equal(ctx.ws.chatInputValue, 'Stop me');
    assert.equal(ctx.ws.runNotice?.text, H.ERROR_COPY.CANCELLED_BY_USER);
    assert.equal(ctx.ws.runControls.stop, 'hidden');
    assert.equal(ctx.ws.runControls.send, true);
    await eventually(() => ctx.ws.messagesForCurrent.some((m) => m.role === 'user' && m.run_status === 'CANCELLED'), 'Stopped badge from run_status');
    assert(!ctx.ws.messagesForCurrent.some((m) => m.role === 'assistant'), 'no assistant reply for a stopped run');
  });

  await test('Stop outcomes: completing shows Finishing…, not_running re-reads the run', async () => {
    const backend = makeBackend();
    const ctx = makeWorkspace(backend);
    await open(ctx);
    let inv = await submit(ctx, backend, 'Almost done');
    inv.ch.send(runStarted('run-P'));
    await eventually(() => ctx.ws.runControls.stop === 'enabled');
    backend.stopResponse = { status: 'completing', run_id: 'run-P', run: null };
    await ctx.ws.onStopClick();
    assert.equal(ctx.ws.runControls.stop, 'completing');
    assert.equal(ctx.ws.runNotice?.text, H.COMPLETING_COPY);
    assert(ctx.toasts.some((t) => t.m === H.COMPLETING_COPY));
    inv.ch.send(done('run-P', 1, { status: 'COMPLETED', result: { reply: 'saved' } }));
    await eventually(() => ctx.ws.runControls.stop === 'hidden' && ctx.ws.runNotice === null);

    inv = await submit(ctx, backend, 'Stale view');
    inv.ch.send(runStarted('run-Q'));
    await eventually(() => ctx.ws.runControls.stop === 'enabled');
    backend.runs.set('run-Q', runRecord('run-Q', 'COMPLETED'));
    backend.onEvents = (channel) => channel.ch.send(done('run-Q', 1, { status: 'COMPLETED', result: { reply: 'finished already' } }));
    backend.stopResponse = { status: 'not_running', run_id: null, run: null };
    await ctx.ws.onStopClick();
    await eventually(() => backend.count('GET', /\/runs\/run-Q$/) === 1, 'run re-read');
    await eventually(() => ctx.ws.runControls.send && ctx.ws.runControls.stop === 'hidden', 'settled');
    assert.equal(backend.invokes().length, 2);
  });

  await test('Stop at the very end: completing still tells the user even when done arrives first', async () => {
    const backend = makeBackend();
    const ctx = makeWorkspace(backend);
    await open(ctx);
    const inv = await submit(ctx, backend, 'Race to the end');
    inv.ch.send(runStarted('run-X'));
    await eventually(() => ctx.ws.runControls.stop === 'enabled');
    backend.stopResponse = { status: 'Completing ', run_id: 'run-X', run: null };
    const realFetch = backend.fetch;
    globalThis.fetch = async (input, opts = {}) => {
      if (String(input).endsWith('/stop')) inv.ch.send(done('run-X', 1, { status: 'COMPLETED', result: { reply: 'saved' } }));
      return realFetch(input, opts);
    };
    await ctx.ws.onStopClick();
    globalThis.fetch = realFetch;
    await eventually(() => ctx.ws.runControls.send, 'run finished');
    assert(ctx.toasts.some((t) => t.m === H.COMPLETING_COPY), 'Almost done toast shown');
    assert.equal(ctx.store.get(ctx.sid).status, 'answered');
  });

  await test('Network offline shows Reconnecting immediately; online resumes after lastEventIndex and finishes', async () => {
    const backend = makeBackend();
    const ctx = makeWorkspace(backend);
    await open(ctx);
    const inv = await submit(ctx, backend, 'Go offline');
    inv.ch.send(runStarted('run-N'));
    inv.ch.send(ev('run-N', 1, 'agent_step', 'slide', 'Slide 1/3 complete', { slide_number: 1, total: 3, phase: 'done' }));
    inv.ch.send(ev('run-N', 2, 'agent_step', 'slide', 'Slide 2/3 complete', { slide_number: 2, total: 3, phase: 'done' }));
    await eventually(() => ctx.ws.liveTurn?.events.length === 2);
    backend.runs.set('run-N', runRecord('run-N', 'RUNNING', { last_event_index: 2 }));
    backend.runFailures = 100;
    // The stream stays open (no error), like DevTools Offline or a dead link.
    ctx.ws.onNetworkOffline();
    assert.equal(ctx.ws.runNotice?.text, H.RECONNECTING_COPY);
    assert.equal(ctx.ws.runNotice.action, 'reconnect');
    assert.equal(ctx.ws.runControls.retry, 'reconnect');
    assert.notEqual(ctx.store.get(ctx.sid).status, 'failed');
    assert(!inv.ch.open, 'stalled stream was dropped');
    await sleep(60);
    assert.equal(ctx.ws.runNotice?.text, H.RECONNECTING_COPY, 'notice persists while offline');
    backend.runFailures = 0;
    ctx.ws.onNetworkOnline();
    await eventually(() => backend.events('run-N').length === 1, 'resumed on online');
    assert.equal(backend.events('run-N')[0].after, 2);
    const stream = backend.events('run-N')[0].ch;
    stream.send(ev('run-N', 3, 'agent_step', 'slide', 'Slide 3/3 complete', { slide_number: 3, total: 3, phase: 'done' }));
    await eventually(() => ctx.ws.runNotice === null && ctx.ws.liveTurn?.events.length === 3, 'progress continues');
    stream.send(done('run-N', 4, { status: 'COMPLETED', result: { reply: 'Done', deck_updated: true, deck_version: 1 } }));
    await eventually(() => ctx.store.get(ctx.sid).status === 'done', 'deck finished');
    assert.equal(backend.invokes().length, 1, 'never re-POSTs /invoke');
  });

  await test('refresh mid-submit resends the pending message with the same client_request_id', async () => {
    const backend = makeBackend();
    const ctx = makeWorkspace(backend);
    const id = '11111111-1111-4111-8111-111111111111';
    sessionStorage.setItem('se_pending_req:web-test', JSON.stringify({ message: 'Pending msg', clientRequestId: id }));
    ctx.ws.selectSession(ctx.sid);
    await eventually(() => backend.invokes().length === 1, 'pending resent');
    assert.deepEqual(backend.lastInvoke().body, { message: 'Pending msg', client_request_id: id });
    backend.lastInvoke().ch.send(runStarted('run-H', 'duplicate_request'));
    await eventually(() => sessionStorage.getItem('se_pending_req:web-test') === null);
    backend.lastInvoke().ch.send(done('run-H', 1, { status: 'COMPLETED', result: { reply: 'ok' } }));
    await eventually(() => ctx.ws.runControls.send);
  });

  await test('pending message is handed back (not resent) when another run is active', async () => {
    const backend = makeBackend();
    const ctx = makeWorkspace(backend);
    sessionStorage.setItem('se_pending_req:web-test', JSON.stringify({ message: 'Blocked msg', clientRequestId: '22222222-2222-4222-8222-222222222222' }));
    const active = runRecord('run-I', 'RUNNING', { client_request_id: 'someone-else' });
    backend.current = { active, latest: active };
    backend.runs.set('run-I', active);
    ctx.ws.selectSession(ctx.sid);
    await eventually(() => backend.events('run-I').length === 1, 'attached');
    assert.equal(ctx.ws.chatInputValue, 'Blocked msg');
    assert.equal(backend.invokes().length, 0);
    assert.equal(sessionStorage.getItem('se_pending_req:web-test'), null);
    backend.events('run-I')[0].ch.send(done('run-I', 1, { status: 'COMPLETED', result: { reply: 'ok' } }));
    await eventually(() => ctx.ws.runControls.send);
  });

  await test('page load reflects a FAILED latest run with Try again and a CANCELLED one as stopped', async () => {
    const backend = makeBackend();
    let ctx = makeWorkspace(backend);
    ctx.store.addMessage(ctx.sid, 'user', 'Earlier request');
    backend.current = { active: null, latest: runRecord('run-J', 'FAILED', { error_code: 'WORKER_LOST', error: H.ERROR_COPY.WORKER_LOST, retryable: true }) };
    ctx.ws.selectSession(ctx.sid);
    await eventually(() => ctx.ws.runNotice?.tone === 'danger');
    assert.equal(ctx.ws.runNotice.text, H.ERROR_COPY.WORKER_LOST);
    assert.equal(ctx.ws.runNotice.action, 'try-again');
    assert.equal(ctx.store.get(ctx.sid).status, 'failed');
    ctx.ws.destroy();
    ctx = makeWorkspace(backend);
    backend.current = { active: null, latest: runRecord('run-K', 'CANCELLED', { error_code: 'CANCELLED_BY_USER' }) };
    ctx.ws.selectSession(ctx.sid);
    await eventually(() => ctx.store.get(ctx.sid).status === 'stopped');
    assert.equal(ctx.ws.runNotice, null);
  });

  for (const ws of live) ws.destroy();
  const failed = results.filter(([ok]) => !ok);
  console.log(`\n${results.length - failed.length}/${results.length} passed`);
  process.exitCode = failed.length ? 1 : 0;
  setTimeout(() => process.exit(process.exitCode), 50);
}

main().catch((err) => { console.error(err); process.exit(1); });

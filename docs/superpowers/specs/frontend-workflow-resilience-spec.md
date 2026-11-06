# Frontend Spec: Workflow Resilience (run-scoped streaming, reconnect, stop)

**Audience:** frontend engineers integrating the deck-generation workflow.
**Backend status:** implemented on branch `f_workflow_stability_fixes`. Everything below describes shipped backend behaviour.
**Goal:** the UI always reflects the backend's authoritative run state. It never shows another run's events, never starts duplicate work, recovers from disconnects and refreshes automatically, and lets the user stop a run.

---

## 1. What changed on the backend (why the UI must change)

| Before | Now |
| --- | --- |
| Every `POST /invoke` started a new run, so a retry while a run was working created a second concurrent run. | **At most one active run per session**, enforced in the database across all pods. A second submission *attaches* to the active run instead. |
| The SSE stream was keyed by session, so events from two runs were interleaved. | Streams are **run-scoped**: a stream carries one run's events, plus run-less ingestion events. |
| Events were persisted only when a run ended; reconnecting to another pod showed nothing. | Events are persisted **as they happen**, and a stream can be **resumed after any event index on any pod**. |
| Raw exceptions (`Unterminated string…`, `500 from gateway`) were sent to the user. | Failures carry a stable **`error_code`** and a **user-safe message**; transient failures are retried automatically and announced with `workflow_recovering` / `workflow_recovered`. |
| No way to stop a run. | **`POST /stop`** stops the session's run and rolls the project back to its state before that message. |

**The core rule for the UI:** treat **`run_id` as the identity of a user operation**, and the **backend run status as the only source of truth**. An SSE disconnect means "reconnect", never "failed".

---

## 2. API reference

All paths are under `/api`. Auth and session ownership are unchanged: use the same auth you use today for `/decks/{session_id}/stream`.

### 2.1 Submit a message: `POST /decks/{session_id}/invoke`

Request body:

```json
{
  "message": "Create a training deck for supervisors",
  "refresh_content": false,
  "client_request_id": "6f1c2c1e-6b8e-4b3e-9a51-6d2a7f0e2b11"
}
```

| Field | Rules |
| --- | --- |
| `message` | Required, non-empty. Otherwise `400`. |
| `client_request_id` | **Strongly recommended.** 1–128 characters, so use a UUID v4. Otherwise `400`. See §4. |
| `refresh_content` | Optional boolean, unchanged. |

**Response:** `text/event-stream`, consumed with `fetch` streaming as today. The **first frame** is always `run_started`:

```json
{
  "type": "run_started",
  "agent": "supervisor",
  "message": "Starting the training-deck workflow",
  "data": {
    "status": "started",
    "workflow_status": "RUNNING",
    "client_request_id": "6f1c2c1e-…",
    "stream_resume_supported": true
  },
  "session_id": "web-…",
  "run_id": "451fb7062e3d4900aa6619155678bebe"
}
```

`data.status` is one of the following. **Handle all three the same way: attach to `run_id`.**

| `data.status` | Meaning | Recommended copy |
| --- | --- | --- |
| `started` | A new run was created for this message. | (none needed) |
| `already_running` | Another run was already active in this session. **This message was NOT recorded or processed.** The stream shows the active run. | "Generation is already running for this project. Showing its progress." Keep the user's text in the input so they can resend it later. |
| `duplicate_request` | This `client_request_id` was already received. The stream shows the run it created, replayed from the start even if it already finished. | (none needed) |

`run_started` has **no `event_index`** and is not part of the replayable history.

**Admission failure (fail-closed):** if the backend cannot reach its run registry, the stream contains a single `done` frame with `run_id: null`, `data.status: "FAILED"`, `data.error_code: "SERVICE_UNAVAILABLE"` and `data.retryable: true`. No work started, and a new submission is safe. A rare `409` JSON response means two starts raced; retry the same request with the same `client_request_id`.

### 2.2 Resume a run's stream: `GET /decks/{session_id}/runs/{run_id}/events?after_event_index=N`

- Replays every event with `event_index > N`, then follows live until the run's terminal `done`, then closes.
- A `Last-Event-ID` header, sent automatically by `EventSource` on reconnect, overrides `after_event_index`.
- Works on any pod, and for runs that already finished: it replays them and closes.
- `404` if the run does not belong to the session.
- **Use this for every reconnect.** Never re-POST `/invoke` to reconnect.

### 2.3 Run status

`GET /decks/{session_id}/runs/current`:

```json
{ "session_id": "web-…", "active": Run | null, "latest": Run | null }
```

`GET /decks/{session_id}/runs/{run_id}` returns a `Run` (`404` if it is not in the session).

`Run` shape:

```json
{
  "run_id": "451f…",
  "session_id": "web-…",
  "client_request_id": "6f1c…",
  "status": "RUNNING",
  "is_active": true,
  "current_stage": "slides",
  "attempt": 1,
  "last_event_index": 243,
  "deck_version": null,
  "artifact_ready": false,
  "error_code": null,
  "error": null,
  "retryable": false,
  "cancel_requested": false,
  "created_at": "…", "updated_at": "…", "finished_at": null
}
```

| `status` | Active? | Meaning |
| --- | --- | --- |
| `RUNNING` | yes | Working. |
| `RECOVERING` | yes | Automatically retrying a temporary failure. |
| `CANCELLING` | yes | A stop was requested; the worker is stopping. |
| `COMPLETED` | no | Finished. If `artifact_ready`, the deck (`deck_version`) is already persisted. |
| `FAILED` | no | Ended with `error_code` / `error` (safe message) / `retryable`. |
| `CANCELLED` | no | Stopped by the user; the project was rolled back. |

`QUEUED` is reserved and currently never emitted. `current_stage` is one of `triage`, `extraction`, `planning`, `slides`, `collect` or `publish`; it is informational only.

**Guarantee:** a run is marked `COMPLETED` only **after** its deck and assistant reply are persisted. The `done` event is delivered only after that. On `COMPLETED`, fetching versions and messages always finds the new deck.

### 2.4 Stop a run: `POST /decks/{session_id}/stop`

Body (optional but recommended): `{ "run_id": "<activeRunId>" }`. Passing `run_id` guarantees a stale tab cannot stop a newer run.

Response:

```json
{ "session_id": "web-…", "status": "cancelling", "run_id": "451f…", "run": Run | null }
```

| `status` | Meaning | UI |
| --- | --- | --- |
| `cancelling` | Accepted. The run stops at its next checkpoint, usually within seconds and at most after one in-flight model call. The backend forces it after 60 s. | Show "Stopping…", keep the stream open, wait for `done` with `status: CANCELLED`. |
| `already_cancelling` | A stop was already in progress (idempotent). | Same as above. |
| `completing` | The run is already saving the deck; it can no longer be stopped and will complete. | "Almost done. Finishing the deck…" |
| `not_running` | Nothing to stop: no active run, or `run_id` is not the active run. | Refresh state via `GET /runs/current`. |

**Effect of a stop:**
- The project's plan, draft and content are **restored to their state before that message**.
- The user's message stays in chat history, marked as stopped (see §2.5).
- No assistant reply and no deck version is created.
- The stream ends with `workflow_cancelled`, then `done` (`data.status: "CANCELLED"`, `error_code: "CANCELLED_BY_USER"`, `retryable: false`).

### 2.5 Chat history: `GET /decks/{session_id}/messages` (additive change)

Each entry in `messages[]` and each `timeline[]` item that has a `run_id` now has **`run_status`**: one of the `Run.status` values, or `null` for legacy turns.

- `CANCELLED`: render the user turn as "Stopped".
- `FAILED`: show the failure state for that turn.
- `RUNNING` / `RECOVERING` / `CANCELLING`: this is the in-progress turn, so attach to it (§5.3).

### 2.6 Legacy `GET /decks/{session_id}/stream` (deprecated)

While a run is active it now behaves like `/runs/{active}/events?after_event_index=0`. Otherwise it follows session events (ingestion) with keepalives. Migrate to §2.2.

---

## 3. Event stream format

### 3.1 Wire format

```
id: 126
data: {"type":"agent_step", ...}

: keepalive

```

- Run events carry an `id:` line equal to `event_index`. Frames without an index (`run_started`, ingestion events, a synthetic `done`) have no `id:` line.
- **Lines starting with `:` are comments** (keepalive every ~15 s during long silent work). `EventSource` ignores them; a custom `fetch` parser **must** skip them, and should treat them as proof the connection is alive.

### 3.2 Event shape

```json
{
  "type": "agent_step",
  "agent": "slide",
  "message": "Slide 21/33: researching",
  "data": {},
  "ts": "2026-10-01T08:42:29.123+00:00",
  "session_id": "web-…",
  "run_id": "451f…",
  "event_index": 126,
  "stage": "slides",
  "attempt": 2
}
```

- `stage` and `attempt` appear only when set. `attempt` appears only on a recovery attempt of 2 or more.
- Run-less events (`ingest_step`, `ingest_done`, `ingest_error`) have **no `run_id` and no `event_index`**.

### 3.3 Event types

| `type` | Notes / `data` |
| --- | --- |
| `run_started` | Per connection; §2.1. |
| `agent_start` | Run began. |
| `agent_step` | Progress (unchanged). |
| `deck` | `{deck_version}`: a new version was persisted. |
| `workflow_recovering` | `{stage, attempt, max_attempts, reason_code}`. `message` is safe, ready-to-show copy. |
| `workflow_recovered` | `{stage, attempt, max_attempts}`. |
| `workflow_cancelled` | `{error_code: "CANCELLED_BY_USER"}`. |
| `agent_error` | `{error_code, retryable}`; `message` is safe copy (never a raw exception). |
| `ingest_*` | Unchanged; run-less. |
| `done` | **Terminal.** See below. |

`done.data` by outcome:

| Outcome | `data` |
| --- | --- |
| Completed | `{ "status": "COMPLETED", "result": { …unchanged… } }` |
| Failed | `{ "status": "FAILED", "error": "<safe message>", "error_code": "…", "retryable": true\|false }` |
| Stopped | `{ "status": "CANCELLED", "error": "Stopped. Nothing from this request was saved.", "error_code": "CANCELLED_BY_USER", "retryable": false }` |
| Synthetic | Same as Failed/Stopped plus `"synthetic": true`, and no `event_index`. Sent when the run ended without its own `done` (worker lost, or a forced stop). |

**Breaking change:** `done.data.error` used to be the raw exception text. It is now a user-safe message. Branch on `data.status` and `data.error_code`, not on text.

### 3.4 Error codes and suggested copy

The backend's `error` / `message` copy is already safe to show; these are the defaults.

| `error_code` | `retryable` | Default copy |
| --- | --- | --- |
| `LLM_GATEWAY_UNAVAILABLE` | yes | The AI service is temporarily unavailable. Please try again in a few minutes. |
| `LLM_RATE_LIMITED` | yes | The AI service is busy right now. Please try again in a few minutes. |
| `LLM_TIMEOUT` | yes | The AI service took too long to respond. Please try again. |
| `LLM_OUTPUT_INVALID` | yes | The AI returned an incomplete answer while preparing the deck. Please try again. |
| `LLM_AUTH_FAILED` | no | The AI service rejected this application's credentials. Please contact support. |
| `LLM_REQUEST_INVALID` | no | The AI service could not process this request. Please contact support. |
| `WORKER_LOST` | yes | The deck generation was interrupted. Please try again. |
| `WORKER_SHUTDOWN` | yes | The service restarted while generating the deck. Please try again. |
| `SERVICE_UNAVAILABLE` | yes | The service is temporarily unavailable. Please try again shortly. |
| `CANCELLED_BY_USER` | no | Stopped. Nothing from this request was saved. |
| `INTERNAL_ERROR` | yes | Something went wrong while generating the deck. Please try again. |

Recovery copy (from `workflow_recovering.message`): "A temporary issue occurred while preparing the deck plan. Retrying automatically." On `workflow_recovered`: "Recovered successfully. Continuing deck generation."

---

## 4. Idempotency: `client_request_id`

- Generate **one UUID v4 per intentional user submission** (the moment the user presses Send).
- **Reuse the same id** for every transport-level retry of that submission: a fetch timeout or network error before `run_started` arrived, a component remount, or a double click that slipped through.
- Generate a **new id** only for a genuinely new user operation, such as a new message after the previous run reached a terminal state, or a deliberate "Try again" after `FAILED`.
- Keep the pending id in memory **and in `sessionStorage`** (keyed by session) until `run_started` arrives. A refresh mid-submit can then resend the same id and get `duplicate_request` instead of a second run.
- An id is scoped to its session. The backend remembers it permanently, so reusing an old id returns the old run.

---

## 5. Required client behaviour

### 5.1 State per session

```ts
type RunStatus = "RUNNING" | "RECOVERING" | "CANCELLING" | "COMPLETED" | "FAILED" | "CANCELLED";

interface WorkflowState {
  sessionId: string;
  activeRunId: string | null;
  pendingClientRequestId: string | null;   // until run_started arrives
  runStatus: RunStatus | null;             // authoritative backend status
  connection: "idle" | "connecting" | "open" | "reconnecting";  // transport only
  lastEventIndex: number;                  // highest event_index applied for activeRunId
  recovering: boolean;                     // between workflow_recovering and _recovered/done
  stopping: boolean;                       // stop accepted, waiting for done
  terminal: { status: RunStatus; errorCode?: string; error?: string; retryable?: boolean } | null;
}
```

Keep **transport state** (`connection`) separate from **run state** (`runStatus`). A broken connection never changes `runStatus`.

### 5.2 Applying an event (reducer guard)

```ts
function apply(state: WorkflowState, ev: StreamEvent): WorkflowState {
  if (ev.type === "run_started") {                       // attach
    return { ...state, activeRunId: ev.run_id, pendingClientRequestId: null,
             runStatus: ev.data.workflow_status, lastEventIndex: 0, terminal: null };
  }
  if (ev.run_id === undefined || ev.run_id === null) {   // run-less (ingestion) or admission failure
    return ev.type === "done" ? handleAdmissionFailure(state, ev) : applyIngest(state, ev);
  }
  if (ev.run_id !== state.activeRunId) return state;     // never mix runs
  if (ev.event_index && ev.event_index <= state.lastEventIndex) return state;  // dedupe
  if (state.terminal) return state;                      // terminal wins over late events
  // ...apply by type; set lastEventIndex = ev.event_index when present
}
```

**Rules:**
- Ignore any event whose `run_id` is not `activeRunId`.
- De-duplicate by `(run_id, event_index)`.
- Once a terminal `done` has been applied, nothing changes the run's UI except a new `run_started`.

### 5.3 Submit

1. If a run is active (`runStatus` is RUNNING, RECOVERING or CANCELLING), **do not submit**: Send is disabled (see §5.7). If a submission happens anyway (another tab), the backend returns `already_running`; show its progress and keep the user's text.
2. Create `pendingClientRequestId` (§4), then `POST /invoke`.
3. On `run_started`, set `activeRunId`, then consume the stream.
4. If the request fails **before** `run_started`, retry the POST with the **same** `client_request_id` (bounded, with backoff).
5. If the stream breaks **after** `run_started`, go to §5.4. Do **not** re-POST.

### 5.4 Reconnect (stream disconnected, `done` not received)

1. Set `connection = "reconnecting"`. **Do not** show failure; show "Reconnecting to the current progress…".
2. `GET /runs/{activeRunId}`.
   - Active (RUNNING / RECOVERING / CANCELLING): open `GET /runs/{activeRunId}/events?after_event_index={lastEventIndex}` and continue applying events.
   - `COMPLETED`: replay (or just apply) the terminal state and load the deck (§5.6).
   - `FAILED` / `CANCELLED`: open the events endpoint anyway. It replays the remaining events, including the terminal `done`, and closes; or render terminal state straight from the `Run` (`error_code`, `error`, `retryable`).
3. Back off between attempts (for example 1 s, 2 s, 5 s, then 10 s capped). Keepalive comments arrive every ~15 s; with no bytes for ~45 s, treat the connection as dead and reconnect.

### 5.5 Refresh / page load (automatic reconciliation)

1. `GET /runs/current`.
2. If `active`, set `activeRunId = active.run_id` and attach with `GET /runs/{id}/events?after_event_index=0` (full replay rebuilds progress). Show recovering, stopping and so on per the replayed events.
3. Else if `latest`:
   - `COMPLETED` → load the deck normally.
   - `FAILED` → show the controlled failure for that turn.
   - `CANCELLED` → show it as stopped.
4. If `sessionStorage` holds a pending `client_request_id` with no attached run, resend that submission (same message, same id).
5. Render chat history from `/messages` using `run_status` (§2.5).

This replaces "manually refresh the page to see the deck".

### 5.6 Completion

On `done` with `status: "COMPLETED"`, refetch versions and messages and switch to the normal deck view. No refresh is needed: the deck is guaranteed persisted before `done`.

### 5.7 Retry, Stop and Send controls

| UI state | Send | Retry | Stop |
| --- | --- | --- | --- |
| Run active (RUNNING / RECOVERING) | disabled | hidden; reconnect is automatic | **visible** |
| CANCELLING (`stopping`) | disabled | hidden | disabled ("Stopping…") |
| Reconnecting | disabled | "Reconnect now" → §5.4 (never re-POST) | visible |
| FAILED and `retryable` | enabled | "Try again" → new submission, **new** `client_request_id`, same message | hidden |
| FAILED and not `retryable` | enabled | hidden (show contact-support copy) | hidden |
| COMPLETED / CANCELLED / idle | enabled | hidden | hidden |

**Stop flow:**
1. `POST /stop {run_id: activeRunId}`.
2. On `cancelling` / `already_cancelling`, set `stopping`.
3. Keep the stream open until `done(CANCELLED)`.
4. Then restore the composer, optionally pre-filled with the stopped message so the user can edit and resend.

On `completing`, show "Finishing the deck…". On `not_running`, reconcile via `/runs/current`.

Frontend disabling is UX only. The backend enforces the invariants even with multiple tabs.

### 5.8 State machine

```
IDLE ─submit─► CONNECTING ─run_started─► RUNNING
RUNNING ─workflow_recovering─► RECOVERING ─workflow_recovered─► RUNNING
RUNNING|RECOVERING ─stop accepted─► CANCELLING ─done(CANCELLED)─► CANCELLED
RUNNING|RECOVERING ─stream lost─► RECONNECTING(run state unchanged) ─resumed─► (previous)
RUNNING|RECOVERING|RECONNECTING ─done(COMPLETED)─► COMPLETED
RUNNING|RECOVERING|RECONNECTING ─done(FAILED)─► FAILED
```

These are invalid and must not render: "Ready + Failed", "Completed + Working", "Failed" while the backend says active. Terminal states are entered only through a terminal `done` or an authoritative `Run` status.

---

## 6. No changes needed

- **Analytics dashboard:** stopped runs count only as usage (tokens and cost stay in spend totals). They are excluded from run counts, rates and latency, and they do not appear in the project drill-down. No new fields or categories; response shapes are unchanged.
- **Uploads / ingestion events:** unchanged, and still delivered on the run stream (run-less).
- **`done.data.result`** for completed runs: unchanged.

---

## 7. Frontend tests to add

| Test | Expectation |
| --- | --- |
| Mixed run events | With `activeRunId=A`, dispatching an event for B changes nothing. |
| Duplicate event | The same `(run_id, event_index)` twice is applied once. |
| Late event after terminal | Progress after `done` changes nothing. |
| SSE disconnect | Status shows `reconnecting`, not failed; `GET /runs/{id}` is called; the stream resumes with `after_event_index=lastEventIndex`. |
| Recovering | `workflow_recovering` shows recovery copy while preserving progress; `workflow_recovered` clears it. |
| Refresh during generation | On load, `/runs/current` returns active, and the client attaches with no new POST. |
| Completion during disconnect | After a disconnect, the run reports `COMPLETED` and the deck loads automatically. |
| Retry while active | Clicking Retry/Reconnect while RUNNING / RECOVERING never POSTs `/invoke`. |
| `already_running` | The client attaches to the returned `run_id`, shows the "already running" notice, and keeps the user's text. |
| Idempotent resend | A network failure before `run_started` resends with the same `client_request_id`. |
| Admission failure | `done` with `run_id: null` / `SERVICE_UNAVAILABLE` shows a retryable error with no attached run. |
| Stop | `POST /stop` → `stopping`, then `done(CANCELLED)` → stopped turn; the user message shows "Stopped" from `run_status`. |
| Stop outcomes | `completing` shows "Finishing…"; `not_running` triggers reconciliation. |
| Error copy | `done(FAILED)` renders `data.error`; branching uses `error_code` / `retryable`; no raw exception text appears. |
| Keepalive parsing | `: keepalive` lines are ignored by the parser and reset the dead-connection timer. |

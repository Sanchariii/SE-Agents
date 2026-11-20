# SE Agent Workflow Reliability & Fault-Tolerance Fix Plan

## Objective

Harden the deck-generation workflow so that transient LLM/parser/gateway failures recover automatically, only one top-level run can execute for a session at a time, SSE/event-stream reconnects never create duplicate work, and the frontend always reflects the authoritative backend run state.

This plan is based on the observed failure where:

- An existing run continued successfully in the backend.
- A second run was started for the same session after the user retried.
- The second run failed with `Unterminated string starting at: line 308 column 9 (char 34664)`.
- Events from the two different `run_id` values appeared in the frontend stream.
- The frontend showed the second run's error even though the original run continued and later generated the deck.
- Refreshing the page loaded the persisted deck and the UI became correct.

Do not solve this with frontend-only guards. Backend concurrency control, idempotency, retry/recovery, and authoritative run state are mandatory.

---

# Part A - Backend Fix 1: Robust structured-output parsing and automatic recovery

## Problem

A planning/supervisor path is attempting to parse generated JSON and can terminate the whole workflow when the model returns malformed or incomplete structured output:

```text
Unterminated string starting at: line 308 column 9 (char 34664)
```

The first task is to determine the exact producer and parser responsible. Do not simply catch `JSONDecodeError` at the outer workflow and restart everything.

## Investigation requirements

Trace every structured LLM call involved in planning, including the path around:

```text
planner_agent -> create_deck_plan -> planner.build_plan -> chat_json
```

and any supervisor/planner parser that can emit the observed exception.

For each structured-output call, identify:

1. The model invocation function.
2. The exact parser/validator used afterward.
3. Whether native JSON/structured-output/schema mode is available and being used.
4. Maximum output-token configuration.
5. Model finish/stop reason.
6. Whether the returned content can be truncated.
7. Whether markdown fences or additional prose are accepted.
8. Whether malformed escaping/newlines/quotes can break parsing.
9. Whether retry currently happens for transport failures but not parsing failures.
10. Whether a partially successful result is persisted before parsing.

## Add diagnostic logging

Log enough metadata to diagnose failures without exposing secrets or entire sensitive prompts.

For every structured-output call, log:

```text
session_id
run_id
attempt_id
workflow_stage
model
route
http_status
finish_reason
input_tokens
output_tokens
configured_max_output_tokens
response_character_count
parser_name
schema_name
exception_type
```

On parse failure, log a bounded/sanitized prefix and suffix of the model output rather than the complete response.

Never log:

```text
Authorization headers
APIM subscription keys
client secrets
access tokens
full confidential source documents
```

## Introduce a dedicated structured-output execution layer

Centralize LLM structured-output behavior rather than allowing every agent to independently perform:

```python
response = llm(...)
json.loads(response)
```

Conceptual interface:

```python
result = structured_llm_call(
    operation="deck_plan",
    schema=DeckPlan,
    messages=messages,
    retry_policy=STRUCTURED_OUTPUT_RETRY_POLICY,
    run_context=run_context,
)
```

The helper should separate:

1. transport invocation;
2. response extraction;
3. truncation detection;
4. JSON parsing;
5. schema validation;
6. repair/regeneration;
7. retry classification.

## Failure classification

Introduce explicit exception classes or error codes such as:

```text
StructuredOutputParseError
StructuredOutputValidationError
ModelOutputTruncatedError
TransientGatewayError
ModelRateLimitError
ModelTimeoutError
PermanentModelRequestError
```

Do not treat every exception as equally retryable.

### Retryable examples

```text
JSONDecodeError / malformed model JSON
schema validation failure caused by generated output
truncated model output
HTTP 429
HTTP 500
HTTP 502
HTTP 503
HTTP 504
connection reset
read/connect timeout
```

### Normally non-retryable examples

```text
401/403 authentication/authorization failure
invalid model/deployment name
invalid request/schema produced deterministically by application code
missing required application configuration
```

## Recovery strategy for malformed JSON

Use bounded recovery in this order:

### Attempt 1

Normal structured generation.

### If parsing fails

Determine whether the response appears truncated or merely malformed.

If a safe deterministic repair utility is already available, attempt local repair once. Do not silently mutate semantically important content.

Validate repaired content against the target schema.

### If still invalid

Retry only the failed LLM operation, not the whole deck workflow.

The retry prompt should clearly require the exact schema and valid JSON only. Prefer native structured/schema output where supported.

### If output was truncated

Do not repeatedly send the identical request with the identical constraints if truncation is deterministic. Apply an appropriate fallback, such as:

- larger supported output limit;
- more compact requested schema/content;
- sectional/chunked planning;
- regenerate only the missing portion and merge through validated application logic.

Choose the solution that fits the existing architecture rather than adding a brittle one-off parser.

### Retry policy

Use bounded retries with exponential backoff and jitter for transient failures.

Retries must expose attempt information internally:

```text
run_id       = stable
attempt_id   = changes per internal attempt
stage        = stable failed stage
```

Do not create a new externally visible run for an internal retry.

## User-visible recovery events

Emit semantic events instead of raw exceptions:

```json
{
  "type": "workflow_recovering",
  "run_id": "...",
  "stage": "planning",
  "message": "A temporary issue occurred while preparing the deck. Retrying automatically.",
  "attempt": 2
}
```

If recovery succeeds:

```json
{
  "type": "workflow_recovered",
  "run_id": "...",
  "stage": "planning",
  "message": "Recovered successfully. Continuing deck generation."
}
```

Only emit terminal `failed` after the configured recovery policy is exhausted.

## Do not repeat completed work

Introduce/reuse checkpoints for expensive workflow stages, for example:

```text
triage
planning
slide research/content
individual slide design/render
assembly
artifact persistence
```

A retry of planning should not duplicate unrelated completed work. A slide-render failure should retry that slide or its relevant design step rather than regenerate the entire deck.

---

# Part B - Backend Fix 2: Prevent concurrent top-level runs for the same session

## Problem

Two distinct top-level runs were active for the same user experience:

```text
original run_id = 451fb7062e3d4900aa6619155678bebe
retry run_id    = 3553569e92ee40489137a477aabb6679
```

The original run continued doing useful work while the retry created another workflow. This must not be possible when the product contract is one active generation per session.

## Required invariant

```text
For a given session_id, at most ONE top-level run may be ACTIVE.
```

ACTIVE should include states such as:

```text
QUEUED
RUNNING
RECOVERING
RETRYING
```

Terminal states should include:

```text
COMPLETED
FAILED
CANCELLED
```

Optionally represent `STALLED` if recovery logic needs it.

## Atomic backend enforcement

Do not implement:

```python
if not active_run_exists(session_id):
    create_run()
```

unless the check and creation are performed atomically. Multiple workers/replicas can race.

Use the application's existing distributed consistency mechanism, such as an atomic database transaction/constraint or Redis lock, depending on the deployed architecture.

Conceptually:

```text
workflow-lock:{session_id}
```

should identify the owning active run.

Persist enough ownership information to reason about stale runs:

```json
{
  "session_id": "...",
  "run_id": "...",
  "status": "RUNNING",
  "owner": "worker-id",
  "started_at": "...",
  "heartbeat_at": "..."
}
```

## Submission behavior

When `POST /run` or the equivalent receives a prompt:

### No active run

Atomically create/acquire the session's active run and execute it.

### Active healthy run exists

Do NOT create another run.

Return something equivalent to:

```json
{
  "status": "already_running",
  "session_id": "...",
  "run_id": "451fb7062e3d4900aa6619155678bebe",
  "stream_resume_supported": true
}
```

The client should reconnect to this run's stream.

### Active run is recovering

Return the same existing run ID and status. Do not launch another run.

### Active run appears stale

Verify persisted status plus heartbeat/ownership. Use deterministic stale-run recovery. Do not assume that a lost frontend/SSE connection means the backend run is dead.

## Add request idempotency

Concurrency locking and idempotency solve different problems and both are needed.

Frontend submissions should include a stable `client_request_id` / idempotency key.

Persist:

```text
(session_id, client_request_id) -> run_id
```

If the same submission reaches the backend again because of retry, timeout, double click, component rerender, etc., return the previously created run instead of creating work again.

Use a unique constraint or equivalent atomic guarantee so two racing identical requests cannot both create runs.

## Run state must be authoritative and persisted

Suggested run record:

```json
{
  "session_id": "web-...",
  "run_id": "...",
  "client_request_id": "...",
  "status": "RUNNING",
  "current_stage": "slide_design",
  "current_step": "slide_8",
  "retry_attempt": 1,
  "last_event_index": 243,
  "heartbeat_at": "...",
  "retryable": false,
  "error_code": null,
  "created_at": "...",
  "updated_at": "..."
}
```

Do not rely only on in-memory dictionaries because deployment may contain several workers/instances and processes can restart.

## Heartbeat and stale-run handling

A distributed lock needs safe crash recovery.

Implement:

- periodic heartbeat from the workflow/worker;
- lock ownership/lease renewal if using expiring locks;
- stale-run detection;
- owner-safe release so an old worker cannot release a newer worker's lock;
- checkpoint-based recovery where appropriate.

Important:

```text
SSE disconnected != workflow failed
browser refreshed != workflow cancelled
frontend timeout != backend timeout
```

## Stream isolation and replay

SSE/event streaming must be run-scoped.

Prefer an API shape equivalent to:

```text
/sessions/{session_id}/runs/{run_id}/events
```

Every event must contain:

```text
session_id
run_id
event_index
type
timestamp
```

Persist events or sufficient progress so reconnect can request:

```text
run_id=<active run>
after_event_index=<last event received>
```

Backend behavior:

1. verify that the requested run belongs to the session/user;
2. replay events after the requested event index;
3. transition into live streaming;
4. never include unrelated run events;
5. if run is already terminal, return/replay its terminal state;
6. emit heartbeat comments/events during long silent processing if necessary.

## Internal retries must not become new top-level runs

This distinction is important:

```text
USER OPERATION = run_id
INTERNAL RECOVERY ATTEMPT = attempt_id
```

Example:

```json
{
  "run_id": "451fb7062e3d4900aa6619155678bebe",
  "attempt_id": 2,
  "stage": "planning",
  "status": "RETRYING"
}
```

The same `run_id` should survive recoverable backend failures.

## Terminal-state semantics

Only one location/service should be responsible for transitioning a run into a terminal state.

Ensure state transitions are atomic/idempotent:

```text
RUNNING -> RECOVERING -> RUNNING
RUNNING -> COMPLETED
RUNNING -> RECOVERING -> FAILED
RUNNING -> CANCELLED
```

Once `COMPLETED`, `FAILED`, or `CANCELLED`, stale worker events must not revert the run to `RUNNING`.

Artifact persistence and `COMPLETED` should have clear ordering. Ideally the UI should never receive successful completion before the artifact needed by the next screen is durably available.

---

# Part C - Frontend fixes

## 1. Maintain a single authoritative active run

Frontend workflow state must include:

```typescript
interface WorkflowState {
  sessionId: string;
  activeRunId: string | null;
  clientRequestId: string | null;
  status:
    | "idle"
    | "connecting"
    | "running"
    | "reconnecting"
    | "recovering"
    | "completed"
    | "failed";
  lastEventIndex: number;
}
```

Do not manage workflow state only by `session_id`.

## 2. Never merge events from different run IDs

Every stream event must be validated before mutating UI state:

```typescript
if (event.run_id !== activeRunId) {
  // Ignore/log stale or unrelated event.
  return;
}
```

Deduplicate using:

```text
run_id + event_index
```

Reject already processed event indexes.

## 3. SSE disconnect must not mean workflow failure

Network/event-stream state and workflow execution state are separate.

If EventSource/fetch streaming disconnects:

```text
RUNNING -> RECONNECTING
```

not:

```text
RUNNING -> FAILED
```

Then:

1. query backend run status;
2. if the run is still active, reconnect to the same `run_id`;
3. resume from `lastEventIndex`;
4. if completed, fetch/display the deck;
5. if recovering, show recovery status;
6. show terminal failure only when backend confirms `FAILED`.

## 4. Fix Retry semantics

Retry must not blindly call “start workflow” again.

When user clicks retry/reconnect:

1. ask backend for session's active run/current requested run state;
2. if `RUNNING`, `RETRYING`, or `RECOVERING`, reconnect to that run;
3. if `COMPLETED`, load the generated deck;
4. if terminal `FAILED` and backend retry policy is exhausted, create a genuinely new run using a new intentional request/idempotency key as defined by product behavior.

If an active run is still working, present wording such as:

```text
Generation is still running. Reconnecting to the current progress...
```

Do not tell users that generation failed merely because streaming failed.

## 5. Use an idempotency key for submission

Generate `clientRequestId` once per intentional user submission.

Reuse it for transport retries of that same submission.

Do not generate a new ID merely because:

- fetch timed out;
- SSE disconnected;
- React component remounted;
- user retried a network request representing the same operation.

Generate a new one only for a genuinely new user operation after a terminal state, subject to product rules.

## 6. Persist enough context to survive refresh

Persist/derive:

```text
session_id
run_id
last known event index where useful
```

On refresh:

1. fetch authoritative session/run state;
2. discover active/latest run;
3. if running, reconnect;
4. if recovering, show recovery status and reconnect;
5. if completed, fetch deck immediately;
6. if failed, render controlled terminal failure.

The manual refresh observed in the bug should become an automatic reconciliation path.

## 7. Handle recovery events

Support semantic events like:

```text
workflow_recovering
workflow_recovered
workflow_failed
workflow_completed
```

Example user-facing copy:

```text
We hit a temporary issue while preparing the deck. Recovering automatically...
```

Then after successful recovery:

```text
Recovered successfully. Continuing generation...
```

Do not expose raw Python/JSON exceptions to normal users.

Keep detailed error information available only through diagnostics/logging as appropriate.

## 8. Prevent contradictory UI states

A screen must not simultaneously show concepts equivalent to:

```text
Ready + Failed
Completed + Working
```

Create an explicit reducer/state machine driven by backend run state.

Terminal state should prevent stale progress events from modifying the UI.

Example allowed transitions:

```text
IDLE -> CONNECTING -> RUNNING
RUNNING -> RECONNECTING -> RUNNING
RUNNING -> RECOVERING -> RUNNING
RUNNING -> COMPLETED
RUNNING -> RECOVERING -> FAILED
RECONNECTING -> COMPLETED
```

## 9. Fetch artifact automatically on completion

When receiving authoritative completion:

1. fetch/refresh deck metadata;
2. load the generated deck;
3. transition UI to the normal deck experience.

Do not require browser refresh to discover generated output.

Handle small persistence races by allowing the backend to return completion only once the artifact is available, or by performing bounded artifact-status reconciliation rather than starting another generation.

## 10. Disable duplicate actions, but treat this only as UX protection

While an active run exists:

- disable duplicate Submit;
- replace Retry with reconnect/recovery behavior;
- optionally expose Cancel only if backend cancellation has safe semantics.

Frontend guards are not concurrency protection. Backend invariants must still hold if requests come from multiple tabs or race over the network.

---

# Part D - Suggested API contract

## Start/attach to workflow

Request:

```json
{
  "session_id": "web-...",
  "client_request_id": "uuid-from-client",
  "message": "..."
}
```

Possible new-run response:

```json
{
  "status": "started",
  "run_id": "abc",
  "workflow_status": "RUNNING"
}
```

Possible deduplicated/active-run response:

```json
{
  "status": "already_running",
  "run_id": "abc",
  "workflow_status": "RUNNING"
}
```

The frontend handles both responses by attaching to `run_id=abc`.

## Run status

Example:

```json
{
  "session_id": "web-...",
  "run_id": "abc",
  "status": "RECOVERING",
  "stage": "planning",
  "retry_attempt": 2,
  "last_event_index": 125,
  "artifact_ready": false
}
```

## Event

```json
{
  "type": "agent_step",
  "session_id": "web-...",
  "run_id": "abc",
  "event_index": 126,
  "agent": "slide",
  "message": "Slide 21/33: researching",
  "data": {},
  "ts": "..."
}
```

---

# Part E - Concurrency and recovery tests that must be added

## Backend tests

### Concurrent submission test

Send two workflow-start requests for the same session concurrently.

Assert:

```text
only one top-level run is created
both responses resolve to the same active run_id
only one worker execution starts
```

### Duplicate idempotency test

Send the same `client_request_id` multiple times.

Assert the same `run_id` is returned.

### Different requests while active test

Send request B while request A is active in the same session.

Assert product-defined behavior is enforced, preferably attaching/returning the active run rather than starting B concurrently.

### Malformed JSON recovery test

Stub model results:

```text
attempt 1 -> malformed/truncated JSON
attempt 2 -> valid JSON
```

Assert:

```text
same run_id
new attempt_id
recovering event emitted
workflow continues
terminal state COMPLETED
```

### Retry exhaustion test

Return malformed output for every permitted attempt.

Assert:

```text
only the failed stage is retried
backend finally marks run FAILED
controlled error code is stored/emitted
raw parser error is not the normal user-facing message
```

### Gateway transient failure test

Stub 500/502/503/504 followed by success.

Assert same run continues.

### Non-retryable failure test

Stub 401/403 or deterministic invalid configuration.

Assert retry policy does not generate an infinite/redundant retry loop.

### SSE disconnect test

Start a run, disconnect consumer, allow workflow to continue, reconnect after event N.

Assert events N+1 onward can be replayed and live streaming resumes.

### Worker crash/stale lock test

Simulate stale ownership/heartbeat according to the selected locking implementation.

Assert safe recovery without two workers owning the same active run.

### Terminal-state race test

Attempt to deliver a stale RUNNING event after COMPLETED.

Assert persisted/externally visible state remains COMPLETED.

## Frontend tests

### Mixed run event test

Set `activeRunId=A`, dispatch event for B.

Assert no UI state changes.

### Duplicate event test

Dispatch identical `(run_id, event_index)` twice.

Assert progress is applied once.

### SSE disconnect test

Assert UI changes to `reconnecting`, not `failed`.

### Recovering test

Assert recovery copy is shown while preserving progress.

### Refresh during generation test

Reload while backend run remains active.

Assert frontend discovers the existing run and reconnects rather than starting another.

### Completion during disconnect test

Disconnect stream, let backend complete, then reconnect/status-check.

Assert frontend loads the completed deck automatically.

### Failed retry interaction test

If backend says existing run is still RUNNING/RECOVERING, clicking retry must not start a new run.

---

# Part F - Observability

Add metrics/logging around workflow robustness:

```text
workflow_runs_started_total
workflow_runs_completed_total
workflow_runs_failed_total
workflow_internal_retries_total by stage/reason
structured_output_parse_failures_total
structured_output_recoveries_total
duplicate_run_requests_blocked_total
idempotent_requests_reused_total
sse_reconnects_total
stale_runs_detected_total
```

Correlate all logs with:

```text
request_id
session_id
run_id
attempt_id
stage
```

The goal is to answer quickly:

- Did the user create a new operation or reconnect to an existing one?
- Was the backend workflow still alive?
- Which exact stage failed?
- Was failure transient?
- How many internal attempts occurred?
- Did recovery succeed?
- Why was a run finally marked failed?

---

# Part G - Implementation constraints for Copilot

1. Inspect the existing architecture and reuse existing persistence, Redis, repository, event, retry, and state-management abstractions where appropriate.
2. Do not introduce a second competing run-state system if one already exists.
3. Do not merely add broad `try/except Exception` blocks.
4. Do not silently swallow exceptions.
5. Preserve exception chaining and detailed server-side logging.
6. Never expose secrets/tokens through new diagnostics.
7. Do not solve session concurrency only with process-local locks.
8. Do not start new top-level runs as the implementation of internal retry.
9. Keep retries bounded and classify retryable/non-retryable failures.
10. Make state transitions idempotent and safe under concurrent workers.
11. Keep existing successful deck-generation behavior unchanged where possible.
12. Add unit/integration tests before considering the issue resolved.

---

# Definition of Done

The work is complete when all of the following are true:

- A malformed planner/model JSON response no longer immediately causes a broken user experience when automatic recovery is possible.
- The exact malformed-output source can be diagnosed from safe correlated logs.
- Structured-output failures are validated, classified, and retried with bounded policy.
- Internal retry maintains the same `run_id`.
- Only one top-level active run can exist per session.
- The one-run invariant is enforced atomically across workers/replicas.
- Duplicate network/user submissions are idempotent.
- A stream disconnect does not start a new workflow or mark a healthy workflow failed.
- The frontend reconnects to the original active run and resumes events.
- Events from other run IDs cannot alter the active UI.
- Workflow progress and events can be restored after refresh/reconnect.
- A temporary backend problem is represented to users as recovery/retry rather than a raw exception.
- Successful backend completion automatically makes the generated deck appear without manual refresh.
- Stale locks/workers can be safely detected and recovered.
- Automated tests cover malformed JSON, retries, duplicate submissions, concurrent requests, stream reconnect, refresh, and terminal-state races.

---

# Recommended Copilot Agent Execution Order

1. Trace the actual run creation, run persistence, event-stream generation, planner structured-output parsing, and frontend SSE consumption code.
2. Produce a short map of the existing implementation and identify exact race/error paths.
3. Add regression tests that reproduce both observed bugs before changing behavior.
4. Implement structured-output failure classification and bounded stage-level retries.
5. Implement persisted/atomic one-active-run-per-session enforcement and idempotency.
6. Implement run-scoped replayable SSE/status APIs using existing abstractions where possible.
7. Update frontend workflow reducer/state to be keyed by active run ID and to reconnect rather than restart.
8. Add recovery UX and automatic artifact reconciliation.
9. Run all existing and new tests and fix regressions.
10. Provide a final summary of changed files, behavior before/after, concurrency guarantees, retry policy, and any migration/configuration changes.

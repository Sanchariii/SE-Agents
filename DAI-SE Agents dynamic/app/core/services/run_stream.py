"""Run-scoped Server-Sent Events: replay after an index, then follow live, on any pod.

A stream belongs to exactly one run. It never carries another run's events, only
run-less session events such as ingestion progress. It works the same whether the
run executes on this pod or another:

1. **subscribe first** to this pod's live bus (filtered to the run), so nothing
   published from now on can be missed;
2. **replay** everything after ``after``: from the run's in-memory capture when it
   executes here (complete, including not-yet-flushed events), otherwise from
   Postgres;
3. **follow**: live from the local queue while the run executes here; otherwise poll
   Postgres every ``STREAM_POLL_SECONDS`` and watch the run row until it is terminal.
   A run that ended without a persisted ``done`` (its worker was lost, or its stop was
   forced) gets a synthetic ``done`` built from the run row.

Every run event goes out as ``id: <event_index>`` + ``data: <json>``, so a browser
``EventSource`` resumes with ``Last-Event-ID``. Events are de-duplicated by index. A
``: keepalive`` comment is sent during long silent work.
"""

import asyncio
import json
from typing import AsyncIterator

from app.config.constants import EventConstants, RunStatus, WorkflowRunConstants
from app.core.handlers import run_supervisor
from app.core.handlers.workflow_event_handler import workflow_event_handler
from app.core.handlers.workflow_run_handler import workflow_run_handler
from app.core.models.workflow_run_model import WorkflowRun
from app.utilities import events_util as events
from app.utilities.llm_errors import is_retryable_code, message_for
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

KEEPALIVE_FRAME = ": keepalive\n\n"
#: How long one live wait blocks before re-checking state (keepalives are separate).
_LIVE_WAIT_SECONDS = 1.0


def sse(event: dict) -> str:
    """Encode one event as an SSE frame, with ``id:`` when it has an event index."""
    index = event.get("event_index")
    head = f"id: {index}\n" if index else ""
    return f"{head}data: {json.dumps(event, default=str)}\n\n"


def connection_event(run: WorkflowRun, outcome: str) -> dict:
    """The per-connection first frame telling the client which run it is attached to."""
    messages = {
        run_supervisor.AcquireOutcome.STARTED: "Starting the training-deck workflow",
        run_supervisor.AcquireOutcome.ALREADY_RUNNING:
            "Generation is already running for this project. Showing its progress.",
        run_supervisor.AcquireOutcome.DUPLICATE_REQUEST:
            "This request was already received. Showing its progress.",
    }
    return {"type": "run_started", "agent": "supervisor",
            "message": messages.get(outcome, ""),
            "data": {"status": outcome, "workflow_status": run.status,
                     "client_request_id": run.client_request_id,
                     "stream_resume_supported": True},
            "session_id": run.session_id, "run_id": run.run_id}


def synthetic_done(run: WorkflowRun | None, run_id: str, session_id: str) -> dict:
    """A terminal event for a run that ended without persisting its own ``done``."""
    status = run.status if run else RunStatus.FAILED
    code = (run.error_code if run else None) or "WORKER_LOST"
    return {"type": "done", "agent": "supervisor",
            "message": "Workflow stopped" if status == RunStatus.CANCELLED
            else "Workflow failed",
            "data": {"status": status, "error_code": code, "error": message_for(code),
                     "retryable": is_retryable_code(code), "synthetic": True},
            "session_id": session_id, "run_id": run_id}


def _db_after(run_id: str, after: int) -> list[dict]:
    """All durable events after ``after``, paging through the replay query."""
    out: list[dict] = []
    while True:
        page = workflow_event_handler.list_after(run_id, after, EventConstants.REPLAY_PAGE_SIZE)
        out.extend(page)
        if len(page) < EventConstants.REPLAY_PAGE_SIZE:
            return out
        after = page[-1]["event_index"]


async def run_stream(session_id: str, run_id: str, after: int = 0,
                     first: dict | None = None) -> AsyncIterator[str]:
    """Stream one run's events after ``after`` until its terminal ``done``.

    Args:
        session_id: The session that owns the run (already authorised).
        run_id: The run to stream.
        after: Last event index the client already has (0 replays everything).
        first: Optional connection frame sent before any event (``run_started``).

    Yields:
        SSE frames.
    """
    loop = asyncio.get_running_loop()
    queue = events.subscribe(session_id, run_id)    # before replay: no gap
    last = after
    last_frame = loop.time()
    last_reap = loop.time()

    def emit(event: dict) -> str:
        nonlocal last, last_frame
        index = event.get("event_index")
        if index:
            last = max(last, index)
        last_frame = loop.time()
        return sse(event)

    def fresh(event: dict) -> bool:
        index = event.get("event_index")
        return not index or index > last

    try:
        if first is not None:
            yield emit(first)

        # 1. Replay.
        replay = events.live_events(run_id, last)
        if replay is None:
            replay = await asyncio.to_thread(_db_after, run_id, last)
        for event in replay:
            yield emit(event)
            if event["type"] == "done":
                return

        # 2. Follow.
        while True:
            if events.is_live(run_id):
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=_LIVE_WAIT_SECONDS)
                except asyncio.TimeoutError:
                    if loop.time() - last_frame >= EventConstants.KEEPALIVE_SECONDS:
                        last_frame = loop.time()
                        yield KEEPALIVE_FRAME
                    continue
                if not fresh(event):
                    continue
                index = event.get("event_index")
                if index and index > last + 1:
                    # A gap (cross-thread dispatch order or queue overflow): backfill
                    # from the capture so the client always sees indexes in order.
                    for missed in events.live_events(run_id, last) or []:
                        if missed["event_index"] >= index:
                            break
                        yield emit(missed)
                yield emit(event)
                if event["type"] == "done":
                    return
                continue

            # Not executing here (another pod, or finished): forward anything local
            # first (a just-dispatched done, run-less ingestion events), then poll.
            while not queue.empty():
                event = queue.get_nowait()
                if fresh(event):
                    yield emit(event)
                    if event.get("type") == "done" and event.get("run_id") == run_id:
                        return
            try:
                batch = await asyncio.to_thread(_db_after, run_id, last)
                for event in batch:
                    yield emit(event)
                    if event["type"] == "done":
                        return
                if not batch:
                    run = await asyncio.to_thread(workflow_run_handler.get, run_id)
                    if run is None or run.status in RunStatus.TERMINAL:
                        # One last read closes the race with a final flush.
                        for event in await asyncio.to_thread(_db_after, run_id, last):
                            yield emit(event)
                            if event["type"] == "done":
                                return
                        yield emit(synthetic_done(run, run_id, session_id))
                        return
                    if loop.time() - last_reap >= WorkflowRunConstants.STALE_AFTER_SECONDS / 3:
                        last_reap = loop.time()
                        await asyncio.to_thread(run_supervisor.reap, session_id)
            except Exception:                        # noqa: BLE001 - keep the stream alive
                log.warning("run stream poll failed for run %s; retrying", run_id,
                            exc_info=True)
            if loop.time() - last_frame >= EventConstants.KEEPALIVE_SECONDS:
                last_frame = loop.time()
                yield KEEPALIVE_FRAME
            await asyncio.sleep(EventConstants.STREAM_POLL_SECONDS)
    finally:
        events.unsubscribe(session_id, queue)


async def session_stream(session_id: str) -> AsyncIterator[str]:
    """Legacy session-wide stream: replay recent in-memory history, then follow live.

    Kept for clients that watch ingestion progress on ``/stream``; it now sends
    keepalives instead of blocking silently forever.
    """
    queue = events.subscribe(session_id)
    try:
        for event in events.history(session_id):
            yield sse(event)
        while True:
            try:
                event = await asyncio.wait_for(queue.get(),
                                               timeout=EventConstants.KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                yield KEEPALIVE_FRAME
                continue
            yield sse(event)
            if event.get("type") == "done":
                return
    finally:
        events.unsubscribe(session_id, queue)


def single_event_stream(event: dict) -> AsyncIterator[str]:
    """A stream that sends one event and closes (fail-closed admission errors)."""
    async def gen():
        yield sse(event)
    return gen()

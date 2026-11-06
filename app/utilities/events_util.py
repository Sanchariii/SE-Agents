"""Session event bus with isolated per-run capture for durable SSE activity history.

Two delivery paths:

* **live** - an in-process bus. A subscriber is either session-wide (``run_id=None``,
  the legacy reconnect stream) or run-scoped, and a run-scoped subscriber never sees
  another run's events.
* **durable** - a run's events are written to Postgres *as they happen* (a small
  background flusher per run, see :class:`RunEventCapture`), so a reconnect - on this
  pod or any other - can replay everything after the last index it saw.
"""

import asyncio
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterator

from app.config.constants import EventConstants
from app.utilities.logger_util import attempt_ctx, get_logger, stage_ctx

log = get_logger(__name__)

#: session_id -> [(queue, run_id or None for a session-wide subscriber)]
_subscribers: dict[str, list[tuple[asyncio.Queue[dict], str | None]]] = {}
_history: dict[str, list[dict]] = {}
#: run_id -> capture, for runs executing in this process (same-pod replay).
_live_captures: dict[str, "RunEventCapture"] = {}
_loop: asyncio.AbstractEventLoop | None = None
_state_lock = threading.Lock()

#: Persists a batch of one run's events; must be idempotent per (run_id, event_index).
EventSink = Callable[[list[dict]], None]


@dataclass
class RunEventCapture:
    """Thread-safe event buffer shared by one workflow run and its worker threads.

    With a ``sink`` the buffer is also flushed incrementally to durable storage: every
    ``EventConstants.FLUSH_SECONDS``, as soon as ``FLUSH_BATCH`` events are pending, and
    synchronously on :meth:`flush` (used before a terminal event is delivered).
    """

    session_id: str
    run_id: str
    sink: EventSink | None = None
    _events: list[dict] = field(default_factory=list)
    _held: set[int] = field(default_factory=set)
    _flushed: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _flush_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _wake: threading.Event = field(default_factory=threading.Event, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)

    def record(self, event_type: str, agent: str, message: str, data: dict) -> dict:
        """Create and append one event with a deterministic run-local sequence.

        Args:
            event_type: SSE event discriminator.
            agent: Agent or subsystem that emitted the event.
            message: User-visible activity text.
            data: Structured event metadata.

        Returns:
            The complete SSE event dictionary.
        """
        with self._lock:
            event = {
                "type": event_type,
                "agent": agent,
                "message": message,
                "data": data,
                "ts": datetime.now(timezone.utc).isoformat(),
                "session_id": self.session_id,
                "run_id": self.run_id,
                "event_index": len(self._events) + 1,
            }
            stage, attempt = stage_ctx.get(), attempt_ctx.get()
            if stage:
                event["stage"] = stage
            if attempt:
                event["attempt"] = int(attempt)
            self._events.append(event)
            pending = len(self._events) - self._flushed
        if self.sink is not None and pending >= EventConstants.FLUSH_BATCH:
            self._wake.set()
        return event

    def snapshot(self) -> list[dict]:
        """Return a stable copy of all events captured so far.

        Returns:
            Captured events in publication order.
        """
        with self._lock:
            return [dict(event) for event in self._events]

    @property
    def last_index(self) -> int:
        """Index of the newest captured event (0 when none)."""
        with self._lock:
            return len(self._events)

    # --- delivery-ordered replay (same-pod reconnects) ------------------------------
    def hold(self, event_index: int) -> None:
        """Keep an undelivered event (``publish(deliver=False)``) out of replay."""
        with self._lock:
            self._held.add(event_index)

    def release(self, event_index: int) -> None:
        """Make a held event replayable once it has been dispatched."""
        with self._lock:
            self._held.discard(event_index)

    def replay_after(self, after: int) -> list[dict]:
        """Delivered events with ``event_index > after``, in order.

        Stops at the first held event so a reconnect never sees the terminal ``done``
        before the run's outcome has been recorded and dispatched.
        """
        with self._lock:
            out = []
            for event in self._events[after:]:
                if event["event_index"] in self._held:
                    break
                out.append(dict(event))
            return out

    # --- incremental durability ------------------------------------------------
    def flush(self) -> bool:
        """Write every not-yet-persisted event through the sink, synchronously.

        Returns:
            True when everything captured so far is durable (or there is no sink).
        """
        if self.sink is None:
            return True
        with self._flush_lock:
            with self._lock:
                batch = [dict(e) for e in self._events[self._flushed:]]
                upto = len(self._events)
            if not batch:
                return True
            try:
                self.sink(batch)
            except Exception:                        # noqa: BLE001 - retried next tick
                log.error("could not persist %d event(s) for run %s; will retry",
                          len(batch), self.run_id, exc_info=True)
                return False
            self._flushed = upto
            return True

    def start_flusher(self) -> None:
        """Start the background flusher (no-op without a sink)."""
        if self.sink is None or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run_flusher, daemon=True,
                                        name=f"events-{self.run_id[:8]}")
        self._thread.start()

    def _run_flusher(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(EventConstants.FLUSH_SECONDS)
            self._wake.clear()
            self.flush()

    def close(self) -> None:
        """Stop the flusher and make a final flush."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        self.flush()


_capture_ctx: ContextVar[RunEventCapture | None] = ContextVar(
    "workflow_event_capture", default=None)


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Bind the FastAPI event loop used to notify subscribers from worker threads.

    Args:
        loop: The application's running asyncio loop.

    Returns:
        None.
    """
    global _loop
    _loop = loop


def subscribe(session_id: str, run_id: str | None = None) -> asyncio.Queue[dict]:
    """Subscribe to live events for a session, optionally scoped to one run.

    Args:
        session_id: Session whose events should be delivered.
        run_id: Deliver only this run's events (plus run-less session events such as
            ingestion progress). None subscribes to everything in the session.

    Returns:
        A bounded queue owned by the caller.
    """
    q: asyncio.Queue[dict] = asyncio.Queue(maxsize=EventConstants.MAX_QUEUE)
    with _state_lock:
        _subscribers.setdefault(session_id, []).append((q, run_id))
    return q


def unsubscribe(session_id: str, q: asyncio.Queue[dict]) -> None:
    """Remove a session subscription.

    Args:
        session_id: Session that owns the queue.
        q: Queue returned by :func:`subscribe`.

    Returns:
        None.
    """
    with _state_lock:
        subscribers = _subscribers.get(session_id)
        if subscribers:
            subscribers[:] = [entry for entry in subscribers if entry[0] is not q]
        if subscribers == []:
            _subscribers.pop(session_id, None)


def _wants(scope: str | None, event: dict) -> bool:
    """Whether a subscriber scoped to ``scope`` should receive ``event``."""
    if scope is None:
        return True
    event_run = event.get("run_id")
    return event_run is None or event_run == scope


def history(session_id: str) -> list[dict]:
    """Return a copy of the bounded live replay history for a session.

    Args:
        session_id: Session whose recent events are requested.

    Returns:
        Recent event dictionaries in publication order.
    """
    with _state_lock:
        return list(_history.get(session_id, []))


def clear(session_id: str) -> None:
    """Clear the in-memory replay history for a deleted session.

    Args:
        session_id: Session being removed.

    Returns:
        None.
    """
    with _state_lock:
        _history.pop(session_id, None)


@contextmanager
def capture_run(session_id: str, run_id: str,
                sink: EventSink | None = None) -> Iterator[RunEventCapture]:
    """Capture events emitted by one workflow and its context-aware worker threads.

    Args:
        session_id: Session that owns the workflow.
        run_id: Unique invocation identifier.
        sink: Optional durable writer; when given, events are persisted incrementally
            and a final flush runs on exit.

    Yields:
        The run's thread-safe event buffer.
    """
    capture = RunEventCapture(session_id=session_id, run_id=run_id, sink=sink)
    capture.start_flusher()
    token = _capture_ctx.set(capture)
    with _state_lock:
        _live_captures[run_id] = capture
    try:
        yield capture
    finally:
        _capture_ctx.reset(token)
        with _state_lock:
            if _live_captures.get(run_id) is capture:
                _live_captures.pop(run_id, None)
        capture.close()


def live_events(run_id: str, after: int) -> list[dict] | None:
    """Replay a run executing in this process from its in-memory capture.

    Args:
        run_id: The run.
        after: Return events with ``event_index`` greater than this.

    Returns:
        The delivered events after ``after``, or None when the run is not executing in
        this process (the caller replays from Postgres instead).
    """
    with _state_lock:
        capture = _live_captures.get(run_id)
    return None if capture is None else capture.replay_after(after)


def is_live(run_id: str) -> bool:
    """True while the run's capture is open in this process."""
    with _state_lock:
        return run_id in _live_captures


def publish(session_id: str, type: str, message: str = "",
            agent: str = "supervisor", deliver: bool = True, **data) -> dict:
    """Create, capture, and optionally deliver a user-visible workflow event.

    Args:
        session_id: Session receiving the event.
        type: SSE event discriminator.
        message: User-visible activity text.
        agent: Agent or subsystem emitting the event.
        deliver: Whether to add the event to replay history and live queues now.
        **data: Structured event metadata.

    Returns:
        The complete event dictionary.
    """
    capture = _capture_ctx.get()
    if capture is not None and capture.session_id == session_id:
        event = capture.record(type, agent, message, data)
        if not deliver:
            capture.hold(event["event_index"])
    else:
        event = {
            "type": type,
            "agent": agent,
            "message": message,
            "data": data,
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
        }
    if deliver:
        dispatch(session_id, event)
    return event


def dispatch(session_id: str, event: dict) -> None:
    """Deliver a previously-created event to replay history and live subscribers.

    Args:
        session_id: Session receiving the event.
        event: Complete event dictionary returned by :func:`publish`.

    Returns:
        None.
    """
    with _state_lock:
        bucket = _history.setdefault(session_id, [])
        bucket.append(event)
        if len(bucket) > EventConstants.MAX_HISTORY:
            del bucket[:-EventConstants.MAX_HISTORY]
        subscribers = [q for q, scope in _subscribers.get(session_id, [])
                       if _wants(scope, event)]
        capture = _live_captures.get(event.get("run_id") or "")
    if capture is not None and event.get("event_index"):
        capture.release(event["event_index"])

    for q in subscribers:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None and (_loop is None or running is _loop):
            _safe_put(q, event, session_id)
        elif _loop is not None:
            _loop.call_soon_threadsafe(_safe_put, q, event, session_id)
        else:
            log.warning("event loop is not bound; live event not delivered for session %s",
                        session_id)


def _safe_put(q: asyncio.Queue[dict], event: dict, session_id: str) -> None:
    """Put an event on a subscriber queue without blocking its event loop.

    Args:
        q: Subscriber queue.
        event: Event to enqueue.
        session_id: Session used for overflow diagnostics.

    Returns:
        None.
    """
    try:
        q.put_nowait(event)
    except asyncio.QueueFull:
        log.warning("event queue full; dropped %s event for session %s run %s",
                    event.get("type"), session_id, event.get("run_id") or "-")
"""Cooperative stop/fence signals for one workflow run, visible to every layer.

A workflow runs on a worker thread that cannot be killed, so stopping it is
cooperative: the run's :class:`RunControl` is bound on a ``ContextVar`` and the code
that spends time - the gateway transport, stage boundaries, per-slide loops - calls
:func:`checkpoint` to bail out promptly.

Two reasons stop a run:

* **cancel** - the user asked to stop it (``POST /decks/{sid}/stop``).
* **fence** - this worker no longer owns the run (its heartbeat found the row reaped
  or force-cancelled), so it must not publish a deck or a terminal event.

Both raise exceptions derived from ``BaseException`` - like ``asyncio.CancelledError``
- so the many "one slide must never take the deck down" ``except Exception`` fallbacks
cannot swallow a stop and keep working.

The module is dependency-free so the low-level gateway client can import it without
pulling in workflow code. Exceptions carry ``analytics_cancelled`` so analytics capture
can classify a user stop without importing anything from here either.
"""

import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Protocol

#: Fence reasons (``RunFencedError.reason``).
FENCE_REASON_CANCELLED = "cancelled"
FENCE_REASON_LOST = "lost"


class RunCancelledError(BaseException):
    """The user stopped the run. Never retried; never recorded as a failure."""

    analytics_cancelled = True

    def __init__(self, run_id: str = "") -> None:
        super().__init__(f"run {run_id or '-'} was stopped by the user")
        self.run_id = run_id


class RunFencedError(BaseException):
    """This worker lost ownership of the run and must stop without publishing."""

    def __init__(self, run_id: str = "", reason: str = "") -> None:
        super().__init__(f"run {run_id or '-'} is no longer owned by this worker ({reason})")
        self.run_id = run_id
        self.reason = reason
        # A run fenced because the user's stop was forced is still a stop, not a failure.
        self.analytics_cancelled = reason == FENCE_REASON_CANCELLED


class RetryListener(Protocol):
    """Receives low-level retry signals so they can surface as recovery events."""

    def on_retry(self, reason: str, attempt: int) -> None: ...

    def on_recovered(self) -> None: ...


class RunControl:
    """Thread-safe stop signals shared by one run's worker threads.

    The stage hooks (:meth:`enter_stage`, :meth:`set_recovering`) are no-ops here; the
    run supervisor overrides them to persist run state.

    Attributes:
        run_id: The run this control belongs to.
        session_id: The session that owns the run.
        owner: The worker identity that holds the run lease.
    """

    def __init__(self, run_id: str, session_id: str = "", owner: str = "") -> None:
        self.run_id = run_id
        self.session_id = session_id
        self.owner = owner
        self._cancel = threading.Event()
        self._fence = threading.Event()
        self._fence_reason = ""
        self._listener: RetryListener | None = None

    # --- signals ------------------------------------------------------------
    def request_cancel(self) -> None:
        """Signal a user stop; the worker raises at its next checkpoint."""
        self._cancel.set()

    def fence(self, reason: str) -> None:
        """Mark this worker as no longer owning the run.

        Args:
            reason: ``FENCE_REASON_CANCELLED`` or ``FENCE_REASON_LOST``.
        """
        if not self._fence.is_set():
            self._fence_reason = reason
            self._fence.set()
            self._cancel.set()          # wake any backoff wait immediately

    @property
    def cancelled(self) -> bool:
        """True once a user stop was requested."""
        return self._cancel.is_set()

    @property
    def fenced(self) -> bool:
        """True once this worker lost ownership of the run."""
        return self._fence.is_set()

    @property
    def fence_reason(self) -> str:
        """Why the run was fenced, or ``""``."""
        return self._fence_reason

    def raise_if_stopped(self) -> None:
        """Raise when the run must stop; a fence wins over a cancel.

        Raises:
            RunFencedError: The worker no longer owns the run.
            RunCancelledError: The user stopped the run.
        """
        if self._fence.is_set():
            raise RunFencedError(self.run_id, self._fence_reason)
        if self._cancel.is_set():
            raise RunCancelledError(self.run_id)

    def wait(self, seconds: float) -> None:
        """Sleep up to ``seconds``, waking early - and raising - on a stop.

        Args:
            seconds: Maximum time to wait.
        """
        if seconds > 0:
            self._cancel.wait(seconds)
        self.raise_if_stopped()

    # --- stage hooks (persisted by the run supervisor) ------------------------
    def enter_stage(self, stage: str) -> None:
        """Called as a stage starts; may raise to stop the run. No-op by default."""
        self.raise_if_stopped()

    def set_recovering(self, recovering: bool, attempt: int) -> None:
        """Called when a stage starts or stops recovering. No-op by default."""

    # --- retry visibility ----------------------------------------------------
    def set_retry_listener(self, listener: RetryListener | None) -> None:
        """Install the object that surfaces low-level retries as recovery events."""
        self._listener = listener

    def note_retry(self, reason: str, attempt: int) -> None:
        """Report a transport/structured-output retry; never raises."""
        listener = self._listener
        if listener is not None:
            try:
                listener.on_retry(reason, attempt)
            except Exception:  # noqa: BLE001 - visibility must never break the retry
                pass

    def note_recovered(self) -> None:
        """Report that a call which had to retry has now succeeded; never raises."""
        listener = self._listener
        if listener is not None:
            try:
                listener.on_recovered()
            except Exception:  # noqa: BLE001
                pass


_control_ctx: ContextVar[RunControl | None] = ContextVar("run_control", default=None)


def current() -> RunControl | None:
    """Return the run control bound on this context, if any."""
    return _control_ctx.get()


@contextmanager
def bind(control: RunControl) -> Iterator[RunControl]:
    """Bind ``control`` for the block; worker threads inherit it via context copy.

    Args:
        control: The run's control object.

    Yields:
        The same control.
    """
    token = _control_ctx.set(control)
    try:
        yield control
    finally:
        _control_ctx.reset(token)


def checkpoint() -> None:
    """Raise if the current run was stopped or fenced; a no-op outside a run."""
    control = _control_ctx.get()
    if control is not None:
        control.raise_if_stopped()


def sleep(seconds: float) -> None:
    """Back off for ``seconds``; inside a run, a stop interrupts the wait and raises."""
    control = _control_ctx.get()
    if control is None:
        time.sleep(seconds)
    else:
        control.wait(seconds)


def note_retry(reason: str, attempt: int) -> None:
    """Forward a retry to the current run's listener; a no-op outside a run."""
    control = _control_ctx.get()
    if control is not None:
        control.note_retry(reason, attempt)


def note_recovered() -> None:
    """Forward a post-retry success to the current run's listener; no-op outside a run."""
    control = _control_ctx.get()
    if control is not None:
        control.note_recovered()

"""Owns workflow-run execution: admission, heartbeats, stops and stale-run recovery.

The HTTP layer never starts a workflow itself any more. It asks :func:`acquire` for
the session's run - which either creates one or returns the run already active, or
the run a resent request already created - and only a freshly ``started`` run is
handed to :func:`start`. The SSE response then merely *watches* that run, so a client
disconnect, a refresh or a second tab can never start duplicate work.

Execution runs on a worker thread (the graph is blocking). :func:`start` keeps a
strong reference to its task and retrieves its result in a done-callback, so a failed
run is reported exactly once and never surfaces as "Task exception was never
retrieved".

Liveness is a lease in ``workflow_runs``: the owner heart-beats every
``HEARTBEAT_SECONDS``. A run whose owner stops beating for ``STALE_AFTER_SECONDS`` is
reaped as ``WORKER_LOST`` by whoever next touches the session; a stop the worker does
not acknowledge within ``CANCEL_FORCE_SECONDS`` is forced to ``CANCELLED`` and the
session is rolled back to its pre-run snapshot. Either way the old worker is fenced: it
finds itself no longer the owner and stops without publishing.
"""

import asyncio
import json
import threading
import uuid

from app.config.constants import RunStages, RunStatus, WorkflowRunConstants
from app.config.exceptions import ConflictError
from app.core.handlers.session_store import session_store
from app.core.handlers.workflow_run_handler import WORKER_ID, workflow_run_handler
from app.core.models.workflow_run_model import WorkflowRun
from app.utilities import run_control
from app.utilities.logger_util import get_logger, log_context
from app.utilities.metrics_util import Metrics, incr
from app.utilities.run_control import FENCE_REASON_CANCELLED, FENCE_REASON_LOST, RunControl

log = get_logger(__name__)


class AcquireOutcome:
    """Result of :func:`acquire`, echoed to the client as ``run_started.status``."""
    STARTED = "started"
    ALREADY_RUNNING = "already_running"
    DUPLICATE_REQUEST = "duplicate_request"


class StopOutcome:
    """Result of :func:`request_stop`."""
    CANCELLING = "cancelling"
    ALREADY_CANCELLING = "already_cancelling"
    COMPLETING = "completing"
    NOT_RUNNING = "not_running"


class SupervisedRun(RunControl):
    """A run executing on this pod: lease heartbeat plus persisted stage state."""

    def __init__(self, run: WorkflowRun) -> None:
        super().__init__(run.run_id, run.session_id, run.owner)
        self.pre_run_snapshot: dict | None = None
        if run.pre_run_snapshot:
            try:
                self.pre_run_snapshot = json.loads(run.pre_run_snapshot)
            except ValueError:
                log.warning("run %s has an unreadable pre-run snapshot", run.run_id)
        self._beat_stop = threading.Event()
        self._beat_thread: threading.Thread | None = None

    # --- lease ------------------------------------------------------------------
    def _apply(self, row: WorkflowRun | None) -> None:
        """React to the run row read back by a lease renewal or stage update."""
        if row is None:
            self.fence(self._lookup_fence_reason())
        elif row.status == RunStatus.CANCELLING:
            self.request_cancel()

    def _lookup_fence_reason(self) -> str:
        """Why the lease is gone: a forced user stop, or a lost/reaped worker."""
        try:
            run = workflow_run_handler.get(self.run_id)
        except Exception:                            # noqa: BLE001
            return FENCE_REASON_LOST
        if run is not None and run.status == RunStatus.CANCELLED:
            return FENCE_REASON_CANCELLED
        return FENCE_REASON_LOST

    def beat(self) -> None:
        """Renew the lease once; a database hiccup is logged, never fatal."""
        try:
            self._apply(workflow_run_handler.heartbeat(self.run_id, self.owner))
        except Exception:                            # noqa: BLE001
            log.warning("heartbeat failed for run %s", self.run_id, exc_info=True)

    def start_heartbeat(self) -> None:
        """Start the background lease renewal."""
        def loop() -> None:
            while not self._beat_stop.wait(WorkflowRunConstants.HEARTBEAT_SECONDS):
                self.beat()
                if self.fenced:
                    log.warning("run %s lost its lease (%s); the worker will stop",
                                self.run_id, self.fence_reason)
                    return
        self._beat_thread = threading.Thread(target=loop, daemon=True,
                                             name=f"heartbeat-{self.run_id[:8]}")
        self._beat_thread.start()

    def stop_heartbeat(self) -> None:
        """Stop the lease renewal (the run is finishing)."""
        self._beat_stop.set()

    # --- RunControl stage hooks -----------------------------------------------------
    def enter_stage(self, stage: str) -> None:
        """Persist the stage and use the write as a synchronous stop/lease check.

        Entering ``publish`` this way is also what closes the stop race: once the
        stage is committed, ``request_cancel`` refuses the run, and a stop committed
        just before is seen here and honoured before any deck is persisted.
        """
        self.raise_if_stopped()
        try:
            self._apply(workflow_run_handler.set_stage(self.run_id, self.owner, stage))
        except Exception:                            # noqa: BLE001
            log.warning("could not record stage %s for run %s", stage, self.run_id,
                        exc_info=True)
        self.raise_if_stopped()

    def set_recovering(self, recovering: bool, attempt: int) -> None:
        """Persist RUNNING <-> RECOVERING; best-effort."""
        try:
            workflow_run_handler.set_recovering(self.run_id, self.owner, recovering, attempt)
        except Exception:                            # noqa: BLE001
            log.warning("could not record recovery state for run %s", self.run_id,
                        exc_info=True)


#: Runs executing on this pod, by run id (for same-pod stop delivery and streaming).
_local_runs: dict[str, SupervisedRun] = {}
_local_lock = threading.Lock()
#: Strong references to running tasks; asyncio only keeps weak ones.
_tasks: set[asyncio.Task] = set()


def local_run(run_id: str) -> SupervisedRun | None:
    """Return the run if it executes on this pod."""
    with _local_lock:
        return _local_runs.get(run_id)


# --- stale-run recovery ----------------------------------------------------------------
def reap(session_id: str | None = None) -> None:
    """Finish runs whose owner died or never acknowledged a stop.

    Lost runs become ``FAILED`` / ``WORKER_LOST``. Expired stops become ``CANCELLED``
    and the session is rolled back to the run's pre-run snapshot. A still-running local
    worker for any of them is fenced so it stops without publishing.

    Args:
        session_id: Restrict to one session, or None for all.
    """
    for run in workflow_run_handler.reap_lost(session_id,
                                              WorkflowRunConstants.STALE_AFTER_SECONDS):
        incr(Metrics.STALE_RUNS, reason="worker_lost")
        log.warning("reaped run %s of session %s: owner %s stopped heart-beating",
                    run.run_id, run.session_id, run.owner)
        _fence_local(run.run_id, FENCE_REASON_LOST)
    for run in workflow_run_handler.force_cancel_expired(
            session_id, WorkflowRunConstants.CANCEL_FORCE_SECONDS,
            WorkflowRunConstants.STALE_AFTER_SECONDS):
        incr(Metrics.STALE_RUNS, reason="cancel_forced")
        log.warning("forced the stop of run %s of session %s: the worker did not "
                    "acknowledge it in time", run.run_id, run.session_id)
        _fence_local(run.run_id, FENCE_REASON_CANCELLED)
        _restore(run.session_id, run.pre_run_snapshot, run.run_id)


def _fence_local(run_id: str, reason: str) -> None:
    control = local_run(run_id)
    if control is not None:
        control.fence(reason)


def _restore(session_id: str, snapshot: str | dict | None, run_id: str) -> None:
    """Roll a session back to a run's pre-run state; best-effort."""
    if not snapshot:
        return
    try:
        snap = json.loads(snapshot) if isinstance(snapshot, str) else snapshot
        session_store.restore_snapshot(session_id, snap)
    except Exception:                                # noqa: BLE001
        log.error("could not restore session %s after stopping run %s", session_id,
                  run_id, exc_info=True)


def restore_pre_run_state(control: RunControl) -> None:
    """Undo what a stopped run wrote, using the snapshot taken when it started."""
    if isinstance(control, SupervisedRun):
        _restore(control.session_id, control.pre_run_snapshot, control.run_id)


# --- admission -----------------------------------------------------------------------
def acquire(session_id: str, user_id: str | None,
            client_request_id: str | None) -> tuple[WorkflowRun, str]:
    """Create the session's run, or return the one that already holds the slot.

    Atomic across pods: the partial unique indexes on ``workflow_runs`` decide which of
    two racing requests wins; the loser reads the winner's run.

    Args:
        session_id: The session submitting a message.
        user_id: The caller, for audit.
        client_request_id: The client's idempotency key for this submission.

    Returns:
        ``(run, outcome)`` where outcome is an :class:`AcquireOutcome` value.

    Raises:
        ConflictError: The slot could not be resolved after bounded retries.
        Exception: Database failures propagate; the caller fails closed.
    """
    if client_request_id:
        existing = workflow_run_handler.find_by_client_request(session_id, client_request_id)
        if existing is not None:
            incr(Metrics.IDEMPOTENT_REUSED)
            log.info("resent submission %s maps to run %s (%s)", client_request_id,
                     existing.run_id, existing.status)
            return existing, AcquireOutcome.DUPLICATE_REQUEST
    reap(session_id)
    for _ in range(WorkflowRunConstants.ACQUIRE_MAX_LOOPS):
        active = workflow_run_handler.get_active(session_id)
        if active is not None:
            incr(Metrics.DUPLICATE_RUN_BLOCKED)
            log.info("session %s already has active run %s (%s); attaching instead of "
                     "starting another", session_id, active.run_id, active.status)
            return active, AcquireOutcome.ALREADY_RUNNING
        snapshot = json.dumps(session_store.run_snapshot(session_id), default=str)
        run = workflow_run_handler.try_insert(
            run_id=uuid.uuid4().hex, session_id=session_id, user_id=user_id,
            client_request_id=client_request_id, owner=WORKER_ID,
            pre_run_snapshot=snapshot)
        if run is not None:
            incr(Metrics.RUNS_STARTED)
            return run, AcquireOutcome.STARTED
        if client_request_id:
            existing = workflow_run_handler.find_by_client_request(session_id,
                                                                   client_request_id)
            if existing is not None:
                incr(Metrics.IDEMPOTENT_REUSED)
                return existing, AcquireOutcome.DUPLICATE_REQUEST
    raise ConflictError("another request for this session is being started; retry")


# --- execution -----------------------------------------------------------------------
def start(run: WorkflowRun, message: str, refresh: bool,
          answers: dict | None = None, answers_only: bool = False) -> None:
    """Execute a freshly acquired run on a worker thread. Call from the event loop.

    Args:
        run: The run :func:`acquire` returned with ``STARTED``.
        message: The user's request.
        refresh: Whether retrieval content must be refreshed.
        answers: Validated structured answers sent with the turn.
        answers_only: True when the turn carried answers and no typed message.
    """
    from app.core.handlers import deck_handler

    control = SupervisedRun(run)
    with _local_lock:
        _local_runs[run.run_id] = control
    control.start_heartbeat()
    extra = {"answers": answers, "answers_only": answers_only} if answers else {}

    def execute() -> dict | None:
        try:
            with run_control.bind(control), log_context(run_id=run.run_id):
                return deck_handler.run_deck_flow(run.session_id, message, refresh,
                                                  run_id=run.run_id, control=control,
                                                  **extra)
        finally:
            control.stop_heartbeat()
            with _local_lock:
                _local_runs.pop(run.run_id, None)

    task = asyncio.get_running_loop().create_task(asyncio.to_thread(execute),
                                                  name=f"workflow-run-{run.run_id}")
    _tasks.add(task)
    task.add_done_callback(_on_task_done)


def _on_task_done(task: asyncio.Task) -> None:
    """Retrieve the run's outcome exactly once; the failure itself is already logged."""
    _tasks.discard(task)
    if task.cancelled():
        log.warning("workflow task %s was cancelled by the event loop", task.get_name())
        return
    exc = task.exception()
    if exc is not None:
        log.info("workflow task %s ended with %s (reported by the run)", task.get_name(),
                 type(exc).__name__)


# --- user stop -------------------------------------------------------------------------
def request_stop(session_id: str, run_id: str | None,
                 user_id: str | None) -> tuple[str, WorkflowRun | None]:
    """Ask the session's running workflow to stop.

    Args:
        session_id: The session.
        run_id: Only stop this run (a stale tab must not stop a newer one).
        user_id: The caller, for audit.

    Returns:
        ``(outcome, run)`` with a :class:`StopOutcome` value and the affected run.
    """
    reap(session_id)
    run = workflow_run_handler.request_cancel(session_id, run_id, user_id)
    if run is not None:
        control = local_run(run.run_id)
        if control is not None:
            control.request_cancel()                 # same pod: stop now, not on next beat
        log.info("stop requested for run %s of session %s by user %s", run.run_id,
                 session_id, user_id or "-")
        return StopOutcome.CANCELLING, run
    active = workflow_run_handler.get_active(session_id)
    if active is None or (run_id and active.run_id != run_id):
        return StopOutcome.NOT_RUNNING, workflow_run_handler.get(run_id) if run_id else None
    if active.status == RunStatus.CANCELLING:
        control = local_run(active.run_id)
        if control is not None:
            control.request_cancel()
        return StopOutcome.ALREADY_CANCELLING, active
    if active.current_stage == RunStages.PUBLISH:
        return StopOutcome.COMPLETING, active
    return StopOutcome.NOT_RUNNING, active


# --- shutdown --------------------------------------------------------------------------
def shutdown(error_code: str) -> None:
    """Release this pod's runs on shutdown so their sessions are not locked meanwhile.

    Args:
        error_code: Recorded on each run (``WORKER_SHUTDOWN``).
    """
    try:
        released = workflow_run_handler.fail_owned_by(WORKER_ID, error_code)
    except Exception:                                # noqa: BLE001
        log.warning("could not release this pod's runs on shutdown", exc_info=True)
        released = []
    with _local_lock:
        controls = list(_local_runs.values())
    for control in controls:
        control.fence(FENCE_REASON_LOST)
    if released:
        log.warning("released %d run(s) owned by this pod on shutdown", len(released))

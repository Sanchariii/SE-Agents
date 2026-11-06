"""Stage wrapper for workflow graph nodes: correlation, stop checks and recovery.

Every node in the deck graph runs through :func:`stage_node`, which:

* binds ``stage`` (and the recovery ``attempt``) on the log context, so every line a
  stage writes - and every gateway call it makes - is attributable;
* records the stage on the run (``workflow_runs.current_stage``) and checks for a user
  stop or a lost lease before any work starts (see ``RunControl.enter_stage``);
* turns low-level retries (gateway 5xx/429/timeouts, malformed JSON) into one
  ``workflow_recovering`` event per episode and a ``workflow_recovered`` event once a
  retried call succeeds, instead of silence followed by a raw error;
* for the stages in ``RunStages.RETRYABLE`` (triage, planning), re-runs **only that
  stage** on a retryable model failure the lower layers could not absorb. The run id
  never changes: an internal retry is an attempt, not a new run.

Re-running planning repeats the tool-calling planner. Its only side effect is
``session_store.set_plan``, which overwrites the stored plan, so a repeat is safe.
"""

import threading
from typing import Callable

from app.config.constants import RunStages, WorkflowRunConstants
from app.utilities import events_util as events
from app.utilities import run_control
from app.utilities.llm_errors import LlmError
from app.utilities.logger_util import get_logger, log_context
from app.utilities.metrics_util import Metrics, incr

log = get_logger(__name__)

_RECOVERING_MESSAGE = ("A temporary issue occurred while {what}. Retrying automatically.")
_RECOVERED_MESSAGE = "Recovered successfully. Continuing deck generation."

_STAGE_ACTIVITY = {
    RunStages.TRIAGE: "understanding the request",
    RunStages.EXTRACTION: "reading the source documents",
    RunStages.PLANNING: "preparing the deck plan",
    RunStages.SLIDES: "writing the slides",
    RunStages.COLLECT: "designing the slides",
    RunStages.PUBLISH: "saving the deck",
}


class _RecoveryTracker:
    """Per-stage recovery state; thread-safe because slide workers run in a pool."""

    def __init__(self, session_id: str, stage: str, max_attempts: int) -> None:
        self.session_id = session_id
        self.stage = stage
        self.max_attempts = max_attempts
        self.attempt = 1
        self._recovering = False
        self._lock = threading.Lock()

    def _publish(self, event_type: str, message: str, **data) -> None:
        events.publish(self.session_id, event_type, agent="supervisor", message=message,
                       stage=self.stage, attempt=self.attempt,
                       max_attempts=self.max_attempts, **data)

    def recovering(self, reason_code: str, force: bool = False) -> None:
        """Enter the recovering state once per episode, emitting one event.

        Args:
            reason_code: Error code that triggered the recovery.
            force: Emit even when already recovering (a new whole-stage attempt).
        """
        with self._lock:
            if self._recovering and not force:
                return
            self._recovering = True
        control = run_control.current()
        if control is not None:
            control.set_recovering(True, self.attempt)
        self._publish("workflow_recovering",
                      _RECOVERING_MESSAGE.format(
                          what=_STAGE_ACTIVITY.get(self.stage, "generating the deck")),
                      reason_code=reason_code)

    def recovered(self) -> None:
        """Leave the recovering state, emitting one event, if it was entered."""
        with self._lock:
            if not self._recovering:
                return
            self._recovering = False
        control = run_control.current()
        if control is not None:
            control.set_recovering(False, self.attempt)
        self._publish("workflow_recovered", _RECOVERED_MESSAGE)

    # RetryListener protocol, fed by the gateway transport and structured output.
    def on_retry(self, reason: str, attempt: int) -> None:
        incr(Metrics.INTERNAL_RETRIES, stage=self.stage, reason=reason, layer="call")
        self.recovering(reason)

    def on_recovered(self) -> None:
        self.recovered()


def stage_node(stage: str, fn: Callable[[dict], dict]) -> Callable[[dict], dict]:
    """Wrap a graph node with stage bookkeeping, stop checks and bounded recovery.

    Args:
        stage: Stage name from :class:`RunStages`.
        fn: The node function ``(state) -> partial state update``.

    Returns:
        A node function with the same signature.
    """
    retryable = stage in RunStages.RETRYABLE
    max_attempts = WorkflowRunConstants.STAGE_MAX_ATTEMPTS if retryable else 1

    def node(state: dict) -> dict:
        control = run_control.current()
        tracker = _RecoveryTracker(state["session_id"], stage, max_attempts)
        with log_context(stage=stage):
            if control is not None:
                control.enter_stage(stage)
                control.set_retry_listener(tracker)
            try:
                while True:
                    try:
                        with log_context(attempt=str(tracker.attempt)
                                         if tracker.attempt > 1 else None):
                            result = fn(state)
                        tracker.recovered()
                        return result
                    except LlmError as exc:
                        if not (exc.retryable and tracker.attempt < max_attempts):
                            raise
                        log.warning("stage %s failed with retryable %s (attempt %d/%d); "
                                    "retrying the stage", stage, type(exc).__name__,
                                    tracker.attempt, max_attempts,
                                    extra={"error_code": exc.error_code})
                        incr(Metrics.INTERNAL_RETRIES, stage=stage, reason=exc.error_code,
                             layer="stage")
                        tracker.attempt += 1
                        tracker.recovering(exc.error_code, force=True)
                        run_control.sleep(WorkflowRunConstants.STAGE_BACKOFF_SECONDS)
                        if control is not None:
                            control.enter_stage(stage)
            finally:
                if control is not None:
                    control.set_retry_listener(None)

    node.__name__ = getattr(fn, "__name__", stage)
    return node

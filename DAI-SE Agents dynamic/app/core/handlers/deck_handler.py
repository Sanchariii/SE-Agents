"""Runs the deck workflow and frames it with the SSE events the UI listens for."""

import uuid
from datetime import datetime, timezone

from app.config.constants import (RunErrorCodes, RunStatus, SessionMemoryConstants,
                                  WorkflowRunConstants)
from app.core.handlers.chat_message_handler import chat_message_handler
from app.core.handlers.session_store import session_store
from app.core.handlers.workflow_event_handler import workflow_event_handler
from app.core.handlers.workflow_run_handler import workflow_run_handler
from app.genai.workflows.deck_workflow import run_workflow
from app.utilities import events_util as events
from app.utilities.llm_errors import message_for, to_user_message
from app.utilities.logger_util import get_logger, log_context, redact
from app.utilities.metrics_util import Metrics, incr
from app.utilities.run_control import RunCancelledError, RunControl, RunFencedError

log = get_logger(__name__)


def _persist_turn(session_id: str, role: str, content: str,
                  result: dict | None = None,
                  run_id: str | None = None) -> None:
    """Durably record a chat turn in Postgres, best-effort.

    The `done` SSE event and the Redis working copy are both ephemeral, so this write
    is what makes the conversation — and an assistant turn's final-response metadata —
    survive a cache/Redis loss. A Postgres hiccup must never break the live run, so a
    failure is logged with full context rather than raised.

    Args:
        session_id: The session the turn belongs to.
        role: ``user`` | ``assistant`` | ``system``.
        content: The message text (the agent's reply for an assistant turn).
        result: The structured final-response payload for an assistant turn, else None.
        run_id: Workflow invocation associated with the turn.

    Returns:
        None.
    """
    try:
        chat_message_handler.record(
            session_id, role, content, result=result, run_id=run_id)
    except Exception:                               # noqa: BLE001
        log.error("failed to persist %s turn for session %s run %s to Postgres",
                  role, session_id, run_id or "-", exc_info=True)


def _snapshot(session_id: str) -> dict | None:
    """Capture the session state for persistence, best-effort.

    Args:
        session_id: The session whose Redis state is captured.

    Returns:
        The snapshot, or None if it could not be read.
    """
    try:
        return session_store.snapshot(session_id)
    except Exception:                               # noqa: BLE001
        log.warning("could not snapshot session %s; turn persisted without memory",
                    session_id, exc_info=True)
        return None


def _event_sink(session_id: str, run_id: str, durable: bool):
    """Durable writer for a run's events: idempotent batch insert + high-water mark.

    A write failure is raised to the capture's flusher, which logs it and retries the
    same events on the next tick (the insert is ``ON CONFLICT DO NOTHING``).
    """
    def sink(batch: list[dict]) -> None:
        workflow_event_handler.record_batch(session_id, run_id, batch)
        if durable:
            try:
                workflow_run_handler.set_last_event_index(run_id, batch[-1]["event_index"])
            except Exception:                       # noqa: BLE001 - informational only
                log.warning("could not advance last_event_index for run %s", run_id,
                            exc_info=True)
    return sink


def _flush(capture: events.RunEventCapture) -> None:
    """Make every captured event durable before the terminal event is delivered."""
    try:
        capture.flush()
    except Exception:                               # noqa: BLE001
        log.error("failed to persist workflow events for session %s run %s",
                  capture.session_id, capture.run_id, exc_info=True)


def _finish(control: RunControl | None, status: str, *, error_code: str | None = None,
            error_message: str | None = None, deck_version: int | None = None) -> None:
    """Record the run's terminal state - the single place a run is finalized.

    Best-effort: a database failure here must not hide the outcome from the user.
    """
    if control is None:
        return
    try:
        if not workflow_run_handler.finish(
                control.run_id, control.owner, status, error_code=error_code,
                error_message=error_message, deck_version=deck_version):
            log.warning("run %s was already finalized elsewhere; %s not recorded",
                        control.run_id, status)
    except Exception:                               # noqa: BLE001
        log.error("could not record %s for run %s", status, control.run_id, exc_info=True)


def _timing(started: datetime) -> dict:
    """How long this turn's workflow ran, for the client.

    Every turn ends here: questions asked, a plan shown for review, or a deck built
    after approval. So each of those final messages carries its own timing.

    Args:
        started: When the workflow started (UTC).

    Returns:
        ``started_at`` and ``ended_at`` (ISO-8601 UTC) and ``time_taken_minutes``.
    """
    ended = datetime.now(timezone.utc)
    minutes = round((ended - started).total_seconds() / 60,
                    WorkflowRunConstants.TIME_TAKEN_DECIMALS)
    return {"started_at": started.isoformat(), "ended_at": ended.isoformat(),
            "time_taken_minutes": minutes}


def run_deck_flow(session_id: str, message: str, refresh: bool = False,
                  run_id: str | None = None, control: RunControl | None = None,
                  answers: dict | None = None, answers_only: bool = False) -> dict:
    """Run one deck workflow and persist the exact activity events sent over SSE.

    Events are made durable incrementally while the run works, so a reconnect on any
    pod can replay them. Outcome ordering is fixed: artifacts and the assistant turn
    are persisted, then the events, then the run's terminal state, and only then is the
    terminal ``done`` event delivered - so a client that reacts to ``done`` always
    finds the deck and an authoritative run status.

    Args:
        session_id: Session that owns the workflow.
        message: User request for this turn.
        refresh: Whether retrieval content must be refreshed.
        run_id: Invocation identifier allocated by the run registry.
        control: The run's stop/lease control (``run_supervisor.SupervisedRun``); None
            when run directly (scripts, tests), which skips run-registry writes.
        answers: Validated structured answers sent with the turn.
        answers_only: True when the turn carried answers and no typed message.

    Returns:
        Structured final workflow result (``{"status": "cancelled"}`` for a stop).

    Raises:
        Exception: Re-raises workflow failures after emitting and persisting failure
            activity events; the supervisor retrieves it.
    """
    resolved_run_id = run_id or uuid.uuid4().hex
    started = datetime.now(timezone.utc)
    log.info("deck flow starting for session %s run %s at %s",
             session_id, resolved_run_id, started.isoformat())
    sink = _event_sink(session_id, resolved_run_id, durable=control is not None)
    with log_context(run_id=resolved_run_id), \
            events.capture_run(session_id, resolved_run_id, sink=sink) as capture:
        events.publish(session_id, "agent_start", agent="supervisor",
                       message="Starting the training-deck workflow")
        # Record the user's turn before any work runs, so a failed workflow is auditable.
        # Structured answers ride on the turn's result, so history shows what was picked.
        _persist_turn(session_id, "user", message,
                      result={"answers": answers} if answers else None,
                      run_id=resolved_run_id)
        try:
            result = (run_workflow(session_id, message, refresh, answers=answers,
                                   answers_only=answers_only)
                      if answers else run_workflow(session_id, message, refresh))
            # Measured here, so questions, a plan review and a finished deck each carry
            # the time their own turn took; persisted with the result like the rest.
            result = {**result, **_timing(started)}
            log.info("deck flow for session %s run %s took %s min (action %s)",
                     session_id, resolved_run_id, result["time_taken_minutes"],
                     result.get("action") or "-")
            reply = result.get("reply") or ""
            # Cache and persist every successful final payload, including an empty reply.
            session_store.add_message(
                session_id, "assistant", reply, result=result, run_id=resolved_run_id)
            # The durable row also carries a snapshot of the session's Redis state, so
            # a later turn can be restored after an eviction or restart.
            snapshot = _snapshot(session_id)
            _persist_turn(
                session_id, "assistant", reply,
                result={**result, SessionMemoryConstants.RESULT_KEY: snapshot}
                if snapshot else result,
                run_id=resolved_run_id)
            # Capture the terminal event before delivery so persistence wins the race
            # with a frontend that reloads messages as soon as it receives "done".
            terminal = events.publish(
                session_id, "done", agent="supervisor", message="Workflow complete",
                deliver=False, result=result, status=RunStatus.COMPLETED)
            _flush(capture)
            _finish(control, RunStatus.COMPLETED, deck_version=result.get("deck_version"))
            events.dispatch(session_id, terminal)
            incr(Metrics.RUNS_COMPLETED)
            log.info("deck flow completed for session %s run %s",
                     session_id, resolved_run_id)
            return result
        except (RunCancelledError, RunFencedError) as stop:
            if isinstance(stop, RunFencedError) and not stop.analytics_cancelled:
                # The lease was lost (reaped as WORKER_LOST): another actor owns the
                # outcome now. Publish nothing terminal; the stream reports the reap.
                log.warning("deck flow for session %s run %s stopped: lease lost",
                            session_id, resolved_run_id)
                _flush(capture)
                return {"status": "lost"}
            # A user stop: undo this run's working-state writes, then report it.
            if control is not None:
                from app.core.handlers.run_supervisor import restore_pre_run_state
                restore_pre_run_state(control)
            log.info("run cancelled by user: session %s run %s", session_id,
                     resolved_run_id)
            code = RunErrorCodes.CANCELLED_BY_USER
            events.publish(session_id, "workflow_cancelled", agent="supervisor",
                           message=message_for(code), error_code=code)
            timing = _timing(started)
            terminal = events.publish(
                session_id, "done", agent="supervisor", message="Workflow stopped",
                deliver=False, status=RunStatus.CANCELLED, error=message_for(code),
                error_code=code, retryable=False, **timing)
            _flush(capture)
            _finish(control, RunStatus.CANCELLED, error_code=code)
            events.dispatch(session_id, terminal)
            return {"status": "cancelled", **timing}
        except Exception as exc:                    # noqa: BLE001
            log.exception("deck flow failed for session %s run %s",
                          session_id, resolved_run_id)
            # The user sees a stable code and safe copy; the raw error stays in the
            # log above and (redacted, bounded) on the run row for diagnostics.
            code, safe_message, retryable = to_user_message(exc)
            events.publish(
                session_id, "agent_error", agent="supervisor", message=safe_message,
                error_code=code, retryable=retryable)
            terminal = events.publish(
                session_id, "done", agent="supervisor", message="Workflow failed",
                deliver=False, status=RunStatus.FAILED, error=safe_message,
                error_code=code, retryable=retryable, **_timing(started))
            _flush(capture)
            _finish(control, RunStatus.FAILED, error_code=code,
                    error_message=redact(f"{type(exc).__name__}: {exc}")[
                        :WorkflowRunConstants.ERROR_MESSAGE_MAX_CHARS])
            events.dispatch(session_id, terminal)
            incr(Metrics.RUNS_FAILED, error_code=code)
            raise

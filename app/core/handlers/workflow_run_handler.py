"""Operational registry of workflow runs: one active run per session, across pods.

``workflow_runs`` is the authoritative state of every top-level run. Two partial
unique indexes make the invariants atomic in Postgres itself, so they hold across
replicas with no process-local lock:

* at most one row per ``session_id`` in an active status (QUEUED/RUNNING/RECOVERING/
  CANCELLING) - a second submission attaches to the first instead of starting work;
* at most one row per ``(session_id, client_request_id)`` - a resent submission
  returns the run it already created.

Every transition is a compare-and-set on the status and the owning worker, so a
terminal run can never be revived by a late write from a stale worker.

This module is SQL only. Orchestration (stale-run reaping with session restore,
heartbeats, cancellation delivery) lives in :mod:`app.core.handlers.run_supervisor`.
"""

import os
import socket
import uuid

from app.config.constants import PgTables, RunErrorCodes, RunStages, RunStatus
from app.core.models.workflow_run_model import WorkflowRun
from app.utilities.logger_util import get_logger
from app.utilities.pgsql_util import PgsqlUtil

log = get_logger(__name__)

_T = PgTables.WORKFLOW_RUNS

#: This process's identity as a run owner: host (pod), pid and a per-boot nonce, so a
#: restarted container with a recycled pid is still a different owner.
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

_ACTIVE_SQL = "(" + ", ".join(f"'{s}'" for s in RunStatus.ACTIVE) + ")"
_LIVE_SQL = f"('{RunStatus.RUNNING}', '{RunStatus.RECOVERING}')"

_DDL = f"""
CREATE TABLE IF NOT EXISTS {_T} (
    run_id              TEXT PRIMARY KEY,
    session_id          TEXT NOT NULL,
    user_id             TEXT,
    client_request_id   TEXT,
    status              TEXT NOT NULL,
    current_stage       TEXT,
    attempt             INT NOT NULL DEFAULT 1,
    owner               TEXT NOT NULL,
    last_event_index    INT NOT NULL DEFAULT 0,
    deck_version        INT,
    error_code          TEXT,
    error_message       TEXT,
    cancel_requested_at TIMESTAMPTZ,
    cancel_requested_by TEXT,
    pre_run_snapshot    TEXT,
    heartbeat_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ
);
"""

_DDL_INDEXES = (
    # The one-active-run-per-session invariant, enforced atomically by Postgres.
    f"CREATE UNIQUE INDEX IF NOT EXISTS ux_{_T}_active_session "
    f"ON {_T} (session_id) WHERE status IN {_ACTIVE_SQL}",
    # Idempotent submissions: a resent request maps to the run it created.
    f"CREATE UNIQUE INDEX IF NOT EXISTS ux_{_T}_client_request "
    f"ON {_T} (session_id, client_request_id) WHERE client_request_id IS NOT NULL",
    f"CREATE INDEX IF NOT EXISTS ix_{_T}_session_created ON {_T} (session_id, created_at DESC)",
    f"CREATE INDEX IF NOT EXISTS ix_{_T}_active_heartbeat "
    f"ON {_T} (heartbeat_at) WHERE status IN {_ACTIVE_SQL}",
)

_COLUMNS = """run_id, session_id, user_id, client_request_id, status, current_stage, attempt,
owner, last_event_index, deck_version, error_code, error_message, cancel_requested_at,
cancel_requested_by, pre_run_snapshot, heartbeat_at, created_at, updated_at, finished_at"""

_SECONDS = "CAST(:{name} AS INTEGER) * INTERVAL '1 second'"


def _session_scope(session_id: str | None, params: dict) -> tuple[str, dict]:
    """Optional ``AND session_id = :s`` clause, binding ``:s`` only when it is used."""
    if not session_id:
        return "", params
    return "AND session_id = :s", {**params, "s": session_id}


class WorkflowRunHandler:
    """Atomic, compare-and-set access to ``workflow_runs``."""

    def __init__(self) -> None:
        """Initialize the handler with a lazy shared Postgres utility."""
        self._db: PgsqlUtil | None = None

    def _pg(self) -> PgsqlUtil:
        """Return the lazily-created shared Postgres utility."""
        if self._db is None:
            self._db = PgsqlUtil()
        return self._db

    def ensure_table(self) -> None:
        """Create the run table and its invariant-enforcing indexes when absent."""
        self._pg().execute(_DDL)
        for statement in _DDL_INDEXES:
            self._pg().execute(statement)

    @staticmethod
    def _one(rows: list[dict]) -> WorkflowRun | None:
        return WorkflowRun(**rows[0]) if rows else None

    # --- reads ----------------------------------------------------------------
    def get(self, run_id: str) -> WorkflowRun | None:
        """Return one run by id."""
        return self._one(self._pg().query(
            f"SELECT {_COLUMNS} FROM {_T} WHERE run_id = :r", {"r": run_id}))

    def get_active(self, session_id: str) -> WorkflowRun | None:
        """Return the session's active run, if any."""
        return self._one(self._pg().query(
            f"SELECT {_COLUMNS} FROM {_T} WHERE session_id = :s AND status IN {_ACTIVE_SQL} "
            f"LIMIT 1", {"s": session_id}))

    def get_latest(self, session_id: str) -> WorkflowRun | None:
        """Return the session's most recently created run, if any."""
        return self._one(self._pg().query(
            f"SELECT {_COLUMNS} FROM {_T} WHERE session_id = :s "
            f"ORDER BY created_at DESC LIMIT 1", {"s": session_id}))

    def find_by_client_request(self, session_id: str,
                               client_request_id: str) -> WorkflowRun | None:
        """Return the run a previous delivery of the same submission created."""
        return self._one(self._pg().query(
            f"SELECT {_COLUMNS} FROM {_T} WHERE session_id = :s AND client_request_id = :c",
            {"s": session_id, "c": client_request_id}))

    def statuses(self, session_id: str, run_ids: list[str]) -> dict[str, str]:
        """Map run id -> status for the given runs of a session (for chat history)."""
        if not run_ids:
            return {}
        rows = self._pg().query(
            f"SELECT run_id, status FROM {_T} WHERE session_id = :s "
            f"AND run_id = ANY(CAST(:ids AS TEXT[]))",
            {"s": session_id, "ids": list(dict.fromkeys(run_ids))})
        return {row["run_id"]: row["status"] for row in rows}

    # --- creation ---------------------------------------------------------------
    def try_insert(self, *, run_id: str, session_id: str, user_id: str | None,
                   client_request_id: str | None, owner: str,
                   pre_run_snapshot: str | None) -> WorkflowRun | None:
        """Insert a RUNNING run unless a unique invariant already holds the slot.

        Returns:
            The new run, or None when the session already has an active run or the
            ``client_request_id`` was already used (the caller then reads that run).
        """
        return self._one(self._pg().execute_returning(
            f"""INSERT INTO {_T} (run_id, session_id, user_id, client_request_id, status,
                                  owner, pre_run_snapshot)
                VALUES (:run_id, :session_id, :user_id, :client_request_id, :status,
                        :owner, :snapshot)
                ON CONFLICT DO NOTHING
                RETURNING {_COLUMNS}""",
            {"run_id": run_id, "session_id": session_id, "user_id": user_id,
             "client_request_id": client_request_id, "status": RunStatus.RUNNING,
             "owner": owner, "snapshot": pre_run_snapshot}))

    # --- stale-run recovery -------------------------------------------------------
    def reap_lost(self, session_id: str | None, stale_seconds: int) -> list[WorkflowRun]:
        """Fail live runs whose owner stopped heart-beating (pod died / hung).

        Args:
            session_id: Restrict to one session, or None for all.
            stale_seconds: Heartbeat age after which an owner is presumed dead.

        Returns:
            The runs moved to ``FAILED`` / ``WORKER_LOST``.
        """
        scope, params = _session_scope(session_id, {
            "failed": RunStatus.FAILED, "code": RunErrorCodes.WORKER_LOST,
            "stale": stale_seconds})
        rows = self._pg().execute_returning(
            f"""UPDATE {_T}
                SET status = :failed, error_code = :code, finished_at = now(),
                    updated_at = now()
                WHERE status IN ('{RunStatus.QUEUED}', '{RunStatus.RUNNING}',
                                 '{RunStatus.RECOVERING}')
                  AND heartbeat_at < now() - {_SECONDS.format(name='stale')} {scope}
                RETURNING {_COLUMNS}""", params)
        return [WorkflowRun(**row) for row in rows]

    def force_cancel_expired(self, session_id: str | None, force_seconds: int,
                             stale_seconds: int) -> list[WorkflowRun]:
        """Finish stops the worker never acknowledged (stuck call or dead owner).

        Returns:
            The runs moved from ``CANCELLING`` to ``CANCELLED``; the caller restores
            each one's ``pre_run_snapshot``.
        """
        scope, params = _session_scope(session_id, {
            "cancelled": RunStatus.CANCELLED, "code": RunErrorCodes.CANCELLED_BY_USER,
            "force": force_seconds, "stale": stale_seconds})
        rows = self._pg().execute_returning(
            f"""UPDATE {_T}
                SET status = :cancelled, error_code = :code, finished_at = now(),
                    updated_at = now()
                WHERE status = '{RunStatus.CANCELLING}'
                  AND (cancel_requested_at < now() - {_SECONDS.format(name='force')}
                       OR heartbeat_at < now() - {_SECONDS.format(name='stale')}) {scope}
                RETURNING {_COLUMNS}""", params)
        return [WorkflowRun(**row) for row in rows]

    # --- owner-side transitions ---------------------------------------------------
    def heartbeat(self, run_id: str, owner: str) -> WorkflowRun | None:
        """Renew the owner's lease and read back cancel state.

        Returns:
            The run while this owner still holds it active, else None (it was reaped,
            force-cancelled or finished), which fences the worker.
        """
        return self._one(self._pg().execute_returning(
            f"""UPDATE {_T} SET heartbeat_at = now()
                WHERE run_id = :r AND owner = :o AND status IN {_ACTIVE_SQL}
                RETURNING {_COLUMNS}""", {"r": run_id, "o": owner}))

    def set_stage(self, run_id: str, owner: str, stage: str) -> WorkflowRun | None:
        """Record the stage the run entered; a no-op once the run is terminal."""
        return self._one(self._pg().execute_returning(
            f"""UPDATE {_T} SET current_stage = :stage, updated_at = now(),
                                heartbeat_at = now()
                WHERE run_id = :r AND owner = :o AND status IN {_ACTIVE_SQL}
                RETURNING {_COLUMNS}""", {"r": run_id, "o": owner, "stage": stage}))

    def set_recovering(self, run_id: str, owner: str, recovering: bool,
                       attempt: int) -> bool:
        """Flip RUNNING <-> RECOVERING; never overrides CANCELLING or a terminal state."""
        target = RunStatus.RECOVERING if recovering else RunStatus.RUNNING
        rows = self._pg().execute_returning(
            f"""UPDATE {_T} SET status = :target, attempt = :attempt, updated_at = now()
                WHERE run_id = :r AND owner = :o AND status IN {_LIVE_SQL}
                RETURNING run_id""",
            {"r": run_id, "o": owner, "target": target, "attempt": attempt})
        return bool(rows)

    def set_last_event_index(self, run_id: str, event_index: int) -> None:
        """Advance the run's high-water event index (monotonic)."""
        self._pg().execute(
            f"""UPDATE {_T} SET last_event_index = GREATEST(last_event_index, :i)
                WHERE run_id = :r""", {"r": run_id, "i": event_index})

    def finish(self, run_id: str, owner: str, status: str, *,
               error_code: str | None = None, error_message: str | None = None,
               deck_version: int | None = None) -> bool:
        """Move an active run owned by ``owner`` to a terminal state, exactly once.

        Returns:
            True when this call made the transition; False when the run was already
            terminal or is owned by someone else (a fenced, stale worker).
        """
        rows = self._pg().execute_returning(
            f"""UPDATE {_T}
                SET status = :status, error_code = :code, error_message = :msg,
                    deck_version = COALESCE(:deck, deck_version), finished_at = now(),
                    updated_at = now(), pre_run_snapshot = NULL
                WHERE run_id = :r AND owner = :o AND status IN {_ACTIVE_SQL}
                RETURNING run_id""",
            {"r": run_id, "o": owner, "status": status, "code": error_code,
             "msg": error_message, "deck": deck_version})
        return bool(rows)

    def fail_owned_by(self, owner: str, error_code: str) -> list[WorkflowRun]:
        """Fail every active run this process owns (graceful shutdown)."""
        rows = self._pg().execute_returning(
            f"""UPDATE {_T}
                SET status = :failed, error_code = :code, finished_at = now(),
                    updated_at = now()
                WHERE owner = :o AND status IN {_ACTIVE_SQL}
                RETURNING {_COLUMNS}""",
            {"o": owner, "failed": RunStatus.FAILED, "code": error_code})
        return [WorkflowRun(**row) for row in rows]

    # --- user cancellation -----------------------------------------------------------
    def request_cancel(self, session_id: str, run_id: str | None,
                       user_id: str | None) -> WorkflowRun | None:
        """Atomically move the session's live run to CANCELLING.

        Runs already in the publish stage are not cancellable (the deck is being
        persisted); the caller reads the active run to report why.

        Returns:
            The run now CANCELLING, or None when nothing was eligible.
        """
        params = {"s": session_id, "u": user_id, "cancelling": RunStatus.CANCELLING,
                  "publish": RunStages.PUBLISH}
        scope = ""
        if run_id:
            scope, params["r"] = "AND run_id = :r", run_id
        return self._one(self._pg().execute_returning(
            f"""UPDATE {_T}
                SET status = :cancelling, cancel_requested_at = now(),
                    cancel_requested_by = :u, updated_at = now()
                WHERE session_id = :s {scope} AND status IN {_LIVE_SQL}
                  AND COALESCE(current_stage, '') <> :publish
                RETURNING {_COLUMNS}""", params))

    def delete_all_for_session(self, session_id: str) -> None:
        """Remove a deleted session's run rows."""
        self._pg().execute(f"DELETE FROM {_T} WHERE session_id = :s", {"s": session_id})


workflow_run_handler = WorkflowRunHandler()

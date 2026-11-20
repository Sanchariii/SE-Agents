"""Run registry: atomic one-active-run-per-session, idempotency, CAS transitions, leases.

SQL-shape tests use a Postgres double. The optional ``postgres`` test races real
inserts and runs only when ``TEST_PG_DSN`` points at a scratch database.
"""

import os
import threading
import uuid

import pytest

from app.config.constants import RunStatus
from app.core.handlers import run_supervisor
from app.core.handlers.workflow_run_handler import WORKER_ID, WorkflowRunHandler
from app.core.models.workflow_run_model import WorkflowRun
from app.utilities.run_control import FENCE_REASON_CANCELLED, RunCancelledError


class _FakePg:
    """Records statements; returns scripted rows for RETURNING/SELECT."""

    def __init__(self, rows: list[list[dict]] | None = None) -> None:
        self.statements: list[tuple[str, dict]] = []
        self.rows = list(rows or [])

    def _next(self) -> list[dict]:
        return self.rows.pop(0) if self.rows else []

    def execute(self, sql, params=None):
        self.statements.append((sql, params or {}))

    def execute_returning(self, sql, params=None):
        self.statements.append((sql, params or {}))
        return self._next()

    def query(self, sql, params=None):
        self.statements.append((sql, params or {}))
        return self._next()


def _row(**overrides) -> dict:
    row = {"run_id": "r1", "session_id": "s1", "status": RunStatus.RUNNING,
           "owner": WORKER_ID}
    row.update(overrides)
    return row


def _run(**overrides) -> WorkflowRun:
    return WorkflowRun(**_row(**overrides))


def _handler(rows=None) -> tuple[WorkflowRunHandler, _FakePg]:
    handler, fake = WorkflowRunHandler(), _FakePg(rows)
    handler._db = fake
    return handler, fake


# --- SQL shape ----------------------------------------------------------------------------
def test_schema_enforces_one_active_run_and_idempotent_requests_in_postgres():
    handler, fake = _handler()

    handler.ensure_table()

    sql = "\n".join(s for s, _ in fake.statements)
    assert "CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_runs_active_session" in sql
    assert "WHERE status IN ('QUEUED', 'RUNNING', 'RECOVERING', 'CANCELLING')" in sql
    assert "(session_id, client_request_id) WHERE client_request_id IS NOT NULL" in sql


def test_insert_yields_to_the_invariant_instead_of_failing():
    handler, fake = _handler([[]])

    assert handler.try_insert(run_id="r2", session_id="s1", user_id="3",
                              client_request_id=None, owner=WORKER_ID,
                              pre_run_snapshot="{}") is None
    assert "ON CONFLICT DO NOTHING" in fake.statements[0][0]


def test_finish_is_a_compare_and_set_on_owner_and_active_status():
    handler, fake = _handler([[]])

    assert handler.finish("r1", "other-owner", RunStatus.COMPLETED) is False
    sql, params = fake.statements[0]
    assert "owner = :o" in sql and "status IN ('QUEUED', 'RUNNING', 'RECOVERING'" in sql
    assert params["o"] == "other-owner"


def test_recovery_flip_never_overrides_a_stop_or_terminal_state():
    handler, fake = _handler([[]])

    handler.set_recovering("r1", WORKER_ID, True, 2)

    assert "status IN ('RUNNING', 'RECOVERING')" in fake.statements[0][0]


def test_request_cancel_excludes_publish_and_binds_run_id_only_when_given():
    handler, fake = _handler([[], []])

    handler.request_cancel("s1", None, "3")
    handler.request_cancel("s1", "r9", "3")

    (sql_any, p_any), (sql_one, p_one) = fake.statements
    assert "current_stage, '') <> :publish" in sql_any
    assert "run_id = :r" not in sql_any and "r" not in p_any
    assert "run_id = :r" in sql_one and p_one["r"] == "r9"


def test_reaping_scopes_to_a_session_only_when_asked():
    handler, fake = _handler([[], []])

    handler.reap_lost(None, 90)
    handler.reap_lost("s1", 90)

    assert "session_id = :s" not in fake.statements[0][0]
    assert fake.statements[1][1]["s"] == "s1"


# --- admission ---------------------------------------------------------------------------
@pytest.fixture
def supervisor(monkeypatch):
    calls = {"insert": 0}
    monkeypatch.setattr(run_supervisor, "reap", lambda session_id=None: None)
    monkeypatch.setattr(run_supervisor.session_store, "run_snapshot",
                        lambda sid: {"plan": {"title": "before"}})
    h = run_supervisor.workflow_run_handler
    monkeypatch.setattr(h, "find_by_client_request", lambda sid, crid: None)
    monkeypatch.setattr(h, "get_active", lambda sid: None)

    def try_insert(**kwargs):
        calls["insert"] += 1
        calls["kwargs"] = kwargs
        return _run(run_id=kwargs["run_id"], client_request_id=kwargs["client_request_id"])
    monkeypatch.setattr(h, "try_insert", try_insert)
    return h, calls


def test_a_new_submission_starts_a_run_with_a_pre_run_snapshot(supervisor):
    _, calls = supervisor

    run, outcome = run_supervisor.acquire("s1", "3", "req-1")

    assert outcome == run_supervisor.AcquireOutcome.STARTED
    assert '"before"' in calls["kwargs"]["pre_run_snapshot"]
    assert calls["kwargs"]["owner"] == WORKER_ID


def test_a_second_submission_attaches_to_the_active_run(supervisor, monkeypatch):
    h, calls = supervisor
    monkeypatch.setattr(h, "get_active", lambda sid: _run(run_id="original"))

    run, outcome = run_supervisor.acquire("s1", "3", "req-2")

    assert (run.run_id, outcome) == ("original", run_supervisor.AcquireOutcome.ALREADY_RUNNING)
    assert calls["insert"] == 0


def test_a_resent_submission_returns_the_run_it_created(supervisor, monkeypatch):
    h, calls = supervisor
    monkeypatch.setattr(h, "find_by_client_request",
                        lambda sid, crid: _run(run_id="first", client_request_id=crid))

    run, outcome = run_supervisor.acquire("s1", "3", "req-1")

    assert (run.run_id, outcome) == ("first", run_supervisor.AcquireOutcome.DUPLICATE_REQUEST)
    assert calls["insert"] == 0


def test_losing_the_insert_race_attaches_to_the_winner(supervisor, monkeypatch):
    h, _ = supervisor
    actives = [None, _run(run_id="winner")]
    monkeypatch.setattr(h, "get_active", lambda sid: actives.pop(0))
    monkeypatch.setattr(h, "try_insert", lambda **kwargs: None)

    run, outcome = run_supervisor.acquire("s1", "3", None)

    assert (run.run_id, outcome) == ("winner", run_supervisor.AcquireOutcome.ALREADY_RUNNING)


# --- lease / fencing ---------------------------------------------------------------------
def test_a_lost_lease_fences_the_worker(monkeypatch):
    h = run_supervisor.workflow_run_handler
    monkeypatch.setattr(h, "heartbeat", lambda run_id, owner: None)
    monkeypatch.setattr(h, "get", lambda run_id: _run(status=RunStatus.CANCELLED))
    control = run_supervisor.SupervisedRun(_run())

    control.beat()

    assert control.fenced and control.fence_reason == FENCE_REASON_CANCELLED


def test_a_stop_seen_on_stage_entry_halts_the_worker(monkeypatch):
    monkeypatch.setattr(run_supervisor.workflow_run_handler, "set_stage",
                        lambda run_id, owner, stage: _run(status=RunStatus.CANCELLING))
    control = run_supervisor.SupervisedRun(_run())

    with pytest.raises(RunCancelledError):
        control.enter_stage("publish")


# --- real database race (opt-in) ---------------------------------------------------------
@pytest.mark.skipif(not os.environ.get("TEST_PG_DSN"), reason="needs TEST_PG_DSN")
def test_concurrent_inserts_admit_exactly_one_run():
    from app.utilities.pgsql_util import PgsqlUtil

    handler = WorkflowRunHandler()
    handler._db = PgsqlUtil(os.environ["TEST_PG_DSN"])
    handler.ensure_table()
    session = f"race-{uuid.uuid4().hex}"
    winners: list[WorkflowRun] = []
    barrier = threading.Barrier(10)

    def contend() -> None:
        barrier.wait()
        run = handler.try_insert(run_id=uuid.uuid4().hex, session_id=session, user_id=None,
                                 client_request_id=None, owner=WORKER_ID,
                                 pre_run_snapshot=None)
        if run is not None:
            winners.append(run)

    threads = [threading.Thread(target=contend) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert len(winners) == 1
    finally:
        handler.delete_all_for_session(session)

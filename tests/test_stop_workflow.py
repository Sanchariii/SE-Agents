"""User stop: cooperative cancellation, session rollback, outcomes and forced stops."""

import json

import pytest

from app.config.constants import PlanStatus, RunErrorCodes, RunStages, RunStatus
from app.core.dependencies import auth
from app.core.handlers import deck_handler, run_supervisor
from app.core.handlers.workflow_run_handler import WORKER_ID
from app.core.models.workflow_run_model import WorkflowRun
from app.utilities.run_control import (FENCE_REASON_CANCELLED, RunCancelledError,
                                       RunFencedError)


def _run(**overrides) -> WorkflowRun:
    row = {"run_id": "r1", "session_id": "s1", "status": RunStatus.RUNNING,
           "owner": WORKER_ID}
    row.update(overrides)
    return WorkflowRun(**row)


def test_stop_signals_cannot_be_swallowed_by_broad_fallbacks():
    """Per-slide `except Exception` fallbacks must never turn a stop into a fallback."""
    assert not issubclass(RunCancelledError, Exception)
    assert not issubclass(RunFencedError, Exception)
    with pytest.raises(RunCancelledError):
        try:
            raise RunCancelledError("r1")
        except Exception:                            # noqa: BLE001 - the point of the test
            pytest.fail("a stop was swallowed")


# --- request_stop outcomes ------------------------------------------------------------
@pytest.fixture
def registry(monkeypatch):
    h = run_supervisor.workflow_run_handler
    monkeypatch.setattr(run_supervisor, "reap", lambda session_id=None: None)
    monkeypatch.setattr(h, "request_cancel", lambda sid, rid, uid: None)
    monkeypatch.setattr(h, "get_active", lambda sid: None)
    monkeypatch.setattr(h, "get", lambda rid: None)
    return h


def test_an_accepted_stop_is_delivered_to_a_local_worker_immediately(registry, monkeypatch):
    monkeypatch.setattr(registry, "request_cancel",
                        lambda sid, rid, uid: _run(status=RunStatus.CANCELLING))
    control = run_supervisor.SupervisedRun(_run())
    monkeypatch.setitem(run_supervisor._local_runs, "r1", control)

    outcome, run = run_supervisor.request_stop("s1", "r1", "3")

    assert outcome == run_supervisor.StopOutcome.CANCELLING and run.run_id == "r1"
    assert control.cancelled


def test_stop_with_nothing_running(registry):
    assert run_supervisor.request_stop("s1", None, "3")[0] == \
        run_supervisor.StopOutcome.NOT_RUNNING


def test_stop_for_another_run_does_not_touch_the_active_one(registry, monkeypatch):
    monkeypatch.setattr(registry, "get_active", lambda sid: _run(run_id="newer"))

    assert run_supervisor.request_stop("s1", "older", "3")[0] == \
        run_supervisor.StopOutcome.NOT_RUNNING


def test_a_repeated_stop_is_idempotent(registry, monkeypatch):
    monkeypatch.setattr(registry, "get_active",
                        lambda sid: _run(status=RunStatus.CANCELLING))

    assert run_supervisor.request_stop("s1", "r1", "3")[0] == \
        run_supervisor.StopOutcome.ALREADY_CANCELLING


def test_a_run_already_saving_its_deck_completes(registry, monkeypatch):
    monkeypatch.setattr(registry, "get_active",
                        lambda sid: _run(current_stage=RunStages.PUBLISH))

    assert run_supervisor.request_stop("s1", "r1", "3")[0] == \
        run_supervisor.StopOutcome.COMPLETING


def test_an_unacknowledged_stop_is_forced_and_rolled_back(monkeypatch):
    h = run_supervisor.workflow_run_handler
    snapshot = json.dumps({"plan": {"title": "before"}})
    monkeypatch.setattr(h, "reap_lost", lambda sid, stale: [])
    monkeypatch.setattr(h, "force_cancel_expired",
                        lambda sid, force, stale: [_run(status=RunStatus.CANCELLED,
                                                        pre_run_snapshot=snapshot)])
    restored: list[tuple] = []
    monkeypatch.setattr(run_supervisor.session_store, "restore_snapshot",
                        lambda sid, snap: restored.append((sid, snap)))
    control = run_supervisor.SupervisedRun(_run())
    monkeypatch.setitem(run_supervisor._local_runs, "r1", control)

    run_supervisor.reap("s1")

    assert restored == [("s1", {"plan": {"title": "before"}})]
    assert control.fenced and control.fence_reason == FENCE_REASON_CANCELLED


# --- the worker's side of a stop -----------------------------------------------------------
def test_a_stopped_run_rolls_back_and_ends_with_a_cancelled_done(monkeypatch):
    persisted: list[str] = []
    finished: list[tuple] = []
    restored: list[dict] = []
    monkeypatch.setattr(deck_handler.chat_message_handler, "record",
                        lambda sid, role, content, result=None, run_id=None:
                        persisted.append(role))
    monkeypatch.setattr(deck_handler.workflow_event_handler, "record_batch",
                        lambda *a, **k: None)
    monkeypatch.setattr(deck_handler.workflow_run_handler, "set_last_event_index",
                        lambda *a, **k: None)
    monkeypatch.setattr(deck_handler.workflow_run_handler, "finish",
                        lambda run_id, owner, status, **k: finished.append((status, k)) or True)
    monkeypatch.setattr(run_supervisor.session_store, "restore_snapshot",
                        lambda sid, snap: restored.append(snap))

    def stopped(*a, **k):
        raise RunCancelledError("r1")
    monkeypatch.setattr(deck_handler, "run_workflow", stopped)
    deck_handler.events.clear("s1")
    control = run_supervisor.SupervisedRun(
        _run(pre_run_snapshot=json.dumps({"plan": {"title": "before"}})))

    out = deck_handler.run_deck_flow("s1", "oops", run_id="r1", control=control)

    assert out == {"status": "cancelled"}
    assert restored == [{"plan": {"title": "before"}}]
    assert finished == [(RunStatus.CANCELLED, {"error_code": RunErrorCodes.CANCELLED_BY_USER,
                                               "error_message": None, "deck_version": None})]
    types = [e["type"] for e in deck_handler.events.history("s1")]
    assert types[-2:] == ["workflow_cancelled", "done"]
    assert deck_handler.events.history("s1")[-1]["data"]["status"] == RunStatus.CANCELLED
    assert persisted == ["user"]                    # no assistant turn for a stopped run


def test_restore_snapshot_undoes_a_stopped_runs_writes(session_store):
    session_store.set_plan("s1", {"title": "before"}, PlanStatus.CONFIRMED)
    snap = session_store.run_snapshot("s1")
    session_store.set_plan("s1", {"title": "accidental"}, PlanStatus.CONFIRMED)
    session_store.save_draft("s1", {"slides": [{"slide_number": 1}]})

    session_store.restore_snapshot("s1", snap)

    plan, status = session_store.get_plan("s1")
    assert plan == {"title": "before"} and status == PlanStatus.CONFIRMED
    assert session_store.get_draft("s1") is None


# --- HTTP ---------------------------------------------------------------------------------
def test_stop_endpoint_reports_the_outcome(client, app_instance, claims_member, monkeypatch):
    app_instance.dependency_overrides[auth.require_auth] = lambda: claims_member
    monkeypatch.setattr(auth.session_registry_handler, "owns", lambda _uid, _sid: True)
    monkeypatch.setattr(run_supervisor, "request_stop",
                        lambda sid, rid, uid: (run_supervisor.StopOutcome.CANCELLING,
                                               _run(status=RunStatus.CANCELLING)))
    try:
        body = client.post("/decks/s1/stop", json={"run_id": "r1"}).json()
    finally:
        app_instance.dependency_overrides.pop(auth.require_auth, None)

    assert body["status"] == "cancelling" and body["run_id"] == "r1"
    assert body["run"]["status"] == RunStatus.CANCELLING
    assert "owner" not in body["run"] and "pre_run_snapshot" not in body["run"]

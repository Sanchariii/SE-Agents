"""Run lifecycle: admission at the HTTP edge, supervised execution, terminal ordering."""

import asyncio
import gc

import pytest

from app.config.constants import RunErrorCodes, RunStatus
from app.core.dependencies import auth
from app.core.handlers import deck_handler, run_supervisor
from app.core.handlers.workflow_run_handler import WORKER_ID
from app.core.models.workflow_run_model import WorkflowRun
from app.utilities.llm_errors import TransientGatewayError


def _run(**overrides) -> WorkflowRun:
    row = {"run_id": "r1", "session_id": "s1", "status": RunStatus.RUNNING,
           "owner": WORKER_ID}
    row.update(overrides)
    return WorkflowRun(**row)


@pytest.fixture
def flow(monkeypatch):
    """run_deck_flow with every durable side effect captured instead of performed."""
    log: list[tuple] = []
    monkeypatch.setattr(deck_handler.chat_message_handler, "record", lambda *a, **k: None)
    monkeypatch.setattr(deck_handler.session_store, "add_message", lambda *a, **k: None)
    monkeypatch.setattr(deck_handler.workflow_event_handler, "record_batch",
                        lambda sid, run_id, batch: log.append(("events", [e["type"] for e in batch])))
    monkeypatch.setattr(deck_handler.workflow_run_handler, "set_last_event_index",
                        lambda *a, **k: None)

    def finish(run_id, owner, status, **kwargs):
        delivered = [e["type"] for e in deck_handler.events.history("s1")]
        log.append(("finish", status, kwargs, "done" in delivered))
        return True
    monkeypatch.setattr(deck_handler.workflow_run_handler, "finish", finish)
    deck_handler.events.clear("s1")
    return log


def test_success_records_completed_before_done_is_delivered(flow, monkeypatch):
    monkeypatch.setattr(deck_handler, "run_workflow",
                        lambda sid, msg, refresh: {"reply": "ok", "deck_version": 3})
    control = run_supervisor.SupervisedRun(_run())

    deck_handler.run_deck_flow("s1", "make a deck", run_id="r1", control=control)

    finish = next(entry for entry in flow if entry[0] == "finish")
    assert finish[1] == RunStatus.COMPLETED and finish[2]["deck_version"] == 3
    assert finish[3] is False                      # done not yet delivered at finish time
    done = deck_handler.events.history("s1")[-1]
    assert done["type"] == "done" and done["data"]["status"] == RunStatus.COMPLETED


def test_failure_shows_a_safe_message_and_keeps_the_raw_error_server_side(flow, monkeypatch):
    def fail(*a, **k):
        raise TransientGatewayError("500 from gateway: route=foundry apim_request_id=x")
    monkeypatch.setattr(deck_handler, "run_workflow", fail)
    control = run_supervisor.SupervisedRun(_run())

    with pytest.raises(TransientGatewayError):
        deck_handler.run_deck_flow("s1", "make a deck", run_id="r1", control=control)

    done = deck_handler.events.history("s1")[-1]
    assert done["data"]["error_code"] == RunErrorCodes.LLM_GATEWAY_UNAVAILABLE
    assert done["data"]["retryable"] is True
    assert "500 from gateway" not in done["data"]["error"]
    finish = next(entry for entry in flow if entry[0] == "finish")
    assert finish[1] == RunStatus.FAILED
    assert "500 from gateway" in finish[2]["error_message"]


async def test_a_failed_run_task_never_reports_an_unretrieved_exception(monkeypatch):
    loop = asyncio.get_running_loop()
    seen: list[dict] = []
    loop.set_exception_handler(lambda _loop, context: seen.append(context))
    monkeypatch.setattr(run_supervisor.workflow_run_handler, "heartbeat",
                        lambda *a, **k: _run())

    def boom(*a, **k):
        raise RuntimeError("planner exploded")
    monkeypatch.setattr(deck_handler, "run_deck_flow", boom)

    run_supervisor.start(_run(run_id="r-task"), "hi", False)
    task = next(t for t in run_supervisor._tasks if t.get_name().endswith("r-task"))
    await asyncio.wait([task])
    await asyncio.sleep(0)
    del task
    gc.collect()

    assert seen == []
    assert run_supervisor.local_run("r-task") is None


# --- HTTP admission ----------------------------------------------------------------------
@pytest.fixture
def member_client(client, app_instance, claims_member, monkeypatch):
    app_instance.dependency_overrides[auth.require_auth] = lambda: claims_member
    monkeypatch.setattr(auth.session_registry_handler, "owns", lambda _uid, _sid: True)
    try:
        yield client
    finally:
        app_instance.dependency_overrides.pop(auth.require_auth, None)


@pytest.fixture
def admission(monkeypatch):
    from app.core.services import deck_service

    calls = {"start": [], "user_turns": []}
    monkeypatch.setattr(deck_service.session_store, "restore_if_missing", lambda sid: False)
    monkeypatch.setattr(deck_service.session_store, "add_message",
                        lambda sid, role, content, result, run_id:
                        calls["user_turns"].append((role, content, run_id)))
    monkeypatch.setattr(run_supervisor, "start",
                        lambda run, message, refresh: calls["start"].append(run.run_id))

    async def fake_stream(session_id, run_id, after=0, first=None):
        yield deck_service.run_stream.sse(first)
        yield deck_service.run_stream.sse({"type": "done", "run_id": run_id,
                                           "event_index": 2, "data": {}})
    monkeypatch.setattr(deck_service.run_stream, "run_stream", fake_stream)
    return calls


def test_an_attach_starts_no_work_and_records_no_second_user_turn(member_client, admission,
                                                                monkeypatch):
    monkeypatch.setattr(run_supervisor, "acquire",
                        lambda sid, uid, crid: (_run(run_id="original"),
                                                run_supervisor.AcquireOutcome.ALREADY_RUNNING))

    body = member_client.post("/decks/s1/invoke",
                              json={"message": "again", "client_request_id": "c2"}).text

    assert admission["start"] == [] and admission["user_turns"] == []
    assert '"run_started"' in body and '"already_running"' in body and "original" in body


def test_a_started_run_records_the_user_turn_and_starts_once(member_client, admission,
                                                           monkeypatch):
    monkeypatch.setattr(run_supervisor, "acquire",
                        lambda sid, uid, crid: (_run(run_id="fresh"),
                                                run_supervisor.AcquireOutcome.STARTED))

    member_client.post("/decks/s1/invoke", json={"message": "make a deck"})

    assert admission["start"] == ["fresh"]
    assert admission["user_turns"] == [("user", "make a deck", "fresh")]


def test_admission_fails_closed_when_the_run_registry_is_down(member_client, admission,
                                                             monkeypatch):
    def down(*a, **k):
        raise RuntimeError("postgres unreachable")
    monkeypatch.setattr(run_supervisor, "acquire", down)

    body = member_client.post("/decks/s1/invoke", json={"message": "hi"}).text

    assert RunErrorCodes.SERVICE_UNAVAILABLE in body
    assert admission["start"] == []

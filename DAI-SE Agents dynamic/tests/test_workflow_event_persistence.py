"""Durable workflow-event capture, storage, and messages timeline behavior."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.handlers.workflow_event_handler import WorkflowEventHandler
from app.core.models.chat_message_model import PersistedChatMessage
from app.core.models.document_model import ChatMessage
from app.core.models.workflow_event_model import PersistedWorkflowEvent
from app.utilities import events_util as events
from app.utilities.logger_util import ContextThreadPoolExecutor


class _FakePg:
    """Minimal PostgreSQL utility double that records statements and returns rows."""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.executed: list[tuple[str, dict | None]] = []
        self.batches: list[tuple[str, list[dict]]] = []
        self.queries: list[tuple[str, dict | None]] = []

    def execute(self, sql: str, params: dict | None = None) -> None:
        """Record one write statement."""
        self.executed.append((sql, params))

    def execute_many(self, sql: str, params: list[dict]) -> None:
        """Record one batched write statement."""
        self.batches.append((sql, params))

    def query(self, sql: str, params: dict | None = None) -> list[dict]:
        """Record one query and return configured rows."""
        self.queries.append((sql, params))
        return self.rows


def _event(run_id: str, index: int, ts: datetime,
           event_type: str = "agent_step") -> PersistedWorkflowEvent:
    """Build a typed event fixture."""
    return PersistedWorkflowEvent(
        id=index,
        session_id="s1",
        run_id=run_id,
        event_index=index,
        type=event_type,
        agent="planner",
        message=f"step {index}",
        data={"index": index},
        ts=ts,
    )


def test_run_capture_propagates_to_workers_and_delays_terminal_delivery():
    events.clear("s1")

    with events.capture_run("s1", "run-1") as capture:
        events.publish("s1", "agent_start", message="start")
        with ContextThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(
                lambda index: events.publish(
                    "s1", "agent_step", agent="slide", message=f"slide {index}"),
                range(6),
            ))
        terminal = events.publish("s1", "done", message="complete", deliver=False)

        captured = capture.snapshot()
        assert [event["event_index"] for event in captured] == list(range(1, 9))
        assert all(event["run_id"] == "run-1" for event in captured)
        assert events.history("s1")[-1]["type"] == "agent_step"

        events.dispatch("s1", terminal)

    assert events.history("s1")[-1]["type"] == "done"
    uncaptured = events.publish("s1", "ingest_step", message="outside workflow")
    assert "run_id" not in uncaptured and "event_index" not in uncaptured


def test_record_batch_uses_one_idempotent_transaction_payload():
    handler = WorkflowEventHandler()
    fake = _FakePg()
    handler._db = fake
    captured = [
        {
            "type": "agent_start",
            "agent": "supervisor",
            "message": "start",
            "data": {},
            "ts": "2026-09-29T10:00:00+00:00",
            "run_id": "run-1",
            "event_index": 1,
        },
        {
            "type": "done",
            "agent": "supervisor",
            "message": "complete",
            "data": {"result": {"deck_version": 2}},
            "ts": "2026-09-29T10:01:00+00:00",
            "run_id": "run-1",
            "event_index": 2,
        },
    ]

    handler.record_batch("s1", "run-1", captured)

    assert len(fake.batches) == 1
    sql, rows = fake.batches[0]
    assert "ON CONFLICT (run_id, event_index) DO NOTHING" in sql
    assert [row["event_index"] for row in rows] == [1, 2]
    assert rows[1]["data"] == '{"result": {"deck_version": 2}}'


def test_event_row_mapping_decodes_data_and_preserves_timestamps():
    ts = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)
    event = WorkflowEventHandler._to_event({
        "id": 7,
        "session_id": "s1",
        "run_id": "run-1",
        "event_index": 3,
        "event_type": "agent_step",
        "agent": "planner",
        "message": "planned",
        "data": '{"slides": 6}',
        "event_ts": ts,
        "created_at": ts,
    })

    assert event.type == "agent_step"
    assert event.data == {"slides": 6}
    assert event.ts == ts


def test_list_events_restricts_message_limited_reads_to_selected_runs():
    handler = WorkflowEventHandler()
    fake = _FakePg()
    handler._db = fake

    assert handler.list_events("s1", ["run-2", "run-2", "run-1"]) == []

    sql, params = fake.queries[0]
    assert "ANY(CAST(:run_ids AS TEXT[]))" in sql
    assert params == {"sid": "s1", "run_ids": ["run-2", "run-1"]}


def test_timeline_orders_each_run_as_user_events_then_assistant():
    start = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)
    messages = [
        PersistedChatMessage(
            id=1, session_id="s1", run_id="run-1", role="user",
            content="make a deck", created_at=start),
        PersistedChatMessage(
            id=2, session_id="s1", run_id="run-1", role="assistant",
            content="done", created_at=start + timedelta(minutes=2)),
    ]
    workflow_events = [
        _event("run-1", 2, start + timedelta(minutes=1), "done"),
        _event("run-1", 1, start + timedelta(seconds=10), "agent_start"),
    ]

    timeline = WorkflowEventHandler.build_timeline(messages, workflow_events)

    assert [item.kind for item in timeline] == [
        "message", "workflow_event", "workflow_event", "message"]
    assert [getattr(item, "event_index", None) for item in timeline] == [
        None, 1, 2, None]
    assert timeline[0].role == "user"
    assert timeline[-1].role == "assistant"


def test_timeline_keeps_legacy_and_event_only_runs_visible():
    start = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)
    messages = [ChatMessage(role="user", content="legacy", ts=start)]
    workflow_events = [_event("event-only", 1, start + timedelta(minutes=1))]

    timeline = WorkflowEventHandler.build_timeline(messages, workflow_events)

    assert [item.kind for item in timeline] == ["message", "workflow_event"]
    assert timeline[0].run_id is None
    assert timeline[1].run_id == "event-only"


def test_delete_all_for_session_returns_removed_count():
    handler = WorkflowEventHandler()
    fake = _FakePg(rows=[{"n": 4}])
    handler._db = fake

    assert handler.delete_all_for_session("s1") == 4
    assert "DELETE FROM workflow_events" in fake.executed[0][0]


@pytest.mark.asyncio
async def test_messages_keeps_legacy_array_and_adds_discriminated_timeline(monkeypatch):
    from app.core.services import deck_service

    start = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)
    history = [
        PersistedChatMessage(
            id=1, session_id="s1", run_id="run-1", role="user",
            content="make a deck", created_at=start),
        PersistedChatMessage(
            id=2, session_id="s1", run_id="run-1", role="assistant",
            content="ready", result={"deck_version": 1},
            created_at=start + timedelta(minutes=2)),
    ]
    workflow_events = [
        _event("run-1", 1, start + timedelta(seconds=5), "agent_start"),
        _event("run-1", 2, start + timedelta(minutes=1), "done"),
    ]
    monkeypatch.setattr(
        deck_service.chat_message_handler, "list_messages",
        lambda session_id, limit: history,
    )
    monkeypatch.setattr(
        deck_service.workflow_event_handler, "list_events",
        lambda session_id, run_ids: workflow_events,
    )

    response = await deck_service.messages("s1", None, {})

    assert "run_id" not in response["messages"][0]
    assert response["messages"][1]["result"] == {"deck_version": 1}
    assert [item["kind"] for item in response["timeline"]] == [
        "message", "workflow_event", "workflow_event", "message"]
    assert {item["run_id"] for item in response["timeline"]} == {"run-1"}


@pytest.mark.asyncio
async def test_messages_never_leaks_the_session_restore_snapshot(monkeypatch):
    """`chat_messages.result` also stores the Redis-restore snapshot. That is internal
    recovery state and must never reach the client or the timeline."""
    from app.config.constants import SessionMemoryConstants
    from app.core.services import deck_service

    start = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)
    history = [
        PersistedChatMessage(
            id=2, session_id="s1", run_id="run-1", role="assistant",
            content="ready",
            result={
                "deck_version": 1,
                SessionMemoryConstants.RESULT_KEY: {
                    "plan": {"title": "internal"},
                    "draft": {"slides": [{"title": "internal"}]},
                },
            },
            created_at=start),
    ]
    monkeypatch.setattr(
        deck_service.chat_message_handler, "list_messages",
        lambda session_id, limit: history,
    )
    monkeypatch.setattr(
        deck_service.workflow_event_handler, "list_events",
        lambda session_id, run_ids: [],
    )

    response = await deck_service.messages("s1", None, {})

    assert response["messages"][0]["result"] == {"deck_version": 1}
    assert SessionMemoryConstants.RESULT_KEY not in response["messages"][0]["result"]
    timeline_result = response["timeline"][0]["result"]
    assert SessionMemoryConstants.RESULT_KEY not in timeline_result


@pytest.mark.asyncio
async def test_message_limit_fetches_all_events_for_the_selected_turns(monkeypatch):
    from app.core.services import deck_service

    selected_at = datetime.now(timezone.utc)
    selected = [
        PersistedChatMessage(
            id=9, session_id="s1", run_id="run-5", role="assistant",
            content="ready", created_at=selected_at),
    ]
    observed: dict[str, object] = {}

    def list_messages(session_id: str, limit: int | None):
        observed["message_limit"] = limit
        return selected

    def list_events(session_id: str, run_ids: list[str] | None):
        observed["run_ids"] = run_ids
        return [_event(
            "run-5", 1,
            selected_at - timedelta(seconds=1),
            "done",
        )]

    monkeypatch.setattr(deck_service.chat_message_handler, "list_messages", list_messages)
    monkeypatch.setattr(deck_service.workflow_event_handler, "list_events", list_events)

    response = await deck_service.messages("s1", 1, {})

    assert observed == {"message_limit": 1, "run_ids": ["run-5"]}
    assert [item["kind"] for item in response["timeline"]] == [
        "workflow_event", "message"]
    assert response["timeline"][1]["role"] == "assistant"


@pytest.mark.asyncio
async def test_messages_uses_run_correlated_redis_fallback(monkeypatch):
    from app.core.services import deck_service

    start = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        deck_service.chat_message_handler, "list_messages",
        lambda session_id, limit: [],
    )
    monkeypatch.setattr(
        deck_service.session_store, "get_messages",
        lambda session_id, limit=None: [
            ChatMessage(run_id="run-1", role="user", content="cached", ts=start)
        ],
    )
    monkeypatch.setattr(
        deck_service.workflow_event_handler, "list_events",
        lambda session_id, run_ids: [_event("run-1", 1, start + timedelta(seconds=1))],
    )

    response = await deck_service.messages("s1", 2, {})

    assert response["messages"][0]["content"] == "cached"
    assert [item["kind"] for item in response["timeline"]] == [
        "message", "workflow_event"]


def test_session_deletion_cascades_to_workflow_events(monkeypatch):
    from app.core.handlers import document_handler
    from app.core.handlers.chat_message_handler import chat_message_handler
    from app.core.handlers.deck_version_handler import deck_version_handler
    from app.core.handlers.document_registry_handler import document_registry_handler
    from app.core.handlers.workflow_event_handler import workflow_event_handler
    from app.genai.retrieval import indexer

    monkeypatch.setattr(
        document_handler.session_store, "delete_session",
        lambda session_id: SimpleNamespace(decks=[object(), object()]),
    )
    monkeypatch.setattr(document_handler.events, "clear", lambda session_id: None)
    monkeypatch.setattr(document_handler.storage, "delete_prefix", lambda *a, **k: None)
    monkeypatch.setattr(
        deck_version_handler, "delete_all_for_session", lambda session_id: 2)
    monkeypatch.setattr(
        document_registry_handler, "delete_all_for_session", lambda session_id: 3)
    monkeypatch.setattr(
        chat_message_handler, "delete_all_for_session", lambda session_id: 4)
    monkeypatch.setattr(
        workflow_event_handler, "delete_all_for_session", lambda session_id: 12)
    from app.core.handlers.workflow_run_handler import workflow_run_handler
    runs_deleted: list[str] = []
    monkeypatch.setattr(
        workflow_run_handler, "delete_all_for_session", runs_deleted.append)
    monkeypatch.setattr(indexer, "search_configured", lambda: False)

    result = document_handler.delete_session("s1")

    assert runs_deleted == ["s1"]

    assert result["events_removed"] == 12
    assert result["messages_removed"] == 4

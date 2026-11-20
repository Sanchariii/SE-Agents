"""Durable chat persistence: user turns and agent final responses must be written to
Postgres (the system of record), not left only in the Redis/in-memory working copy.

The pure-mapping tests import just the handler; the flow tests import `deck_handler`
lazily (its module graph pulls the agent stack) and monkeypatch every dependency, so
they need no Postgres, Redis or network.
"""

from app.core.handlers.chat_message_handler import ChatMessageHandler


class _FakePg:
    """Small PostgreSQL double for chat schema and insert assertions."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, dict | None]] = []

    def execute(self, sql: str, params: dict | None = None) -> None:
        """Record a statement and its bound parameters."""
        self.executed.append((sql, params))


# --------------------------------------------------------------------------- #
# Row mapping                                                                  #
# --------------------------------------------------------------------------- #

def test_to_message_decodes_the_result_json_column():
    msg = ChatMessageHandler._to_message(
        {"id": 1, "session_id": "s1", "role": "assistant", "content": "done",
         "result": '{"deck_version": 2, "deck_updated": true}', "created_at": None})
    assert msg.result == {"deck_version": 2, "deck_updated": True}


def test_to_message_tolerates_a_malformed_result_column():
    msg = ChatMessageHandler._to_message(
        {"id": 1, "session_id": "s1", "role": "assistant", "content": "done",
         "result": "not-json", "created_at": None})
    assert msg.result is None


def test_to_message_leaves_a_user_turn_result_empty():
    msg = ChatMessageHandler._to_message(
        {"id": 1, "session_id": "s1", "role": "user", "content": "hi",
         "result": None, "created_at": None})
    assert msg.role == "user" and msg.result is None


def test_chat_schema_adds_run_correlation_without_recreating_existing_rows():
    handler = ChatMessageHandler()
    fake = _FakePg()
    handler._db = fake

    handler.ensure_table()

    statements = [sql for sql, _ in fake.executed]
    assert any("ADD COLUMN IF NOT EXISTS run_id TEXT" in sql for sql in statements)
    assert any("(session_id, run_id, id)" in sql for sql in statements)


def test_record_binds_the_workflow_run_id():
    handler = ChatMessageHandler()
    fake = _FakePg()
    handler._db = fake

    handler.record("s1", "user", "hello", run_id="run-1")

    sql, params = fake.executed[0]
    assert "(session_id, run_id, role, content, result)" in sql
    assert params and params["run_id"] == "run-1"


# --------------------------------------------------------------------------- #
# run_deck_flow persistence                                                    #
# --------------------------------------------------------------------------- #

def _stub_flow(monkeypatch, result: dict):
    """Neutralise every side effect of run_deck_flow except the durable write, and
    capture the chat turns and workflow-event batch it persists."""
    from app.core.handlers import deck_handler

    persisted: list[tuple] = []
    event_batches: list[tuple] = []
    monkeypatch.setattr(deck_handler.chat_message_handler, "record",
                        lambda sid, role, content, result=None, run_id=None:
                        persisted.append((sid, role, content, result, run_id)))
    monkeypatch.setattr(
        deck_handler.workflow_event_handler,
        "record_batch",
        lambda sid, run_id, captured:
        event_batches.append((sid, run_id, captured)),
    )
    monkeypatch.setattr(deck_handler, "run_workflow", lambda sid, msg, refresh: result)
    monkeypatch.setattr(deck_handler.session_store, "add_message", lambda *a, **k: None)
    deck_handler.events.clear("s1")
    return deck_handler, persisted, event_batches


def test_the_user_turn_and_the_final_response_are_persisted(monkeypatch):
    result = {"reply": "Your deck is ready.", "deck_version": 1, "deck_updated": True,
              "intent": "plan"}
    deck_handler, persisted, event_batches = _stub_flow(monkeypatch, result)

    out = deck_handler.run_deck_flow("s1", "make a deck", run_id="run-1")

    # The workflow's result, plus this turn's timing.
    assert {k: out[k] for k in result} == result
    assert {"started_at", "ended_at", "time_taken_minutes"} <= set(out)
    assert out["time_taken_minutes"] >= 0 and out["ended_at"] >= out["started_at"]
    # The user's message is stored durably, with no result metadata.
    assert ("s1", "user", "make a deck", None, "run-1") in persisted
    # The assistant reply is stored with the full final-response metadata attached.
    assert any(role == "assistant" and content == "Your deck is ready."
               and meta and meta.get("deck_version") == 1
               and run_id == "run-1"
               for (_, role, content, meta, run_id) in persisted)
    assert [event["type"] for event in event_batches[0][2]] == ["agent_start", "done"]
    assert [event["event_index"] for event in event_batches[0][2]] == [1, 2]


def test_a_chat_turn_without_a_deck_still_persists_the_reply(monkeypatch):
    deck_handler, persisted, _ = _stub_flow(
        monkeypatch, {"reply": "Hello!", "intent": "chat"})

    deck_handler.run_deck_flow("s1", "hi there")

    roles = [role for (_, role, _, _, _) in persisted]
    assert roles == ["user", "assistant"]


def test_an_empty_reply_still_persists_the_final_response_metadata(monkeypatch):
    deck_handler, persisted, _ = _stub_flow(monkeypatch, {"reply": ""})

    deck_handler.run_deck_flow("s1", "…")

    assert [role for (_, role, _, _, _) in persisted] == ["user", "assistant"]
    meta = persisted[-1][3]
    assert persisted[-1][2] == "" and meta["reply"] == ""
    # The Postgres row carries the session snapshot; the live result does not.
    assert "session_memory" in meta


def test_the_snapshot_is_left_out_when_it_cannot_be_captured(monkeypatch):
    deck_handler, persisted, _ = _stub_flow(monkeypatch, {"reply": "ok"})

    def boom(*a, **k):
        raise RuntimeError("redis down")

    monkeypatch.setattr(deck_handler.session_store, "snapshot", boom)

    out = deck_handler.run_deck_flow("s1", "hi")

    timing = ("started_at", "ended_at", "time_taken_minutes")
    assert {k: v for k, v in out.items() if k not in timing} == {"reply": "ok"}
    assert persisted[-1][3] == out and "session_memory" not in persisted[-1][3]


def test_a_postgres_outage_never_breaks_the_live_run(monkeypatch):
    """Persistence is best-effort: a DB failure is logged, not raised, so the user's
    run and its SSE stream still complete."""
    from app.core.handlers import deck_handler

    def boom(*a, **k):
        raise RuntimeError("postgres down")

    monkeypatch.setattr(deck_handler.chat_message_handler, "record", boom)
    monkeypatch.setattr(
        deck_handler.workflow_event_handler, "record_batch", lambda *a, **k: None)
    monkeypatch.setattr(deck_handler, "run_workflow", lambda *a, **k: {"reply": "ok"})
    monkeypatch.setattr(deck_handler.session_store, "add_message", lambda *a, **k: None)

    assert deck_handler.run_deck_flow("s1", "hi")["reply"] == "ok"


def test_a_failed_workflow_still_records_the_user_turn(monkeypatch):
    """The user asked something; that turn must survive even when the workflow raises."""
    from app.core.handlers import deck_handler

    persisted: list[tuple] = []
    event_batches: list[list[dict]] = []
    monkeypatch.setattr(deck_handler.chat_message_handler, "record",
                        lambda sid, role, content, result=None, run_id=None:
                        persisted.append((role, content)))
    monkeypatch.setattr(
        deck_handler.workflow_event_handler,
        "record_batch",
        lambda sid, run_id, captured: event_batches.append(captured),
    )

    def blow_up(*a, **k):
        raise RuntimeError("workflow failed")

    monkeypatch.setattr(deck_handler, "run_workflow", blow_up)

    try:
        deck_handler.run_deck_flow("s1", "make a deck", run_id="failed-run")
    except RuntimeError:
        pass

    assert ("user", "make a deck") in persisted
    assert [event["type"] for event in event_batches[0]] == [
        "agent_start", "agent_error", "done"]
    assert all(event["run_id"] == "failed-run" for event in event_batches[0])


def test_an_event_persistence_outage_never_breaks_the_live_run(monkeypatch):
    """A failed event batch write is logged while the workflow still emits done."""
    from app.core.handlers import deck_handler

    monkeypatch.setattr(deck_handler.chat_message_handler, "record", lambda *a, **k: None)
    monkeypatch.setattr(deck_handler.session_store, "add_message", lambda *a, **k: None)
    monkeypatch.setattr(deck_handler, "run_workflow", lambda *a, **k: {"reply": "ok"})

    def fail_batch(*args, **kwargs):
        raise RuntimeError("postgres down")

    monkeypatch.setattr(deck_handler.workflow_event_handler, "record_batch", fail_batch)
    deck_handler.events.clear("s1")

    result = deck_handler.run_deck_flow("s1", "hi", run_id="run-db-failure")

    assert result["reply"] == "ok"
    assert deck_handler.events.history("s1")[-1]["type"] == "done"


def test_terminal_event_is_persisted_before_it_is_delivered(monkeypatch):
    """A frontend may reload immediately on done, so the batch must already exist."""
    from app.core.handlers import deck_handler

    observed: dict[str, list[str]] = {}
    monkeypatch.setattr(deck_handler.chat_message_handler, "record", lambda *a, **k: None)
    monkeypatch.setattr(deck_handler.session_store, "add_message", lambda *a, **k: None)
    monkeypatch.setattr(deck_handler, "run_workflow", lambda *a, **k: {"reply": "ok"})
    deck_handler.events.clear("s1")

    def record_batch(session_id: str, run_id: str, captured: list[dict]) -> None:
        observed["captured"] = [event["type"] for event in captured]
        observed["delivered"] = [
            event["type"] for event in deck_handler.events.history(session_id)
        ]

    monkeypatch.setattr(
        deck_handler.workflow_event_handler, "record_batch", record_batch)

    deck_handler.run_deck_flow("s1", "hi", run_id="run-race")

    assert observed["captured"][-1] == "done"
    assert "done" not in observed["delivered"]
    assert deck_handler.events.history("s1")[-1]["type"] == "done"

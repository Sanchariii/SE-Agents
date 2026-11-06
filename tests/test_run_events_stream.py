"""Run-scoped SSE: isolation, replay after an index, live follow, terminal handling."""

import asyncio
import json

import pytest

from app.config.constants import RunErrorCodes, RunStatus
from app.core.handlers.workflow_run_handler import WORKER_ID
from app.core.models.workflow_run_model import WorkflowRun
from app.core.services import run_stream
from app.utilities import events_util as events


def _frames_to_events(frames: list[str]) -> list[dict]:
    out = []
    for frame in frames:
        for line in frame.splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


async def _collect(gen, limit: int = 50, timeout: float = 5.0) -> list[str]:
    frames = []
    for _ in range(limit):
        try:
            frames.append(await asyncio.wait_for(anext(gen), timeout))
        except StopAsyncIteration:
            break
    return frames


def _db_event(index: int, kind: str = "agent_step", run_id: str = "r1") -> dict:
    return {"type": kind, "agent": "slide", "message": f"step {index}", "data": {},
            "ts": "2026-10-01T00:00:00+00:00", "session_id": "s1", "run_id": run_id,
            "event_index": index}


@pytest.fixture
def bound_loop(monkeypatch):
    monkeypatch.setattr(events, "_loop", None)
    events.clear("s1")


def test_a_run_subscriber_never_receives_another_runs_events(bound_loop):
    async def scenario():
        queue = events.subscribe("s1", "r1")
        try:
            events.dispatch("s1", _db_event(1, run_id="r2"))
            events.dispatch("s1", {"type": "ingest_step", "run_id": None, "data": {}})
            events.dispatch("s1", _db_event(1, run_id="r1"))
            got = [queue.get_nowait() for _ in range(queue.qsize())]
        finally:
            events.unsubscribe("s1", queue)
        return got

    got = asyncio.run(scenario())
    assert [(e["type"], e.get("run_id")) for e in got] == [("ingest_step", None),
                                                          ("agent_step", "r1")]


def test_an_undelivered_done_is_not_replayed_before_it_is_dispatched(bound_loop):
    with events.capture_run("s1", "r1") as capture:
        events.publish("s1", "agent_step", message="a")
        terminal = events.publish("s1", "done", deliver=False)
        assert [e["type"] for e in events.live_events("r1", 0)] == ["agent_step"]
        events.dispatch("s1", terminal)
        assert [e["type"] for e in events.live_events("r1", 0)] == ["agent_step", "done"]
    assert capture.last_index == 2


async def test_replay_after_an_index_from_postgres_then_close_on_done(monkeypatch, bound_loop):
    stored = [_db_event(1), _db_event(2), _db_event(3), _db_event(4, "done")]
    monkeypatch.setattr(run_stream.workflow_event_handler, "list_after",
                        lambda run_id, after, limit: [e for e in stored
                                                      if e["event_index"] > after][:limit])

    frames = await _collect(run_stream.run_stream("s1", "r1", after=2))

    assert [f.split("\n")[0] for f in frames] == ["id: 3", "id: 4"]
    assert _frames_to_events(frames)[-1]["type"] == "done"


async def test_a_run_that_ended_without_done_gets_a_synthetic_terminal(monkeypatch, bound_loop):
    monkeypatch.setattr(run_stream.workflow_event_handler, "list_after",
                        lambda run_id, after, limit: [])
    monkeypatch.setattr(run_stream.workflow_run_handler, "get",
                        lambda run_id: WorkflowRun(run_id="r1", session_id="s1",
                                                   status=RunStatus.FAILED, owner=WORKER_ID,
                                                   error_code=RunErrorCodes.WORKER_LOST))

    frames = await _collect(run_stream.run_stream("s1", "r1", after=0))

    done = _frames_to_events(frames)[-1]
    assert done["type"] == "done" and done["data"]["synthetic"] is True
    assert done["data"]["error_code"] == RunErrorCodes.WORKER_LOST


async def test_a_local_run_streams_live_and_ignores_other_runs(bound_loop):
    with events.capture_run("s1", "r1"):
        events.publish("s1", "agent_step", message="before connect")
        gen = run_stream.run_stream("s1", "r1", after=0, first={"type": "run_started",
                                                                 "run_id": "r1"})
        first = await asyncio.wait_for(anext(gen), 5)
        replayed = await asyncio.wait_for(anext(gen), 5)
        events.dispatch("s1", _db_event(1, run_id="r-other"))
        events.publish("s1", "agent_step", message="live")
        live = await asyncio.wait_for(anext(gen), 5)
        terminal = events.publish("s1", "done", deliver=False)
        events.dispatch("s1", terminal)
        rest = await _collect(gen)

    got = _frames_to_events([first, replayed, live, *rest])
    assert [e["type"] for e in got] == ["run_started", "agent_step", "agent_step", "done"]
    assert all(e.get("run_id") == "r1" for e in got)
    assert [e.get("event_index") for e in got[1:]] == [1, 2, 3]


async def test_long_silence_sends_keepalive_comments(monkeypatch, bound_loop):
    monkeypatch.setattr(run_stream, "_LIVE_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(run_stream.EventConstants, "KEEPALIVE_SECONDS", 0.02)
    with events.capture_run("s1", "r1"):
        gen = run_stream.run_stream("s1", "r1", after=0)
        frame = await asyncio.wait_for(anext(gen), 5)
        await gen.aclose()

    assert frame == run_stream.KEEPALIVE_FRAME

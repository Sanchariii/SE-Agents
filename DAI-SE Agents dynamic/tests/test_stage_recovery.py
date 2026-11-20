"""Stage recovery: same run, bounded whole-stage retries, visible recovery events."""

import pytest

from app.config.constants import RunErrorCodes, RunStages
from app.genai.workflows import recovery
from app.utilities import events_util as events
from app.utilities import run_control
from app.utilities.llm_errors import ModelAuthError, StructuredOutputParseError


class _Control(run_control.RunControl):
    def __init__(self) -> None:
        super().__init__("r1", "s1", "owner")
        self.stages: list[str] = []
        self.recovering: list[tuple[bool, int]] = []

    def enter_stage(self, stage: str) -> None:
        self.stages.append(stage)
        super().enter_stage(stage)

    def set_recovering(self, recovering: bool, attempt: int) -> None:
        self.recovering.append((recovering, attempt))


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.setattr(events, "_loop", None)
    monkeypatch.setattr(recovery.run_control, "sleep", lambda seconds: None)
    events.clear("s1")
    return _Control()


def _run_node(control, stage, fn):
    node = recovery.stage_node(stage, fn)
    with events.capture_run("s1", "r1") as capture, run_control.bind(control):
        try:
            return node({"session_id": "s1"}), capture.snapshot()
        except BaseException as exc:                 # noqa: BLE001
            return exc, capture.snapshot()


def test_a_retryable_planning_failure_reruns_only_the_stage_in_the_same_run(harness):
    calls = []

    def planning(state):
        calls.append(state)
        if len(calls) == 1:
            raise StructuredOutputParseError("deck_plan: invalid JSON")
        return {"action": "plan"}

    result, captured = _run_node(harness, RunStages.PLANNING, planning)

    assert result == {"action": "plan"}
    assert len(calls) == 2
    kinds = [(e["type"], e["data"].get("attempt")) for e in captured]
    assert kinds == [("workflow_recovering", 2), ("workflow_recovered", 2)]
    assert all(e["run_id"] == "r1" for e in captured)
    assert captured[0]["data"]["reason_code"] == RunErrorCodes.LLM_OUTPUT_INVALID
    assert harness.recovering == [(True, 2), (False, 2)]
    assert harness.stages == [RunStages.PLANNING, RunStages.PLANNING]


def test_exhausted_stage_attempts_raise_the_typed_error(harness):
    def planning(state):
        raise StructuredOutputParseError("deck_plan: invalid JSON")

    error, _ = _run_node(harness, RunStages.PLANNING, planning)

    assert isinstance(error, StructuredOutputParseError)


def test_a_non_retryable_failure_is_not_retried(harness):
    calls = []

    def planning(state):
        calls.append(1)
        raise ModelAuthError("403 from gateway")

    error, captured = _run_node(harness, RunStages.PLANNING, planning)

    assert isinstance(error, ModelAuthError)
    assert calls == [1] and captured == []


def test_stages_outside_the_retryable_set_run_once(harness):
    calls = []

    def slides(state):
        calls.append(1)
        raise StructuredOutputParseError("x")

    error, _ = _run_node(harness, RunStages.SLIDES, slides)

    assert isinstance(error, StructuredOutputParseError) and calls == [1]


def test_low_level_retries_surface_as_one_recovery_episode(harness):
    def triage(state):
        run_control.note_retry(RunErrorCodes.LLM_GATEWAY_UNAVAILABLE, 1)
        run_control.note_retry(RunErrorCodes.LLM_GATEWAY_UNAVAILABLE, 2)
        run_control.note_recovered()
        return {}

    _, captured = _run_node(harness, RunStages.TRIAGE, triage)

    assert [e["type"] for e in captured] == ["workflow_recovering", "workflow_recovered"]


def test_a_stopped_run_does_not_enter_the_next_stage(harness):
    harness.request_cancel()

    error, _ = _run_node(harness, RunStages.COLLECT, lambda state: {})

    assert isinstance(error, run_control.RunCancelledError)

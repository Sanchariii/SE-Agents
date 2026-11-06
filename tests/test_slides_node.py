"""The slides node: work-order execution, ordering and failure isolation.

Concurrency is the point of this node, so these tests pin the two properties that make
fanning out safe: results come back in slide order however they finish, and one slide
blowing up never takes the deck with it.
"""

import threading
import time

import pytest

from app.config.constants import SlideAgentConstants
from app.core.models.deck_model import (
    ContentBlock, DeckPlan, SlideDraft, SlidePlan, SlideResult, SlideTask,
)
from app.genai.workflows import deck_workflow as wf


@pytest.fixture
def plan() -> DeckPlan:
    return DeckPlan(title="SMT Quality", audience="engineers",
                   slides=[SlidePlan(slide_number=n, title=f"Slide {n}",
                                     key_points=[f"point {n}"],
                                     layout_hint="Title + Bullets")
                           for n in range(1, 7)])


@pytest.fixture
def wired(monkeypatch, plan):
    """Bind the node to a stored plan, with no model anywhere."""
    monkeypatch.setattr(wf.session_store, "get_plan", lambda sid: (plan.model_dump(), "confirmed"))
    monkeypatch.setattr(wf.session_store, "get_draft", lambda sid: None)
    monkeypatch.setattr(wf.session_store, "get_content", lambda sid: None)
    monkeypatch.setattr(wf, "check_content", lambda *args: None)
    return plan


def _work_order(plan, op=SlideAgentConstants.OP_CREATE):
    return [SlideTask(slide_number=s.slide_number, op=op).model_dump()
            for s in plan.slides]


def _result(number):
    return SlideResult(
        slide_number=number,
        slide_draft=SlideDraft(slide_number=number, title=f"Slide {number}",
                               blocks=[ContentBlock(slot="body1", bullets=["x"])]))


def test_results_come_back_in_slide_order_however_they_finish(wired, monkeypatch):
    """Later slides finish first here; collect must still see 1..6 in order."""
    monkeypatch.setattr(SlideAgentConstants, "MAX_WORKERS", 6)

    def slow_in_reverse(session_id, task, planned, context, **kwargs):
        time.sleep(0.02 * task.slide_number)          # slide 1 finishes last
        return _result(task.slide_number)

    monkeypatch.setattr(wf, "run_ppt_agent", slow_in_reverse)
    out = wf._slides({"session_id": "s1", "work_order": _work_order(wired)})
    assert [r["slide_number"] for r in out["slide_results"]] == [1, 2, 3, 4, 5, 6]


def test_slides_actually_run_concurrently(wired, monkeypatch):
    monkeypatch.setattr(SlideAgentConstants, "MAX_WORKERS", 4)
    live, peak, lock = 0, 0, threading.Lock()

    def tracked(session_id, task, planned, context, **kwargs):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.05)
        with lock:
            live -= 1
        return _result(task.slide_number)

    monkeypatch.setattr(wf, "run_ppt_agent", tracked)
    wf._slides({"session_id": "s1", "work_order": _work_order(wired)})
    assert peak > 1


def test_one_worker_stays_sequential(wired, monkeypatch):
    monkeypatch.setattr(SlideAgentConstants, "MAX_WORKERS", 1)
    order = []

    def record(session_id, task, planned, context, **kwargs):
        order.append(task.slide_number)
        return _result(task.slide_number)

    monkeypatch.setattr(wf, "run_ppt_agent", record)
    wf._slides({"session_id": "s1", "work_order": _work_order(wired)})
    assert order == [1, 2, 3, 4, 5, 6]


def test_a_crashing_slide_does_not_take_the_deck_down(wired, monkeypatch):
    """The deck must still have every slide, with the failure recorded as a gap."""
    monkeypatch.setattr(SlideAgentConstants, "MAX_WORKERS", 4)

    def boom_on_three(session_id, task, planned, context, **kwargs):
        if task.slide_number == 3:
            raise RuntimeError("gateway exploded")
        return _result(task.slide_number)

    monkeypatch.setattr(wf, "run_ppt_agent", boom_on_three)
    out = wf._slides({"session_id": "s1", "work_order": _work_order(wired)})
    assert [r["slide_number"] for r in out["slide_results"]] == [1, 2, 3, 4, 5, 6]

    failed = next(r for r in out["slide_results"] if r["slide_number"] == 3)
    assert "slide build failed" in failed["notes"]
    assert failed["slide_draft"]["title"] == "Slide 3"      # rebuilt from the plan


def test_a_crashing_slide_prefers_its_stored_version(wired, monkeypatch):
    monkeypatch.setattr(SlideAgentConstants, "MAX_WORKERS", 1)
    stored = SlideDraft(slide_number=2, layout="Title + Bullets", title="Approved copy",
                        blocks=[ContentBlock(slot="body1", bullets=["kept"])])
    monkeypatch.setattr(wf.session_store, "get_draft",
                        lambda sid: {"title": "D", "slides": [stored.model_dump()]})

    def boom(session_id, task, planned, context, **kwargs):
        raise RuntimeError("nope")

    monkeypatch.setattr(wf, "run_ppt_agent", boom)
    out = wf._slides({"session_id": "s1", "work_order": _work_order(wired)})
    slide_two = next(r for r in out["slide_results"] if r["slide_number"] == 2)
    assert slide_two["slide_draft"]["title"] == "Approved copy"


def test_kept_slides_never_reach_the_content_agent(wired, monkeypatch):
    """A keep costs nothing: its copy is already stored, and its design is cached."""
    monkeypatch.setattr(SlideAgentConstants, "MAX_WORKERS", 6)
    seen = []
    monkeypatch.setattr(wf, "run_ppt_agent",
                        lambda *a, **k: (seen.append(a[1].slide_number),
                                         _result(a[1].slide_number))[1])
    order = _work_order(wired, op=SlideAgentConstants.OP_KEEP)
    out = wf._slides({"session_id": "s1", "work_order": order})
    assert seen == [] and out["slide_results"] == []


def test_no_plan_means_no_slides(monkeypatch):
    monkeypatch.setattr(wf.session_store, "get_plan", lambda sid: (None, "none"))
    assert wf._slides({"session_id": "s1"})["slide_results"] == []


def test_a_work_order_naming_an_unknown_slide_is_skipped(wired, monkeypatch):
    monkeypatch.setattr(wf, "run_ppt_agent",
                        lambda *a, **k: _result(a[1].slide_number))
    out = wf._slides({"session_id": "s1",
                      "work_order": [SlideTask(slide_number=99).model_dump()]})
    assert out["slide_results"] == []


def test_without_a_work_order_a_first_build_creates_everything(wired, monkeypatch):
    seen = []
    monkeypatch.setattr(wf, "run_ppt_agent",
                        lambda *a, **k: (seen.append(a[1].op), _result(a[1].slide_number))[1])
    wf._slides({"session_id": "s1"})
    assert set(seen) == {SlideAgentConstants.OP_CREATE}


def test_without_a_work_order_an_existing_deck_is_left_alone(wired, monkeypatch):
    """A classification-only turn must not rebuild content it already has."""
    stored = SlideDraft(slide_number=1, title="Kept")
    monkeypatch.setattr(wf.session_store, "get_draft",
                        lambda sid: {"title": "D", "slides": [stored.model_dump()]})
    seen = []
    monkeypatch.setattr(wf, "run_ppt_agent",
                        lambda *a, **k: (seen.append(a[1].op), _result(a[1].slide_number))[1])
    wf._slides({"session_id": "s1"})
    assert seen == []                       # every slide defaulted to keep


def test_feedback_reaches_actionable_slides_only(wired, monkeypatch):
    seen = {}
    monkeypatch.setattr(wf, "run_ppt_agent",
                        lambda *a, **k: (seen.update({a[1].slide_number: a[1].feedback}),
                                         _result(a[1].slide_number))[1])
    order = [SlideTask(slide_number=1, op=SlideAgentConstants.OP_KEEP).model_dump(),
             SlideTask(slide_number=2, op=SlideAgentConstants.OP_REWRITE).model_dump()]
    wf._slides({"session_id": "s1", "work_order": order, "feedback": ["fix the bullets"]})
    assert 1 not in seen and seen[2] == ["fix the bullets"]


def test_a_content_edit_does_not_rebuild_kept_agenda_and_takeaways(wired, monkeypatch):
    """The reported bug: editing quiz slides also rebuilt slides 2 and 13. Whether the
    agenda and takeaways need a rebuild is the work order's call, not this node's."""
    monkeypatch.setattr(wf, "run_ppt_agent", lambda *a, **k: _result(a[1].slide_number))
    roles = []
    monkeypatch.setattr(wf, "run_role_slide_agent",
                        lambda **k: (roles.append(k["task"].slide_number),
                                     _result(k["task"].slide_number))[1])
    order = [SlideTask(slide_number=1, kind="agenda",
                       op=SlideAgentConstants.OP_KEEP).model_dump(),
             SlideTask(slide_number=2, op=SlideAgentConstants.OP_REWRITE).model_dump(),
             SlideTask(slide_number=3, kind="takeaways",
                       op=SlideAgentConstants.OP_KEEP).model_dump()]
    out = wf._slides({"session_id": "s1", "work_order": order,
                      "removed_slides": [7]})
    assert roles == []
    assert [r["slide_number"] for r in out["slide_results"]] == [2]


def test_role_slides_the_work_order_refreshes_are_rebuilt(wired, monkeypatch):
    monkeypatch.setattr(wf, "run_ppt_agent", lambda *a, **k: _result(a[1].slide_number))
    roles = []
    monkeypatch.setattr(wf, "run_role_slide_agent",
                        lambda **k: (roles.append(k["task"].slide_number),
                                     _result(k["task"].slide_number))[1])
    order = [SlideTask(slide_number=1, kind="agenda",
                       op=SlideAgentConstants.OP_REWRITE).model_dump(),
             SlideTask(slide_number=2, op=SlideAgentConstants.OP_CREATE).model_dump()]
    wf._slides({"session_id": "s1", "work_order": order})
    assert roles == [1]


def test_a_slide_draft_no_longer_carries_hand_placed_geometry():
    """Bug 3: shapes/icons were declared but never populated. Geometry is decided in
    generated code now, so carrying empty lists through the model only invites a
    regression."""
    from app.core.models.deck_model import SlideDraft

    assert "shapes" not in SlideDraft.model_fields
    assert "icons" not in SlideDraft.model_fields
    assert "tables" in SlideDraft.model_fields      # write_content does populate this


def test_a_stored_draft_from_before_the_change_still_parses():
    """Saved sessions carry the old fields; extra keys must not break a reload."""
    from app.core.models.deck_model import SlideDraft

    slide = SlideDraft(slide_number=1, title="T",
                       shapes=[{"kind": "rectangle", "left": 1, "top": 1,
                                "width": 10, "height": 10}],
                       icons=[{"icon_ref": "x", "left": 1, "top": 1,
                               "width": 10, "height": 10}])
    assert slide.title == "T"
    assert not hasattr(slide, "shapes")


def test_the_code_brief_carries_the_deck_design_contract():
    from app.core.models.deck_model import DeckContext, SlideCodeBrief, SlideDraft

    brief = SlideCodeBrief(
        slide_number=1, content=SlideDraft(slide_number=1, title="T"),
        context=DeckContext(title="Deck"),
        mode="canvas", layout_key="M1-L10",
        grid={"columns": 12, "rows": 6, "gutter_in": 0.15},
        frame={"footer_band": [0, 7.05, 13.333, 0.45]},
        tokens={"red": "CD163F"}, free_region=[0.42, 1.05, 12.49, 6.0])
    assert brief.mode == "canvas" and brief.layout_key == "M1-L10"
    assert brief.free_region[3] == 6.0

    # The default is the conservative one: fill a real layout's placeholders.
    plain = SlideCodeBrief(slide_number=1, content=SlideDraft(slide_number=1, title="T"),
                           context=DeckContext(title="Deck"))
    assert plain.mode == "layout" and plain.free_region is None

"""Consistency fixes: "this slide / this deck looks inconsistent" is handled in code."""

import pytest

from app.config.constants import CreativityLevels, PlanStatus, SlideAgentConstants as S
from app.core.models.deck_model import DeckPlan, SlidePlan
from app.genai.agents.work_order import derive_work_order
from app.genai.tools import plan_tools
from app.genai.workflows import deck_workflow as wf


def _plan(*levels: str) -> DeckPlan:
    return DeckPlan(title="Deck", slides=[
        SlidePlan(slide_number=n, title=f"Slide {n}", creativity_level=level)
        for n, level in enumerate(levels, 1)])


@pytest.fixture
def store(session_store, monkeypatch):
    monkeypatch.setattr(plan_tools, "session_store", session_store)
    monkeypatch.setattr(plan_tools.planner, "status_after_edit",
                        lambda sid: PlanStatus.CONFIRMED)
    monkeypatch.setattr(plan_tools.events, "publish", lambda *a, **k: None)
    return session_store


def _stored(store, plan: DeckPlan) -> None:
    store.set_plan("s1", plan.model_dump(), status=PlanStatus.CONFIRMED)


def _load(store) -> DeckPlan:
    plan_dict, _ = store.get_plan("s1")
    return DeckPlan(**plan_dict)


def test_a_high_slide_in_a_high_deck_is_redesigned_to_match(store):
    _stored(store, _plan("", "", ""))
    out = plan_tools.fix_slide_consistency("s1", {2}, CreativityLevels.HIGH)
    slide = _load(store).slides[1]
    assert out["matched"] == [2] and slide.match_neighbours
    assert slide.creativity_level == "" and slide.design_revision == 1


def test_the_odd_high_slide_in_a_med_deck_is_demoted(store):
    _stored(store, _plan("", CreativityLevels.HIGH, ""))
    out = plan_tools.fix_slide_consistency("s1", {2}, CreativityLevels.MED)
    slide = _load(store).slides[1]
    assert out["demoted"] == [2] and slide.creativity_level == CreativityLevels.MED
    assert slide.design_revision == 1 and not slide.match_neighbours


def test_a_med_slide_is_redesigned_at_its_level(store):
    _stored(store, _plan(CreativityLevels.LOW, "", ""))
    plan_tools.fix_slide_consistency("s1", {1}, CreativityLevels.MED)
    slide = _load(store).slides[0]
    assert slide.creativity_level == CreativityLevels.LOW and slide.match_neighbours


def test_the_whole_deck_reset_clears_high_and_redesigns_everything(store):
    _stored(store, _plan(CreativityLevels.HIGH, CreativityLevels.LOW, ""))
    out = plan_tools.reset_deck_design("s1")
    plan = _load(store)
    assert out["cleared"] == [1]
    assert [s.creativity_level for s in plan.slides] == ["", CreativityLevels.LOW, ""]
    assert all(s.design_revision == 1 for s in plan.slides)


def test_a_redesign_is_a_relayout_not_a_rewrite():
    before = _plan("", "")
    after = before.model_copy(deep=True)
    after.slides[1].design_revision = 1
    ops = [t.op for t in derive_work_order(before, after)]
    assert ops == [S.OP_KEEP, S.OP_RELAYOUT]


def test_design_key_changes_only_with_a_revision():
    slide = SlidePlan(slide_number=1, title="T")
    params = {"slide_type": "NORMAL", "creativity_level": CreativityLevels.MED}
    assert wf._design_key(params, slide) == ""
    revised = slide.model_copy(update={"design_revision": 2})
    assert wf._design_key(params, revised).endswith("rev:2")

"""Quiz/FAQ family looks: a follow-up turn must be able to change how a family looks.

The failure this guards against: "change the quiz layout, it looks boring" was planned
as per-slide notes, but every quiz slide follows the family layout established on an
earlier turn, so the deck came back looking exactly the same.
"""

import pytest

from app.config.constants import PlannerConstants, QuizRoles, SlideTypes
from app.core.handlers.session_store import session_store
from app.core.models.deck_model import DeckPlan, SlidePlan
from app.genai.agents.planner_agent import _TOOL_INTENT
from app.genai.tools.plan_tools import apply_family_style
from app.genai.workflows import deck_workflow as wf


@pytest.fixture
def session(request) -> str:
    """A session carrying a built training plan with one quiz pair."""
    sid = f"family_{request.node.name}"
    plan = DeckPlan(title="Deck", audience="engineers", slides=[
        SlidePlan(slide_number=1, title="Architecture"),
        SlidePlan(slide_number=2, title="Quiz 1", slide_type=SlideTypes.QUIZ,
                  quiz_role=QuizRoles.QUESTION, quiz_id="q1"),
        SlidePlan(slide_number=3, title="Quiz 1", slide_type=SlideTypes.QUIZ,
                  quiz_role=QuizRoles.ANSWER, quiz_id="q1")])
    session_store.set_plan(sid, plan.model_dump(), status="confirmed")
    return sid


def _stored(sid: str) -> DeckPlan:
    plan_dict, _ = session_store.get_plan(sid)
    return DeckPlan(**plan_dict)


def test_a_quiz_look_is_stored_on_the_plan(session):
    result = apply_family_style(session, "quiz", "bold game-show style, 2x2 answer tiles")
    assert result == {"updated": True, "slide_type": "QUIZ", "slides": [2, 3]}
    assert _stored(session).family_styles == {"QUIZ": "bold game-show style, 2x2 answer tiles"}


def test_the_slides_are_untouched_by_a_look_change(session):
    before = _stored(session)
    apply_family_style(session, "QUIZ", "bold game-show style")
    assert _stored(session).slides == before.slides


def test_the_same_look_again_changes_nothing(session):
    apply_family_style(session, "QUIZ", "bold game-show style")
    result = apply_family_style(session, "QUIZ", "bold game-show style")
    assert result["updated"] is False and "already" in result["reason"]


def test_a_family_the_deck_does_not_have_is_refused(session):
    result = apply_family_style(session, "FAQ", "cards")
    assert result["updated"] is False and "no FAQ slides" in result["reason"]
    assert _stored(session).family_styles == {}


def test_a_normal_slide_type_is_refused(session):
    result = apply_family_style(session, "NORMAL", "cards")
    assert result["updated"] is False and "QUIZ" in result["reason"]


def test_an_empty_look_is_refused(session):
    assert apply_family_style(session, "QUIZ", "   ")["updated"] is False


def test_a_long_look_is_capped(session):
    apply_family_style(session, "QUIZ", "x" * (PlannerConstants.MAX_FAMILY_STYLE_CHARS + 50))
    stored = _stored(session).family_styles["QUIZ"]
    assert len(stored) == PlannerConstants.MAX_FAMILY_STYLE_CHARS


def test_no_plan_means_no_look_change():
    result = apply_family_style("family_session_without_a_plan", "QUIZ", "cards")
    assert result["updated"] is False and "no plan" in result["reason"]


def test_a_look_change_turn_builds_slides():
    """The tool's intent must route the turn to the slides node, not end it."""
    assert _TOOL_INTENT["set_family_style"] == "relayout"
    assert wf._after_planning({"actions_taken": ["relayout"],
                               "session_id": "s1"}) == "slides"


def test_the_new_look_reaches_the_designer():
    """A changed look must change the family's design key, or the cached slide is reused."""
    question = SlidePlan(slide_number=1, title="Quiz 1", slide_type=SlideTypes.QUIZ,
                         quiz_role=QuizRoles.QUESTION, quiz_id="q1")
    prefs = wf.DeckBrief()
    plain = DeckPlan(title="Deck", slides=[question])
    styled = DeckPlan(title="Deck", slides=[question],
                      family_styles={"QUIZ": "bold game-show style"})
    params = wf._generation_params(question, prefs, styled)
    assert params["deck_design_context"].quiz_layout.description == "bold game-show style"
    assert (wf._design_key(params, question)
            != wf._design_key(wf._generation_params(question, prefs, plain), question))

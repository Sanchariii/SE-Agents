"""Deck size: the `number_of_slides` question, the request limit and the plan ceiling."""

import pytest

from app.config.constants import PlanStatus, QuestionTypes, SlideCountConstants
from app.config.exceptions import AppError, ConflictError
from app.core.models.deck_model import (
    DeckPlan, SlidePlan, SlideRevisionOutput, UnderstandingOutput,
)
from app.core.models.question_model import (
    describe_answers, slide_range_bounds, validate_answers,
)
from app.genai.agents import plan_rules, planner
from app.genai.agents.brief import BriefUpdate, DeckBrief, QuestionBook, slide_bucket
from app.genai.agents.triage_agent import TriageDecision, TriageInstruction
from app.genai.tools import plan_tools
from app.genai.workflows import deck_workflow as wf

LIMIT = SlideCountConstants.MAX_REQUESTED
CEILING = SlideCountConstants.MAX_PLAN_SLIDES


def _plan(normal: int = 0, quiz: int = 0) -> DeckPlan:
    """A plan of `normal` NORMAL slides followed by `quiz` quiz questions (no answers)."""
    slides = [SlidePlan(slide_number=n, title=f"Slide {n}") for n in range(1, normal + 1)]
    slides += [SlidePlan(slide_number=normal + n, title=f"Quiz {n}", slide_type="QUIZ")
               for n in range(1, quiz + 1)]
    return DeckPlan(title="Deck", slides=slides)


def _use_brief(monkeypatch, **fields) -> None:
    monkeypatch.setattr(DeckBrief, "load", classmethod(lambda cls, sid: cls(**fields)))


# --- the brief -----------------------------------------------------------------

@pytest.mark.parametrize("count, bucket", [(3, "5-10"), (10, "5-10"), (11, "10-15"),
                                           (15, "10-15"), (20, "15-20"), (21, "20+"),
                                           (45, "20+")])
def test_a_slide_count_maps_onto_its_range(count, bucket):
    assert slide_bucket(count) == bucket


def test_ranges_carry_their_bounds_and_the_open_one_stops_at_the_agent_limit():
    assert slide_range_bounds("10-15") == (10, 15)
    assert slide_range_bounds("20+") == (20, SlideCountConstants.MAX_AGENT_DECIDED)
    assert slide_range_bounds("99-100") is None


def test_a_stated_count_answers_the_size_question():
    brief = DeckBrief().merge(BriefUpdate(slide_count=12))
    assert (brief.number_of_slides, brief.slide_count) == ("10-15", 12)
    assert "number_of_slides" not in brief.missing()
    assert "12 slides" in brief.as_prompt()


def test_a_hard_cap_also_answers_the_size_question():
    brief = DeckBrief().merge(BriefUpdate(max_slides=8))
    assert (brief.number_of_slides, brief.max_slides, brief.slide_count) == ("5-10", 8, 0)


def test_a_picked_range_replaces_an_earlier_count_outside_it():
    brief = DeckBrief().merge(BriefUpdate(slide_count=12)).apply_answers(
        {"number_of_slides": "20+"})
    assert (brief.number_of_slides, brief.slide_count) == ("20+", 0)
    kept = DeckBrief().merge(BriefUpdate(slide_count=12)).apply_answers(
        {"number_of_slides": "10-15"})
    assert kept.slide_count == 12


def test_you_decide_skips_the_size_question():
    assert DeckBrief().merge(BriefUpdate(use_defaults=True)).missing() == []


def test_an_unreadable_model_count_is_ignored():
    assert BriefUpdate(slide_count=None).slide_count == 0
    assert BriefUpdate(slide_count="12").slide_count == 12


def test_the_planner_is_told_the_size_rule():
    assert "at most 35 slides" in DeckBrief().slide_target()
    assert "between 15 and 20 slides" in DeckBrief(number_of_slides="15-20").slide_target()
    assert "20 to 35 slides" in DeckBrief(number_of_slides="20+").slide_target()
    assert f"never more than {LIMIT} planned" in DeckBrief().as_prompt()


def test_the_request_limit_counts_counts_and_caps():
    assert BriefUpdate(slide_count=LIMIT + 1).over_request_limit()
    assert BriefUpdate(max_slides=80).over_request_limit()
    assert not BriefUpdate(slide_count=LIMIT).over_request_limit()
    stripped = BriefUpdate(slide_count=100, goal="g").without_slide_count()
    assert (stripped.slide_count, stripped.goal) == (0, "g")


# --- answers and the slider ----------------------------------------------------

@pytest.mark.parametrize("raw, value", [("10-15", "10-15"), ("10 \u2013 15", "10-15"),
                                        ("20 plus", "20+"), ("20+ slides", "20+")])
def test_slide_answers_are_normalised(raw, value):
    assert validate_answers({"number_of_slides": raw}) == {"number_of_slides": value}


@pytest.mark.parametrize("raw", ["100", "LOW", "3-4"])
def test_an_unknown_slide_answer_is_a_400(raw):
    with pytest.raises(AppError):
        validate_answers({"number_of_slides": raw})


def test_a_slide_answer_reads_sensibly():
    assert describe_answers({"number_of_slides": "20+"}) == "Number of slides: 20+."


def test_the_size_question_is_a_four_stop_slider():
    brief = DeckBrief(goal="g", audience="a", deck_type="quiz", creativity_level="MED")
    [question] = QuestionBook().build(brief, [])
    assert (question.id, question.type, question.default) == (
        "number_of_slides", QuestionTypes.SCALE, "10-15")
    assert [o.value for o in question.options] == ["5-10", "10-15", "15-20", "20+"]
    done = brief.model_copy(update={"number_of_slides": "5-10"})
    assert QuestionBook().build(done, []) == []
    assert [q.id for q in QuestionBook().build(done, [], force=("number_of_slides",))] \
        == ["number_of_slides"]


# --- the triage refusal --------------------------------------------------------

@pytest.fixture
def triage(monkeypatch):
    """Run `_triage` with a scripted decision; returns (run, saved briefs, events)."""
    saved, published = [], []
    monkeypatch.setattr(wf, "apply_classification", lambda sid, message: None)
    monkeypatch.setattr(wf, "_step", lambda *args: None)
    monkeypatch.setattr(wf.DeckBrief, "save", lambda self, sid: saved.append(self))
    monkeypatch.setattr(wf.events, "publish", lambda *a, **k: published.append(k))

    def run(decision, plan=None, brief=None, answers=None, built=False,
            status=PlanStatus.CONFIRMED, message="m"):
        monkeypatch.setattr(wf.DeckBrief, "load",
                            classmethod(lambda cls, sid: brief or cls()))
        monkeypatch.setattr(wf.session_store, "get_plan", lambda sid: (
            (plan.model_dump(), status) if plan else (None, PlanStatus.NONE)))
        monkeypatch.setattr(wf.session_store, "list_decks",
                            lambda sid: [object()] if built else [])
        monkeypatch.setattr(wf, "triage_classify", lambda sid, message: decision)
        return wf._triage({"session_id": "s1", "message": message,
                           "answers": answers or {}, "answers_only": decision is None})

    return run, saved, published


def _over_limit(**update) -> TriageDecision:
    return TriageDecision(needs_planner=True, turn_type="request",
                          reply=update.pop("reply", ""),
                          brief_update=BriefUpdate(**update),
                          instructions=[TriageInstruction(intent="plan", text="m")])


def test_a_new_deck_over_the_limit_is_refused_with_the_size_slider(triage):
    run, saved, published = triage
    result = run(_over_limit(slide_count=100, goal="spot faults",
                             reply="Sorry, 100 slides is more than I can build."))
    assert result["needs_planner"] is False and result["route"] == "clarify"
    assert result["reply"].startswith("Sorry, 100 slides is more than I can build.")
    assert "number_of_slides" in [q["id"] for q in result["questions"]]
    assert published and published[-1]["questions"] == result["questions"]
    # The rest of the turn is kept; the refused size is not.
    assert saved[-1].goal == "spot faults" and saved[-1].slide_count == 0
    assert saved[-1].number_of_slides == ""


def test_a_cap_over_the_limit_on_an_existing_deck_is_refused_without_a_slider(triage):
    run, saved, _ = triage
    result = run(_over_limit(max_slides=80), plan=_plan(normal=5))
    assert result["route"] == "chat" and result["questions"] == []
    # The model wrote no reply, so the code fallback names the real numbers.
    assert "80" in result["reply"] and str(LIMIT) in result["reply"]
    assert saved[-1].max_slides == 0


def test_a_request_within_the_limit_carries_on(triage):
    run, saved, _ = triage
    brief = DeckBrief(goal="g", audience="a", deck_type="training", creativity_level="MED")
    result = run(_over_limit(slide_count=LIMIT), brief=brief)
    assert result["route"] == "extraction"
    assert saved[-1].slide_count == LIMIT and saved[-1].number_of_slides == "20+"


def test_a_size_answer_alone_is_not_a_style_turn(triage):
    run, _, _ = triage
    brief = DeckBrief(goal="g", audience="a", deck_type="training", creativity_level="MED")
    result = run(None, brief=brief, answers={"number_of_slides": "15-20"})
    assert result["route"] == "extraction"


# --- the hard plan ceiling -----------------------------------------------------

@pytest.fixture
def store(session_store, monkeypatch):
    for module in (plan_tools, plan_rules, planner):
        monkeypatch.setattr(module, "session_store", session_store)
    monkeypatch.setattr(plan_tools.events, "publish", lambda *a, **k: None)
    monkeypatch.setattr(planner, "status_after_edit", lambda sid: PlanStatus.CONFIRMED)
    return session_store


def _stored(store) -> DeckPlan:
    plan_dict, _ = store.get_plan("s1")
    return DeckPlan(**plan_dict) if plan_dict else None


def test_the_ceiling_counts_the_answer_slides_the_rules_add():
    plan = _plan(quiz=60)
    assert plan_rules.projected_total(plan, "slide") == 120
    message = plan_rules.ceiling_violation(plan, "slide")
    assert "120" in message and str(CEILING) in message
    assert plan_rules.ceiling_violation(plan, "notes") == ""


def test_inserting_past_the_ceiling_keeps_the_previous_plan(store, monkeypatch):
    _use_brief(monkeypatch, quiz_answer_placement="slide")
    store.set_plan("s1", _plan(normal=CEILING - 1).model_dump(),
                   status=PlanStatus.CONFIRMED)
    new = [SlidePlan(slide_number=0, title="New A"), SlidePlan(slide_number=0, title="New B")]
    result = plan_tools.insert_slides("s1", new)
    assert result["added"] == [] and str(CEILING) in result["reason"]
    assert len(_stored(store).slides) == CEILING - 1
    assert plan_tools.insert_slides("s1", new[:1])["added"]


def test_a_revision_that_adds_answer_slides_past_the_ceiling_is_rejected(store, monkeypatch):
    _use_brief(monkeypatch, quiz_answer_placement="slide")
    full, _ = plan_rules.normalize_plan(_plan(normal=2, quiz=49), "slide")
    assert len(full.slides) == CEILING
    store.set_plan("s1", full.model_dump(), status=PlanStatus.CONFIRMED)

    class _Client:
        def chat_json(self, *args, **kwargs):
            return SlideRevisionOutput(slides=[SlidePlan(
                slide_number=1, title="Slide 1", slide_type="QUIZ")])

    monkeypatch.setattr(planner, "get_apim_llm_client", lambda: _Client())
    with pytest.raises(ConflictError):
        planner.revise_slides("s1", "make slide 1 a quiz", {1})
    assert _stored(store).slides[0].slide_type == "NORMAL"


def test_an_oversized_new_plan_is_never_stored(store, monkeypatch):
    _use_brief(monkeypatch, quiz_answer_placement="slide")

    class _Client:
        def chat_json(self, *args, **kwargs):
            return UnderstandingOutput(action="plan", plan=_plan(normal=10, quiz=50))

    monkeypatch.setattr(planner, "get_apim_llm_client", lambda: _Client())
    output = planner.build_plan("s1", "a huge quiz")
    assert output.action == "chat" and "110" in output.reply
    assert _stored(store) is None


def test_the_plan_rules_never_store_a_plan_past_the_ceiling(store):
    store.set_plan("s1", _plan(quiz=51).model_dump(), status=PlanStatus.CONFIRMED)
    assert plan_rules.apply("s1", "slide") is False
    assert len(_stored(store).slides) == 51


def test_switching_answers_onto_slides_past_the_ceiling_keeps_the_notes(triage):
    run, saved, _ = triage
    decision = TriageDecision(
        needs_planner=True, turn_type="request",
        brief_update=BriefUpdate(quiz_answer_placement="slide"),
        instructions=[TriageInstruction(intent="style", text="answers on slides")])
    brief = DeckBrief(goal="g", audience="a", deck_type="quiz", creativity_level="MED",
                      number_of_slides="20+", quiz_answer_placement="notes")
    result = run(decision, plan=_plan(quiz=60), brief=brief)
    assert result["route"] == "chat" and str(CEILING) in result["reply"]
    assert saved[-1].answer_placement() == "notes"

# --- when the size question is (re)asked across turns ------------------------------

_DONE = dict(goal="g", audience="a", deck_type="training", creativity_level="MED")


def _turn(intent: str = "plan", size_intent: str = "", turn_type: str = "request",
          targets: list[int] | None = None, **update) -> TriageDecision:
    return TriageDecision(needs_planner=True, turn_type=turn_type, size_intent=size_intent,
                          brief_update=BriefUpdate(**update),
                          instructions=[TriageInstruction(intent=intent, text="m",
                                                          target_slides=targets or [])])


def test_an_unknown_size_intent_means_none():
    assert TriageDecision(size_intent="Resize").size_intent == "resize"
    assert TriageDecision(size_intent="shrink").size_intent == ""


def test_the_first_deck_asks_the_size(triage):
    run, _, published = triage
    result = run(_turn(size_intent="new_deck"), brief=DeckBrief(**_DONE))
    assert result["route"] == "clarify"
    assert [q["id"] for q in result["questions"]] == ["number_of_slides"]
    assert published[-1]["questions"] == result["questions"]


@pytest.mark.parametrize("intent, targets", [("edit", [2]), ("enrich", [3]),
                                             ("remove", [4]), ("plan", [])])
def test_slide_edits_on_a_built_deck_never_ask_the_size(triage, intent, targets):
    run, saved, _ = triage
    # An older session that never stored a size must not be asked it on an edit either.
    result = run(_turn(intent, targets=targets), plan=_plan(normal=8),
                 brief=DeckBrief(**_DONE), built=True)
    assert result["route"] == "extraction" and result["questions"] == []
    assert saved[-1].pending_request == ""


def test_adding_slides_with_a_number_updates_the_size_without_asking(triage):
    run, saved, _ = triage
    result = run(_turn("plan", slide_count=12), plan=_plan(normal=10),
                 brief=DeckBrief(**_DONE, number_of_slides="5-10", slide_count=10),
                 built=True)
    assert result["route"] == "extraction"
    assert (saved[-1].slide_count, saved[-1].number_of_slides) == (12, "10-15")


def test_feedback_on_a_proposed_plan_never_asks_the_size(triage):
    run, _, _ = triage
    result = run(_turn("plan"), plan=_plan(normal=8), status=PlanStatus.DRAFT,
                 brief=DeckBrief(**_DONE, number_of_slides="5-10"))
    assert result["route"] == "extraction"


def test_resizing_a_built_deck_asks_only_the_size_then_plans_the_request(triage):
    run, saved, _ = triage
    brief = DeckBrief(**_DONE, number_of_slides="15-20", slide_count=18)
    first = run(_turn("plan", size_intent="resize"), plan=_plan(normal=18), brief=brief,
                built=True, message="make the deck shorter")
    assert first["route"] == "clarify" and first["needs_planner"] is False
    assert [q["id"] for q in first["questions"]] == ["number_of_slides"]
    asked = saved[-1]
    assert (asked.number_of_slides, asked.slide_count) == ("", 0)
    assert asked.pending_request == "make the deck shorter"

    # The next turn answers the slider: the pending request is planned at that size.
    second = run(None, plan=_plan(normal=18), brief=asked, built=True,
                 answers={"number_of_slides": "5-10"})
    assert second["route"] == "extraction"
    assert "make the deck shorter" in second["hints"]
    assert saved[-1].number_of_slides == "5-10" and saved[-1].pending_request == ""


def test_resizing_to_a_stated_number_does_not_ask(triage):
    run, saved, _ = triage
    result = run(_turn("plan", size_intent="resize", slide_count=8), plan=_plan(normal=18),
                 brief=DeckBrief(**_DONE, number_of_slides="15-20"), built=True)
    assert result["route"] == "extraction"
    assert (saved[-1].slide_count, saved[-1].number_of_slides) == (8, "5-10")


def test_a_new_deck_in_the_same_session_asks_the_size_again(triage):
    run, saved, _ = triage
    result = run(_turn("plan", size_intent="new_deck"), plan=_plan(normal=10),
                 brief=DeckBrief(**_DONE, number_of_slides="10-15"), built=True,
                 message="now create a new deck on safety")
    assert result["route"] == "clarify"
    assert "number_of_slides" in [q["id"] for q in result["questions"]]
    assert saved[-1].pending_request == "now create a new deck on safety"


def test_resizing_a_proposed_plan_asks_the_size(triage):
    run, _, _ = triage
    result = run(_turn("plan", size_intent="resize"), plan=_plan(normal=8),
                 status=PlanStatus.DRAFT, brief=DeckBrief(**_DONE, number_of_slides="5-10"))
    assert result["route"] == "clarify"


def test_you_decide_with_a_new_deck_leaves_the_size_to_the_agent(triage):
    run, saved, _ = triage
    result = run(_turn("plan", size_intent="new_deck", use_defaults=True),
                 plan=_plan(normal=10), built=True,
                 brief=DeckBrief(**_DONE, number_of_slides="10-15"))
    assert result["route"] == "extraction"
    assert saved[-1].number_of_slides == "" and saved[-1].pending_request == ""


def test_an_answer_turn_never_reopens_the_size(triage):
    run, saved, _ = triage
    brief = DeckBrief(**_DONE, number_of_slides="10-15", slide_count=12)
    result = run(_turn("plan", size_intent="new_deck", turn_type="answer"),
                 brief=brief)
    assert result["route"] == "extraction" and saved[-1].slide_count == 12


def test_an_unanswered_size_question_is_dropped_by_a_new_request(triage):
    run, saved, _ = triage
    brief = DeckBrief(**_DONE, pending_request="make the deck shorter")
    result = run(_turn("edit", targets=[2]), plan=_plan(normal=8), brief=brief,
                 built=True)
    assert result["route"] == "extraction"
    assert "make the deck shorter" not in result["hints"]
    assert saved[-1].pending_request == ""


def test_a_new_deck_over_the_limit_in_a_built_session_shows_the_slider(triage):
    run, saved, _ = triage
    result = run(_turn("plan", size_intent="new_deck", slide_count=100),
                 plan=_plan(normal=10), built=True,
                 brief=DeckBrief(**_DONE, number_of_slides="10-15"))
    assert result["route"] == "clarify" and result["needs_planner"] is False
    assert "number_of_slides" in [q["id"] for q in result["questions"]]
    assert saved[-1].number_of_slides == ""


def test_adding_too_many_slides_to_a_built_deck_shows_no_slider(triage):
    run, _, _ = triage
    result = run(_turn("plan", slide_count=70), plan=_plan(normal=10), built=True,
                 brief=DeckBrief(**_DONE, number_of_slides="5-10"))
    assert result["route"] == "chat" and result["questions"] == []


def test_a_bigger_size_lifts_an_older_cap_and_a_fitting_one_keeps_it():
    capped = DeckBrief(max_slides=8, number_of_slides="5-10")
    assert capped.apply_answers({"number_of_slides": "15-20"}).max_slides == 0
    assert capped.apply_answers({"number_of_slides": "5-10"}).max_slides == 8


def test_classify_drops_a_size_intent_outside_a_request(monkeypatch):
    from app.genai.agents import triage_agent

    class _Client:
        def chat_json(self, *a, **k):
            return TriageDecision(turn_type="answer", needs_planner=True,
                                  size_intent="new_deck")

    monkeypatch.setattr(triage_agent, "get_apim_llm_client", lambda: _Client())
    monkeypatch.setattr(triage_agent.session_store, "get_plan", lambda sid: (None, ""))
    monkeypatch.setattr(triage_agent.session_store, "get_messages",
                        lambda sid, limit=12: [])
    monkeypatch.setattr(triage_agent.session_store, "list_decks", lambda sid: [])
    monkeypatch.setattr(triage_agent.session_store, "get_draft", lambda sid: None)
    monkeypatch.setattr(triage_agent.session_store, "latest_docs", lambda sid: [])
    monkeypatch.setattr(triage_agent.DeckBrief, "load",
                        classmethod(lambda cls, sid: cls()))
    assert triage_agent.classify("s1", "operators").size_intent == ""

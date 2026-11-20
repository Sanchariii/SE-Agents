"""Clarifying questions and plan approval: ask little, show the plan, build only when approved."""

from app.config.constants import PlanStatus
from app.core.models.deck_model import DeckPlan, SlidePlan
from app.genai.agents.brief import BriefUpdate, DeckBrief, PlanReview, QuestionBook
from app.genai.agents.triage_agent import TriageDecision, TriageInstruction
from app.genai.workflows import deck_workflow as wf


def _decision(**kw):
    kw.setdefault("needs_planner", True)
    kw.setdefault("instructions", [TriageInstruction(intent="plan", text="x")])
    return TriageDecision(**kw)


# --- the brief ---------------------------------------------------------------

def test_merge_keeps_earlier_answers():
    brief = DeckBrief().merge(BriefUpdate(audience="operators", deck_type="training"))
    brief = brief.merge(BriefUpdate(goal="spot faults"))
    assert (brief.audience, brief.deck_type, brief.goal) == ("operators", "training", "spot faults")


def test_unknown_deck_type_is_ignored():
    assert DeckBrief().merge(BriefUpdate(deck_type="poster")).deck_type == ""


def test_goal_audience_deck_type_and_style_levels_are_required():
    assert DeckBrief().missing() == ["goal", "audience", "deck_type", "creativity_level",
                                     "number_of_slides"]
    done = DeckBrief(goal="g", audience="a", deck_type="quiz", creativity_level="MED",
                     number_of_slides="10-15")
    assert done.missing() == []                    # intention and requirements stay optional


def test_a_retired_detailing_answer_is_accepted_and_ignored():
    from app.core.models.question_model import describe_answers, validate_answers

    answers = validate_answers({"detailing_level": "high"})
    assert answers == {"detailing_level": "HIGH"}     # never a 400 for an old client
    assert describe_answers(answers) == "Detailing: High."
    brief = DeckBrief(creativity_level="LOW").apply_answers(answers)
    assert brief.creativity_level == "LOW" and "detailing_level" not in brief.model_dump()


def test_an_old_brief_with_a_detailing_level_still_loads():
    assert DeckBrief(**{"goal": "g", "detailing_level": "HIGH"}).goal == "g"


def test_you_decide_skips_every_question():
    assert DeckBrief().merge(BriefUpdate(use_defaults=True)).missing() == []


def test_requirements_accumulate_without_duplicates():
    brief = DeckBrief().merge(BriefUpdate(requirements=["10 slides"]))
    brief = brief.merge(BriefUpdate(requirements=["10 slides", "friendly tone"]))
    assert brief.requirements == ["10 slides", "friendly tone"]


# --- source material -----------------------------------------------------------

def test_source_material_accumulates_without_duplicates_and_is_capped(monkeypatch):
    from app.genai.agents import brief as module

    monkeypatch.setattr(module.SourceMaterialConstants, "MAX_STORED_CHARS", 12)
    brief = DeckBrief().with_source_material("abc").with_source_material("abc")
    assert brief.source_material == "abc"
    assert len(brief.with_source_material("defghijklmnop").source_material) == 12


def test_needs_source_content_only_without_documents_or_material():
    assert DeckBrief().needs_source_content(has_documents=False)
    assert not DeckBrief().needs_source_content(has_documents=True)
    assert not DeckBrief(source_material="facts").needs_source_content(has_documents=False)


def test_the_brief_questions_never_include_a_content_question():
    questions = QuestionBook().build(DeckBrief(), [])
    assert "source_content" not in [q.id for q in questions]


def test_pasted_material_reaches_the_planner(monkeypatch):
    from app.genai.agents import planner

    monkeypatch.setattr(planner.session_store, "latest_docs", lambda sid: [])
    monkeypatch.setattr(planner.DeckBrief, "load", classmethod(
        lambda cls, sid: cls(source_material="Pods run containers.")))
    assert "Pods run containers." in planner.document_summaries("s1")


def test_pasted_material_reaches_the_slide_writer():
    from app.core.models.deck_model import DeckContext, SlideTask
    from app.genai.agents.ppt_agent import _task_brief

    text = _task_brief(SlideTask(slide_number=1), SlidePlan(slide_number=1, title="Pods"),
                       DeckContext(title="K8s"), False, "Pods run containers.")
    assert "USER-PROVIDED SOURCE MATERIAL" in text and "Pods run containers." in text


# --- routing -----------------------------------------------------------------

def test_new_request_with_gaps_asks_before_planning():
    assert wf._route(_decision(), DeckBrief(), PlanStatus.NONE, False) == "clarify"


def test_complete_brief_goes_straight_to_planning():
    brief = DeckBrief(goal="g", audience="a", deck_type="training",
                      creativity_level="MED", number_of_slides="10-15")
    assert wf._route(_decision(), brief, PlanStatus.NONE, False) == "extraction"


def test_feedback_on_a_proposed_plan_never_asks_again():
    assert wf._route(_decision(), DeckBrief(), PlanStatus.DRAFT, False) == "extraction"


def test_small_edit_on_a_built_deck_needs_no_questions():
    edit = _decision(instructions=[TriageInstruction(intent="edit", target_slides=[2], text="x")])
    assert wf._route(edit, DeckBrief(), PlanStatus.CONFIRMED, True) == "extraction"


def test_full_replan_on_a_built_deck_goes_back_through_approval():
    brief = DeckBrief(goal="g", audience="a", deck_type="training",
                      creativity_level="MED", number_of_slides="10-15")
    assert wf._route(_decision(), brief, PlanStatus.CONFIRMED, True) == "extraction"


def test_approve_and_off_topic_and_chat_routes():
    assert wf._route(_decision(turn_type="approve", needs_planner=False),
                     DeckBrief(), PlanStatus.DRAFT, False) == "approve"
    assert wf._route(_decision(needs_planner=False, on_topic=False),
                     DeckBrief(), PlanStatus.NONE, False) == "chat"


def test_triage_graph_routes_follow_route_key():
    for route in ("chat", "clarify", "approve", "extraction"):
        assert wf._after_triage({"route": route, "session_id": "s"}) == route


# --- replies -----------------------------------------------------------------

def test_clarify_reply_caps_written_questions_at_three_plus_the_style_sliders():
    decision = _decision(reply="A few questions:", questions=["1?", "2?", "3?", "4?"])
    brief = DeckBrief(goal="g", audience="a", deck_type="quiz")
    reply = wf._triage_reply(decision, brief, "clarify")
    # three written questions + creativity + number of slides
    assert reply.count("\n") == 5 and "4?" not in reply
    assert "Low / Medium / High" in reply
    assert "5-10 slides / 10-15 slides / 15-20 slides / 20+ slides" in reply


def test_clarify_falls_back_to_one_question_per_missing_item():
    brief = DeckBrief(goal="g", creativity_level="LOW", number_of_slides="5-10")
    reply = wf._triage_reply(_decision(reply="Questions:"), brief, "clarify")
    assert len(reply.splitlines()) == 3            # intro + audience + deck type


def test_off_topic_reply_declines_and_suggests():
    decision = _decision(needs_planner=False, on_topic=False, reply="Not covered.",
                         suggestions=["Gateway overview", "Auth quiz"])
    reply = wf._triage_reply(decision, DeckBrief(), "chat")
    assert reply.splitlines() == ["Not covered.", "- Gateway overview", "- Auth quiz"]


def test_plan_review_lists_slides_and_waits_for_approval():
    plan = DeckPlan(title="Gateway basics", audience="operators", objective="use the gateway",
                    slides=[SlidePlan(slide_number=1, title="Intro", key_points=["Why"])])
    text = PlanReview().format(plan, "English")
    assert "Gateway basics" in text and "1. Intro" in text and "- Why" in text
    assert "Nothing is built yet" in text and "approve" in text


# --- approval ----------------------------------------------------------------

def test_approve_confirms_the_plan_and_builds_every_slide(monkeypatch):
    plan = DeckPlan(title="D", slides=[SlidePlan(slide_number=1, title="A"),
                                        SlidePlan(slide_number=2, title="B")])
    saved = {}
    monkeypatch.setattr(wf.session_store, "get_plan",
                        lambda sid: (plan.model_dump(), PlanStatus.DRAFT))
    monkeypatch.setattr(wf.session_store, "set_plan",
                        lambda sid, p, status: saved.update(status=status))
    monkeypatch.setattr(wf, "_step", lambda *a: None)
    monkeypatch.setattr(wf, "check_content", lambda *args: None)

    out = wf._approve({"session_id": "s"})

    assert saved["status"] == PlanStatus.CONFIRMED
    assert [t["op"] for t in out["work_order"]] == ["create", "create"]


def test_proposed_plan_ends_the_turn_without_building():
    assert wf._after_planning({"action": "await_approval", "session_id": "s"}) == "await_approval"


def test_graph_has_the_approve_node():
    assert "approve" in set(wf.build_graph().get_graph().nodes)


# --- persistence: the conversation state must survive a Redis loss ---------------

_BRIEF = {"goal": "g", "audience": "a", "deck_type": "training"}


def test_snapshot_carries_the_brief(session_store):
    session_store.set_brief("s1", _BRIEF)
    assert session_store.snapshot("s1")["brief"] == _BRIEF


def test_brief_proposed_plan_and_history_are_rebuilt_after_a_redis_loss(session_store, monkeypatch):
    from app.core.handlers import session_store as module
    from app.core.models.chat_message_model import PersistedChatMessage

    memory = {"version": 1, "plan": {"title": "T"}, "plan_status": "draft", "brief": _BRIEF}
    monkeypatch.setattr(module.chat_message_handler, "latest_session_memory",
                        lambda *a, **k: memory)
    monkeypatch.setattr(module.chat_message_handler, "list_messages", lambda *a, **k: [
        PersistedChatMessage(session_id="s1", role="user", content="make a deck")])

    assert session_store.restore_if_missing("s1") is True

    assert session_store.get_brief("s1") == _BRIEF
    assert session_store.get_plan("s1") == ({"title": "T"}, "draft")
    assert [m.content for m in session_store.get_messages("s1")] == ["make a deck"]


def test_a_restored_proposed_plan_can_still_be_approved(session_store, monkeypatch):
    from app.genai.agents import triage_agent

    session_store.set_plan("s1", {"title": "T"}, status=PlanStatus.DRAFT)
    monkeypatch.setattr(triage_agent, "session_store", session_store)

    assert triage_agent.classify("s1", "/approve-plan").turn_type == "approve"


def test_a_stopped_run_puts_the_brief_back(session_store):
    session_store.set_brief("s1", _BRIEF)
    before = session_store.run_snapshot("s1")
    session_store.set_brief("s1", {**_BRIEF, "goal": "changed during the run"})

    session_store.restore_snapshot("s1", before)

    assert session_store.get_brief("s1") == _BRIEF


def test_deleting_a_session_clears_its_brief(session_store):
    session_store.set_brief("s1", _BRIEF)
    session_store.delete_session("s1")
    assert session_store.get_brief("s1") is None

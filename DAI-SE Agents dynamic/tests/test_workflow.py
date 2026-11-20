"""Workflow routing: the graph must pick the cheapest correct path for every outcome.

The front half of the graph (triage + planning) delegates to the agent-driven
triage/planner (see test_planner_agent.py for their own behavior); these tests cover
only the routing functions here, which are pure functions of the state dict they moved
to — `_after_planning` branches on what the planning agent actually did
(`actions_taken`), not a pre-guessed intent.

The back half is per-slide (`slides` -> `collect` -> `publish`); the whole-deck
research/write/critic path is gone.
"""

from types import SimpleNamespace

import pytest

from app.core.models.deck_model import SlideDraft
from app.core.models.document_model import DocStatus
from app.genai.agents import brief as brief_module
from app.genai.agents.brief import BriefUpdate
from app.genai.agents.triage_agent import TriageDecision, TriageInstruction
from app.genai.workflows import deck_workflow as wf


@pytest.fixture(autouse=True)
def allow_screening(monkeypatch):
    """Keep routing tests offline; policy enforcement has its own integration tests."""
    monkeypatch.setattr(wf, "check_content", lambda *args: None)


def test_chat_turn_ends_immediately():
    assert wf._after_triage({"route": "chat", "session_id": "s"}) == "chat"


def test_action_turn_goes_through_extraction_first():
    """Planning must never run against half-indexed sources."""
    assert wf._after_triage({"route": "extraction", "session_id": "s"}) == "extraction"


def test_classification_only_change_re_renders_directly(monkeypatch):
    """No content changed, so every slide is kept and only the template swaps."""
    monkeypatch.setattr(wf.session_store, "list_decks", lambda sid: [object()])
    assert wf._after_triage({"route": "chat", "session_id": "s",
                             "classification_changed": "confidential"}) == "slides"


def test_triage_node_calls_classifier_with_current_turn(monkeypatch):
    calls = []

    monkeypatch.setattr(wf, "apply_classification", lambda sid, message: None)
    monkeypatch.setattr(wf, "_step", lambda *args: None)
    monkeypatch.setattr(wf.DeckBrief, "load", classmethod(lambda cls, sid: cls(
        goal="g", audience="a", deck_type="training", creativity_level="MED",
        number_of_slides="10-15")))
    monkeypatch.setattr(wf.DeckBrief, "save", lambda self, sid: None)
    monkeypatch.setattr(wf.session_store, "get_plan", lambda sid: (None, "none"))
    monkeypatch.setattr(wf.session_store, "list_decks", lambda sid: [])

    def classify(session_id, message):
        calls.append((session_id, message))
        return TriageDecision(
            needs_planner=True,
            reply="",
            brief_update=BriefUpdate(content_provided=True),
            instructions=[TriageInstruction(intent="plan", text=message)],
        )

    monkeypatch.setattr(wf, "triage_classify", classify)

    result = wf._triage({"session_id": "s1", "message": "Build a deck"})

    assert calls == [("s1", "Build a deck")]
    assert result["needs_planner"] is True
    assert result["hints"] == "[plan] Build a deck"
    assert result["enrich_slides"] == []


def _new_deck_triage(monkeypatch, decision, docs=(), brief=None, answers=None):
    """Run the triage node for a brand-new deck with a stubbed classifier.

    Args:
        monkeypatch: pytest's monkeypatch fixture.
        decision: The TriageDecision the classifier returns.
        docs: The session's latest documents.
        brief: The stored brief (complete by default, so only content can block).
        answers: Structured answers sent with the turn, if any.

    Returns:
        (the node's result, the briefs it saved).
    """
    saved = []
    stored = brief or wf.DeckBrief(goal="g", audience="a", deck_type="training",
                                   creativity_level="MED")
    monkeypatch.setattr(wf, "apply_classification", lambda sid, message: None)
    monkeypatch.setattr(wf, "_step", lambda *args: None)
    monkeypatch.setattr(wf.DeckBrief, "load", classmethod(lambda cls, sid: stored))
    monkeypatch.setattr(wf.DeckBrief, "save", lambda self, sid: saved.append(self))
    monkeypatch.setattr(wf.session_store, "get_plan", lambda sid: (None, "none"))
    monkeypatch.setattr(wf.session_store, "list_decks", lambda sid: [])
    monkeypatch.setattr(brief_module.session_store, "latest_docs", lambda sid: list(docs))
    monkeypatch.setattr(wf, "triage_classify", lambda sid, message: decision)
    state = {"session_id": "s1", "message": "Create slides on Kubernetes"}
    if answers:
        state.update(answers=answers, answers_only=True, message="")
    return wf._triage(state), saved


def _plan_decision(**brief_update):
    return TriageDecision(needs_planner=True, turn_type="request",
                          brief_update=BriefUpdate(**brief_update),
                          instructions=[TriageInstruction(intent="plan", text="x")])


def test_a_bare_topic_with_no_documents_stops_and_asks_for_content(monkeypatch):
    result, _ = _new_deck_triage(monkeypatch, _plan_decision())

    assert result["route"] == "clarify" and result["needs_planner"] is False
    # One plain chat message, no question card.
    assert result["questions"] == []
    assert result["reply"] == brief_module.SOURCE_CONTENT_REQUEST


def test_you_decide_never_waives_the_content_request(monkeypatch):
    result, _ = _new_deck_triage(monkeypatch, _plan_decision(use_defaults=True),
                                 brief=wf.DeckBrief())

    assert result["route"] == "clarify"
    assert result["questions"] == []


def test_content_typed_in_the_message_is_stored_and_planning_goes_ahead(monkeypatch):
    result, saved = _new_deck_triage(monkeypatch, _plan_decision(content_provided=True))

    assert result["route"] == "extraction"
    assert saved[-1].source_material == "Create slides on Kubernetes"


def test_an_uploaded_document_lets_planning_go_ahead(monkeypatch):
    doc = SimpleNamespace(status=DocStatus.extracting)
    result, _ = _new_deck_triage(monkeypatch, _plan_decision(), docs=[doc])
    assert result["route"] == "extraction"


def test_a_failed_upload_is_not_source_material(monkeypatch):
    doc = SimpleNamespace(status=DocStatus.failed)
    result, _ = _new_deck_triage(monkeypatch, _plan_decision(), docs=[doc])
    assert result["route"] == "clarify"


def test_structured_answers_alone_do_not_unblock_planning(monkeypatch):
    result, saved = _new_deck_triage(
        monkeypatch, None, answers={"goal": "Understand Pods", "audience": "Students"},
        brief=wf.DeckBrief())

    assert result["route"] == "clarify" and result["questions"] == []
    assert saved[-1].goal == "Understand Pods" and not saved[-1].source_material


def test_an_existing_deck_is_never_blocked_on_content():
    edit = TriageDecision(needs_planner=True, instructions=[
        TriageInstruction(intent="edit", target_slides=[2], text="x")])
    assert wf._route(edit, wf.DeckBrief(), "confirmed", True) == "extraction"


def test_clarify_stops_before_any_building():
    assert wf._after_planning({"actions_taken": [], "session_id": "s1"}) == "clarify"


def test_every_content_action_builds_slides():
    for action in ("plan", "add", "edit_or_enrich", "relayout", "classification",
                   "language"):
        assert wf._after_planning({"actions_taken": [action],
                                   "session_id": "s1"}) == "slides"


def test_removal_without_a_rendered_draft_ends_the_turn(monkeypatch):
    monkeypatch.setattr(wf.session_store, "get_draft", lambda sid: None)
    assert wf._after_planning({"actions_taken": ["remove"],
                               "session_id": "s1"}) == "done"


def test_removal_with_a_rendered_draft_rebuilds_the_framing_slides(monkeypatch):
    """Removal changed no copy, but the agenda and takeaways depend on what is left."""
    monkeypatch.setattr(wf.session_store, "get_draft", lambda sid: {"slides": []})
    assert wf._after_planning({"actions_taken": ["remove"],
                               "session_id": "s1"}) == "slides"


def test_collect_publishes_only_when_something_was_assembled():
    assert wf._after_collect({"pending_publish": True}) == "publish"
    assert wf._after_collect({}) == "done"


def test_graph_compiles_with_every_node_wired():
    graph = wf.build_graph()
    nodes = set(graph.get_graph().nodes)
    assert {"triage", "extraction", "planning", "slides", "collect", "publish"} <= nodes


def test_the_whole_deck_path_is_gone():
    """Its nodes must not linger as unreachable branches."""
    nodes = set(wf.build_graph().get_graph().nodes)
    assert not {"research", "write", "critic"} & nodes


def test_duplicate_titles_are_reported():
    slides = [SlideDraft(slide_number=1, layout="L", title="Same"),
              SlideDraft(slide_number=2, layout="L", title="Same")]
    assert any("duplicate the same topic" in note for note in wf._deck_level_checks(slides))


def test_near_duplicate_titles_are_reported():
    """A real run produced 'Best Practices & Next Steps' twice, once spelled 'and'."""
    slides = [SlideDraft(slide_number=1, layout="L", title="Best Practices & Next Steps"),
              SlideDraft(slide_number=2, layout="L", title="Best Practices and Next Steps")]
    assert any("duplicate the same topic" in note for note in wf._deck_level_checks(slides))


def test_a_repeated_visual_steer_raises_no_note():
    """Design repetition is the designer's business now, not a deck-level fault."""
    slides = [SlideDraft(slide_number=i, layout="Title + Bullets", title=f"S{i}")
              for i in range(1, 5)]
    assert wf._deck_level_checks(slides) == []


def test_distinct_titles_raise_no_note():
    slides = [SlideDraft(slide_number=i, layout=f"Layout {i}", title=f"S{i}")
              for i in range(1, 5)]
    assert wf._deck_level_checks(slides) == []


def test_deck_level_checks_tolerate_an_empty_deck():
    assert wf._deck_level_checks([]) == []


def test_reply_falls_back_to_the_triage_text_for_chat_turns():
    assert wf._compose_reply("s1", {"actions_taken": [], "reply": "Hi!"}, []) == "Hi!"


def test_reply_for_a_clarify_turn_keeps_the_questions():
    final = {"action": "clarify", "reply": "Who is the audience?"}
    assert wf._compose_reply("s1", final, []) == "Who is the audience?"
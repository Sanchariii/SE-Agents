"""Manual test of the triage node in deck_workflow — no full graph, no planner dispatch.
Run from repo root:
    python -m scripts.manual_testing.test_worflow_triage
Comment out steps at the bottom to isolate just the one you're debugging.
"""

from app.core.handlers.session_store import session_store
from app.core.models.deck_model import DeckPlan, SlidePlan
from app.genai.agents.planner import apply_classification
from app.genai.agents.triage_agent import classify
from app.genai.workflows.deck_workflow import _triage, _after_triage

SESSION_ID_NO_DECK = "manual-triage-no-deck"
SESSION_ID_WITH_DECK = "manual-triage-with-deck"
MESSAGE = "update slide 3's numbers, remove slide 5, improve the layout of slide 2, and add a new slide 6 with a summary of the KPIs"


def seed_conversation(session_id: str):
    """Optional: give triage some history to reason against."""
    print(f"Seeding conversation for session {session_id!r}\n")
    session_store.add_message(session_id, "user", "Build a deck on SMT PPM KPIs")
    session_store.add_message(session_id, "assistant", "Here's your deck v1.")
    print(f"Seeded conversation for session {session_id!r}")
    print(f" messages : {session_store.get_messages(session_id)}\n")


def seed_plan(session_id: str):
    """A confirmed plan with 5 slides, so triage's has_deck guard keeps
    edit/enrich/remove instructions instead of collapsing them to 'plan'."""
    plan = DeckPlan(
        title="SMT PPM KPIs", audience="engineers", objective="review KPIs",
        slides=[SlidePlan(slide_number=n, title=f"Slide {n}") for n in range(1, 6)])
    session_store.set_plan(session_id, plan.model_dump(), status="confirmed")
    print(f"Seeded a 5-slide confirmed plan for session {session_id!r}")


def step1_apply_classification(session_id: str, message: str):
    print(f"STEP1 : Applying classification for message={message!r}\n")
    changed = apply_classification(session_id, message)
    print(f"apply_classification -> {changed!r}")
    return changed


def step2_classify(session_id: str, message: str):
    print(f"STEP2 : Classifying message={message!r}\n")
    decision = classify(session_id, message)
    print(f"classify -> {decision!r}")
    print(f"needs_planner={decision.needs_planner}")
    print(f"reply={decision.reply!r}")
    for i in decision.instructions:
        print(f"  instruction: intent={i.intent} target_slides={i.target_slides} "
              f"text={i.text!r}")
    return decision


def step3_run_node(session_id: str, message: str):
    print(f"STEP3 : Running triage node for message={message!r}\n")
    """Calls the actual LangGraph node function used in the real workflow."""
    state = {"session_id": session_id, "message": message}
    result = _triage(state)
    print("node output:")
    for k, v in result.items():
        print(f"  {k}: {v}")
    return result


def step4_routing(session_id: str, node_result: dict):
    next_node = _after_triage({**node_result, "session_id": session_id})
    print(f"_after_triage routes to -> {next_node!r}")
    return next_node




def run_scenario(session_id: str, label: str, with_deck: bool):
    print(f"\n{'=' * 70}\nSCENARIO: {label} (with_deck={with_deck})\n{'=' * 70}")
    seed_conversation(session_id)
    if with_deck:
        seed_plan(session_id)

    step1_apply_classification(session_id, MESSAGE)
    step2_classify(session_id, MESSAGE)
    result = step3_run_node(session_id, MESSAGE)
    step4_routing(session_id, result)



if __name__ == "__main__":
    run_scenario(SESSION_ID_NO_DECK, "no existing deck", with_deck=False)
    run_scenario(SESSION_ID_WITH_DECK, "existing 5-slide deck", with_deck=True)
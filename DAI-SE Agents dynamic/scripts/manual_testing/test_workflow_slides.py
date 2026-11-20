"""Manual test of the slides node in deck_workflow — no triage, no planning agent.
Run from repo root:
    python -m scripts.manual_testing.test_workflow_slides
Comment out steps at the bottom to isolate just the one you're debugging.
"""

from app.core.handlers.session_store import session_store
from app.core.models.deck_model import (
    ContentBundle, DeckContext, DeckPlan, SlideContent, SlidePlan, SlideTask,
)
from app.genai.agents.ppt_agent import run_ppt_agent
from app.genai.ppt.template import catalog as catalog_mod
from app.genai.workflows.deck_workflow import _slides, _deck_context

SESSION_ID_FIRST_BUILD = "manual-slides-first-build"
SESSION_ID_MIXED_OPS = "manual-slides-mixed-ops"


def seed_plan(session_id: str, n_slides: int = 3) -> DeckPlan:
    plan = DeckPlan(
        title="SMT PPM KPIs", audience="engineers", objective="review KPIs",
        slides=[SlidePlan(slide_number=n, title=f"Slide {n} title",
                          key_points=[f"point {n}.1", f"point {n}.2"],
                          content_needs=[f"topic for slide {n}"],
                          layout_hint="")
                for n in range(1, n_slides + 1)])
    session_store.set_plan(session_id, plan.model_dump(), status="confirmed")
    print(f"Seeded a {n_slides}-slide confirmed plan for session {session_id!r}")
    return plan


def seed_draft_and_content(session_id: str, plan: DeckPlan):
    """Fakes a previous turn's output, so op=keep/rewrite/enrich have something to
    reuse instead of only exercising op=create."""
    from app.core.models.deck_model import ContentBlock, DeckDraft, SlideDraft

    draft = DeckDraft(
        template="internal", title=plan.title, subtitle=plan.objective,
        takeaways=["Prior takeaway"],
        slides=[SlideDraft(slide_number=s.slide_number, layout="Title and body",
                           title=s.title,
                           blocks=[ContentBlock(slot="body1", bullets=s.key_points)])
                for s in plan.slides])
    session_store.save_draft(session_id, draft.model_dump(mode="json"))
    session_store.save_content(session_id, ContentBundle(
        plan=plan.model_dump(),
        slides=[SlideContent(slide_number=s.slide_number, title=s.title,
                             chunks=[], coverage="good") for s in plan.slides]
    ).model_dump(mode="json"))
    print(f"Seeded a prior draft + content bundle for session {session_id!r}")


def step1_run_node(session_id: str, work_order: list[SlideTask] | None = None):
    """Calls the actual LangGraph node function used in the real workflow."""
    print(f"STEP1 : Running slides node (work_order={'given' if work_order else 'none — node will synthesize its own'})\n")
    state = {"session_id": session_id,
            "work_order": [t.model_dump() for t in (work_order or [])]}
    result = _slides(state)
    print(f"node produced {len(result['slide_results'])} SlideResult(s):")
    for r in result["slide_results"]:
        print(f"  slide {r['slide_number']}: layout={r['slide_draft']['layout']!r} "
              f"coverage={r['coverage']} verdict={r['verdict']} "
              f"rounds_used={r['rounds_used']} notes={r['notes']!r}")
    return result


def step2_run_single_slide(session_id: str, plan: DeckPlan, task: SlideTask):
    """Calls run_ppt_agent directly for ONE slide — the finest-grained way to inspect
    the research -> get_layout_options -> write_slide -> critique_slide tool loop
    (or the keep/remove/relayout shortcuts) without the ThreadPoolExecutor fan-out."""
    print(f"STEP2 : run_ppt_agent directly for slide {task.slide_number} (op={task.op})\n")
    slide_plan = next(s for s in plan.slides if s.slide_number == task.slide_number)
    classification = "internal"
    catalog = catalog_mod.for_classification(classification)

    draft = session_store.get_draft(session_id)
    previous = None
    if draft:
        from app.core.models.deck_model import DeckDraft
        previous = next((s for s in DeckDraft(**draft).slides
                         if s.slide_number == task.slide_number), None)

    result = run_ppt_agent(
        session_id, task, slide_plan,
        _deck_context(plan, task.slide_number), previous=previous, chunks=[])
    print(f"SlideResult: layout={result.slide_draft.layout!r} title={result.slide_draft.title!r}")
    print(f"  blocks: {[b.model_dump() for b in result.slide_draft.blocks]}")
    print(f"  coverage={result.coverage} verdict={result.verdict} "
          f"rounds_used={result.rounds_used} notes={result.notes!r}")
    return result


def scenario_first_build():
    """No work_order, no prior draft: _slides should synthesize op=create for every
    slide (the 'first-ever build' branch)."""
    print(f"\n{'=' * 70}\nSCENARIO: first build, no work_order, no prior draft\n{'=' * 70}")
    plan = seed_plan(SESSION_ID_FIRST_BUILD, n_slides=2)
    step1_run_node(SESSION_ID_FIRST_BUILD, work_order=None)


def scenario_mixed_ops():
    """A prior draft exists; give the node an explicit work_order mixing keep,
    rewrite and create so all three execution paths run in one call."""
    print(f"\n{'=' * 70}\nSCENARIO: mixed ops (keep / rewrite / create) against a prior draft\n{'=' * 70}")
    plan = seed_plan(SESSION_ID_MIXED_OPS, n_slides=3)
    seed_draft_and_content(SESSION_ID_MIXED_OPS, plan)

    work_order = [
        SlideTask(slide_number=1, op="keep", reuse_chunks=True),
        SlideTask(slide_number=2, op="rewrite", instruction="make it punchier",
                  reuse_chunks=True),
        SlideTask(slide_number=3, op="create", reuse_chunks=False),
    ]
    step1_run_node(SESSION_ID_MIXED_OPS, work_order=work_order)

    # Same slide 2 rewrite, but through run_ppt_agent directly for a closer look.
    step2_run_single_slide(SESSION_ID_MIXED_OPS, plan, work_order[1])


if __name__ == "__main__":
    scenario_first_build()
    scenario_mixed_ops()
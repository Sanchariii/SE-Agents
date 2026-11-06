"""Manual test of the collect node in deck_workflow — no slides node, no publish.
Run from repo root:
    python -m scripts.manual_testing.test_workflow_collect
Comment out steps at the bottom to isolate just the one you're debugging.
"""

from app.core.handlers.session_store import session_store
from app.core.models.deck_model import (
    ContentBundle, DeckPlan, SlideContent, SlidePlan, SlideResult, SlideDraft,
    ContentBlock,
)
from app.genai.workflows.deck_workflow import _collect, _deck_level_checks

SESSION_ID_CLEAN = "manual-collect-clean"
SESSION_ID_GAPS = "manual-collect-gaps"


def seed_plan(session_id: str, n_slides: int = 3) -> DeckPlan:
    plan = DeckPlan(
        title="SMT PPM KPIs", audience="engineers", objective="review KPIs",
        slides=[SlidePlan(slide_number=n, title=f"Slide {n} title",
                          key_points=[f"point {n}.1"], layout_hint="")
                for n in range(1, n_slides + 1)])
    session_store.set_plan(session_id, plan.model_dump(), status="confirmed")
    print(f"Seeded a {n_slides}-slide confirmed plan for session {session_id!r}")
    return plan


def make_slide_results(plan: DeckPlan, duplicate_titles: bool = False,
                       repeat_layout: bool = False, overflow_text: bool = False
                       ) -> list[SlideResult]:
    """Builds SlideResult objects by hand — no LLM — so _collect's DETERMINISTIC checks
    can be exercised on demand instead of hoping a real writer run produces a fault."""
    results = []
    for s in plan.slides:
        title = plan.slides[0].title if duplicate_titles and s.slide_number > 1 else s.title
        layout = "Title and body" if repeat_layout else f"layout-{s.slide_number}"
        text = ("x" * 900) if overflow_text and s.slide_number == 1 else "normal body text"
        draft = SlideDraft(slide_number=s.slide_number, layout=layout, title=title,
                           blocks=[ContentBlock(slot="body1", bullets=[text])])
        results.append(SlideResult(slide_number=s.slide_number, slide_draft=draft,
                                   chunks=[], coverage="good", verdict="approve",
                                   rounds_used=1, notes=""))
    return results


def step1_deck_level_checks(results: list[SlideResult]):
    """The deterministic, no-LLM half of collect's checks: duplicate titles and
    runs of 3+ consecutive identical layouts."""
    slides = [r.slide_draft for r in results]
    print("STEP1 : _deck_level_checks (duplicate titles / repeated layouts)\n")
    notes = _deck_level_checks(slides)
    for n in notes:
        print(f"  - {n}")
    if not notes:
        print("  (no deck-level faults found)")
    return notes


def step2_gate_faults(results: list[SlideResult], plan: DeckPlan):
    """Renders the REAL .pptx per slide and runs the geometry gate over each one.

    Replaces the retired `_render_faults` / `audit.render_report` pair: overflow, an
    element over the footer band and a colour off the theme are all rules the gate
    decides now, from the file that was actually produced.
    """
    import tempfile
    from pathlib import Path

    from app.core.models.deck_model import DeckContext, SlideCodeBrief
    from app.genai.agents.ppt_creator_agent import PptCreatorAgent
    from app.genai.ppt import geometry as geo
    from app.genai.ppt.template import catalog as catalog_mod

    print("STEP2 : per-slide render -> geometry.check_slide\n")
    catalog = catalog_mod.load()
    faults: list[str] = []
    with tempfile.TemporaryDirectory(prefix="collect_") as tmp:
        workdir = Path(tmp)
        agent = PptCreatorAgent("manual-collect", workdir / "work")
        for result in results:
            brief = SlideCodeBrief(
                slide_number=result.slide_draft.slide_number,
                kind=result.slide_draft.kind, content=result.slide_draft,
                context=DeckContext(title=plan.title, position=1,
                                    total=len(results)),
                classification="internal")
            path = agent._render_on_layout(
                brief, workdir / f"s{result.slide_draft.slide_number}.pptx")
            from pptx import Presentation
            name = next(iter(Presentation(str(path)).slides)).slide_layout.name
            violations = geo.check_slide(path, catalog.resolve_layout(name),
                                         theme_colors=catalog.theme.colors)
            for violation in violations:
                faults.append(f"slide {brief.slide_number}: {violation.message}")
    for fault in faults:
        print(f"  - {fault}")
    if not faults:
        print("  (no gate faults found)")
    return faults


def step3_run_node(session_id: str, results: list[SlideResult]):
    """Calls the actual LangGraph node function used in the real workflow."""
    print(f"\nSTEP3 : Running collect node for session {session_id!r}\n")
    state = {"session_id": session_id,
            "slide_results": [r.model_dump(mode="json") for r in results]}
    result = _collect(state)
    print("node output:")
    print(f"  pending_publish: {result.get('pending_publish')}")
    print(f"  deck_gaps ({len(result.get('deck_gaps', []))}):")
    for g in result.get("deck_gaps", []):
        print(f"    - {g}")

    draft = session_store.get_draft(session_id)
    content = session_store.get_content(session_id)
    print(f"  stored draft slide count: {len(draft.get('slides', [])) if draft else 0}")
    print(f"  stored content gaps: {(content or {}).get('gaps')}")
    print(f"  stored content rounds_used: {(content or {}).get('rounds_used')}")
    return result


def scenario_clean_deck():
    """No duplicate titles, no repeated layouts, no overflow — collect should produce
    zero gaps and pending_publish=True."""
    print(f"\n{'=' * 70}\nSCENARIO: clean deck, no faults\n{'=' * 70}")
    plan = seed_plan(SESSION_ID_CLEAN, n_slides=3)
    results = make_slide_results(plan)
    step1_deck_level_checks(results)
    step2_gate_faults(results, plan)
    step3_run_node(SESSION_ID_CLEAN, results)


def scenario_deck_with_gaps():
    """Duplicate titles + 3 consecutive identical layouts + one overflowing slide —
    exercises every _deck_level_checks branch in one turn."""
    print(f"\n{'=' * 70}\nSCENARIO: deck with duplicate titles, repeated layout, overflow\n{'=' * 70}")
    plan = seed_plan(SESSION_ID_GAPS, n_slides=4)
    results = make_slide_results(plan, duplicate_titles=True, repeat_layout=True,
                                 overflow_text=True)
    step1_deck_level_checks(results)
    step2_gate_faults(results, plan)
    step3_run_node(SESSION_ID_GAPS, results)


if __name__ == "__main__":
    scenario_clean_deck()
    scenario_deck_with_gaps()
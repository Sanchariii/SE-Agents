"""Drive the PPT Agent over ONE slide of an existing session's plan.

Phase 1 check: the agent is not wired into the workflow yet, so this runs it directly
against a session that already has a plan and indexed sources.

    python scripts/manual_testing/05_slide_agent.py --session sess_123456 --slide 3
    python scripts/manual_testing/05_slide_agent.py --session sess_123456 --slide 3 \
        --op rewrite --instruction "tighten the bullets" --render slide3.pptx
"""

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.handlers.session_store import session_store
from app.core.models.deck_model import (
    ContentBundle, DeckContext, DeckDraft, DeckPlan, SlideTask,
)
from app.genai.agents.ppt_agent import run_ppt_agent
from app.genai.ppt.template import registry
from app.utilities.logger_util import get_logger, setup_logging

log = get_logger(__name__)


class C:
    OK, FAIL, INFO, DIM, RESET = ("\033[92m", "\033[91m", "\033[96m",
                                  "\033[90m", "\033[0m")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run the PPT agent over one slide.")
    p.add_argument("--session", required=True, help="Session id with a stored plan.")
    p.add_argument("--slide", type=int, required=True, help="slide_number to build.")
    p.add_argument("--op", default="create",
                   choices=["create", "enrich", "rewrite", "relayout", "keep"])
    p.add_argument("--instruction", default="", help="User instruction for this slide.")
    p.add_argument("--layout", default="", help="Layout name, for --op relayout.")
    p.add_argument("--render", type=Path, help="Also write a one-slide .pptx here.")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args()


def _context(plan: DeckPlan, position: int) -> DeckContext:
    """Deck-level context for the slide at 1-based `position`."""
    titles = [s.title for s in plan.slides]
    return DeckContext(
        title=plan.title, audience=plan.audience, objective=plan.objective,
        tone=plan.tone, position=position, total=len(plan.slides),
        previous_title=titles[position - 2] if position > 1 else "",
        next_title=titles[position] if position < len(titles) else "")


def _print_slide(slide) -> None:
    print(f"\n{C.OK}=== Slide {slide.slide_number}: {slide.title} ==={C.RESET}")
    print(f"{C.DIM}layout:{C.RESET} {slide.layout}")
    for block in slide.blocks:
        if block.image_ref:
            print(f"  [{block.slot}] {C.INFO}(image){C.RESET} {block.image_ref}")
            continue
        for item in (block.bullets or ([block.text] if block.text else [])):
            print(f"  [{block.slot}] {item}")
    print(f"{C.DIM}notes:{C.RESET} {slide.notes or '(none)'}")


def main() -> int:
    args = _parse_args()
    setup_logging(args.log_level)

    plan_dict, status = session_store.get_plan(args.session)
    if not plan_dict:
        print(f"{C.FAIL}No plan stored for session {args.session!r}.{C.RESET}",
              file=sys.stderr)
        return 1
    plan = DeckPlan(**plan_dict)
    print(f"{C.INFO}plan{C.RESET} '{plan.title}' ({status}) — {len(plan.slides)} slide(s)")

    slide_plan = next((s for s in plan.slides if s.slide_number == args.slide), None)
    if slide_plan is None:
        print(f"{C.FAIL}Slide {args.slide} is not in the plan "
              f"(have 1..{len(plan.slides)}).{C.RESET}", file=sys.stderr)
        return 1

    # Reuse whatever this slide already has, so rewrite/relayout behave as they will
    # inside the workflow.
    stored_content = session_store.get_content(args.session)
    chunks = []
    if stored_content:
        bundle = ContentBundle(**stored_content)
        chunks = next((s.chunks for s in bundle.slides
                       if s.slide_number == args.slide), [])
    stored_draft = session_store.get_draft(args.session)
    previous = next((s for s in DeckDraft(**stored_draft).slides
                     if s.slide_number == args.slide), None) if stored_draft else None

    classification = registry.normalize(plan.classification)
    task = SlideTask(slide_number=args.slide, op=args.op,
                     instruction=args.instruction, layout_hint=args.layout)
    print(f"{C.INFO}running{C.RESET} op={args.op} template={registry.label(classification)} "
          f"reused_chunks={len(chunks)} previous={'yes' if previous else 'no'}")

    result = run_ppt_agent(args.session, task, slide_plan,
                           _context(plan, args.slide), previous=previous, chunks=chunks)

    _print_slide(result.slide_draft)
    print(f"\n{C.INFO}verdict{C.RESET}={result.verdict}  "
          f"{C.INFO}coverage{C.RESET}={result.coverage}  "
          f"{C.INFO}critic_rounds{C.RESET}={result.rounds_used}  "
          f"{C.INFO}excerpts{C.RESET}={len(result.chunks)}")
    if result.notes:
        print(f"{C.FAIL}notes{C.RESET}: {result.notes}")

    if args.render:
        # Round-trips through the deterministic on-layout render, so slot and overflow
        # faults show up as they would in a published deck.
        from tempfile import TemporaryDirectory

        from app.core.models.deck_model import DeckContext, SlideCodeBrief
        from app.genai.agents.ppt_creator_agent import PptCreatorAgent

        brief = SlideCodeBrief(
            slide_number=result.slide_draft.slide_number,
            kind=result.slide_draft.kind, content=result.slide_draft,
            context=DeckContext(title=plan.title, objective=plan.objective,
                                audience=plan.audience, position=1, total=1),
            classification=classification)
        with TemporaryDirectory(prefix="slide_") as tmp:
            PptCreatorAgent("manual", Path(tmp))._render_on_layout(brief, args.render)
        print(f"{C.OK}rendered{C.RESET} {args.render} "
              f"({args.render.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
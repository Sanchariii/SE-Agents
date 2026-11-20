"""Manual test of the publish node in deck_workflow — no collect, no slides.
Run from repo root:
    python -m scripts.manual_testing.test_workflow_publish
Comment out steps at the bottom to isolate just the one you're debugging.
Requires Blob storage configured, or falls back to local storage (see storage_util.py).
"""

from pathlib import Path
import tempfile

from app.core.handlers.session_store import session_store
from app.core.models.deck_model import (
    ContentBlock, ContentBundle, DeckContext, DeckDraft, DeckPlan, SlideCodeBrief,
    SlideContent, SlideDraft, SlidePlan,
)
from app.genai.agents.ppt_creator_agent import PptCreatorAgent
from app.genai.ppt.builder import persist_deck
from app.genai.ppt.stitcher import DeckStitcher
from app.genai.workflows.deck_workflow import _publish

SESSION_ID_HAPPY = "manual-publish-happy"
SESSION_ID_MISSING = "manual-publish-missing-draft"


def seed_deck_file(session_id: str, workdir: Path) -> Path:
    """Stand in for _collect: stitch a deck from the stored draft, no model calls.

    Uses the agent's deterministic on-layout render, which is what a slide falls back
    to when its repair budget runs out, so the deck is on the real brand template.
    """
    draft = DeckDraft(**session_store.get_draft(session_id))
    agent = PptCreatorAgent(session_id, workdir / "work")
    files = []
    for slide in draft.slides:
        brief = SlideCodeBrief(slide_number=slide.slide_number, kind=slide.kind,
                               content=slide,
                               context=DeckContext(title=draft.title,
                                                   position=slide.slide_number,
                                                   total=len(draft.slides)))
        path = workdir / f"slide_{slide.slide_number}.pptx"
        files.append((slide.slide_number, agent._render_on_layout(brief, path)))
    return DeckStitcher().stitch(files, workdir / "deck.pptx")


def seed_draft_and_content(session_id: str, n_slides: int = 3):
    """A ready-to-publish draft + content bundle — what _collect would have already
    written to the store before _publish ever runs."""
    plan = DeckPlan(
        title="SMT PPM KPIs", audience="engineers", objective="review KPIs",
        slides=[SlidePlan(slide_number=n, title=f"Slide {n} title") for n in range(1, n_slides + 1)])
    session_store.set_plan(session_id, plan.model_dump(), status="confirmed")

    draft = DeckDraft(
        template="internal", title=plan.title, subtitle=plan.objective,
        takeaways=["Key takeaway one", "Key takeaway two"],
        slides=[SlideDraft(slide_number=n, layout="Title and body", title=f"Slide {n} title",
                           blocks=[ContentBlock(slot="body1", bullets=[f"point {n}"])])
                for n in range(1, n_slides + 1)])
    session_store.save_draft(session_id, draft.model_dump(mode="json"))
    session_store.save_content(session_id, ContentBundle(
        plan=plan.model_dump(),
        slides=[SlideContent(slide_number=n, title=f"Slide {n} title", chunks=[],
                             coverage="good") for n in range(1, n_slides + 1)]
    ).model_dump(mode="json"))
    print(f"Seeded a ready-to-publish draft + content for session {session_id!r}")


def step1_run_node(session_id: str):
    """Calls the actual LangGraph node function used in the real workflow."""
    print(f"STEP1 : Running publish node for session {session_id!r}\n")
    with tempfile.TemporaryDirectory(prefix="manual_publish_") as tmp:
        deck_path = seed_deck_file(session_id, Path(tmp))
        result = _publish({"session_id": session_id, "deck_path": str(deck_path)})
    print(f"node output: {result}")
    versions = session_store.list_decks(session_id)
    print(f"stored deck versions: {[v.deck_version for v in versions]}")
    return result


def step2_persist_deck_directly(session_id: str):
    """Calls persist_deck directly — the one thing _publish actually does — to see the
    DeckArtifact (storage_path, plan_snapshot) without going through node state."""
    draft = session_store.get_draft(session_id)
    content = session_store.get_content(session_id)
    plan_dict, _ = session_store.get_plan(session_id)
    bundle = ContentBundle(**{**content, "plan": plan_dict or content.get("plan")})
    with tempfile.TemporaryDirectory(prefix="manual_publish_") as tmp:
        deck_path = seed_deck_file(session_id, Path(tmp))
        artifact = persist_deck(session_id, DeckDraft(**draft), bundle, deck_path)
    print(f"STEP2 : persist_deck -> deck_version={artifact.deck_version} "
          f"storage_path={artifact.storage_path!r}")
    return artifact


def scenario_happy_path():
    """Draft + content exist: publish should write exactly one new version."""
    print(f"\n{'=' * 70}\nSCENARIO: happy path — draft and content both present\n{'=' * 70}")
    seed_draft_and_content(SESSION_ID_HAPPY)
    before = len(session_store.list_decks(SESSION_ID_HAPPY))
    step1_run_node(SESSION_ID_HAPPY)
    after = len(session_store.list_decks(SESSION_ID_HAPPY))
    print(f"versions before -> after: {before} -> {after} (expect +1)")
    step2_persist_deck_directly(SESSION_ID_HAPPY)


def scenario_missing_draft():
    """No draft/content seeded at all: _publish's guard should log a warning and return
    {} without publishing anything — this is the race-condition safety path."""
    print(f"\n{'=' * 70}\nSCENARIO: missing draft/content — guard path\n{'=' * 70}")
    result = _publish({"session_id": SESSION_ID_MISSING})
    print(f"node output: {result} (expect {{}} — nothing published)")
    versions = session_store.list_decks(SESSION_ID_MISSING)
    print(f"stored deck versions: {[v.deck_version for v in versions]} (expect [])")


if __name__ == "__main__":
    scenario_happy_path()
    scenario_missing_draft()
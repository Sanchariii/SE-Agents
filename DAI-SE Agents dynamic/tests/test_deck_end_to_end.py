"""The whole pipeline, with no model anywhere.

A fixed design contract plus hand-written DeckBuilder programs must produce a deck that
passes the geometry gate on every slide. If this cannot be made to pass, the design is
not buildable by a model either.
"""

import pytest
from pptx import Presentation

from app.core.models.deck_model import (
    ContentBlock, DeckDraft, DeckPlan, SlideDraft, SlidePlan,
)
from app.genai.ppt import deck_builder as db, design_contract as dc, geometry as geo
from app.genai.ppt.stitcher import DeckStitcher
from app.genai.ppt.template import catalog as catalog_mod, registry


@pytest.fixture(scope="module")
def cat():
    return catalog_mod.load()


@pytest.fixture
def plan():
    return DeckPlan(title="SMT Quality", audience="Plant managers",
                    classification="internal", slides=[
        SlidePlan(slide_number=1, kind="cover", title="SMT Quality"),
        SlidePlan(slide_number=2, kind="content", title="Reflow profile control"),
        SlidePlan(slide_number=3, kind="content", title="Yield by line",
                  visual="table"),
        SlidePlan(slide_number=4, kind="thanks", title="Thank you"),
    ])


@pytest.fixture
def draft(plan):
    return DeckDraft(title=plan.title, slides=[
        SlideDraft(slide_number=1, kind="cover", title="SMT Quality"),
        SlideDraft(slide_number=2, kind="content", title="Reflow profile control",
                   blocks=[ContentBlock(slot="body1", bullets=[
                       "Preheat ramp under 3 C/s",
                       "Soak 150-180 C for 90 s",
                       "Peak 245 C, 45 s above liquidus"])],
                   notes="the three numbers that matter"),
        SlideDraft(slide_number=3, kind="content", title="Yield by line"),
        SlideDraft(slide_number=4, kind="thanks", title="Thank you"),
    ])


@pytest.fixture
def contract(plan, draft, cat):
    # No model: the contract falls back to recommended layouts for every slide.
    return dc.build(plan, draft, catalog=cat, ask=lambda system, user: [])


def build_slide(cat, contract, number, slide, out_path):
    """Hand-written stand-in for what the model would produce."""
    layout = cat.get(contract.assignments[number].layout_key)
    fields = contract.brief_fields(number, layout)
    deck = db.DeckBuilder(registry.template_path(), cat.to_dict(),
                          frame=fields["frame"])
    handle = deck.add_slide(layout.key)
    # Mirrors PptCreatorAgent._render_on_layout: a layout with no placeholders is
    # fixed artwork and the brand guide says to add nothing to it.
    if layout.placeholders:
        handle.set_title(slide.title)
    lines = [item for block in slide.blocks
             for item in (block.bullets or ([block.text] if block.text else []))]
    if lines:
        target = next((p["slot"] for p in handle.layout.placeholders
                       if p.get("role") in ("body", "content")), None)
        if target:
            handle.set_bullets(target, lines)
        else:
            handle.add_textbox(lines, *handle.grid(0, 0, cols=12, rows=4), size=15)
    if slide.notes:
        handle.set_notes(slide.notes)
    return deck.save(out_path), layout, fields


def _stitch_all(cat, contract, draft, tmp_path):
    files = []
    for slide in draft.slides:
        path, _, _ = build_slide(cat, contract, slide.slide_number, slide,
                                 tmp_path / f"s{slide.slide_number}.pptx")
        files.append((slide.slide_number, path))
    return DeckStitcher().stitch(files, tmp_path / "deck.pptx")


def test_every_slide_of_a_hand_built_deck_passes_the_gate(cat, contract, draft,
                                                          tmp_path):
    for slide in draft.slides:
        path, layout, fields = build_slide(cat, contract, slide.slide_number, slide,
                                           tmp_path / f"s{slide.slide_number}.pptx")
        violations = geo.check_slide(path, layout, frame=fields["frame"],
                                     theme_colors=cat.theme.colors)
        assert geo.is_clean(violations), (
            f"slide {slide.slide_number} on {layout.key}:\n"
            + geo.format_violations(violations))


def test_the_stitched_deck_has_every_slide_on_its_own_layout(cat, contract, draft,
                                                             tmp_path):
    out = _stitch_all(cat, contract, draft, tmp_path)

    slides = list(Presentation(str(out)).slides)
    assert len(slides) == 4
    for number, slide in enumerate(slides, start=1):
        expected = cat.get(contract.assignments[number].layout_key)
        assert slide.slide_layout.name == expected.name


def test_the_stitched_deck_ships_no_prompt_text(cat, contract, draft, tmp_path):
    out = _stitch_all(cat, contract, draft, tmp_path)

    for slide in Presentation(str(out)).slides:
        text = " ".join(s.text_frame.text for s in slide.shapes if s.has_text_frame)
        assert "Click to edit" not in text
        assert "Second level" not in text


def test_every_content_slide_is_on_the_same_master(cat, contract, plan):
    content = [s.slide_number for s in plan.slides if s.kind == "content"]
    masters = {cat.get(contract.assignments[n].layout_key).master_index
               for n in content}
    assert masters == {cat.preferred_master}


def test_the_deck_carries_the_classification_on_every_slide(cat, contract, draft,
                                                            tmp_path):
    out = _stitch_all(cat, contract, draft, tmp_path)

    for slide in Presentation(str(out)).slides:
        text = " ".join(s.text_frame.text for s in slide.shapes if s.has_text_frame)
        assert "Internal" in text


def test_the_notes_survive_the_whole_pipeline(cat, contract, draft, tmp_path):
    out = _stitch_all(cat, contract, draft, tmp_path)
    slides = list(Presentation(str(out)).slides)
    assert slides[1].notes_slide.notes_text_frame.text == "the three numbers that matter"


def test_the_content_the_planner_wrote_is_all_on_the_slide(cat, contract, draft,
                                                           tmp_path):
    """Nothing is silently dropped between the draft and the deck."""
    out = _stitch_all(cat, contract, draft, tmp_path)
    slides = list(Presentation(str(out)).slides)
    text = " ".join(s.text_frame.text for s in slides[1].shapes if s.has_text_frame)
    for bullet in draft.slides[1].blocks[0].bullets:
        assert bullet in text

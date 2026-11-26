"""Section dividers like the reference's: planned, placed on its section layout, and
carrying its section-number artwork (`reference_design`, `reference_art`)."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pptx import Presentation
from pptx.util import Inches

from app.core.models.deck_model import DeckContext, SlidePlan, SlideTask
from app.genai.ppt import reference_design as rd
from app.genai.ppt.reference_art import SectionArt
from app.genai.ppt.reference_profile import ReferenceProfiler

PPTS = Path(__file__).resolve().parents[1] / "PPTs"
ILT = PPTS / "D2R ILT - Account Planning.pptx"


@pytest.fixture(scope="module")
def ilt_prepared(tmp_path_factory):
    if not ILT.is_file():
        pytest.skip("the D2R reference deck is not in this checkout")
    data = ILT.read_bytes()
    record = {"doc_id": "d", "version": 1, "filename": ILT.name,
              "sha256": hashlib.sha256(data).hexdigest(), "storage_path": "x"}
    prepared, note = rd.prepare("s", record, read_bytes=lambda _p: data,
                                cache_dir=tmp_path_factory.mktemp("cache"))
    assert note == ""
    return prepared


# --- planning ----------------------------------------------------------------------- #
def test_the_planner_is_told_about_sections_only_when_the_reference_has_them():
    sectioned = {"sequence": ["cover", "section", "content", "content", "section",
                              "content"], "cadence": {"section_every": 2}}
    text = rd.structure_text(sectioned)

    assert '"section" is ALSO allowed' in text and "2 topics" in text
    assert rd.structure_text({"sequence": ["cover", "content", "thanks"]}) == ""
    assert rd.structure_text({}) == ""


def test_planning_without_a_reference_adds_nothing(monkeypatch):
    from app.genai.agents import brief as brief_module

    monkeypatch.setattr(brief_module.DeckBrief, "load",
                        classmethod(lambda cls, _sid: cls()))
    assert rd.planning_block("no-reference") == ""


def test_the_ilt_reference_gives_the_planner_its_section_structure(ilt_prepared):
    text = rd.structure_text(ilt_prepared.profile)
    assert "6 topics" in text and "section" in text
    assert "Account" not in text and "Salesforce" not in text      # no reference words


# --- writing ------------------------------------------------------------------------ #
def test_a_section_slide_is_its_title_only_with_no_model_call(monkeypatch):
    from app.genai.agents import ppt_agent

    monkeypatch.setattr(ppt_agent, "get_apim_llm_client",
                        lambda: pytest.fail("no model call for a section slide"),
                        raising=False)
    plan = SlidePlan(slide_number=3, kind="section", title="Applying Locks")
    task = SlideTask(slide_number=3, kind="section", op="create")
    context = DeckContext(title="LOTO", audience="", objective="", total=8)

    result = ppt_agent.run_role_slide_agent("s", task, plan, context, [])

    assert result.slide_draft.kind == "section"
    assert result.slide_draft.title == "Applying Locks"
    assert result.slide_draft.blocks == []


# --- layout ------------------------------------------------------------------------- #
def test_section_slides_go_on_the_references_section_layout(ilt_prepared):
    from app.genai.ppt import design_contract as dc
    from app.genai.ppt.template import catalog as catalog_mod, registry

    slides = {1: SimpleNamespace(kind="cover"), 2: SimpleNamespace(kind="section"),
              3: SimpleNamespace(kind="content")}
    accepted = {n: dc.SlideAssignment(n, "layout", "M0-L1") for n in slides}
    with registry.using_template(ilt_prepared.path):
        catalog = catalog_mod.load(str(ilt_prepared.path))
        dc._reference_sections_on_their_layout(accepted, slides, catalog)
    section_layout = ReferenceProfiler.top_layout(ilt_prepared.path, "section_usage")

    assert accepted[2].layout_key == section_layout != ""
    assert accepted[1].layout_key == accepted[3].layout_key == "M0-L1"


def test_without_a_reference_the_art_directors_choice_stands():
    from app.genai.ppt import design_contract as dc
    from app.genai.ppt.template import catalog as catalog_mod

    slides = {2: SimpleNamespace(kind="section")}
    accepted = {2: dc.SlideAssignment(2, "layout", "M0-L1")}
    dc._reference_sections_on_their_layout(accepted, slides, catalog_mod.load())
    assert accepted[2].layout_key == "M0-L1"


# --- the section-number artwork ---------------------------------------------------- #
def test_the_ilt_section_artwork_is_kept_in_order_and_carries_no_text(ilt_prepared):
    art = json.loads(SectionArt.sidecar(ilt_prepared.path).read_text())

    assert len(art) == 6 and all(art)
    joined = "".join("".join(shapes) for shapes in art)
    assert "<a:t>" not in joined and "r:embed=" not in joined


def test_artwork_k_lands_on_the_kth_section_slide_found_by_title(ilt_prepared,
                                                                 tmp_path):
    key = ReferenceProfiler.top_layout(ilt_prepared.path, "section_usage")
    mi, li = (int(part[1:]) for part in key.split("-"))
    prs = Presentation(str(ilt_prepared.path))
    layout = prs.slide_masters[mi].slide_layouts[li]
    for title in ("Energy Basics", "Locks and Tags"):
        prs.slides.add_slide(layout).shapes.title.text = title
    deck = tmp_path / "deck.pptx"
    prs.save(str(deck))
    before = [len(s.shapes) for s in Presentation(str(deck)).slides]

    stamped = SectionArt().stamp(deck, ["Energy Basics", "Locks and Tags"],
                                 ilt_prepared.path)

    after = [len(s.shapes) for s in Presentation(str(deck)).slides]
    assert stamped == 2 and after[0] == before[0] + 1 and after[1] == before[1] + 1
    ids = [s.shape_id for s in Presentation(str(deck)).slides[0].shapes]
    assert len(ids) == len(set(ids))                         # fresh shape ids


def test_shapes_with_words_or_pictures_are_never_harvested(tmp_path):
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_shape(1, Inches(6), Inches(0), Inches(6), Inches(4))
    box.text_frame.text = "SECRET-SECTION-WORDS"
    slide.shapes.add_shape(9, Inches(0), Inches(0), Inches(5), Inches(5))   # oval
    path = tmp_path / "ref.pptx"
    prs.save(str(path))

    art = SectionArt().harvest(path, {"slides": [{"role": "section"}]})

    assert len(art) == 1 and len(art[0]) == 1                # only the plain oval
    assert "SECRET" not in art[0][0]

"""The structure profile of a reference deck (`app.genai.ppt.reference_profile`).

The profile tells the planner how the reference is built - slide roles, patterns,
density, cadence - and must never carry any of the reference's words.
"""
import hashlib
import json
from pathlib import Path

import pytest
from pptx import Presentation
from pptx.util import Inches

from app.genai.ppt import reference_design as rd
from app.genai.ppt.reference_profile import ReferenceProfiler

CANARY = "ZEBRA-CANARY-7731 confidential pricing"
PPTS = Path(__file__).resolve().parents[1] / "PPTs"


@pytest.fixture
def small_deck(tmp_path):
    """Cover, a section divider, two content slides (one with a table), a closing."""
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    layouts = prs.slide_layouts
    prs.slides.add_slide(layouts[0]).shapes.title.text = CANARY
    prs.slides.add_slide(layouts[2]).shapes.title.text = "Section one"
    body = prs.slides.add_slide(layouts[1])
    body.shapes.title.text = "Why it matters"
    body.placeholders[1].text_frame.text = CANARY + " in the body"
    table = prs.slides.add_slide(layouts[5])
    table.shapes.title.text = "Numbers"
    table.shapes.add_table(3, 3, Inches(1), Inches(2), Inches(8), Inches(3))
    prs.slides.add_slide(layouts[5]).shapes.title.text = "Thank you"
    path = tmp_path / "Small.pptx"
    prs.save(str(path))
    return path


def test_the_profile_lists_every_slide_with_its_role_pattern_and_density(small_deck):
    profile = ReferenceProfiler().profile(small_deck)

    assert profile["slide_count"] == 5
    assert profile["sequence"] == ["cover", "section", "content", "content", "closing"]
    assert profile["slides"][3]["pattern"] == "table"
    assert profile["slides"][2]["paragraphs"] == 1
    assert {s["density"] for s in profile["slides"]} <= {"light", "medium", "heavy"}
    assert sum(profile["layout_usage"].values()) == 5


def test_no_reference_text_reaches_the_profile(small_deck):
    text = json.dumps(ReferenceProfiler().profile(small_deck))

    assert "ZEBRA" not in text and "pricing" not in text and "Why it matters" not in text


def test_prepare_keeps_the_profile_beside_the_prepared_copy(small_deck, tmp_path):
    data = small_deck.read_bytes()
    record = {"doc_id": "d", "version": 1, "filename": "Small.pptx",
              "sha256": hashlib.sha256(data).hexdigest(), "storage_path": "x"}

    prepared, note = rd.prepare("s", record, read_bytes=lambda _p: data,
                                cache_dir=tmp_path / "cache")
    again, _ = rd.prepare("s", record, read_bytes=lambda _p: b"never read",
                          cache_dir=tmp_path / "cache")

    assert note == "" and prepared.profile["slide_count"] == 5
    assert again.profile == prepared.profile                 # served from the cache


def test_a_deck_that_cannot_be_profiled_still_gives_its_design(small_deck, tmp_path,
                                                               monkeypatch):
    def broken(_self, _path):
        raise RuntimeError("boom")
    monkeypatch.setattr(ReferenceProfiler, "profile", broken)
    data = small_deck.read_bytes()
    record = {"doc_id": "d", "version": 1, "filename": "Small.pptx",
              "sha256": hashlib.sha256(data).hexdigest(), "storage_path": "x"}

    prepared, note = rd.prepare("s", record, read_bytes=lambda _p: data,
                                cache_dir=tmp_path / "cache")

    assert note == "" and prepared is not None and prepared.profile == {}


def test_the_d2r_reference_deck_is_profiled_as_its_authors_built_it():
    path = PPTS / "D2R ILT - Account Planning.pptx"
    if not path.is_file():
        pytest.skip("the D2R reference deck is not in this checkout")
    profile = ReferenceProfiler().profile(path)
    roles = profile["sequence"]

    assert profile["slide_count"] == 45
    assert roles[0] == "cover" and roles[-1] == "closing"
    assert roles.count("section") == 6
    patterns = {s["pattern"] for s in profile["slides"]}
    assert {"columns-3", "screenshot", "table"} <= patterns
    assert profile["cadence"]["section_every"] > 0


def test_the_catalog_builds_on_the_master_the_references_content_slides_use(tmp_path):
    """Stripping the slides used to zero every usage count, so the catalog fell back to
    master 0 - for the Job Aid deck a plain default Office master - and the deck lost
    the reference's look. The profile's counts now pick the real content master."""
    from app.genai.ppt.template import catalog as catalog_mod

    path = PPTS / "D2R JOB AID TEMPLATE 1.pptx"
    if not path.is_file():
        pytest.skip("the D2R reference deck is not in this checkout")
    data = path.read_bytes()
    record = {"doc_id": "d", "version": 1, "filename": path.name,
              "sha256": hashlib.sha256(data).hexdigest(), "storage_path": "x"}
    prepared, note = rd.prepare("s", record, read_bytes=lambda _p: data,
                                cache_dir=tmp_path / "cache")

    catalog = catalog_mod.load(str(prepared.path))

    assert note == ""
    assert catalog.themes[catalog.preferred_master].name == "1_CONTENT LAYOUTS"
    assert max(l.usage_count for l in catalog.layouts) > 0


@pytest.fixture
def job_aid_reference(tmp_path):
    """The D2R Job Aid deck, prepared as a reference (slides stripped, profile kept)."""
    path = PPTS / "D2R JOB AID TEMPLATE 1.pptx"
    if not path.is_file():
        pytest.skip("the D2R reference deck is not in this checkout")
    data = path.read_bytes()
    record = {"doc_id": "d", "version": 1, "filename": path.name,
              "sha256": hashlib.sha256(data).hexdigest(), "storage_path": "x"}
    prepared, _ = rd.prepare("s", record, read_bytes=lambda _p: data,
                             cache_dir=tmp_path / "cache")
    return prepared.path


def _plan_and_draft():
    from app.core.models.deck_model import DeckDraft, DeckPlan, SlideDraft, SlidePlan

    plan = DeckPlan(title="LOTO", classification="internal", slides=[
        SlidePlan(slide_number=1, kind="cover", title="LOTO"),
        SlidePlan(slide_number=2, kind="content", title="Step 1")])
    draft = DeckDraft(title="LOTO", slides=[
        SlideDraft(slide_number=s.slide_number, kind=s.kind, title=s.title)
        for s in plan.slides])
    return plan, draft


def test_the_art_director_is_shown_the_layouts_the_reference_uses(job_aid_reference):
    from app.genai.ppt import design_contract as dc
    from app.genai.ppt.template import catalog as catalog_mod, registry

    plan, draft = _plan_and_draft()
    with registry.using_template(job_aid_reference):
        prompt = dc._describe_deck(plan, draft, catalog_mod.load(str(job_aid_reference)))
    brand = dc._describe_deck(plan, draft, catalog_mod.load())

    assert "LAYOUTS THE USER'S REFERENCE DECK USES" in prompt
    assert "'2 Column C'" in prompt and "'1 Column H'" in prompt
    assert "REFERENCE DECK USES" not in brand              # brand-template prompt unchanged


def test_a_layout_the_reference_used_is_allowed_off_the_preferred_master(
        job_aid_reference):
    from app.genai.ppt import design_contract as dc
    from app.genai.ppt.template import catalog as catalog_mod, registry

    plan, _ = _plan_and_draft()
    with registry.using_template(job_aid_reference):
        catalog = catalog_mod.load(str(job_aid_reference))
        used_elsewhere = next(l for l in dc._reference_layouts(catalog)
                              if l.master_index != catalog.preferred_master
                              and "content" in l.tags)
        parsed, problems = dc.validate_assignments(
            [{"slide_number": 2, "mode": "layout", "layout_key": used_elsewhere.key}],
            plan, catalog)

    assert 2 in parsed and not problems

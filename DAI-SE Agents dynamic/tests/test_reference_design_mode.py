"""Reference design mode: a deck built on the DESIGN of a PowerPoint the user uploaded.

The uploaded deck is claimed when the user asks for its design, stripped of every slide
(so none of its wording can travel), and the designer node builds the whole deck - the
design contract, every slide and the stitched deck - on its layouts instead of the
brand template.
"""
import hashlib
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from pptx import Presentation

from app.genai.ppt import reference_design as rd
from app.genai.ppt.template import catalog as catalog_mod, registry
from app.utilities.logger_util import ContextThreadPoolExecutor

SECRET = "SECRET REFERENCE WORDING"


@pytest.fixture
def reference_bytes(tmp_path):
    """A non-Rockwell deck with text on its slides."""
    prs = Presentation()
    prs.slides.add_slide(prs.slide_layouts[0]).shapes.title.text = SECRET
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "Quarterly targets"
    slide.placeholders[1].text_frame.text = "Grow revenue by 12%"
    path = tmp_path / "Reference.pptx"
    prs.save(str(path))
    return path.read_bytes()


def _record(data, name="Reference.pptx"):
    return {"doc_id": "doc-1", "version": 1, "filename": name,
            "sha256": hashlib.sha256(data).hexdigest(), "storage_path": "s/doc-1"}


def _prepare(data, tmp_path, record=None):
    return rd.prepare("session-1", record or _record(data), read_bytes=lambda _p: data,
                      cache_dir=tmp_path / "cache")


# --- asking for it ------------------------------------------------------------------ #
@pytest.mark.parametrize("message", [
    "Copy the design of the uploaded PPT and make a deck on lockout/tagout",
    "Use the same template as the attached presentation",
    "Create a LOTO training deck that matches the look and feel of my reference deck",
    "Build it based on the design of this ppt, but with my content",
])
def test_a_request_for_the_uploaded_design_is_recognised(message):
    assert rd.asks_to_copy_design(message)


@pytest.mark.parametrize("message", [
    "Make a deck from the attached PPT in a modern style",
    "Create a training deck on lockout/tagout",
    "Use the uploaded document as the source and add a quiz",
    "Copy slide 3 of the design document",
])
def test_ordinary_requests_are_not_design_requests(message):
    assert not rd.asks_to_copy_design(message)


def test_going_back_to_the_rockwell_template_is_recognised():
    assert rd.asks_to_drop_design("Stop using the reference design, please")
    assert rd.asks_to_drop_design("Go back to the Rockwell template")
    assert not rd.asks_to_drop_design("Copy the design of the uploaded PPT")


def test_the_reference_is_the_named_or_latest_uploaded_deck():
    def doc(name, at, status="ready", latest=True):
        return SimpleNamespace(filename=name, uploaded_at=at, version=1, status=status,
                               is_latest=latest)
    manual = doc("Manual.pdf", "2026-11-19T10:00")
    older = doc("Team deck.pptx", "2026-11-19T09:00")
    newer = doc("Roadmap.pptx", "2026-11-19T11:00")
    named = doc("Design template.pptx", "2026-11-19T08:00")
    broken = doc("Broken template.pptx", "2026-11-19T12:00", status="failed")
    assert rd.pick_reference([manual, older, newer]) is newer
    assert rd.pick_reference([manual, older, newer, named, broken]) is named
    assert rd.pick_reference([manual]) is None


# --- preparing it ------------------------------------------------------------------- #
def test_the_prepared_reference_keeps_its_layouts_and_drops_every_slide(
        reference_bytes, tmp_path):
    prepared, note = _prepare(reference_bytes, tmp_path)
    assert note == "" and prepared is not None and prepared.path.is_file()
    prs = Presentation(str(prepared.path))
    assert len(prs.slides) == 0
    assert len(prs.slide_layouts) == len(Presentation(
        __import__("io").BytesIO(reference_bytes)).slide_layouts)
    assert SECRET.encode() not in prepared.path.read_bytes()
    catalog = catalog_mod.load(str(prepared.path))
    assert SECRET not in str(catalog.to_dict())
    assert prepared.cache_key.startswith("|ref:")


def test_a_reference_that_cannot_be_used_falls_back_to_the_brand_template(tmp_path):
    prepared, note = _prepare(b"not a presentation", tmp_path)
    assert prepared is None and "Rockwell template instead" in note
    data = b"PK" + b"0" * 50
    prepared, note = _prepare(data, tmp_path, dict(_record(data), sha256="f" * 64))
    assert prepared is None and "does not match" in note
    assert rd.prepare("session-1", {}) == (None, "")


# --- building on it ----------------------------------------------------------------- #
def test_the_template_switch_reaches_worker_threads_and_ends_with_the_block(
        reference_bytes, tmp_path):
    prepared, _ = _prepare(reference_bytes, tmp_path)
    brand = registry.template_path()
    with registry.using_template(prepared.path):
        with ContextThreadPoolExecutor(max_workers=2) as pool:
            seen = set(pool.map(lambda _i: registry.template_path(), range(4)))
        assert catalog_mod.load().template_path == str(prepared.path)
    assert seen == {prepared.path}
    assert registry.template_path() == brand


def test_collect_designs_every_slide_on_the_reference_and_skips_polish(
        reference_bytes, tmp_path, monkeypatch):
    from app.core.models.deck_model import DeckDraft, DeckPlan, SlideDraft, SlidePlan
    from app.genai.workflows import deck_workflow as workflow

    prepared, _ = _prepare(reference_bytes, tmp_path)
    plan = DeckPlan(title="LOTO", slides=[SlidePlan(slide_number=1, title="Overview")])
    draft = DeckDraft(title="LOTO", slides=[SlideDraft(slide_number=1, title="Overview")])
    store = SimpleNamespace(
        get_plan=lambda _sid: (plan.model_dump(), ""),
        get_draft=lambda _sid: draft.model_dump(),
        get_content=lambda _sid: None,
        get_active_skill=lambda _sid: None,
        save_draft=lambda *_args: None,
        save_content=lambda *_args: None)
    seen, steps = {}, []

    def contract(*_args, skill_guidance="", **_kwargs):
        seen["contract_template"] = registry.template_path()
        seen["guidance"] = skill_guidance
        return SimpleNamespace(master_index=0, assignments={})

    def design(_sid, _draft, _plan, workdir, _contract, _enrichment, reference):
        seen["design_template"] = registry.template_path()
        seen["reference"] = reference
        return [(1, Path(workdir) / "slide_1.pptx")], {}, []

    class Stitcher:
        notes = []

        def __init__(self, _sid):
            pass

        def stitch(self, _files, out):
            seen["stitch_template"] = registry.template_path()
            return out

    def polish(*_args, **_kwargs):
        raise AssertionError("polish must not restyle a copied design")

    monkeypatch.setattr(workflow, "session_store", store)
    monkeypatch.setattr(workflow.DeckBrief, "load", lambda _sid: SimpleNamespace(
        design_reference=_record(reference_bytes), effective_creativity=lambda: "MED"))
    monkeypatch.setattr(workflow.reference_design, "prepare",
                        lambda _sid, reference: (prepared, ""))
    monkeypatch.setattr(workflow.skill_deck, "enabled", lambda: False)
    monkeypatch.setattr(workflow.design_contract, "build", contract)
    monkeypatch.setattr(workflow, "_design_slides", design)
    monkeypatch.setattr(workflow, "DeckStitcher", Stitcher)
    monkeypatch.setattr(workflow.slide_cache, "save", lambda *_args: None)
    monkeypatch.setattr(workflow.deck_polish, "enabled", lambda: True)
    monkeypatch.setattr(workflow, "_polish", polish)
    monkeypatch.setattr(workflow, "_step", lambda _sid, _stage, message: steps.append(message))
    monkeypatch.setattr(workflow.tempfile, "mkdtemp", lambda **_kw: str(tmp_path / "work"))

    result = workflow._collect({"session_id": "session-1"})

    assert result["pending_publish"] is True
    assert (seen["contract_template"] == seen["design_template"]
            == seen["stitch_template"] == prepared.path)
    assert seen["reference"] is prepared
    assert "REFERENCE DESIGN MODE" in seen["guidance"]
    assert "Copying the design of Reference.pptx." in steps
    assert any("Design copied from Reference.pptx" in gap for gap in result["deck_gaps"])
    assert registry.template_path() != prepared.path


def test_every_slide_gets_the_copy_the_design_direction():
    params = {"planner_special_instructions": "Use a timeline."}
    updated = rd.with_guidance(params)
    assert updated["planner_special_instructions"].startswith("REFERENCE DESIGN MODE")
    assert updated["planner_special_instructions"].endswith("Use a timeline.")
    assert params == {"planner_special_instructions": "Use a timeline."}


# --- claiming it at the start of the turn ------------------------------------------- #
def test_the_claimed_deck_is_design_only_and_dropping_it_brings_it_back(
        session_store, monkeypatch):
    from app.core.handlers import session_store as store_module
    from app.genai.agents import brief as brief_module

    monkeypatch.setattr(store_module, "session_store", session_store)
    monkeypatch.setattr(brief_module, "session_store", session_store)
    manual = session_store.register_upload("s1", "LOTO manual.pdf", "application/pdf",
                                           "a" * 64, "p/manual")
    deck = session_store.register_upload(
        "s1", "Template.pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "b" * 64, "p/template")

    note = rd.claim_from_message("s1", "Copy the design of the uploaded PPT for a LOTO deck")
    assert "Template.pptx" in note
    assert brief_module.DeckBrief.load("s1").design_reference["doc_id"] == deck.doc_id
    # Design only: retrieval, planning and triage no longer see it as a source.
    assert [d.doc_id for d in session_store.latest_docs("s1")] == [manual.doc_id]
    assert len(session_store.all_docs("s1")) == 2
    assert rd.claim_from_message("s1", "Copy the design of the uploaded PPT again") == ""

    assert "Rockwell template" in rd.claim_from_message(
        "s1", "Go back to the Rockwell template")
    assert brief_module.DeckBrief.load("s1").design_reference == {}
    assert len(session_store.latest_docs("s1")) == 2

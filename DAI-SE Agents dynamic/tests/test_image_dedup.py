"""Images are used once per deck, and at most once (and twice in total) per slide."""

from app.core.models.deck_model import ContentBlock, SlideDraft, SlideResult
from app.genai.tools.slide_tools import _unique_images
from app.genai.workflows import deck_workflow as wf


def _result(number, *blocks):
    return SlideResult(slide_number=number, slide_draft=SlideDraft(
        slide_number=number, title=f"Slide {number}", blocks=list(blocks)))


def test_a_slide_written_this_turn_loses_an_image_another_slide_already_shows():
    kept = {1: SlideDraft(slide_number=1, title="Kept", blocks=[
        ContentBlock(slot="picture1", image_ref="docs/fig1.png")])}
    written = [_result(2, ContentBlock(slot="body1", text="Why", image_ref="docs/fig1.png"),
                       ContentBlock(slot="picture1", image_ref="docs/fig1.png"))]
    assert wf._dedupe_images("s1", written, kept) == 2
    blocks = written[0].slide_draft.blocks
    assert [(b.slot, b.image_ref) for b in blocks] == [("body1", "")]   # image-only dropped
    assert kept[1].blocks[0].image_ref == "docs/fig1.png"              # kept slide untouched


def test_among_this_turns_slides_the_earliest_keeps_the_image():
    written = [_result(2, ContentBlock(slot="picture1", image_ref="a.png")),
               _result(3, ContentBlock(slot="picture1", image_ref="a.png"),
                       ContentBlock(slot="body1", bullets=["x"]))]
    wf._dedupe_images("s1", written, {})
    assert written[0].slide_draft.blocks[0].image_ref == "a.png"
    assert [b.image_ref for b in written[1].slide_draft.blocks] == [""]


def test_write_content_drops_repeated_and_surplus_images_on_one_slide():
    blocks = [ContentBlock(slot="p1", image_ref="a.png"),
              ContentBlock(slot="p2", text="caption", image_ref="a.png"),
              ContentBlock(slot="p3", image_ref="b.png"),
              ContentBlock(slot="p4", image_ref="c.png")]
    kept = _unique_images(blocks, 4)
    assert [(b.slot, b.image_ref) for b in kept] == [("p1", "a.png"), ("p2", ""),
                                                     ("p3", "b.png")]


# --- image candidates: chosen by the writer, assigned once per deck --------------- #
from types import SimpleNamespace

from app.core.models.deck_model import ImageCandidate, SlidePlan
from app.genai.tools import slide_tools


def _cands(number, *refs):
    return SlideResult(slide_number=number, slide_draft=SlideDraft(
        slide_number=number, title=f"Slide {number}",
        image_candidates=[ImageCandidate(ref=r, description=r) for r in refs]))


def _plans(*plans):
    return {p.slide_number: p for p in plans}


def test_requested_slide_wins_a_shared_candidate_and_others_lose_it():
    written = [_cands(2, "a.png", "b.png"), _cands(3, "a.png")]
    plans = _plans(SlidePlan(slide_number=2, title="2"),
                   SlidePlan(slide_number=3, title="3", images_requested=True))
    wf._assign_images("s1", written, {}, plans)
    assert [c.ref for c in written[0].slide_draft.image_candidates] == ["b.png"]
    assert [c.ref for c in written[1].slide_draft.image_candidates] == ["a.png"]


def test_kept_slide_image_is_taken_unless_reuse_is_allowed():
    kept = {1: SlideDraft(slide_number=1, title="Kept",
                          image_candidates=[ImageCandidate(ref="a.png")])}
    written = [_cands(2, "a.png"), _cands(3, "a.png")]
    plans = _plans(SlidePlan(slide_number=2, title="2"),
                   SlidePlan(slide_number=3, title="3", allow_image_reuse=True))
    wf._assign_images("s1", written, kept, plans)
    assert written[0].slide_draft.image_candidates == []
    assert [c.ref for c in written[1].slide_draft.image_candidates] == ["a.png"]


def test_quiz_and_role_slides_get_no_images_unless_requested():
    written = [_cands(1, "a.png"), _cands(2, "b.png"), _cands(3, "c.png")]
    plans = _plans(SlidePlan(slide_number=1, kind="cover", title="1"),
                   SlidePlan(slide_number=2, title="2", slide_type="QUIZ"),
                   SlidePlan(slide_number=3, kind="cover", title="3", images_requested=True))
    wf._assign_images("s1", written, {}, plans)
    assert written[0].slide_draft.image_candidates == []
    assert written[1].slide_draft.image_candidates == []
    assert [c.ref for c in written[2].slide_draft.image_candidates] == ["c.png"]


def _workspace(plan, *image_refs):
    chunks = [SimpleNamespace(chunk_id=r, kind="image_description", text=f"about {r}",
                              section="fig", score=1.0, meta={"frame_path": r})
              for r in image_refs]
    return SimpleNamespace(plan=plan, session_id="s1",
                           image_assets=lambda: slide_tools.SlideWorkspace.image_assets(
                               SimpleNamespace(plan=plan, chunks=chunks)))


def test_writer_candidates_keep_only_offered_refs_capped_at_three():
    plan = SlidePlan(slide_number=4, title="4")
    ws = _workspace(plan, "a.png", "b.png", "c.png", "d.png")
    got = slide_tools._image_candidates_of(
        ws, ["x.png", "a.png", "b.png", "a.png", "c.png", "d.png"], [])
    assert [c.ref for c in got] == ["a.png", "b.png", "c.png"]
    assert got[0].description == "about a.png"


def test_writer_candidates_are_empty_on_a_faq_slide():
    plan = SlidePlan(slide_number=5, title="5", slide_type="FAQ")
    ws = _workspace(plan, "a.png")
    assert slide_tools._image_candidates_of(ws, ["a.png"], []) == []


def test_candidates_stay_out_of_the_design_json_hash():
    plain = SlideDraft(slide_number=1, title="T")
    with_images = SlideDraft(slide_number=1, title="T",
                             image_candidates=[ImageCandidate(ref="a.png")])
    assert plain.design_json() == with_images.design_json()
    assert wf._image_key(plain, None) == ""
    assert wf._image_key(with_images, None).startswith("|img:")


def test_an_uploaded_image_is_placeable_from_its_own_storage_path(monkeypatch):
    from app.genai.ingestion.extractors import media
    client = SimpleNamespace(chat_completion=lambda **_: None,
                             reply_text=lambda _: "A wiring diagram")
    monkeypatch.setattr(media, "get_apim_llm_client", lambda: client)
    record = SimpleNamespace(filename="panel.png", mime_type="image/png",
                             storage_path="s1/doc/1/panel.png")
    units = media.ImageExtractor().extract(b"png-bytes", record)
    assert [(u.kind, u.meta["frame_path"]) for u in units] == [
        ("image_description", "s1/doc/1/panel.png")]


def test_a_rewrite_brief_shows_its_excerpts_and_image_assets():
    from app.core.models.deck_model import DeckContext, SlideTask
    from app.genai.agents import ppt_agent
    plan = SlidePlan(slide_number=4, title="Architecture", images_requested=True)
    chunks = [SimpleNamespace(chunk_id="t1", kind="text", text="The hub talks to PLCs",
                              section="s", filename="spec.pdf", score=0.5, meta={}),
              SimpleNamespace(chunk_id="i1", kind="image_description",
                              text="Architecture diagram", section="fig",
                              filename="spec.pdf", score=0.9,
                              meta={"frame_path": "s1/figures/figure_001.png"})]
    ws = slide_tools.SlideWorkspace(session_id="s1",
                                    task=SlideTask(slide_number=4, op="rewrite"),
                                    plan=plan, context=DeckContext(title="Deck"),
                                    chunks=chunks)
    brief = ppt_agent._task_brief(ws.task, plan, ws.context, True, ws)
    assert "EXCERPTS GATHERED FOR THIS SLIDE" in brief
    assert "The hub talks to PLCs" in brief
    assert "s1/figures/figure_001.png" in brief
    assert "IMAGES REQUESTED" in brief


def test_an_image_request_changes_the_fingerprint_and_survives_a_revision():
    from app.genai.agents import planner, work_order
    plain = SlidePlan(slide_number=4, title="Arch")
    asked = SlidePlan(slide_number=4, title="Arch", images_requested=True,
                      allow_image_reuse=True)
    assert work_order._fingerprint(plain) != work_order._fingerprint(asked)
    revised = planner._carry_over(asked, SlidePlan(slide_number=4, title="Arch v2"))
    assert (revised.images_requested, revised.allow_image_reuse) == (True, True)


def test_same_named_figures_from_two_documents_stay_distinct(tmp_path, monkeypatch):
    from app.genai.agents import ppt_creator_agent as creator
    monkeypatch.setattr(creator.storage, "read_file", lambda ref: ref.encode())
    slide = SlideDraft(slide_number=4, title="T", image_candidates=[
        ImageCandidate(ref="doc1/figures/figure_001.png", description="first"),
        ImageCandidate(ref="doc2/figures/figure_001.png", description="second")])
    notes = {}
    paths = creator.materialize_images(slide, tmp_path, notes=notes)
    assert len(set(paths)) == 2
    assert sorted(notes.values()) == ["first", "second"]


def test_the_designer_image_block_is_unchanged_without_images():
    from app.genai.agents.ppt_creator_agent import (
        IMAGE_OPTIONAL_NOTE, IMAGE_REQUESTED_NOTE, image_block_text,
    )
    none = SimpleNamespace(slide_number=1, image_paths=[], image_notes={},
                           images_requested=False)
    assert image_block_text(none) == "(none)"
    offered = SimpleNamespace(slide_number=1, image_paths=["/w/img1_a.png"],
                              image_notes={"/w/img1_a.png": "A diagram"},
                              images_requested=False)
    assert image_block_text(offered) == "- /w/img1_a.png - A diagram\n" + IMAGE_OPTIONAL_NOTE
    offered.images_requested = True
    assert image_block_text(offered).endswith(IMAGE_REQUESTED_NOTE)

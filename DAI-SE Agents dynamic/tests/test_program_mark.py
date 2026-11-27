"""The D2R logo appears only on slides about D2R; every other slide shows Rockwell's."""

import io
import json

import pytest
from pptx import Presentation
from pptx.util import Inches

from app.config.constants import PptConstants, ReferenceDesignConstants as C
from app.core.models.deck_model import ContentBlock, SlideDraft
from app.genai.ppt import design_contract
from app.genai.ppt.program_mark import ProgramMark
from app.genai.ppt.rebrand import Rebrander
from app.genai.ppt.template import registry

LOGOS = registry.template_root() / PptConstants.LOGO_DIR


def _slide(title, text="", notes=""):
    return SlideDraft(slide_number=1, title=title, notes=notes,
                      blocks=[ContentBlock(slot="body1", text=text)])


@pytest.mark.parametrize("slide, about", [
    (_slide("D2R account planning"), True),
    (_slide("Overview", "How the D-2-R programme works"), True),
    (_slide("Lockout/tagout basics", "Isolate energy sources"), False),
    (_slide("Overview", "Safety first", notes="Taken from the D2R deck"), False),
    (None, False),
])
def test_a_slide_is_about_d2r_only_when_its_content_mentions_it(slide, about):
    assert ProgramMark.is_about(slide) is about


def test_the_default_frame_logo_is_rockwells():
    layout = type("L", (), {"background_luminance": 1.0, "key": "M0-L0"})()
    frame = design_contract.frame_for_layout("internal", 13.333, 7.5, 1, layout)
    assert frame["logo"]["path"].endswith(C.LOCKUP_COLOR)


def _slide_file(tmp_path, logo_file):
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    mark = slide.shapes.add_picture(str(LOGOS / logo_file), Inches(0.42), Inches(0.28),
                                    Inches(0.79))
    mark.name = "frame:logo"
    path = tmp_path / "slide_1.pptx"
    prs.save(str(path))
    return path


def _logo_blob(path):
    (shape,) = [s for s in Presentation(str(path)).slides[0].shapes
                if s.name == "frame:logo"]
    return shape.image.blob


def test_a_d2r_slide_gets_the_d2r_mark_in_the_frame(tmp_path):
    path = _slide_file(tmp_path, C.LOCKUP_COLOR)
    out = ProgramMark().brand_file(path, _slide("D2R overview"), tmp_path / "out")
    assert out != path
    assert _logo_blob(out) == (LOGOS / C.PROGRAM_MARK).read_bytes()
    assert _logo_blob(path) == (LOGOS / C.LOCKUP_COLOR).read_bytes()   # cache untouched


def test_a_slide_not_about_d2r_shows_rockwells_logo(tmp_path):
    # A slide cached before this change still carries the D2R mark.
    path = _slide_file(tmp_path, C.PROGRAM_MARK_WHITE)
    out = ProgramMark().brand_file(path, _slide("Safety basics"), tmp_path / "out")
    assert _logo_blob(out) == (LOGOS / C.LOCKUP_WHITE).read_bytes()
    already = _slide_file(tmp_path, C.LOCKUP_COLOR)
    assert ProgramMark().brand_file(already, _slide("Safety"), tmp_path / "o2") == already


def test_a_reference_layout_with_a_d2r_mark_gets_a_hidden_twin_for_d2r_slides(
        tmp_path):
    from pptx.oxml.shapes.picture import CT_Picture

    from app.genai.ppt.rebrand import LayoutTwin, MarkLibrary
    from app.genai.ppt.template import catalog as catalog_mod

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    layout = prs.slide_layouts[6]
    d2r = (LOGOS / "recognised/d2r_ribbon.png").read_bytes()
    _, rel = layout.part.get_or_add_image_part(io.BytesIO(d2r))
    pic = CT_Picture.new_pic(layout.shapes._next_shape_id, "Logo", "", rel,
                             Inches(0.2), Inches(0.3), Inches(1.5), Inches(0.5))
    layout.shapes._spTree.insert_element_before(pic, "p:extLst")
    count = len(prs.slide_layouts)

    Rebrander().run(prs)

    # The layout itself now shows Rockwell's mark; its twin still shows D2R.
    assert len(prs.slide_layouts) == count + 1
    assert not any(MarkLibrary.matches(MarkLibrary.fingerprint(s.image.blob),
                                       MarkLibrary.program_marks())
                   for s in layout.shapes if s.shape_type == 13)
    twin = LayoutTwin.find(layout)
    assert twin is not None and any(s.image.blob == d2r for s in twin.shapes
                                    if s.shape_type == 13)
    template = tmp_path / "ref.pptx"
    prs.save(str(template))
    names = [l.name for l in catalog_mod.load(str(template)).layouts]
    assert twin.name not in names and layout.name in names

    prs.slides.add_slide(layout)
    slide_path = tmp_path / "slide_2.pptx"
    prs.save(str(slide_path))
    marks = ProgramMark()
    assert marks.brand_file(slide_path, _slide("Safety"), tmp_path / "a") == slide_path
    out = marks.brand_file(slide_path, _slide("D2R roles"), tmp_path / "b")
    assert Presentation(str(out)).slides[0].slide_layout.name == twin.name

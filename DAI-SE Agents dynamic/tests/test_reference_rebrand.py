"""Rockwell's logo on a reference design (`app.genai.ppt.rebrand`).

A deck built on someone else's template must carry Rockwell's logo and nobody
else's: their logos are swapped for Rockwell's in place, Rockwell marks already there
are kept, and a layout with no logo at all gets one.
"""
import hashlib
import io
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Inches

from app.config.constants import PptConstants, ReferenceDesignConstants as C
from app.genai.ppt import design_contract, reference_design as rd
from app.genai.ppt.rebrand import Geometry, LayoutTwin, MarkLibrary, Rebrander
from app.genai.ppt.rebrand.placer import R_EMBED
from app.genai.ppt.template import catalog as catalog_mod, registry

LOGOS = registry.template_root() / PptConstants.LOGO_DIR
POLICY = registry.template_root() / PptConstants.CLASSIFICATION_ICON_DIR


def _png(draw, size=(300, 100)) -> bytes:
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    draw(ImageDraw.Draw(image))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def _acme() -> bytes:
    """Another company's wordmark."""
    def draw(d):
        d.ellipse((5, 5, 95, 95), fill=(20, 60, 200))
        d.rectangle((115, 30, 290, 70), fill=(20, 60, 200))
    return _png(draw)


def _social() -> bytes:
    return _png(lambda d: d.rectangle((10, 10, 90, 90), outline=(255, 255, 255),
                                      width=8), size=(100, 100))


def _picture(owner, data: bytes, left, top, width, height):
    """Masters and layouts have no add_picture; build the <p:pic> directly."""
    from pptx.oxml.shapes.picture import CT_Picture

    _, rel_id = owner.part.get_or_add_image_part(io.BytesIO(data))
    pic = CT_Picture.new_pic(owner.shapes._next_shape_id, "Logo", "", rel_id,
                             Inches(left), Inches(top), Inches(width), Inches(height))
    owner.shapes._spTree.insert_element_before(pic, "p:extLst")


def _grouped_picture(prs, owner, data: bytes, left, top, width, height):
    """A picture inside a group, built on a scratch slide and moved onto ``owner``."""
    scratch = prs.slides.add_slide(prs.slide_layouts[6])
    group = scratch.shapes.add_group_shape()
    group.shapes.add_picture(io.BytesIO(data), Inches(left), Inches(top),
                             Inches(width), Inches(height))
    element = group._element
    owner.shapes._spTree.insert_element_before(element, "p:extLst")
    _, rel_id = owner.part.get_or_add_image_part(io.BytesIO(data))
    for blip in element.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}blip"):
        blip.set(R_EMBED, rel_id)


def _is_rockwell(shape, marks) -> bool:
    try:
        return MarkLibrary.is_rockwell(shape.image.blob, marks)
    except ValueError:                                # a linked picture
        return False


@pytest.fixture
def foreign_deck(tmp_path):
    """A 16:9 deck from another company: its logo on the master's top-right corner,
    a policy symbol in the footer, a row of social icons on one layout, a dark layout
    that hides the master's graphics, and a sample slide."""
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    master = prs.slide_masters[0]
    _picture(master, _acme(), 11.6, 0.3, 1.4, 0.47)
    _picture(master, (POLICY / "Internal_Symbol.png").read_bytes(), 0.2, 7.0,
             0.28, 0.35)
    closing = master.slide_layouts[6]                  # "Blank"
    for i in range(4):
        _picture(closing, _social(), 5.0 + 0.4 * i, 6.3, 0.3, 0.3)
    dark = master.slide_layouts[5]                     # "Title Only"
    dark.element.set("showMasterSp", "0")
    dark.background.fill.solid()
    dark.background.fill.fore_color.rgb = RGBColor(0x10, 0x10, 0x30)
    prs.slides.add_slide(master.slide_layouts[0]).shapes.title.text = "Their words"
    path = tmp_path / "Other Company.pptx"
    prs.save(str(path))
    return path


def _prepare(path: Path, tmp_path):
    data = path.read_bytes()
    record = {"doc_id": "d", "version": 1, "filename": path.name,
              "sha256": hashlib.sha256(data).hexdigest(), "storage_path": "x"}
    return rd.prepare("s", record, read_bytes=lambda _p: data,
                      cache_dir=tmp_path / "cache")


def _marks_on(owner) -> list:
    return [s for s in owner.shapes if s.name == C.MARK_SHAPE_NAME]


def _blobs(owner) -> list[bytes]:
    return [s.image.blob for s in owner.shapes if s.shape_type == 13]


# --- recognising Rockwell's marks --------------------------------------------------- #
@pytest.mark.parametrize("name", ["ra_mark.png", "ra_lockup_color.png",
                                  "ra_lockup_white.png"])
def test_rockwell_marks_are_recognised(name):
    assert MarkLibrary.is_rockwell((LOGOS / name).read_bytes())


@pytest.mark.parametrize("name", ["recognised/d2r_ribbon.png", "d2r_mark.png"])
def test_d2r_marks_are_programme_marks_not_rockwell_marks(name):
    probe = MarkLibrary.fingerprint((LOGOS / name).read_bytes())
    assert MarkLibrary.matches(probe, MarkLibrary.program_marks())
    assert not MarkLibrary.is_rockwell((LOGOS / name).read_bytes())


def test_a_resized_rockwell_mark_is_still_recognised():
    image = Image.open(LOGOS / "ra_mark.png")
    out = io.BytesIO()
    image.resize((image.width * 3, image.height * 3)).save(out, "PNG")
    assert MarkLibrary.is_rockwell(out.getvalue())


def test_another_companys_logo_is_not_a_rockwell_mark():
    assert not MarkLibrary.is_rockwell(_acme())


# --- the pass ----------------------------------------------------------------------- #
def test_another_companys_logo_is_replaced_and_every_layout_shows_rockwells(
        foreign_deck, tmp_path):
    prepared, note = _prepare(foreign_deck, tmp_path)

    assert note == "" and prepared is not None
    assert prepared.rebrand.replaced == 1
    assert prepared.rebrand.no_room == []
    prs = Presentation(str(prepared.path))
    master = prs.slide_masters[0]
    assert not any(MarkLibrary.matches(MarkLibrary.fingerprint(b), [MarkLibrary.fingerprint(_acme())])
                   for b in _blobs(master))
    # The replacement is a Rockwell mark, still in the master's top-right corner.
    (mark,) = _marks_on(master)
    assert MarkLibrary.is_rockwell(mark.image.blob)
    assert mark.left / 914400 > 10 and mark.top / 914400 < 1
    for layout in master.slide_layouts:
        shows_master = layout.element.get("showMasterSp") != "0"
        assert _marks_on(layout) or shows_master, layout.name


def test_a_dark_layout_without_a_logo_gets_the_white_lockup(foreign_deck, tmp_path):
    prepared, _ = _prepare(foreign_deck, tmp_path)
    prs = Presentation(str(prepared.path))
    dark = prs.slide_masters[0].slide_layouts[5]
    (mark,) = _marks_on(dark)
    assert mark.image.blob == (LOGOS / C.LOCKUP_WHITE).read_bytes()


def test_classification_symbols_and_one_off_icons_are_left_alone(foreign_deck,
                                                                 tmp_path):
    prepared, _ = _prepare(foreign_deck, tmp_path)
    prs = Presentation(str(prepared.path))
    master = prs.slide_masters[0]
    assert (POLICY / "Internal_Symbol.png").read_bytes() in _blobs(master)
    assert _blobs(master.slide_layouts[6]).count(_social()) == 4


def test_a_rockwell_logo_already_there_is_kept_and_nothing_is_added(tmp_path):
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    master = prs.slide_masters[0]
    hexagon = (LOGOS / "ra_mark.png").read_bytes()
    # Inside a group, as the brand template's own closing slides draw it.
    _grouped_picture(prs, master, hexagon, 0.3, 0.3, 0.6, 0.65)

    report = Rebrander().run(prs)

    assert (report.kept, report.replaced, report.added) == (1, 0, 0)
    assert report.note() == ""


def test_a_d2r_mark_beside_a_rockwell_mark_is_removed():
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    master = prs.slide_masters[0]
    _picture(master, (LOGOS / "ra_lockup_color.png").read_bytes(), 0.3, 6.8, 1.6, 0.4)
    _picture(master, (LOGOS / "recognised/d2r_ribbon.png").read_bytes(),
             11.5, 0.2, 1.5, 0.5)

    report = Rebrander().run(prs)

    assert (report.kept, report.removed, report.replaced) == (1, 1, 0)
    assert all(MarkLibrary.is_rockwell(b) for b in _blobs(master))


def test_a_d2r_mark_alone_is_swapped_for_the_rockwell_mark_in_place():
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    layout = prs.slide_masters[0].slide_layouts[6]
    layout.element.set("showMasterSp", "0")
    _picture(layout, (LOGOS / "d2r_mark.png").read_bytes(), 5.0, 3.0, 1.2, 1.2)

    report = Rebrander().run(prs)

    assert report.replaced >= 1 and report.removed == 0
    (mark,) = _marks_on(layout)
    assert MarkLibrary.is_rockwell(mark.image.blob)
    assert abs(mark.left / 914400 - 5.0) < 1.0


def test_a_layout_with_a_full_width_title_gets_the_lockup_in_the_footer():
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    layout = prs.slide_masters[0].slide_layouts[5]       # "Title Only"
    layout.element.set("showMasterSp", "0")
    title = layout.placeholders[0]
    title.left, title.top = Inches(0.4), Inches(0.3)
    title.width, title.height = Inches(12.5), Inches(1.0)

    Rebrander().run(prs)

    (mark,) = _marks_on(layout)
    assert mark.image.blob == (LOGOS / C.LOCKUP_COLOR).read_bytes()
    assert mark.top / 914400 > 7.0                         # in the footer band


def test_a_prepared_reference_from_an_older_build_is_not_reused(foreign_deck,
                                                                tmp_path):
    prepared, _ = _prepare(foreign_deck, tmp_path)
    assert prepared.path.name.endswith(f"-v{C.PREPARED_VERSION}.pptx")
    assert prepared.cache_key.endswith(f":v{C.PREPARED_VERSION}")
    again, _ = _prepare(foreign_deck, tmp_path)            # from the cache
    assert again.rebrand.to_dict() == prepared.rebrand.to_dict()


def test_a_reference_that_cannot_be_rebranded_falls_back_to_the_brand_template(
        foreign_deck, tmp_path, monkeypatch):
    def broken(_self, _prs, *_a, **_k):
        raise RuntimeError("boom")
    monkeypatch.setattr(Rebrander, "run", broken)

    prepared, note = _prepare(foreign_deck, tmp_path)

    assert prepared is None and "Rockwell template" in note
    assert not list((tmp_path / "cache").glob("*.pptx"))


# --- the frame ---------------------------------------------------------------------- #
def test_the_frame_adds_no_second_logo_on_a_reference(foreign_deck, tmp_path):
    prepared, _ = _prepare(foreign_deck, tmp_path)
    layout = catalog_mod.load(str(prepared.path)).layouts[0]
    with registry.using_template(prepared.path):
        on_reference = design_contract.frame_for_layout("internal", 13.333, 7.5, 1,
                                                        layout)
    on_brand = design_contract.frame_for_layout("internal", 13.333, 7.5, 1, layout)

    assert on_reference["logo"] is None
    assert on_reference["classification"]["label_text"] == "Internal"
    assert on_brand["logo"] is not None


# --- the two reference decks the SE team supplied ----------------------------------- #
PPTS = Path(__file__).resolve().parents[1] / "PPTs"


@pytest.mark.parametrize("name", ["D2R ILT - Account Planning.pptx",
                                  "D2R JOB AID TEMPLATE 1.pptx"])
def test_the_supplied_reference_decks_end_up_with_a_rockwell_logo_on_every_layout(
        name, tmp_path):
    path = PPTS / name
    if not path.is_file():
        pytest.skip(f"{name} is not in this checkout")
    prepared, note = _prepare(path, tmp_path)

    assert note == "" and prepared is not None
    assert prepared.rebrand.no_room == []
    marks = MarkLibrary.known_marks()
    prs = Presentation(str(prepared.path))
    width, height = prs.slide_width / 914400, prs.slide_height / 914400
    for master in prs.slide_masters:
        master_mark = any(_is_rockwell(s, marks)
                          for s, box in Geometry.pictures(master.shapes)
                          if Geometry.is_logo_sized(box, width, height))
        for layout in master.slide_layouts:
            if LayoutTwin.is_twin(layout):
                continue                # shown only on slides about D2R
            own = any(_is_rockwell(s, marks)
                      for s, box in Geometry.pictures(layout.shapes)
                      if Geometry.is_logo_sized(box, width, height))
            assert own or (master_mark and Geometry.shows_master_shapes(layout)), layout.name


def test_a_row_of_social_icons_is_not_mistaken_for_a_logo():
    """Seen on the 2026 template: four 0.22in social icons repeated on three
    'Thank you' layouts were swapped for the Rockwell mark."""
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    layouts = list(prs.slide_masters[0].slide_layouts)[:3]
    for layout in layouts:
        for i in range(4):
            _picture(layout, _png(lambda d, i=i: d.ellipse((0, 0, 20 + i, 20)),
                                  size=(24, 24)), 6.0 + 0.5 * i, 6.9, 0.22, 0.22)

    report = Rebrander(marks=(), icons=()).run(prs)

    assert report.replaced == 0

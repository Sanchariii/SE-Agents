"""Stitching. Because every slide now comes from the same template, a stitched slide
must land on the same layout it was built on — that is what makes theme, fonts and
master furniture inherit instead of being re-drawn."""

import zipfile

import pytest
from pptx import Presentation

from app.genai.ppt import deck_builder as db
from app.genai.ppt.stitcher import DeckStitcher
from app.genai.ppt.template import catalog as catalog_mod, registry


@pytest.fixture(scope="module")
def cat():
    return catalog_mod.load()


@pytest.fixture
def slide_file(cat, tmp_path):
    def _make(number, layout_key, title, with_image=False):
        deck = db.DeckBuilder(registry.template_path(), cat.to_dict())
        handle = deck.add_slide(layout_key)
        handle.set_title(title)
        handle.set_notes(f"notes for {number}")
        if with_image:
            handle.add_image(str(registry.logo_path("ra_lockup_full.png")),
                             1.0, 3.0, 2.0, 1.0)
        return deck.save(tmp_path / f"s{number}.pptx")
    return _make


def test_each_slide_keeps_the_layout_it_was_built_on(slide_file, tmp_path):
    files = [(1, slide_file(1, "M1-L10", "First")),
             (2, slide_file(2, "M2-L4", "Second"))]
    out = DeckStitcher().stitch(files, tmp_path / "deck.pptx")

    slides = list(Presentation(str(out)).slides)
    assert len(slides) == 2
    assert slides[0].slide_layout.name == "Title with Subhead"
    assert slides[1].slide_layout.name == "Title with Subhead on Gradient"


def test_the_stitched_deck_keeps_the_template_theme_and_masters(slide_file, tmp_path):
    out = DeckStitcher().stitch([(1, slide_file(1, "M1-L10", "T"))],
                                tmp_path / "deck.pptx")
    deck = Presentation(str(out))
    assert len(deck.slide_masters) == 4
    from pptx.util import Emu
    assert Emu(deck.slide_width).inches == pytest.approx(13.33, abs=0.01)


def test_slides_are_stitched_in_slide_number_order(slide_file, tmp_path):
    files = [(2, slide_file(2, "M1-L10", "Second")),
             (1, slide_file(1, "M1-L10", "First"))]
    out = DeckStitcher().stitch(files, tmp_path / "deck.pptx")

    titles = [s.shapes.title.text_frame.text for s in Presentation(str(out)).slides]
    assert titles == ["First", "Second"]


def test_images_survive_the_stitch(slide_file, tmp_path):
    out = DeckStitcher().stitch([(1, slide_file(1, "M2-L4", "T", with_image=True))],
                                tmp_path / "deck.pptx")
    slide = next(iter(Presentation(str(out)).slides))
    assert [s for s in slide.shapes if s.shape_type == 13]


def test_stitched_images_have_unique_package_part_names(slide_file, tmp_path):
    """Images from separate source packages must not collide in the output ZIP."""
    files = [(1, slide_file(1, "M2-L4", "First", with_image=True)),
             (2, slide_file(2, "M2-L4", "Second", with_image=True))]

    out = DeckStitcher().stitch(files, tmp_path / "deck.pptx")

    with zipfile.ZipFile(out) as package:
        names = package.namelist()
    assert len(names) == len(set(names))


def test_notes_survive_the_stitch(slide_file, tmp_path):
    out = DeckStitcher().stitch([(1, slide_file(1, "M1-L10", "T"))],
                                tmp_path / "deck.pptx")
    slide = next(iter(Presentation(str(out)).slides))
    assert slide.notes_slide.notes_text_frame.text == "notes for 1"


def test_no_empty_placeholder_survives_the_stitch(slide_file, tmp_path):
    """The target slide inherits its layout's placeholders and the source already
    filled its own, so the inherited ones must go or every slide ships a stray
    'Click to edit' box under the design."""
    out = DeckStitcher().stitch([(1, slide_file(1, "M1-L10", "T"))],
                                tmp_path / "deck.pptx")
    slide = next(iter(Presentation(str(out)).slides))
    for shape in slide.shapes:
        if shape.has_text_frame:
            assert "Click to edit" not in shape.text_frame.text


def test_an_unreadable_slide_is_noted_and_the_deck_still_ships(slide_file, tmp_path):
    broken = tmp_path / "broken.pptx"
    broken.write_bytes(b"not a pptx at all")
    stitcher = DeckStitcher()
    out = stitcher.stitch([(1, slide_file(1, "M1-L10", "Good")), (2, broken)],
                          tmp_path / "deck.pptx")

    assert len(list(Presentation(str(out)).slides)) == 1
    assert any("Slide 2" in note for note in stitcher.notes)


def test_a_deck_with_no_readable_slide_is_an_error(tmp_path):
    broken = tmp_path / "broken.pptx"
    broken.write_bytes(b"nope")
    with pytest.raises(ValueError, match="no slide"):
        DeckStitcher().stitch([(1, broken)], tmp_path / "deck.pptx")


def test_a_slide_built_on_an_unknown_layout_still_stitches(cat, tmp_path):
    """A source deck that did not come from our template must not lose its slide."""
    from pptx import Presentation as P

    foreign = P()
    foreign.slides.add_slide(foreign.slide_layouts[6])
    path = tmp_path / "foreign.pptx"
    foreign.save(str(path))

    stitcher = DeckStitcher()
    out = stitcher.stitch([(1, path)], tmp_path / "deck.pptx")
    assert len(list(Presentation(str(out)).slides)) == 1

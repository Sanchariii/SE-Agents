"""The High Creativity master is put on brand in generated files, never in SAMPLE.pptx.

A slide built on it gets the brand colour scheme, Barlow and no baked sample title, and
so does the stitched deck. A deck that never uses it is left byte-for-byte alone.
"""

import zipfile

import pytest
from pptx import Presentation
from pptx.opc.constants import RELATIONSHIP_TYPE as RT

from app.genai.ppt.deck_builder import DeckBuilder
from app.genai.ppt.stitcher import DeckStitcher
from app.genai.ppt.template import catalog as catalog_mod, registry

_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


@pytest.fixture(scope="module")
def catalog():
    cat = catalog_mod.load()
    if cat.high_creativity_title() is None:
        pytest.skip("this template has no usable High Creativity master")
    return cat


def _build(catalog, key, path, title="Hello"):
    deck = DeckBuilder(registry.template_path(),
                       catalog.to_dict(include_high_creativity=True), frame=None)
    deck.add_slide(key).set_title(title)
    return deck.save(path)


def _theme_xml(path, master_index):
    from lxml import etree
    prs = Presentation(str(path))
    part = list(prs.slide_masters)[master_index].part.part_related_by(RT.THEME)
    return etree.fromstring(part.blob)


def _theme_bytes(path, master_index):
    prs = Presentation(str(path))
    return list(prs.slide_masters)[master_index].part.part_related_by(RT.THEME).blob


def _check_on_brand(path, catalog):
    root = _theme_xml(path, catalog.high_creativity_master)
    accent1 = root.find(f".//{_A}clrScheme/{_A}accent1/{_A}srgbClr").get("val")
    assert accent1.upper() == catalog.theme.colors["accent1"].upper()
    for tag in ("majorFont", "minorFont"):
        assert root.find(f".//{_A}fontScheme/{_A}{tag}/{_A}latin").get("typeface") == "Barlow"
    prs = Presentation(str(path))
    for layout in list(prs.slide_masters)[catalog.high_creativity_master].slide_layouts:
        assert not any("click to edit" in (s.text_frame.text.lower() if s.has_text_frame
                                           else "") for s in layout.shapes)


def test_a_slide_on_the_master_is_put_on_brand(catalog, tmp_path):
    title = catalog.high_creativity_title()
    path = _build(catalog, title.key, tmp_path / "hc.pptx")

    _check_on_brand(path, catalog)
    slide = list(Presentation(str(path)).slides)[0]
    box = next(s for s in slide.shapes if s.has_text_frame and s.text_frame.text == "Hello")
    assert abs(box.left / 914400 - title.title_rect.left) < 0.02
    assert abs(box.top / 914400 - title.title_rect.top) < 0.02


def test_a_slide_elsewhere_leaves_the_master_untouched(catalog, tmp_path):
    path = _build(catalog, "M1-L10", tmp_path / "med.pptx")
    hc = catalog.high_creativity_master
    assert _theme_bytes(path, hc) == _theme_bytes(registry.template_path(), hc)


def test_the_stitched_deck_is_put_on_brand_only_when_it_uses_the_master(catalog, tmp_path):
    hc_file = _build(catalog, catalog.high_creativity_title().key, tmp_path / "hc.pptx")
    med_file = _build(catalog, "M1-L10", tmp_path / "med.pptx")
    hc = catalog.high_creativity_master

    mixed = DeckStitcher().stitch([(1, med_file), (2, hc_file)], tmp_path / "mixed.pptx")
    _check_on_brand(mixed, catalog)

    plain = DeckStitcher().stitch([(1, med_file)], tmp_path / "plain.pptx")
    assert _theme_bytes(plain, hc) == _theme_bytes(registry.template_path(), hc)
    assert zipfile.is_zipfile(plain)

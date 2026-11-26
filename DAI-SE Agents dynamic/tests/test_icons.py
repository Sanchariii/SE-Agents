"""Pre-built icons (`app.genai.ppt.icons`, `DeckBuilder.add_icon`): the designer is
offered a few matching icons and places them; it never draws its own."""
import json

import pytest
from PIL import Image, ImageDraw

from app.genai.ppt import deck_builder as db, geometry as geo
from app.genai.ppt.icons import IconLibrary
from app.genai.ppt.template import catalog as catalog_mod, registry


def _icon(path):
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((8, 8, 56, 56), fill=(0, 0, 0, 255))
    image.save(path)


@pytest.fixture
def library(tmp_path):
    folder = tmp_path / "icons"
    folder.mkdir()
    for name in ("factory", "padlock", "person"):
        _icon(folder / f"{name}.png")
    (folder / "index.json").write_text(json.dumps([
        {"name": "factory", "file": "factory.png", "keywords": ["plant", "site"]},
        {"name": "padlock", "file": "padlock.png", "keywords": ["lock", "lockout"]},
        {"name": "person", "file": "person.png", "keywords": ["worker", "employee"]},
        {"name": "ghost", "file": "missing.png", "keywords": ["lock"]},
    ]))
    return IconLibrary(folder)


def test_the_library_loads_what_is_on_disk(library):
    assert sorted(icon.name for icon in library.icons) == ["factory", "padlock",
                                                           "person"]


def test_search_matches_names_keywords_and_plurals(library):
    found = library.search("Apply your lock before workers enter the plant")
    assert [icon.name for icon in found] == ["factory", "padlock", "person"]
    assert [i.name for i in library.search("Lockout steps")] == ["padlock"]
    assert library.search("Quarterly revenue") == []
    assert len(library.search("lock plant worker", limit=2)) == 2


def test_an_empty_library_leaves_the_prompt_unchanged(tmp_path):
    empty = IconLibrary(tmp_path / "none")
    assert empty.search("lockout padlock") == []
    assert IconLibrary.prompt_block([]) == ""


def test_the_offered_icons_are_staged_and_listed(library, tmp_path):
    offered = library.search("lockout")
    IconLibrary.stage(offered, tmp_path / "slide_1")

    assert (tmp_path / "slide_1" / "icons" / "padlock.png").is_file()
    block = IconLibrary.prompt_block(offered)
    assert "s.add_icon(" in block and "- padlock (lock, lockout)" in block


def test_add_icon_places_a_recoloured_icon_and_the_gate_allows_repeats(
        library, tmp_path, monkeypatch):
    slide_dir = tmp_path / "slide_1"
    IconLibrary.stage(library.icons, slide_dir)
    monkeypatch.chdir(slide_dir)
    catalog = catalog_mod.load()
    deck = db.DeckBuilder(registry.template_path(), catalog.to_dict())
    slide = deck.add_slide("M1-L10")
    slide.set_title("Lockout")
    first = slide.add_icon("padlock", 1.0, 2.0, size=0.8, color="red")
    slide.add_icon("padlock", 3.0, 2.0, size=0.8, color="red")
    missing = slide.add_icon("not-offered", 5.0, 2.0)
    out = deck.save(tmp_path / "icons.pptx")

    assert first is not None and first.name == "icon:padlock"
    assert "a:duotone" in first._element.xml
    assert missing is None and any("not-offered" in w for w in deck.warnings)
    found = geo.check_slide(out, catalog.resolve_layout("M1-L10"),
                            theme_colors=catalog.theme.colors)
    assert 13 not in {v.rule for v in found}

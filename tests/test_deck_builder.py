"""The constrained builder generated code writes against.

Everything here runs against the real SAMPLE.pptx with no model in the loop: the point
is that a program written to this API lands on a real layout, in theme colours, at
theme fonts, or fails loudly.
"""

import json
from pathlib import Path

import pytest
from pptx import Presentation
from pptx.util import Emu

from app.genai.ppt import deck_builder as db
from app.genai.ppt.template import catalog as catalog_mod, registry


@pytest.fixture(scope="module")
def catalog_dict():
    return catalog_mod.load().to_dict()


@pytest.fixture
def deck(catalog_dict):
    return db.DeckBuilder(registry.template_path(), catalog_dict)


def test_the_builder_needs_no_app_imports_to_work(catalog_dict, tmp_path):
    """The sandbox copies this module on its own; it must stand up without app/."""
    source = Path(db.__file__).read_text(encoding="utf-8")
    assert "import app." not in source and "from app." not in source


def test_a_new_deck_starts_from_the_template_with_its_sample_slides_dropped(deck):
    assert len(deck.prs.slides) == 0
    assert len(deck.prs.slide_masters) == 4
    assert Emu(deck.prs.slide_width).inches == pytest.approx(13.33, abs=0.01)


def test_a_slide_lands_on_the_layout_it_asked_for(deck, tmp_path):
    handle = deck.add_slide("M1-L10")
    handle.set_title("Reflow profile control")
    out = deck.save(tmp_path / "one.pptx")

    reopened = Presentation(str(out))
    slide = next(iter(reopened.slides))
    assert slide.slide_layout.name == "Title with Subhead"
    assert "Reflow profile control" in slide.shapes.title.text_frame.text


def test_a_layout_can_be_named_as_well_as_keyed(deck):
    assert deck.add_slide("Title with Subhead").layout.key == "M1-L10"


def test_an_unknown_layout_fails_loudly(deck):
    with pytest.raises(LookupError):
        deck.add_slide("A Layout That Does Not Exist")


# --- colour discipline ----------------------------------------------------- #

def test_brand_tokens_resolve_against_the_template_theme(catalog_dict):
    palette = db.brand_palette(catalog_dict["themes"][1]["colors"])
    assert palette["red"] == "CD163F"        # accent1
    assert palette["blue"] == "003E7E"       # accent2
    assert palette["orange"] == "F58024"     # accent3
    assert palette["white"] == "FFFFFF"      # lt1


def test_a_hex_literal_is_refused_even_though_pptx_would_accept_it():
    palette = db.brand_palette({})
    with pytest.raises(ValueError, match="token"):
        db.to_rgb("#CD163F", palette)
    with pytest.raises(ValueError, match="token"):
        db.to_rgb("CD163F", palette)


def test_an_unknown_token_is_refused_and_lists_what_is_allowed():
    palette = db.brand_palette({})
    with pytest.raises(ValueError) as excinfo:
        db.to_rgb("chartreuse", palette)
    assert "red" in str(excinfo.value) and "navy" in str(excinfo.value)


def test_every_component_writes_theme_font_text(deck, tmp_path):
    handle = deck.add_slide("M1-L10")
    handle.add_card("Yield", ["Up 4 points"], 0.5, 2.0, 3.0, 1.5)
    handle.add_stat("98%", "first-pass yield", 4.0, 2.0, 2.5, 1.2)
    handle.add_textbox("Body copy", 7.0, 2.0, 3.0, 0.6)
    out = deck.save(tmp_path / "fonts.pptx")

    slide = next(iter(Presentation(str(out)).slides))
    runs = [run for shape in slide.shapes if shape.has_text_frame
            for para in shape.text_frame.paragraphs for run in para.runs]
    assert runs
    for run in runs:
        assert run.font.name in (None, "Barlow")


def test_every_component_font_size_is_on_the_type_scale(deck, tmp_path):
    handle = deck.add_slide("M1-L10")
    handle.add_card("Yield", ["Up 4 points"], 0.5, 2.0, 3.0, 1.5)
    handle.add_stat("98%", "first-pass yield", 4.0, 2.0, 2.5, 1.2)
    handle.add_icon_circle("01", 7.0, 2.0)
    out = deck.save(tmp_path / "sizes.pptx")

    slide = next(iter(Presentation(str(out)).slides))
    sizes = {run.font.size.pt for shape in slide.shapes if shape.has_text_frame
             for para in shape.text_frame.paragraphs for run in para.runs
             if run.font.size is not None}
    assert sizes, "components must set an explicit size"
    assert sizes <= set(db.TYPE_SCALE), f"off-scale sizes: {sizes - set(db.TYPE_SCALE)}"


# --- components ------------------------------------------------------------ #

def test_a_table_arrives_branded_without_being_asked(deck, tmp_path):
    handle = deck.add_slide("M1-L10")
    handle.add_table(["Stage", "Target"], [["Preheat", "3 C/s"]], 1.0, 2.0, 6.0, 1.5)
    out = deck.save(tmp_path / "table.pptx")

    slide = next(iter(Presentation(str(out)).slides))
    table = next(s.table for s in slide.shapes if getattr(s, "has_table", False))
    header = table.cell(0, 0)
    assert header.text_frame.text == "STAGE"
    run = header.text_frame.paragraphs[0].runs[0]
    assert run.font.bold and run.font.size.pt == 11
    assert str(run.font.color.rgb) == "FFFFFF"
    assert str(header.fill.fore_color.rgb) == db.BRAND_DEFAULTS["navy"]


def test_a_chart_arrives_in_brand_series_colours(deck, tmp_path):
    handle = deck.add_slide("M1-L10")
    handle.add_chart("column", ["Q1", "Q2"],
                     [{"name": "Yield", "values": [1, 2]},
                      {"name": "Scrap", "values": [3, 4]}],
                     1.0, 2.0, 6.0, 3.0)
    out = deck.save(tmp_path / "chart.pptx")

    slide = next(iter(Presentation(str(out)).slides))
    chart = next(s.chart for s in slide.shapes if getattr(s, "has_chart", False))
    colours = [str(s.format.fill.fore_color.rgb) for s in chart.plots[0].series]
    assert colours[0] == db.BRAND_DEFAULTS["red"]
    assert colours[1] == db.BRAND_DEFAULTS["blue"]
    assert chart.has_legend


def test_an_unknown_shape_or_chart_type_fails_loudly(deck):
    handle = deck.add_slide("M1-L10")
    with pytest.raises(ValueError, match="unknown shape"):
        handle.add_shape("dodecahedron", 1, 1, 1, 1)
    with pytest.raises(ValueError, match="unknown chart_type"):
        handle.add_chart("sunburst", ["a"], [{"name": "s", "values": [1]}], 1, 1, 2, 2)


def test_a_missing_image_is_a_warning_not_a_crash(deck):
    handle = deck.add_slide("M1-L10")
    assert handle.add_image("no_such_image.png", 1, 2, 2, 2) is None
    assert any("no_such_image" in w for w in deck.warnings)


def test_components_are_placed_where_asked_and_never_quietly_moved(deck, tmp_path):
    """The gate reports off-canvas placement; the builder must not hide it."""
    handle = deck.add_slide("M1-L10")
    handle.add_shape("rectangle", 12.8, 2.0, 2.0, 1.0, fill="red")
    out = deck.save(tmp_path / "offcanvas.pptx")

    slide = next(iter(Presentation(str(out)).slides))
    box = next(s for s in slide.shapes if not s.is_placeholder
               and Emu(s.left).inches > 12)
    assert Emu(box.left).inches == pytest.approx(12.8, abs=0.01)
    assert Emu(box.width).inches == pytest.approx(2.0, abs=0.01)


# --- the grid -------------------------------------------------------------- #

def test_the_grid_maps_cells_to_inches_inside_the_free_region(deck):
    handle = deck.add_slide("M1-L10")
    left, top, width, height = handle.grid(0, 0)
    region = handle.layout.free_region
    assert left == pytest.approx(region[0], abs=0.01)
    assert top == pytest.approx(region[1], abs=0.01)
    assert width < region[2] / 6      # one of twelve columns, minus gutters

    full = handle.grid(0, 0, cols=12, rows=6)
    assert full[2] == pytest.approx(region[2], abs=0.01)
    assert full[3] == pytest.approx(region[3], abs=0.01)


def test_two_adjacent_grid_cells_do_not_overlap(deck):
    handle = deck.add_slide("M1-L10")
    a = handle.grid(0, 0, cols=6)
    b = handle.grid(6, 0, cols=6)
    assert a[0] + a[2] <= b[0] + 1e-6


# --- placeholders ---------------------------------------------------------- #

def test_unfilled_placeholders_are_removed_so_no_prompt_text_ships(deck, tmp_path):
    handle = deck.add_slide("M1-L10")
    handle.set_title("Only a title")
    out = deck.save(tmp_path / "clean.pptx")

    slide = next(iter(Presentation(str(out)).slides))
    text = " ".join(s.text_frame.text for s in slide.shapes if s.has_text_frame)
    assert "Click to edit" not in text
    assert "Second level" not in text


def test_placeholder_bounds_report_the_templates_own_geometry(deck):
    handle = deck.add_slide("M1-L10")
    left, top, width, height = handle.placeholder_bounds("title")
    assert (round(left, 2), round(top, 2)) == (1.14, 0.38)
    assert round(width, 2) == 11.75


def test_notes_land_on_the_notes_slide(deck, tmp_path):
    handle = deck.add_slide("M1-L10")
    handle.set_title("T")
    handle.set_notes("say this out loud")
    out = deck.save(tmp_path / "notes.pptx")

    slide = next(iter(Presentation(str(out)).slides))
    assert slide.notes_slide.notes_text_frame.text == "say this out loud"


def test_long_text_is_shrunk_to_fit_rather_than_overflowing(deck):
    handle = deck.add_slide("M1-L10")
    handle.set_text("subtitle", "x" * 600)
    shape = next(p for p in handle.slide.placeholders
                 if p.placeholder_format.idx == 10)
    size = shape.text_frame.paragraphs[0].runs[0].font.size
    assert size is not None and size.pt <= 10 + 4      # shrunk, floor is 10pt


# --------------------------------------------------------------------------- #
# the frame                                                                    #
# --------------------------------------------------------------------------- #

from app.config.constants import GeometryConstants


@pytest.fixture
def frame():
    return db.frame_spec(
        13.333, 7.5, label="Internal",
        symbol_path=str(registry.classification_symbol("internal")),
        logo_path=str(registry.logo_path("d2r_mark.png")),
        logo_variant="color", slide_number=3)


def test_the_grid_constants_match_the_app_side_ones():
    """The builder cannot import GeometryConstants; these two copies must not drift."""
    assert db.GRID_COLUMNS == GeometryConstants.GRID_COLUMNS
    assert db.GRID_ROWS == GeometryConstants.GRID_ROWS
    assert db.GRID_GUTTER_IN == GeometryConstants.GRID_GUTTER_IN
    assert db.TYPE_SCALE == GeometryConstants.TYPE_SCALE


def test_the_frame_constants_match_the_app_side_ones():
    assert db.FOOTER_BAND_HEIGHT_IN == GeometryConstants.FOOTER_BAND_HEIGHT_IN
    assert db.FRAME_SIDE_MARGIN_IN == GeometryConstants.FRAME_SIDE_MARGIN_IN
    assert db.CLASSIFICATION_SYMBOL_IN == GeometryConstants.CLASSIFICATION_SYMBOL_IN
    assert db.LOGO_WIDTH_IN == GeometryConstants.LOGO_WIDTH_IN


def test_the_logo_rect_follows_the_files_real_aspect_ratio(frame):
    """The brand marks are wide, not square. Reserving a square would block a strip of
    slide that is actually free, and `check_geometry` would report it as unavailable."""
    _, _, width, height = frame["logo"]["rect"]
    assert width == pytest.approx(GeometryConstants.LOGO_WIDTH_IN)
    # d2r_mark.png is 158x60 => 2.63:1, so 0.79in wide renders ~0.30in tall.
    assert height == pytest.approx(0.30, abs=0.02)
    assert height < width


def test_an_unreadable_logo_does_not_break_the_frame():
    spec = db.frame_spec(13.333, 7.5, label="Internal", logo_path="no_such_file.png")
    assert spec["logo"]["rect"][3] > 0


def test_the_footer_band_spans_the_canvas_along_the_bottom(frame):
    left, top, width, height = frame["footer_band"]
    assert left == 0 and width == pytest.approx(13.333, abs=0.01)
    assert height == pytest.approx(GeometryConstants.FOOTER_BAND_HEIGHT_IN)
    assert top + height == pytest.approx(7.5, abs=0.01)


def test_the_classification_mark_sits_inside_the_footer_band(frame):
    band = frame["footer_band"]
    for key in ("symbol", "label"):
        left, top, width, height = frame["classification"][key]
        assert top >= band[1] - 1e-6
        assert top + height <= band[1] + band[3] + 1e-6


def test_the_classification_label_and_symbol_are_stamped_on_the_slide(deck, frame,
                                                                      tmp_path):
    deck.frame = frame
    handle = deck.add_slide("M1-L10")
    handle.set_title("T")
    out = deck.save(tmp_path / "framed.pptx")

    slide = next(iter(Presentation(str(out)).slides))
    text = " ".join(s.text_frame.text for s in slide.shapes if s.has_text_frame)
    assert "Internal" in text
    assert "3" in text                                     # the slide number
    pictures = [s for s in slide.shapes if s.shape_type == 13]
    assert pictures, "the policy symbol must be placed"


def test_a_missing_symbol_degrades_to_the_label_alone(deck, tmp_path):
    deck.frame = db.frame_spec(13.333, 7.5, label="Co-branded", symbol_path=None,
                               slide_number=1)
    handle = deck.add_slide("M1-L10")
    handle.set_title("T")
    out = deck.save(tmp_path / "nosymbol.pptx")

    slide = next(iter(Presentation(str(out)).slides))
    text = " ".join(s.text_frame.text for s in slide.shapes if s.has_text_frame)
    assert "Co-branded" in text
    assert any("symbol" in w.lower() for w in deck.warnings)


def test_the_white_logo_is_chosen_on_a_dark_layout(catalog_dict):
    dark = next(l for l in catalog_dict["layouts"] if l["background_luminance"] < 0.5)
    assert db.logo_variant_for(dark["background_luminance"]) == "white"
    assert db.logo_variant_for(1.0) == "color"


def test_no_second_mark_is_added_when_the_layout_already_carries_one(deck, frame,
                                                                    tmp_path):
    """M1-L10 ships a brand mark as layout decor; the frame must not stack another."""
    deck.frame = frame
    handle = deck.add_slide("M1-L10")
    assert handle.layout.decor_rects           # the layout really does carry decor
    handle.set_title("T")
    out = deck.save(tmp_path / "onelogo.pptx")

    slide = next(iter(Presentation(str(out)).slides))
    logos = [s for s in slide.shapes if s.shape_type == 13
             and Emu(s.top).inches < 1.2 and Emu(s.left).inches < 1.2]
    assert len(logos) == 0, "the layout's own mark is inherited; do not add another"


def test_the_logo_is_added_when_the_layout_leaves_that_corner_free(deck, frame,
                                                                   tmp_path):
    deck.frame = frame
    handle = deck.add_slide("M2-L31")          # no decor, nothing in the top-left
    assert handle.layout.decor_rects == []
    handle.set_title("T")
    out = deck.save(tmp_path / "logo.pptx")

    slide = next(iter(Presentation(str(out)).slides))
    logos = [s for s in slide.shapes if s.shape_type == 13
             and Emu(s.top).inches < 1.2]
    assert len(logos) == 1


def test_no_logo_is_stamped_onto_the_layouts_own_title(deck, frame, tmp_path):
    """M2-L4 has no decor but runs its title across the top of the slide, as 23 of
    this template's 34 decor-free layouts do. Placing the mark at (0.42, 0.28) lands
    it on the first words of that title."""
    deck.frame = frame
    handle = deck.add_slide("M2-L4")
    assert handle.layout.decor_rects == []
    handle.set_title("A title that starts at the very left")
    out = deck.save(tmp_path / "nologo.pptx")

    slide = next(iter(Presentation(str(out)).slides))
    logos = [s for s in slide.shapes if s.shape_type == 13
             and Emu(s.top).inches < 1.2]
    assert logos == []


def test_the_logo_rect_is_reserved_only_where_the_logo_is_placed(frame, catalog_dict):
    """Reserving a corner nothing fills would report a good arrangement as a rule 5
    violation, so the two answers have to come from the same question."""
    view = db.CatalogView(catalog_dict)
    busy, free = view.get("M2-L4"), view.get("M2-L31")

    assert not db.logo_is_placeable(frame, busy)
    assert db.logo_is_placeable(frame, free)

    logo_rect = list(frame["logo"]["rect"])
    assert logo_rect not in db.reserved_rects(frame, busy)
    assert logo_rect in db.reserved_rects(frame, free)


def test_reserved_rects_cover_the_frame_and_the_layouts_own_decor(frame, catalog_dict):
    layout = db.CatalogView(catalog_dict).get("M1-L10")
    reserved = db.reserved_rects(frame, layout)
    assert len(reserved) >= len(layout.decor_rects) + 1
    # The footer band must be among them: rule 5 is what keeps content out of it.
    assert any(abs(r[1] + r[3] - 7.5) < 0.01 and r[2] > 13 for r in reserved)


# --- fitting, composition helpers and the builder report ------------------- #

def test_long_text_box_copy_steps_down_the_type_scale_and_stops_at_the_floor(deck, tmp_path):
    handle = deck.add_slide("M1-L10")
    box = handle.add_textbox("word " * 40, 1.0, 2.0, 4.0, 0.9, size=15)
    sizes = {r.font.size.pt for p in box.text_frame.paragraphs for r in p.runs}
    assert sizes and max(sizes) < 15 and min(sizes) >= db.COMPONENT_FLOOR_PT
    handle.add_textbox("word " * 200, 1.0, 3.5, 2.0, 0.5, size=15)
    assert any("does not fit" in w for w in handle.warnings)


def test_a_two_line_card_title_pushes_the_body_down(deck, tmp_path):
    handle = deck.add_slide("M1-L10")
    handle.add_card("A long card title that certainly wraps onto two lines here",
                    ["one", "two"], 1.0, 2.0, 2.6, 2.6)
    out = deck.save(tmp_path / "card.pptx")
    shapes = list(next(iter(Presentation(str(out)).slides)).shapes)
    texts = [s for s in shapes if s.has_text_frame and s.text_frame.text.strip()
             and not s.is_placeholder and not s.name.startswith(db.FRAME_SHAPE_PREFIX)]
    title, body = texts[0], texts[1]
    assert Emu(title.top + title.height).inches <= Emu(body.top).inches + 0.001


def test_split_returns_non_overlapping_parts(deck):
    handle = deck.add_slide("M1-L10")
    parts = handle.split((1.0, 2.0, 9.0, 3.0), 3, gap=0.3)
    assert len(parts) == 3
    for a, b in zip(parts, parts[1:]):
        assert a[0] + a[2] + 0.3 == pytest.approx(b[0])
    stacked = handle.split((1.0, 2.0, 9.0, 3.0), [2, 1], axis="column", gap=0.0)
    assert stacked[0][3] == pytest.approx(2.0) and stacked[1][3] == pytest.approx(1.0)
    assert handle.fits("short", 3.0, 1.0) and handle.text_height("short", 3.0) > 0


def test_save_writes_the_builder_report_with_a_component_ledger(deck, tmp_path):
    handle = deck.add_slide("M1-L10")
    handle.add_card("Title", "Body", 1.0, 2.0, 3.0, 2.0)
    handle.add_line(1.0, 4.5, 3.0, 0.0)
    deck.save(tmp_path / "ledger.pptx")
    report = json.loads((tmp_path / db.REPORT_FILENAME).read_text(encoding="utf-8"))
    kinds = [(c["kind"], c["content"]) for c in report["components"]]
    assert kinds == [("card", True), ("line", False)]   # the card's parts count once


def test_a_wide_image_is_contained_rather_than_cropped_in_a_square_slot(tmp_path):
    from PIL import Image

    image = tmp_path / "wide.png"
    Image.new("RGB", (1200, 300), "white").save(image)
    assert db._crop_fraction(image, 3.0, 3.0) > 0.5
    assert db._crop_fraction(image, 4.0, 1.0) == pytest.approx(0.0)

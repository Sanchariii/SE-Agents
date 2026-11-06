"""The single brand template: it resolves, it is the file we think it is, and the
classification marks and logos beside it are found by name rather than by glob."""

from pathlib import Path

import pytest

from app.config.constants import GeometryConstants, PptConstants
from app.genai.ppt.template import registry


def test_the_template_resolves_to_a_real_file():
    path = registry.template_path()
    assert path.is_file(), f"brand template missing at {path}"
    assert path.name == PptConstants.TEMPLATE_FILENAME


def test_the_template_lives_beside_the_logos_not_under_scripts():
    path = registry.template_path()
    assert "scripts" not in path.parts
    assert (path.parent / PptConstants.LOGO_DIR).is_dir()


@pytest.mark.parametrize("classification", ["public", "internal", "confidential",
                                            "restricted"])
def test_every_classification_with_a_symbol_on_disk_resolves_to_it(classification):
    symbol = registry.classification_symbol(classification)
    assert symbol is not None and symbol.is_file()
    assert registry.label(classification) in symbol.name


def test_cobranded_has_no_symbol_and_that_is_not_an_error(caplog):
    # There is no Cobranded_Symbol.png in the brand pack; the slide renders without it.
    assert registry.classification_symbol("cobranded") is None


def test_a_known_logo_resolves_and_an_unknown_one_returns_none():
    assert registry.logo_path("ra_lockup_full.png").is_file()
    assert registry.logo_path("no_such_logo.png") is None


def test_the_type_scale_is_the_closed_set_the_spec_defines():
    assert GeometryConstants.TYPE_SCALE == (10, 11, 12, 14, 15, 20, 28, 32)


# --------------------------------------------------------------------------- #
# catalog                                                                      #
# --------------------------------------------------------------------------- #

from app.genai.ppt.template import catalog as catalog_mod


@pytest.fixture(scope="module")
def catalog():
    return catalog_mod.load()


def test_the_whole_template_loads(catalog):
    # 181 brand layouts plus the six Quiz/FAQ reference layouts (Quiz-1,
    # Quiz-1-Answered, Quiz-2, Quiz-2-Answered, FAQ-1, FAQ-2) and their sample slides.
    assert len(catalog.layouts) == 187
    assert len(catalog.themes) == 4
    assert (round(catalog.slide_width, 2), catalog.slide_height) == (13.33, 7.5)
    assert catalog.sample_slide_count == 34


def test_the_theme_palette_is_the_known_rockwell_one(catalog):
    for theme in catalog.themes:
        assert theme.major_font == "Barlow" and theme.minor_font == "Barlow"
        assert theme.colors["accent1"] == "CD163F"
        assert theme.colors["accent2"] == "003E7E"
        assert theme.colors["accent3"] == "F58024"
        assert theme.colors["accent4"] == "00AEEF"
        assert theme.colors["accent5"] == "F9C20A"
        assert theme.colors["accent6"] == "77C14C"
        assert theme.colors["dk1"] == "000000" and theme.colors["lt1"] == "FFFFFF"


def test_the_preferred_content_master_is_the_one_the_samples_use(catalog):
    # M1 carries 17 of the 33 sample slides; M0 carries the covers.
    assert catalog.preferred_master == 1
    assert catalog.cover_master == 0


def test_layout_keys_are_unique_and_addressable(catalog):
    keys = [layout.key for layout in catalog.layouts]
    assert len(set(keys)) == len(keys)
    assert catalog.get("M1-L10").name == "Title with Subhead"
    assert catalog.resolve_layout("M1-L10").layout_index == 10
    assert catalog.resolve_layout("Title with Subhead").key.startswith("M")


def test_an_unknown_layout_raises_with_suggestions(catalog):
    with pytest.raises(LookupError) as excinfo:
        catalog.resolve_layout("Definitely Not A Layout Name At All")
    assert "did you mean" in str(excinfo.value).lower()


def test_decor_rects_carry_real_geometry_not_just_a_count(catalog):
    # M1-L10 has exactly two non-placeholder shapes: a rule line and the brand mark.
    layout = catalog.get("M1-L10")
    assert len(layout.decor_rects) == 2
    mark = max(layout.decor_rects, key=lambda r: r.width * r.height)
    assert mark.left == pytest.approx(0.23, abs=0.02)
    assert mark.top == pytest.approx(0.28, abs=0.02)
    assert mark.width == pytest.approx(0.65, abs=0.02)


def test_a_layout_with_no_decor_reports_no_rects(catalog):
    assert catalog.get("M2-L4").decor_rects == ()


def test_background_luminance_is_reported_for_every_layout(catalog):
    for layout in catalog.layouts:
        assert 0.0 <= layout.background_luminance <= 1.0


def test_a_picture_background_is_measured_not_assumed(catalog):
    # These backgrounds are <p:cSld><p:bg> fills, not shapes: master 2's is a blipFill,
    # and a shape-only scan reports every one of its 47 layouts as plain white.
    # M2-L41 carries its own dark photo background and must read as dark...
    presenter = catalog.get("M2-L41")
    assert presenter.background_luminance < 0.6
    assert presenter.background == "dark"
    # ...while master 2's own wash is a near-white image, so the layouts that inherit it
    # must stay light. Classification is by measured luminance, never by fill type: a
    # blanket "blipFill means photo/dark" rule would mislabel 47 white-looking layouts.
    for key in ("M2-L4", "M2-L31"):
        layout = catalog.get(key)
        assert layout.background_luminance > 0.8
        assert layout.background == "light"


def test_canvas_capable_layouts_are_the_ones_with_only_a_title_and_room_below(catalog):
    canvas = [l for l in catalog.layouts if l.canvas_capable]
    keys = {l.key for l in canvas}
    # Every layout the design names by hand must qualify.
    assert {"M1-L10", "M2-L4", "M2-L30", "M2-L38"} <= keys
    # The count is threshold-dependent (see the plan's "departures" note); assert a
    # floor rather than an exact number so tuning the floor does not break the suite.
    assert len(canvas) >= 12
    for layout in canvas:
        assert layout.free_region is not None
        assert layout.free_region.height >= 4.0
        assert {p.role for p in layout.placeholders} <= {"title", "subtitle"}


def test_the_free_region_clears_every_placeholder_the_layout_still_renders(catalog):
    # A region that clears only the title composes straight over the subhead, which the
    # layout goes on rendering underneath it.
    for layout in catalog.layouts:
        if not layout.canvas_capable:
            continue
        for placeholder in layout.placeholders:
            assert placeholder.top + placeholder.height <= layout.free_region.top, (
                f"{layout.key} free region overlaps its own {placeholder.slot}")


def test_a_layout_whose_title_has_no_resolvable_geometry_is_not_canvas_capable(catalog):
    # Master 2 declares no placeholders, so a layout that omits a:off/a:ext inherits
    # nothing and python-pptx reports None for every dimension. Measuring a free region
    # against a (0,0,0,0) title yields a region starting at y=0.10 that swallows the
    # title band; both of these would otherwise qualify on that bogus 6.95in.
    for key in ("M2-L44", "M2-L31"):
        layout = catalog.get(key)
        title = layout.find("title")
        assert title.width == 0 and title.height == 0
        assert not layout.canvas_capable and layout.free_region is None
        # ...and no fictitious "1 char, 1 line" capacity hint is published for it.
        assert title.approx_chars == 0 and title.approx_lines == 0


def test_the_named_canvas_layouts_have_the_free_height_left_after_the_gap_and_footer_band(catalog):
    # Chain from the design document's raw figures, which are `slide_height - title_bottom`:
    #   M1-L10 6.55 / M2-L4 6.58
    #   - CANVAS_TITLE_GAP_IN (0.10) + FOOTER_BAND_HEIGHT_IN (0.45)  -> 6.00 / 6.03
    #   - clearance below the subhead, which the layout still renders -> 5.69 / 5.58
    # The gap and the band are load-bearing: without them canvas mode composes into the
    # footer band and geometry rule 5 rejects every slide.
    assert catalog.get("M1-L10").free_region.height == pytest.approx(5.69, abs=0.05)
    for key in ("M2-L4", "M2-L30", "M2-L38"):
        assert catalog.get(key).free_region.height == pytest.approx(5.58, abs=0.05)


def test_a_layout_with_body_placeholders_is_not_canvas_capable(catalog):
    busy = next(l for l in catalog.layouts if any(p.role == "body" for p in l.placeholders))
    assert not busy.canvas_capable and busy.free_region is None


def test_placeholders_carry_geometry_and_a_capacity_hint(catalog):
    title = catalog.get("M1-L10").find("title")
    assert title.idx == 0 and title.role == "title"
    assert title.left == pytest.approx(1.14, abs=0.02)
    assert title.width == pytest.approx(11.75, abs=0.02)
    assert title.approx_chars > 0 and title.approx_lines >= 1
    assert catalog.get("M1-L10").find(0) is title


def test_search_and_recommend_return_layouts_from_the_catalog(catalog):
    found = catalog.search("agenda", limit=5)
    assert found and all(l.key in {x.key for x in catalog.layouts} for l in found)
    ranked = catalog.recommend("cover", limit=3)
    assert ranked and "cover" in ranked[0][0].tags


def test_the_catalog_serializes_to_json_for_the_sandbox(catalog):
    import json

    data = catalog.to_dict()
    round_tripped = json.loads(json.dumps(data))       # must be JSON-clean
    assert round_tripped["slide_width"] == pytest.approx(13.33, abs=0.01)
    assert round_tripped["preferred_master"] == 1
    assert len(round_tripped["layouts"]) == len(catalog.layouts)
    entry = next(l for l in round_tripped["layouts"] if l["key"] == "M1-L10")
    assert entry["decor_rects"] and len(entry["decor_rects"][0]) == 4
    assert entry["placeholders"][0]["idx"] == 0
    assert round_tripped["themes"][1]["colors"]["accent1"] == "CD163F"


def test_the_catalog_is_cached_per_file(catalog):
    assert catalog_mod.load() is catalog
    assert catalog_mod.for_classification("confidential") is catalog


def test_the_legacy_prompt_block_shim_still_answers(catalog):
    # plan_tools.py and layout_tools.py call these and must not need editing.
    assert catalog.prompt_block().strip()
    assert catalog.content_layouts
    assert all(hasattr(l, "name") for l in catalog.content_layouts)


def test_furniture_placeholders_are_not_the_agents_to_fill(catalog):
    # The frame draws the date, footer and page number itself. Published as content
    # roles they invite a composing agent to write copy into the page-number box.
    for layout in catalog.layouts:
        for placeholder in layout.placeholders:
            assert placeholder.type not in ("DATE", "FOOTER", "SLIDE_NUMBER", "HEADER")
            assert placeholder.role not in ("date", "footer", "slide_number", "header")


def test_every_content_layout_can_actually_hold_a_content_slide(catalog):
    # `_derive_tags` tags anything not cover/agenda/section/quote/closing/blank as
    # 'content', which sweeps in title-less and slot-less layouts. plan_tools builds its
    # menu from this list and by_role falls back to its first entry, so a full-bleed
    # photo cover reaching it puts a title-less layout under a slide that has a title.
    assert catalog.content_layouts
    for layout in catalog.content_layouts:
        assert layout.has_role("title"), f"{layout.key} has no title"
        assert (layout.has_role("body") or layout.has_role("content")
                or layout.has_role("heading")), f"{layout.key} has no text slot"


def test_the_renderer_fallbacks_land_on_a_usable_content_layout(catalog):
    # by_role() with nothing it recognises falls through to content_layouts[0]; that is
    # a path ppt_creator_agent._resolve_layout actually reaches.
    for layout in (catalog.by_role(()), catalog.default_content(),
                   catalog.resolve("no such layout anywhere")):
        assert layout.has_role("title")
        assert layout.has_role("body") or layout.has_role("content") or layout.has_role("heading")

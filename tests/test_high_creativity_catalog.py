"""The High Creativity master is invisible to every LOW/MED layout choice.

Runs against the real SAMPLE.pptx. While the master is still off brand (Office colours,
Aptos, a baked "Click to edit title"), validation must say so and refuse it.
"""

import pytest

from app.genai.ppt.template import catalog as catalog_mod


@pytest.fixture(scope="module")
def catalog():
    cat = catalog_mod.load()
    if cat.high_creativity_master is None:
        pytest.skip("this template has no High Creativity master")
    return cat


def test_its_layouts_are_kept_out_of_every_selection(catalog):
    keys = {l.key for l in catalog.high_creativity_layouts}
    assert keys and not keys & {l.key for l in catalog.layouts}
    assert not keys & {l.key for l, _, _ in catalog.recommend("cover", limit=10)}
    assert not keys & {l.key for l in catalog.search("title", limit=50)}
    assert not keys & {l.key for l in catalog.content_layouts}
    assert catalog.preferred_master != catalog.high_creativity_master
    assert catalog.cover_master != catalog.high_creativity_master


def test_its_layouts_still_resolve_by_key(catalog):
    for layout in catalog.high_creativity_layouts:
        assert catalog.get(layout.key) is layout and layout.high_creativity


def test_only_slides_that_use_it_get_it_serialized(catalog):
    keys = {l.key for l in catalog.high_creativity_layouts}
    assert not keys & {l["key"] for l in catalog.to_dict()["layouts"]}
    serialized = catalog.to_dict(include_high_creativity=True)["layouts"]
    assert keys <= {l["key"] for l in serialized}


def test_master_furniture_is_part_of_every_layouts_decor(catalog):
    for layout in catalog.high_creativity_layouts:
        assert layout.decor_rects, layout.key


def test_the_master_is_usable_despite_its_own_colours_and_sample_title(catalog):
    # Colours and fonts are forced in generated files, so they are no longer a problem.
    assert catalog.high_creativity_problems() == []
    title = catalog.high_creativity_title()
    assert title is not None and title.canvas_capable and title.title_rect is not None


def test_the_sample_title_box_becomes_the_title_slot(catalog):
    for layout in catalog.high_creativity_layouts:
        assert not any("click to edit" in t.lower() for t in layout.fixed_texts)
        if layout.prompt_shapes:
            rect = layout.title_rect
            assert not any(r == rect for r in layout.decor_rects)
            assert layout.free_region.top >= rect.top + rect.height

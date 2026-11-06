"""The geometry gate, rule by rule.

Every rule gets a positive and a negative fixture, and legitimate layering gets its own
block: a text box inside a card, an accent bar on a card edge and a heading over a
gradient panel all overlap something, and all three are correct design. A gate that
flags them is worse than no gate at all.
"""

from pathlib import Path

import pytest
from pptx import Presentation

from app.config.constants import GeometryConstants as GC
from app.genai.ppt import deck_builder as db, geometry as geo
from app.genai.ppt.template import catalog as catalog_mod, registry

W, H = 13.333, 7.5


def box(ident, left, top, width, height, **kw):
    return geo.Box(id=ident, left=left, top=top, width=width, height=height, **kw)


def rules(violations):
    return sorted({v.rule for v in violations})


def check(boxes, reserved=()):
    return geo.check_rects(boxes, slide_width=W, slide_height=H, reserved=reserved)


# --- rule 1: inside the canvas --------------------------------------------- #

def test_a_shape_inside_the_margins_passes():
    assert check([box("a", 1.0, 1.0, 4.0, 2.0)]) == []


@pytest.mark.parametrize("rect", [
    (-0.5, 1.0, 4.0, 2.0),        # off the left edge
    (1.0, -0.5, 4.0, 2.0),        # off the top
    (11.0, 1.0, 4.0, 2.0),        # past the right edge
    (1.0, 6.5, 4.0, 2.0),         # past the bottom
])
def test_a_shape_outside_the_canvas_is_rule_1(rect):
    assert 1 in rules(check([box("a", *rect)]))


def test_the_canvas_tolerance_forgives_a_hairline_overhang():
    # 0.01in past the margin is rounding, not a design error.
    inside = GC.CANVAS_MARGIN_IN - GC.CANVAS_TOLERANCE_IN / 2
    assert 1 not in rules(check([box("a", inside, 1.0, 4.0, 2.0)]))


# --- rule 2: text never overlaps text -------------------------------------- #

def test_two_separated_text_boxes_pass():
    assert check([box("a", 1.0, 1.0, 4.0, 1.0, has_text=True, text="one"),
                  box("b", 6.0, 1.0, 4.0, 1.0, has_text=True, text="two")]) == []


def test_two_overlapping_text_boxes_are_rule_2():
    found = check([box("a", 1.0, 1.0, 4.0, 1.0, has_text=True, text="one"),
                   box("b", 3.0, 1.0, 4.0, 1.0, has_text=True, text="two")])
    assert 2 in rules(found)


def test_text_inside_text_is_still_rule_2_because_there_is_no_exception():
    found = check([box("a", 1.0, 1.0, 6.0, 3.0, has_text=True, text="outer"),
                   box("b", 2.0, 1.5, 2.0, 1.0, has_text=True, text="inner")])
    assert 2 in rules(found)


def test_an_empty_text_frame_does_not_count_as_text_bearing():
    # A shape with a text frame but no text is not text-bearing (spec's definition).
    assert check([box("a", 1.0, 1.0, 4.0, 1.0, has_text=False, text="   "),
                  box("b", 3.0, 1.0, 4.0, 1.0, has_text=True, text="two")]) == []


def test_hairline_abutment_is_not_an_overlap():
    slack = GC.OVERLAP_TOLERANCE_IN / 2
    found = check([box("a", 1.0, 1.0, 4.0, 1.0, has_text=True, text="one"),
                   box("b", 5.0 - slack, 1.0, 4.0, 1.0, has_text=True, text="two")])
    assert 2 not in rules(found)


# --- rule 3: text over a shape must be contained in it --------------------- #

def test_a_label_fully_inside_a_card_passes():
    assert check([box("card", 1.0, 1.0, 4.0, 2.0, filled=True),
                  box("label", 1.2, 1.2, 3.6, 0.5, has_text=True, text="Yield")]) == []


def test_a_label_half_off_its_card_is_rule_3():
    found = check([box("card", 1.0, 1.0, 4.0, 2.0, filled=True),
                   box("label", 4.0, 1.2, 3.0, 0.5, has_text=True, text="Yield")])
    assert 3 in rules(found)


def test_a_caption_half_off_its_picture_is_rule_3():
    """A picture paints a surface even with no fill of its own."""
    found = check([box("photo", 1.0, 1.0, 4.0, 2.0, is_picture=True),
                   box("caption", 4.0, 1.2, 3.0, 0.5, has_text=True, text="Line 3")])
    assert 3 in rules(found)


def test_text_crossing_an_empty_unfilled_box_is_not_rule_3():
    """An emptied placeholder or a spacer paints nothing, so nothing is obscured."""
    found = check([box("spacer", 1.0, 1.0, 4.0, 2.0),
                   box("copy", 4.0, 1.2, 3.0, 0.5, has_text=True, text="Yield")])
    assert 3 not in rules(found)


# --- rule 4: filled shapes nest or separate -------------------------------- #

def test_an_accent_bar_sitting_on_a_card_passes_because_it_is_contained():
    assert check([box("card", 1.0, 1.0, 4.0, 2.0, filled=True),
                  box("accent", 1.15, 1.12, 0.5, 0.06, filled=True)]) == []


def test_two_partially_overlapping_filled_shapes_are_rule_4():
    found = check([box("a", 1.0, 1.0, 4.0, 2.0, filled=True),
                   box("b", 3.0, 1.5, 4.0, 2.0, filled=True)])
    assert 4 in rules(found)


def test_a_heading_over_a_gradient_panel_passes():
    found = check([box("panel", 0.5, 1.0, 12.0, 2.0, filled=True),
                   box("heading", 1.0, 1.4, 6.0, 0.6, has_text=True, text="Quality")])
    assert found == []


# --- rule 5: reserved zones ------------------------------------------------ #

def test_a_shape_clear_of_the_footer_band_passes():
    footer = [0.0, 7.05, W, 0.45]
    assert check([box("a", 1.0, 1.0, 4.0, 2.0)], reserved=[footer]) == []


def test_a_shape_over_the_footer_band_is_rule_5():
    footer = [0.0, 7.05, W, 0.45]
    found = check([box("a", 1.0, 6.8, 4.0, 0.6)], reserved=[footer])
    assert 5 in rules(found)


def test_a_shape_over_the_layouts_inherited_decor_is_rule_5():
    decor = [0.23, 0.28, 0.65, 0.71]          # M1-L10's brand mark
    found = check([box("a", 0.3, 0.3, 2.0, 1.0)], reserved=[decor])
    assert 5 in rules(found)


# --- rule 6: filled placeholders ------------------------------------------- #

def test_an_added_shape_over_a_filled_placeholder_is_rule_6():
    found = check([
        box("ph", 1.0, 1.0, 6.0, 3.0, filled=True, is_placeholder=True),
        box("added", 2.0, 1.5, 4.0, 1.0),
    ])
    assert 6 in rules(found)


def test_an_added_shape_beside_a_filled_placeholder_passes():
    assert check([
        box("ph", 1.0, 1.0, 5.0, 3.0, filled=True, is_placeholder=True),
        box("added", 7.0, 1.0, 4.0, 1.0),
    ]) == []


# --- rule 7: prompt text --------------------------------------------------- #

@pytest.mark.parametrize("text", ["Click to edit Master title style", "Second level",
                                  "Click to add text", "Lorem ipsum dolor"])
def test_leftover_template_prompt_text_is_rule_7(text):
    found = geo.check_prompt_text([box("a", 1, 1, 4, 1, has_text=True, text=text)])
    assert rules(found) == [7]


def test_real_copy_is_not_mistaken_for_prompt_text():
    good = "Click-through rate rose at every level of the funnel"
    assert geo.check_prompt_text([box("a", 1, 1, 4, 1, has_text=True, text=good)]) == []


# --- the whole picture ----------------------------------------------------- #

def test_a_realistic_three_card_row_passes_every_rule():
    """The layout the repair loop should converge on, not fight."""
    boxes = []
    for index, left in enumerate((0.42, 4.75, 9.08)):
        boxes += [
            box(f"card{index}", left, 2.0, 3.85, 2.2, filled=True),
            box(f"accent{index}", left + 0.15, 2.12, 0.5, 0.06, filled=True),
            box(f"title{index}", left + 0.12, 2.25, 3.6, 0.45,
                has_text=True, text=f"Pillar {index + 1}"),
            box(f"body{index}", left + 0.12, 2.75, 3.6, 1.3,
                has_text=True, text="Short supporting sentence."),
        ]
    footer = [0.0, 7.05, W, 0.45]
    assert check(boxes, reserved=[footer]) == []


def test_violations_name_the_shapes_and_say_what_to_do():
    # Both cards stay on-canvas, so rule 2 is the only thing wrong and its message is
    # what gets inspected. (The plan's own coordinates put card 3 0.75in off the right
    # edge, which made this a rule 1 test by accident.)
    found = check([box("card 2", 6.80, 2.40, 3.60, 1.80, has_text=True, text="a"),
                   box("card 3", 9.20, 2.40, 3.60, 1.80, has_text=True, text="b")])
    assert rules(found) == [2]
    message = found[0].message
    assert "card 2" in message and "card 3" in message
    assert "overlaps" in message
    # Actionable, per the spec: it must suggest a fix, not merely state the fact.
    assert any(word in message for word in ("shift", "narrow", "move"))


def test_the_builder_and_the_gate_agree_on_capacity():
    """deck_builder duplicates measure.py's arithmetic because it cannot import app.*.
    The two copies must return identical numbers or the builder's shrink-to-fit and the
    gate's overflow rule will disagree about the same box."""
    from app.genai.ppt import measure

    for width, height, size in ((6.0, 3.0, 15), (3.6, 1.8, 11), (12.5, 0.4, 28)):
        per_line, lines = db._capacity(width, height, size, bullets=False)
        assert per_line == measure.chars_per_line(width, size, bullets=False)
        assert lines == measure.line_capacity(height, size)

    for text in ("Preheat ramp under three degrees per second", "WWWWWWWWWW", "iiii", ""):
        assert db._string_em(text) == pytest.approx(measure.string_em(text))
        assert db._rendered_lines([text], 6.0, 15, bullets=False) == (
            measure.rendered_lines([text], 6.0, 15, bullets=False))


# --------------------------------------------------------------------------- #
# reading a real slide, and rules 8-10                                         #
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def cat():
    return catalog_mod.load()


@pytest.fixture
def build(cat, tmp_path):
    """Build a one-slide deck with DeckBuilder and hand the gate the result."""
    def _build(layout_key, populate, frame=None):
        deck = db.DeckBuilder(registry.template_path(), cat.to_dict(), frame=frame)
        handle = deck.add_slide(layout_key)
        populate(handle)
        out = deck.save(tmp_path / f"{layout_key}.pptx")
        return out, cat.get(layout_key)
    return _build


def test_a_clean_deckbuilder_slide_passes_the_whole_gate(build, cat):
    def populate(slide):
        slide.set_title("Reflow profile control")
        slide.add_card("Preheat", ["Ramp under 3 C/s"], *slide.grid(0, 1, cols=3, rows=2))
        slide.add_card("Soak", ["150-180 C for 90 s"], *slide.grid(4, 1, cols=3, rows=2))
        slide.add_card("Reflow", ["Peak 245 C"], *slide.grid(8, 1, cols=3, rows=2))

    out, layout = build("M1-L10", populate)
    found = geo.check_slide(out, layout, theme_colors=cat.theme.colors)
    assert [v.message for v in found if v.blocking] == []


def test_the_reader_sees_text_fills_and_placeholders(build, cat):
    def populate(slide):
        slide.set_title("A title")
        slide.add_shape("rectangle", 1.0, 3.0, 2.0, 1.0, fill="red")
        slide.add_textbox("free text", 5.0, 3.0, 2.0, 0.5)

    out, layout = build("M1-L10", populate)
    slide = next(iter(Presentation(str(out)).slides))
    boxes = geo.boxes_from_slide(slide, layout)

    assert any(b.has_text and "A title" in b.text for b in boxes)
    assert any(b.filled and not b.has_text for b in boxes)
    assert any(b.is_placeholder for b in boxes)
    for b in boxes:
        assert b.width > 0 and b.height > 0


def test_an_overlapping_pair_of_cards_is_caught_on_a_real_slide(build, cat):
    def populate(slide):
        slide.set_title("Overlapping by construction")
        slide.add_card("A", ["one"], 1.0, 2.5, 4.0, 2.0)
        slide.add_card("B", ["two"], 3.0, 2.5, 4.0, 2.0)      # 2in of overlap

    out, layout = build("M1-L10", populate)
    found = geo.check_slide(out, layout, theme_colors=cat.theme.colors)
    assert 2 in rules(found) or 4 in rules(found)


def test_a_shape_in_the_footer_band_is_caught_on_a_real_slide(build, cat):
    frame = db.frame_spec(13.333, 7.5, label="Internal", slide_number=1)

    def populate(slide):
        slide.set_title("T")
        slide.add_shape("rectangle", 2.0, 7.1, 3.0, 0.3, fill="red")

    out, layout = build("M2-L4", populate, frame=frame)
    found = geo.check_slide(out, layout, frame=frame, theme_colors=cat.theme.colors)
    assert 5 in rules(found)


# --- rule 8: colours ------------------------------------------------------- #

def test_theme_and_brand_colours_pass_rule_8(cat):
    palette = db.brand_palette(cat.theme.colors)
    clean = [box("a", 1, 1, 2, 1, filled=True, colors=(palette["red"],)),
             box("b", 4, 1, 2, 1, filled=True, colors=(palette["navy"],))]
    assert geo.check_colors(clean, palette) == []


def test_an_off_palette_colour_is_rule_8(cat):
    palette = db.brand_palette(cat.theme.colors)
    found = geo.check_colors(
        [box("a", 1, 1, 2, 1, filled=True, colors=("7F00FF",))], palette)
    assert rules(found) == [8]
    assert "7F00FF" in found[0].message


# --- rule 9: fonts and the type scale -------------------------------------- #

def test_theme_font_at_a_scale_size_passes_rule_9():
    clean = [box("a", 1, 1, 2, 1, has_text=True, text="x",
                 font_name="Barlow", font_size=15)]
    assert geo.check_typography(clean, theme_font="Barlow") == []


def test_an_off_scale_size_is_rule_9():
    found = geo.check_typography(
        [box("a", 1, 1, 2, 1, has_text=True, text="x",
             font_name="Barlow", font_size=13)], theme_font="Barlow")
    assert rules(found) == [9]
    assert "13" in found[0].message


def test_a_placeholder_inherited_size_is_exempt_from_rule_9():
    exempt = [box("a", 1, 1, 2, 1, has_text=True, text="x", font_name="Barlow",
                  font_size=13, is_placeholder=True, inherited_size=True)]
    assert geo.check_typography(exempt, theme_font="Barlow") == []


def test_a_shrunk_placeholder_is_exempt_even_though_its_size_is_explicit():
    """DeckBuilder's shrink-to-fit decrements 1pt at a time from the placeholder's own
    base, so it writes explicit off-scale sizes like 13pt or 17pt. That is correct
    behaviour - the spec exempts placeholder text as such, not merely un-set text - and
    rule 9 must not fire on it, or every shrunk placeholder becomes a violation."""
    for size in (13, 16, 17, 19):
        shrunk = [box("a", 1, 1, 2, 1, has_text=True, text="x", font_name="Barlow",
                      font_size=size, is_placeholder=True, inherited_size=True)]
        assert geo.check_typography(shrunk, theme_font="Barlow") == []


def test_a_component_box_is_not_exempt_even_next_to_a_placeholder():
    mixed = [box("ph", 1, 1, 2, 1, has_text=True, text="x", font_name="Barlow",
                 font_size=13, is_placeholder=True, inherited_size=True),
             box("card", 4, 1, 2, 1, has_text=True, text="y", font_name="Barlow",
                 font_size=13)]
    found = geo.check_typography(mixed, theme_font="Barlow")
    assert [v.rule for v in found] == [9]
    assert "card" in found[0].message


def test_a_foreign_font_is_rule_9():
    found = geo.check_typography(
        [box("a", 1, 1, 2, 1, has_text=True, text="x",
             font_name="Comic Sans MS", font_size=15)], theme_font="Barlow")
    assert rules(found) == [9]
    assert "Barlow" in found[0].message


# --- rule 10: overflow, advisory until it is wide -------------------------- #

def test_text_that_fits_raises_nothing():
    fits = [box("a", 1, 1, 6, 2, has_text=True, text="A short line.", font_size=15)]
    assert geo.check_overflow(fits) == []


def test_mild_overflow_is_advisory_not_blocking():
    mild = [box("a", 1, 1, 6, 0.55, has_text=True, font_size=15,
                text="A line of copy that needs a little more room than it has here.")]
    found = geo.check_overflow(mild)
    assert found and all(v.rule == 10 for v in found)
    assert not any(v.blocking for v in found)


def test_wide_overflow_blocks():
    wide = [box("a", 1, 1, 3, 0.4, has_text=True, font_size=15, text="word " * 200)]
    found = geo.check_overflow(wide)
    assert found and any(v.blocking for v in found)
    assert "shorten" in found[0].message.lower()


def test_is_clean_ignores_advisory_violations():
    advisory = [geo.Violation(10, "overflow", "a bit tight", blocking=False)]
    assert geo.is_clean(advisory)
    assert not geo.is_clean(advisory + [geo.Violation(2, "text-over-text", "no", True)])


def test_the_formatted_report_is_ordered_and_numbered():
    found = [geo.Violation(5, "reserved", "footer"), geo.Violation(2, "t", "overlap")]
    report = geo.format_violations(found)
    assert report.index("overlap") < report.index("footer")     # rule order
    assert "rule 2" in report and "rule 5" in report


def test_a_file_with_no_slide_in_it_is_reported_not_raised(cat, tmp_path):
    """A program that saved an empty deck must be repairable, not fatal."""
    deck = db.DeckBuilder(registry.template_path(), cat.to_dict())
    empty = deck.save(tmp_path / "empty.pptx")
    found = geo.check_slide(empty, cat.get("M1-L10"))
    assert found and not geo.is_clean(found)


def test_the_frame_the_builder_stamps_does_not_fail_its_own_gate(build, cat):
    """The classification band sits flush to the bottom edge, inside the zone rule 5
    protects — by design, from one code path. Measuring it as if the program had drawn
    it made rules 1 and 5 fire on every framed slide in every deck."""
    frame = db.frame_spec(13.333, 7.5, label="Internal",
                          symbol_path=str(registry.classification_symbol("internal")),
                          logo_path=str(registry.logo_path("d2r_mark.png")),
                          slide_number=4)

    out, layout = build("M2-L4", lambda s: s.set_title("Framed"), frame=frame)
    found = geo.check_slide(out, layout, frame=frame, theme_colors=cat.theme.colors)
    assert geo.is_clean(found), geo.format_violations(found)


def test_content_in_the_footer_band_is_still_caught_on_a_framed_slide(build, cat):
    """Exempting the frame must not exempt the band."""
    frame = db.frame_spec(13.333, 7.5, label="Internal", slide_number=4)

    def populate(slide):
        slide.set_title("T")
        slide.add_card("A", ["one"], 3.0, 6.9, 4.0, 0.5)

    out, layout = build("M2-L4", populate, frame=frame)
    found = geo.check_slide(out, layout, frame=frame, theme_colors=cat.theme.colors)
    assert 5 in rules(found)


def test_a_full_bleed_background_is_not_a_reserved_zone(cat):
    """Ten layouts open with a full-bleed background picture. A background is
    something you compose ON TOP OF; reserving it would make the layout unusable for
    anything at all, including its own title."""
    cover = next(l for l in cat.layouts
                 if any(r.width * r.height >= 0.8 * cat.slide_width * cat.slide_height
                        for r in l.decor_rects))
    frame = db.frame_spec(cat.slide_width, cat.slide_height, label="Internal")
    reserved = db.reserved_rects(frame, cover, cat.slide_width, cat.slide_height)
    area = cat.slide_width * cat.slide_height
    assert not any(r[2] * r[3] >= 0.8 * area for r in reserved)


def test_a_layouts_own_placeholder_is_not_measured_against_its_own_decor():
    """The program fills placeholders, it cannot move them. On 89 of 181 layouts a
    placeholder overlaps the layout's own decor, and a violation the model has no way
    to fix is a repair round it can only lose."""
    decor = [0.23, 0.28, 0.65, 0.71]
    inherited = box("title", 0.3, 0.3, 6.0, 1.0, has_text=True, text="T",
                    is_placeholder=True)
    added = box("card", 0.3, 0.3, 2.0, 1.0, filled=True)
    assert 5 not in rules(check([inherited], reserved=[decor]))
    assert 5 in rules(check([added], reserved=[decor]))


def test_the_templates_own_arrangement_is_never_a_violation():
    """63 of 181 layouts put a placeholder outside the margin and 29 overlap two of
    their own. The program cannot move a placeholder, so none of that is its to fix."""
    pair = [box("title", 0.10, 0.10, 8.0, 1.0, has_text=True, text="T", filled=True,
                is_placeholder=True),
            box("body", 0.10, 0.60, 8.0, 3.0, has_text=True, text="B", filled=True,
                is_placeholder=True)]
    assert check(pair) == []


def test_program_text_over_a_placeholder_is_still_reported():
    """Only placeholder-against-placeholder is exempt: the model can always move its
    own box."""
    found = check([box("title", 1.0, 1.0, 8.0, 1.0, has_text=True, text="T",
                       is_placeholder=True),
                   box("mine", 2.0, 1.0, 4.0, 1.0, has_text=True, text="M")])
    assert 2 in rules(found)


def test_a_program_shape_outside_the_canvas_is_still_rule_1():
    assert 1 in rules(check([box("mine", 12.0, 1.0, 4.0, 1.0)]))


# --- rules 11-15: a lean slide ---------------------------------------------- #

def test_an_empty_added_text_box_is_rule_11():
    empty = box("TextBox 3", 1.0, 1.0, 3.0, 0.5, is_textbox=True)
    assert rules(geo.check_content([empty])) == [11]
    filled_panel = box("TextBox 4", 1.0, 2.0, 3.0, 0.5, is_textbox=True, filled=True)
    assert geo.check_content([filled_panel]) == []


def test_the_same_line_in_two_shapes_is_rule_12():
    title = box("Title 1", 1.0, 0.4, 10.0, 0.6, has_text=True, text="Torque sequence basics",
                paragraphs=("Torque sequence basics",))
    restated = box("TextBox 5", 1.0, 1.2, 10.0, 0.4, has_text=True,
                   text="torque  sequence basics", paragraphs=("torque  sequence basics",))
    assert rules(geo.check_content([title, restated])) == [12]
    short = box("TextBox 6", 1.0, 2.0, 2.0, 0.4, has_text=True, text="Step 1",
                paragraphs=("Step 1",))
    again = box("TextBox 7", 4.0, 2.0, 2.0, 0.4, has_text=True, text="Step 1",
                paragraphs=("Step 1",))
    assert geo.check_content([short, again]) == []     # short labels may repeat


def test_an_image_placed_twice_or_stretched_is_rules_13_and_14():
    first = box("Picture 2", 1.0, 1.0, 3.0, 2.0, is_picture=True, image_key="abc")
    second = box("Picture 3", 5.0, 1.0, 3.0, 2.0, is_picture=True, image_key="abc",
                 aspect_error=0.3)
    assert rules(geo.check_images([first, second])) == [13, 14]


def test_clutter_is_advisory_and_relative_to_content():
    busy = [{"kind": "shape", "content": False}] * 6 + [{"kind": "card", "content": True}]
    found = geo.check_clutter(busy, placeholders_filled=1)
    assert rules(found) == [15] and not found[0].blocking
    assert geo.check_clutter([{"kind": "line", "content": False}] * 3) == []


def test_a_builder_slide_with_one_image_twice_is_caught_on_the_real_file(tmp_path):
    from PIL import Image

    image = tmp_path / "panel.png"
    Image.new("RGB", (400, 200), "white").save(image)
    catalog = catalog_mod.load()
    deck = db.DeckBuilder(registry.template_path(), catalog.to_dict())
    slide = deck.add_slide("M1-L10")
    slide.set_title("Panel")
    slide.add_image(str(image), 1.0, 2.0, 4.0, 2.0)
    slide.add_image(str(image), 6.0, 2.0, 4.0, 2.0)
    out = deck.save(tmp_path / "twice.pptx")
    found = geo.check_slide(out, catalog.resolve_layout("M1-L10"),
                            theme_colors=catalog.theme.colors)
    assert 13 in rules(found) and 14 not in rules(found)

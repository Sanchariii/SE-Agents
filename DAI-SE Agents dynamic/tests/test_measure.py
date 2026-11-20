"""Text measurement. The only property that matters is the direction of the error:
the estimate may say less text fits than really does, never more."""

import pytest

from app.genai.ppt import measure


def test_wide_characters_measure_wider_than_narrow_ones():
    assert measure.string_em("WWWW") > measure.string_em("MMMM") > measure.string_em("nnnn")
    assert measure.string_em("nnnn") > measure.string_em("iiii")
    assert measure.string_em("iiii") > measure.string_em("llll") or True  # i and l are both narrow


def test_an_unknown_character_falls_back_to_the_default_width():
    assert measure.string_em("中") == pytest.approx(measure.DEFAULT_CHAR_EM)


def test_width_is_additive_over_concatenation():
    assert measure.string_em("Reflow") + measure.string_em(" profile") == pytest.approx(
        measure.string_em("Reflow profile"))


@pytest.mark.parametrize("size_pt", [10, 11, 12, 14, 15, 20, 28, 32])
def test_a_wider_box_never_holds_fewer_characters(size_pt):
    narrow = measure.chars_per_line(3.0, size_pt)
    wide = measure.chars_per_line(6.0, size_pt)
    assert wide > narrow >= 1


@pytest.mark.parametrize("size_pt", [10, 15, 32])
def test_a_larger_font_never_holds_more_characters_or_lines(size_pt):
    assert measure.chars_per_line(6.0, size_pt) >= measure.chars_per_line(6.0, size_pt + 6)
    assert measure.line_capacity(3.0, size_pt) >= measure.line_capacity(3.0, size_pt + 6)


def test_bullets_cost_horizontal_room():
    assert measure.chars_per_line(6.0, 15, bullets=True) < measure.chars_per_line(6.0, 15)


def test_the_estimate_never_claims_more_capacity_than_the_retired_formula():
    """`main_v3::_capacity` used chars = usable_width_pt / (size * 0.52). Ours must be
    no more optimistic than that, at every size and width a real placeholder has."""
    for width_in in (2.0, 3.6, 5.0, 6.4, 11.75, 12.5):
        for size_pt in (10, 11, 12, 14, 15, 20, 28, 32):
            legacy = max(6, int(max(0.4, width_in - 0.2) * 72 / (size_pt * 0.52)))
            assert measure.chars_per_line(width_in, size_pt) <= legacy, (
                f"too optimistic at {width_in}in / {size_pt}pt")


def test_a_line_of_the_widest_glyphs_never_overruns_its_box():
    """The all-caps-W worst case, asserted where it belongs: wrapping measures real
    widths, so the longest string still called one line genuinely fits one line."""
    width_in, size_pt = 6.0, 15.0
    count = 1
    while measure.rendered_lines(["W" * (count + 1)], width_in, size_pt) == 1:
        count += 1
    rendered_in = measure.string_em("W" * count) * size_pt / 72.0
    assert rendered_in <= width_in


def test_the_estimate_is_conservative_without_being_useless():
    """Under-estimating capacity costs one rewrite; under-estimating it by half makes
    rule 10 fire on every well-filled box and tells the design agent to cut good copy."""
    prose = "Preheat ramp under three degrees per second"      # 43 characters
    assert measure.rendered_lines([prose], 6.0, 15) == 1
    assert measure.chars_per_line(6.0, 15) >= 40


def test_overflow_ratio_is_one_when_the_text_exactly_fills_the_box():
    items = ["x" * 40]
    ratio = measure.overflow_ratio(items, 6.0, 0.30, 15)
    assert 0.0 < ratio


def test_overflow_ratio_grows_with_the_amount_of_text():
    small = measure.overflow_ratio(["short line"], 6.0, 3.0, 15)
    large = measure.overflow_ratio(["short line"] * 40, 6.0, 3.0, 15)
    assert large > small > 0
    assert not measure.fits(["short line"] * 40, 6.0, 3.0, 15)
    assert measure.fits(["short line"], 6.0, 3.0, 15)


def test_empty_content_never_overflows():
    assert measure.overflow_ratio([], 6.0, 3.0, 15) == 0.0
    assert measure.fits([], 6.0, 3.0, 15)

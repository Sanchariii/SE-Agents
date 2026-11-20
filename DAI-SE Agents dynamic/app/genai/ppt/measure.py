"""How much text fits in a box, without a font file.

Decision 7: measuring text properly needs real font metrics, and vendoring Barlow plus
`fonttools` was declined. What is left is a per-character advance-width table, and the
only thing that makes such a table safe is the direction of its error: every width here
is rounded UP, so the estimate says less text fits than really does. An over-estimate of
capacity ships an overflowing slide; an under-estimate costs the model one rewrite.

The values are em widths for Barlow at weight 400, grouped by character class rather
than measured per glyph — see `scripts/calibrate_barlow.py` for how to regenerate them
from real metrics on a machine that has the font and `fonttools`.
"""

import math
from typing import Sequence

from app.config.constants import GeometryConstants
from app.utilities.logger_util import get_logger

# These functions run per shape per gate pass, several hundred times for one deck, so
# everything here logs at DEBUG and nothing logs per character. Turn the module to
# DEBUG when a box's capacity is the thing under suspicion.
log = get_logger(__name__)

# Em widths, rounded up from Barlow's advance widths. Grouped by class: within a class
# the widest member's width is used for every member, which is where the conservatism
# comes from.
_CLASSES: tuple[tuple[str, float], ...] = (
    ("il|!.,;:'`", 0.30),
    ("jft()[]{}/\\-", 0.38),
    ("rIJ ", 0.42),
    ("abcdeghknopqsuvxyz1234567890", 0.56),
    ("wLTZEFSCXYPBRKANDGHUVQOM", 0.68),
    ("m", 0.86),
    ("W", 0.94),
)

#: Width used for any character not in the table (CJK, emoji, box drawing).
DEFAULT_CHAR_EM = 0.72

CHAR_EM: dict[str, float] = {
    char: width for chars, width in _CLASSES for char in chars
}

#: Vertical advance as a multiple of the font size. Barlow's default line spacing is
#: ~1.2; 1.25 leaves room for the descender clipping PowerPoint applies.
LINE_HEIGHT_FACTOR = 1.25
#: Internal text-frame insets python-pptx applies by default, both sides, in inches.
_SIDE_INSET_IN = 0.20
_VERTICAL_INSET_IN = 0.10
#: Horizontal room a bullet glyph and its hanging indent take, in inches.
_BULLET_INDENT_IN = 0.22


def string_em(text: str, bold: bool = False) -> float:
    """Advance width of `text` in em, over-estimating rather than under-estimating.

    Bold text is charged `GeometryConstants.BOLD_WIDTH_FACTOR` more width.
    """
    total = 0.0
    unknown = 0
    for char in text:
        width = CHAR_EM.get(char)
        if width is None:
            width, unknown = DEFAULT_CHAR_EM, unknown + 1
        total += width
    if unknown:
        # Every CJK glyph, emoji or box-drawing character is charged the same default
        # width. A string that is mostly unknown is measured badly, and a surprising
        # overflow verdict usually starts here.
        log.debug("%d of %d characters are outside the width table (%.0f%%): %r",
                  unknown, len(text), 100.0 * unknown / max(len(text), 1), text[:60])
    return total * GeometryConstants.BOLD_WIDTH_FACTOR if bold else total


def chars_per_line(width_in: float, size_pt: float, bullets: bool = False) -> int:
    """Roughly how many characters of ordinary copy fit on one line.

    A coarse hint, used for the catalog's per-placeholder capacity display. Wrapping
    is done by `rendered_lines`, which measures real string widths — this uses the
    lowercase class width (0.56 em) because real copy is mostly lowercase, and a
    global mean would read as optimistic for prose.
    """
    usable_in = max(0.4, width_in - _SIDE_INSET_IN - (_BULLET_INDENT_IN if bullets else 0))
    em_per_in = 72.0 / max(size_pt, 1.0)
    # 0.56 is the lowercase class width; the table never rounds a lowercase glyph below
    # it. The worst case of all-wide glyphs is not this function's concern — that is
    # handled where it belongs, in rendered_lines, by measuring the real string width.
    count = max(1, int(usable_in * em_per_in / 0.56))
    if width_in - _SIDE_INSET_IN < 0.4:
        log.debug("box is only %.2fin wide, narrower than the %.2fin insets; capacity "
                  "clamped to the 0.4in floor", width_in, _SIDE_INSET_IN)
    log.debug("chars_per_line: %.2fin at %.1fpt (bullets=%s) -> %d",
              width_in, size_pt, bullets, count)
    return count


def line_capacity(height_in: float, size_pt: float) -> int:
    """How many lines of `size_pt` text a box `height_in` inches tall holds."""
    usable_in = max(0.05, height_in - _VERTICAL_INSET_IN)
    lines = max(1, int(usable_in * 72.0 / (max(size_pt, 1.0) * LINE_HEIGHT_FACTOR)))
    log.debug("line_capacity: %.2fin at %.1fpt -> %d line(s)",
              height_in, size_pt, lines)
    return lines


def rendered_lines(items: Sequence[str], width_in: float, size_pt: float,
                    bullets: bool = False, bold: bool = False) -> int:
    """How many lines `items` wrap to, measuring each string's real width.

    Wrapping measures the actual string rather than counting characters against an
    average, so a line of wide glyphs is estimated as wide as it really is. That is
    where the conservatism belongs: `string_em` already rounds every glyph up.
    """
    usable_in = max(0.4, width_in - _SIDE_INSET_IN
                    - (_BULLET_INDENT_IN if bullets else 0))
    em_per_line = usable_in * 72.0 / max(size_pt, 1.0)
    total = 0
    for item in items:
        text = str(item)
        if not text.strip():
            continue
        total += max(1, math.ceil(string_em(text, bold) / em_per_line))
    log.debug("rendered_lines: %d item(s) in %.2fin at %.1fpt (bullets=%s) -> %d line(s)",
              len(items), width_in, size_pt, bullets, total)
    return total


def overflow_ratio(items: Sequence[str], width_in: float, height_in: float,
                    size_pt: float, bullets: bool = False, bold: bool = False) -> float:
    """Height the wrapped text needs, over the height the box has. 0.0 for no content.

    Measured against the box's real height rather than against `line_capacity`, which
    floors. Flooring makes the ratio jump in whole lines, so a box that holds 1.7 lines
    and needs 2 reports 2.0 - past the blocking threshold - when it is only 16% over.
    Rule 10 is advisory precisely so a heuristic costs tokens rather than correctness,
    and on a one- or two-line box (a card title, a caption, a stat label) the integer
    form made the advisory band unreachable and every hairline overflow blocking.

    `fits()` is unaffected: for an integer line count, `needed > capacity` and
    `needed_height > usable_height` are the same test.

    Returns:
        1.0 means exactly full; 1.5 means half again as much text as there is room for.
    """
    needed = rendered_lines(items, width_in, size_pt, bullets, bold)
    if not needed:
        log.debug("overflow_ratio: nothing to measure in a %.2fx%.2fin box",
                  width_in, height_in)
        return 0.0
    needed_in = needed * max(size_pt, 1.0) * LINE_HEIGHT_FACTOR / 72.0
    usable_in = max(0.05, height_in - _VERTICAL_INSET_IN)
    ratio = needed_in / usable_in
    log.debug("overflow_ratio: %d line(s) need %.2fin, box gives %.2fin -> %.2f",
              needed, needed_in, usable_in, ratio)
    return ratio


def fits(items: Sequence[str], width_in: float, height_in: float, size_pt: float,
         bullets: bool = False) -> bool:
    """True when `items` are estimated to fit without shrinking."""
    ratio = overflow_ratio(items, width_in, height_in, size_pt, bullets)
    verdict = ratio <= 1.0
    log.debug("fits: %.2fx%.2fin at %.1fpt -> %s (ratio %.2f)",
              width_in, height_in, size_pt, verdict, ratio)
    return verdict

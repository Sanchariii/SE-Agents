"""The constrained API every generated slide program is written against.

This module is the whole of the contract with model-written code. It is deliberately
SELF-CONTAINED: stdlib, python-pptx and lxml only, and no ``app.*`` import anywhere. That
is not a style preference - the file is copied verbatim into each sandbox slide
directory (see ``app/genai/ppt/sandbox.py``), so a single ``app.config``
import would break every generated slide. The same file is imported in-process by the
deterministic fallback renderer and by the tests, so there is exactly one copy of this
API and it cannot drift.

Two rules follow from that isolation:
  * the catalog arrives as a plain dict (``TemplateCatalog.to_dict()``), never as objects;
  * anything this module cannot resolve is appended to ``warnings`` and reported, never
    logged, because there is no logger on the other side of the sandbox boundary.

Colours are token names. :func:`to_rgb` refuses a hex literal outright, so Decision 4
holds even for a program that slips past the AST guard.

A third rule is just as load-bearing: the ``add_*`` methods clamp nothing and hide
nothing. A component asked for at 12.8in on a 13.33in canvas is placed at 12.8in. The
geometry gate is what reports that, and quietly nudging the shape back on-canvas would
hide the very violation the repair loop exists to surface.
"""

import json
import logging
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

from lxml import etree
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE, PP_PLACEHOLDER
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

#: The stdlib logger, deliberately not `app.utilities.logger_util.get_logger`: that is
#: only a `logging.getLogger` wrapper, and this module may not import `app.*` because
#: it is copied into the sandbox. In-process it picks up the service's handlers like
#: any other module; inside the sandbox there are no handlers and the records go
#: nowhere, which is correct - the model-facing channel there is `warnings`, which the
#: repair loop reads back.
log = logging.getLogger(__name__)

EMU_PER_INCH = 914400

#: The closed set of sizes a component may set. Placeholder-inherited sizes are exempt
#: (the template's own choices are correct by definition); this is geometry rule 9.
TYPE_SCALE: Tuple[int, ...] = (10, 11, 12, 14, 15, 20, 28, 32)

#: Brand tokens. Values are defaults; `brand_palette` overrides the theme-backed ones
#: from the template so the deck follows the template, not this table.
BRAND_DEFAULTS: Dict[str, str] = {
    "red": "CD163F", "red_dark": "A8112F", "navy": "1B2A4A", "blue": "003E7E",
    "light_blue": "00AEEF", "orange": "F58024", "yellow": "F9C20A", "green": "77C14C",
    "body": "5A6470", "caption": "8A929E", "border": "D8DBDF", "surface": "F6F7F8",
    "white": "FFFFFF", "black": "000000", "red_light": "FEF2F2", "gray": "CCCCCC",
    "dark_gray": "4C4C4C",
}
THEME_TO_BRAND: Dict[str, str] = {
    "accent1": "red", "accent2": "blue", "accent3": "orange", "accent4": "light_blue",
    "accent5": "yellow", "accent6": "green",
    "lt1": "white", "dk1": "black", "dk2": "gray", "lt2": "dark_gray",
}

#: Quiz answer reveal. Mirrors QuizConstants (this module cannot import `app.config`):
#: SAMPLE.pptx "Quiz-N-Answered" fills the correct card with accent6 at lumMod 60% /
#: lumOff 40% and outlines it in accent6. `green_light` is derived from `green` the
#: same way in `brand_palette`.
ANSWER_FILL_TOKEN = "green_light"
ANSWER_LINE_TOKEN = "green"
_ANSWER_LUM_MOD, _ANSWER_LUM_OFF = 0.60, 0.40
#: Shapes `add_choice` draws are named with this prefix.
CHOICE_SHAPE_PREFIX = "choice-"
#: Sidecar `save()` writes beside the deck listing every choice drawn.
CHOICES_FILENAME = "choices.json"
#: Sidecar `save()` writes beside the deck: every warning, plus a ledger of the
#: components drawn (kind, and whether each carried content) for the clutter check.
REPORT_FILENAME = "warnings.json"
#: Component text is shrunk along the type scale to fit its box, never below this,
#: unless the caller itself asked for a smaller size.
COMPONENT_FLOOR_PT = 11
#: A picture slot crops an image to fill it. Past this fraction cut away, the image is
#: placed whole ("contain") in the slot's bounds instead, so a diagram keeps its edges.
_MAX_CROP_FRACTION = 0.15
#: Marker file the sandbox stages beside OUTPUT_PATH for an answer slide; its presence
#: switches the reveal on without the program having to know.
REVEAL_MARKER_FILENAME = "reveal_answers.flag"


def _lum_mod_off(hex_value: str, mod: float, off: float) -> str:
    """Apply DrawingML lumMod/lumOff to a colour, as PowerPoint does (HSL lightness).

    Args:
        hex_value: A 6-digit hex colour.
        mod: Lightness multiplier.
        off: Lightness offset.

    Returns:
        The adjusted colour as uppercase 6-digit hex.
    """
    import colorsys

    r, g, b = (int(hex_value[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    l = max(0.0, min(1.0, l * mod + off))
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return "".join(f"{round(c * 255):02X}" for c in (r, g, b))

_SHAPES = {
    "rectangle": MSO_SHAPE.RECTANGLE, "rounded_rectangle": MSO_SHAPE.ROUNDED_RECTANGLE,
    "oval": MSO_SHAPE.OVAL, "chevron": MSO_SHAPE.CHEVRON,
    "right_arrow": MSO_SHAPE.RIGHT_ARROW, "left_arrow": MSO_SHAPE.LEFT_ARROW,
    "up_arrow": MSO_SHAPE.UP_ARROW, "down_arrow": MSO_SHAPE.DOWN_ARROW,
    "pentagon": MSO_SHAPE.PENTAGON, "diamond": MSO_SHAPE.DIAMOND,
    "hexagon": MSO_SHAPE.HEXAGON, "triangle": MSO_SHAPE.ISOSCELES_TRIANGLE,
    "snip_rectangle": MSO_SHAPE.SNIP_2_SAME_RECTANGLE,
    "round_top_rectangle": MSO_SHAPE.ROUND_2_SAME_RECTANGLE,
}
_CHART_TYPES = {
    "column": XL_CHART_TYPE.COLUMN_CLUSTERED,
    "stacked_column": XL_CHART_TYPE.COLUMN_STACKED,
    "bar": XL_CHART_TYPE.BAR_CLUSTERED, "stacked_bar": XL_CHART_TYPE.BAR_STACKED,
    "line": XL_CHART_TYPE.LINE_MARKERS, "pie": XL_CHART_TYPE.PIE,
    "doughnut": XL_CHART_TYPE.DOUGHNUT, "area": XL_CHART_TYPE.AREA,
}
_ALIGN = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER,
          "right": PP_ALIGN.RIGHT, "justify": PP_ALIGN.JUSTIFY}
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")

#: Grid geometry, mirrored from GeometryConstants. Duplicated deliberately: this module
#: cannot reach `app.config`. `tests/test_geometry.py` (Task 5) asserts the two stay equal.
GRID_COLUMNS, GRID_ROWS, GRID_GUTTER_IN = 12, 6, 0.15

#: Fallback frame for a layout the catalog gave no free region for. Mirrors
#: GeometryConstants.FRAME_SIDE_MARGIN_IN (0.42, taken off both sides -> 0.84) and
#: FOOTER_BAND_HEIGHT_IN together with the header band (1.30 top + 0.45 footer = 1.75).
#: Literals rather than an import, for the same sandbox reason as the grid constants.
_FALLBACK_TOP_IN = 1.30
_FALLBACK_SIDE_MARGIN_IN = 0.42
_FALLBACK_VERTICAL_TRIM_IN = 1.75


def brand_palette(theme_colors: Optional[Dict[str, str]]) -> Dict[str, str]:
    """Brand token -> hex, with the theme-backed tokens taken from the template.

    Args:
        theme_colors: The `colors` block of one catalog theme (`accent1`, `lt1`, ...).
            An empty dict or None leaves every token at its `BRAND_DEFAULTS` value.

    Returns:
        A token -> uppercase 6-digit hex mapping. Tokens with no theme equivalent
        (`navy`, `body`, `border`, ...) keep their defaults.
    """
    palette = dict(BRAND_DEFAULTS)
    overridden = []
    for scheme_name, brand_name in THEME_TO_BRAND.items():
        value = (theme_colors or {}).get(scheme_name)
        if value and len(str(value)) == 6:
            palette[brand_name] = str(value).upper()
            overridden.append(brand_name)
    # The quiz answer tint follows whatever green the theme resolved to.
    palette[ANSWER_FILL_TOKEN] = _lum_mod_off(palette["green"], _ANSWER_LUM_MOD,
                                              _ANSWER_LUM_OFF)
    if not overridden:
        # Every token is a hardcoded default, so the deck is not following the
        # template's theme at all - and rule 8 will be checking against those
        # defaults rather than against the real brand colours.
        log.warning("no theme colours resolved from %d key(s); the palette is entirely "
                    "BRAND_DEFAULTS", len(theme_colors or {}))
    else:
        log.debug("palette: %d token(s), %d taken from the theme (%s)",
                  len(palette), len(overridden), ", ".join(sorted(overridden)))
    return palette


def to_rgb(color: Union[str, RGBColor, None],
           palette: Optional[Dict[str, str]] = None) -> RGBColor:
    """Resolve a brand or theme token to a colour.

    Unlike the reference implementation this refuses hex outright: Decision 4 says every
    colour comes from the template theme as a named token, and accepting `#CD163F` here
    would let a program drift off-palette while still looking correct.

    Args:
        color: A brand token (`"red"`, `"navy"`, `"light blue"`), an already-resolved
            `RGBColor`, or None for the default body colour.
        palette: The deck's resolved palette. Defaults to `BRAND_DEFAULTS`.

    Returns:
        The resolved `RGBColor`.

    Raises:
        ValueError: For a hex literal, or any name that is not a token.
    """
    palette = palette or BRAND_DEFAULTS
    if isinstance(color, RGBColor):
        return color
    if not color:
        return RGBColor.from_string(palette["body"])
    key = str(color).strip().lower().replace("-", "_").replace(" ", "_")
    if key in palette:
        return RGBColor.from_string(palette[key])
    if re.fullmatch(r"#?[0-9a-fA-F]{6}", str(color).strip()):
        log.error("refused the hex literal %r; only brand tokens resolve here", color)
        raise ValueError(
            f"literal colour {color!r} is not allowed; use a brand token "
            f"({', '.join(sorted(palette))})")
    log.error("unknown colour token %r; the palette has %d: %s",
              color, len(palette), ", ".join(sorted(palette)))
    raise ValueError(
        f"unknown colour token {color!r}; allowed: {', '.join(sorted(palette))}")


def _crop_fraction(path: Path, width: float, height: float) -> float:
    """How much of an image filling a `width` x `height` slot would be cropped away.

    Args:
        path: The image file.
        width: Slot width in inches.
        height: Slot height in inches.

    Returns:
        0.0 for a perfect aspect match, up to just under 1.0; 0.0 when the image
        cannot be read (the old crop-to-fill behaviour then applies).
    """
    try:
        from pptx.parts.image import Image as _PptxImage

        px_w, px_h = _PptxImage.from_file(str(path)).size
    except Exception as exc:                                 # noqa: BLE001
        log.debug("could not read the size of %s (%s); assuming no crop", path, exc)
        return 0.0
    if not px_w or not px_h or width <= 0 or height <= 0:
        return 0.0
    image, slot = px_w / px_h, width / height
    return 1.0 - min(image / slot, slot / image)


def _snap_size(size: float) -> int:
    """Nearest size on the type scale, never rounding up past the largest.

    Args:
        size: Any requested point size, on or off the scale.

    Returns:
        The closest `TYPE_SCALE` member; ties go to the smaller size, because
        overflowing is worse than reading slightly small.
    """
    snapped = min(TYPE_SCALE, key=lambda s: (abs(s - size), s))
    if snapped != size:
        log.debug("snapped %gpt to %dpt on the type scale", size, snapped)
    return snapped


# --------------------------------------------------------------------------------- #
# Text measurement
#
# Mirrors app/genai/ppt/measure.py. Duplicated because this module cannot reach `app/`;
# tests/test_geometry.py::test_the_builder_and_the_gate_agree_on_capacity (Task 7) pins
# them together so the two copies cannot diverge silently.
#
# Wrapping measures each string's real width rather than counting characters against an
# average: `_string_em` already rounds every glyph up, so that is where the conservatism
# belongs. `_CHAR_EM_LOWER` is only the coarse capacity hint.
# --------------------------------------------------------------------------------- #
_CHAR_EM_LOWER = 0.56
_LINE_HEIGHT_FACTOR = 1.25
_SIDE_INSET_IN, _VERTICAL_INSET_IN, _BULLET_INDENT_IN = 0.20, 0.10, 0.22
#: Bold glyphs are wider. Mirrors GeometryConstants.BOLD_WIDTH_FACTOR / measure.py.
_BOLD_WIDTH_FACTOR = 1.05

#: Copied verbatim from measure.CHAR_EM / measure.DEFAULT_CHAR_EM. Keep in step.
_CHAR_EM: Dict[str, float] = {}
_DEFAULT_CHAR_EM = 0.72
for _chars, _width in (("il|!.,;:'`", 0.30), ("jft()[]{}/\\-", 0.38), ("rIJ ", 0.42),
                       ("abcdeghknopqsuvxyz1234567890", 0.56),
                       ("wLTZEFSCXYPBRKANDGHUVQOM", 0.68), ("m", 0.86), ("W", 0.94)):
    for _char in _chars:
        _CHAR_EM[_char] = _width


def _string_em(text: str, bold: bool = False) -> float:
    """Advance width in em, over-estimating rather than under-estimating."""
    width = sum(_CHAR_EM.get(char, _DEFAULT_CHAR_EM) for char in text)
    return width * _BOLD_WIDTH_FACTOR if bold else width


def _capacity(width: float, height: float, size: float,
              bullets: bool = True) -> Tuple[int, int]:
    """Coarse (characters per line, lines) a box of this size holds."""
    usable_w = max(0.4, width - _SIDE_INSET_IN - (_BULLET_INDENT_IN if bullets else 0))
    usable_h = max(0.05, height - _VERTICAL_INSET_IN)
    return (max(1, int(usable_w * 72.0 / max(size, 1.0) / _CHAR_EM_LOWER)),
            max(1, int(usable_h * 72.0 / (max(size, 1.0) * _LINE_HEIGHT_FACTOR))))


def _rendered_lines(items: Sequence[str], width: float, size: float,
                    bullets: bool = True, bold: bool = False) -> int:
    """How many wrapped lines `items` really take, by measured string width."""
    usable_w = max(0.4, width - _SIDE_INSET_IN - (_BULLET_INDENT_IN if bullets else 0))
    em_per_line = usable_w * 72.0 / max(size, 1.0)
    return sum(max(1, math.ceil(_string_em(str(item), bold) / em_per_line))
               for item in items if str(item).strip())


def _text_fits(items: Sequence[str], width: float, height: float, size: float,
               bullets: bool = True, bold: bool = False) -> bool:
    """True when `items` are estimated to fit the box without shrinking."""
    _, lines = _capacity(width, height, size, bullets)
    return _rendered_lines(items, width, size, bullets, bold) <= lines


def _text_height(items: Sequence[str], width: float, size: float,
                 bullets: bool = False, bold: bool = False) -> float:
    """Inches of box height `items` need at `size`, insets included."""
    lines = _rendered_lines(items, width, size, bullets, bold)
    return lines * max(size, 1.0) * _LINE_HEIGHT_FACTOR / 72.0 + _VERTICAL_INSET_IN


# --------------------------------------------------------------------------------- #
# The frame: the brand furniture every slide carries
#
# Decision 3: the classification is rendered, from one code path, into a reserved band.
# Decision 6 and gate rule 5: the agent never draws there. `frame_spec` computes the
# rectangles and `SlideHandle._apply_frame` fills exactly those same rectangles, which
# is what lets the gate protect a zone it knows the frame is going to occupy.
# --------------------------------------------------------------------------------- #
#: Mirrored from GeometryConstants; literals rather than an import, for the same
#: sandbox reason as the grid constants above.
FOOTER_BAND_HEIGHT_IN = 0.45
FRAME_SIDE_MARGIN_IN = 0.42
CLASSIFICATION_SYMBOL_IN = 0.22
CLASSIFICATION_LABEL_WIDTH_IN = 1.60
LOGO_WIDTH_IN = 0.79
_LOGO_TOP_IN = 0.28
_SLIDE_NUMBER_WIDTH_IN = 0.60
_PNG_MAGIC = bytes((0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A))

#: The 16:9 canvas, used only when nothing else says otherwise.
DEFAULT_SLIDE_WIDTH_IN, DEFAULT_SLIDE_HEIGHT_IN = 13.333, 7.5

#: A decor shape covering at least this fraction of the slide is its background, not an
#: obstacle. See `reserved_rects`.
BACKGROUND_DECOR_AREA_RATIO = 0.80

#: A decor shape covering at least this fraction of a layout's FREE REGION is the panel
#: that region sits on, not an obstacle inside it. Mirrors
#: `GeometryConstants.CANVAS_PANEL_COVERAGE`, which `catalog._canvas_region` uses to
#: build the region; the two must stay equal or the gate and the brief disagree about
#: where a slide may be composed. Duplicated rather than imported because this module
#: is copied into the sandbox verbatim and may import nothing from `app`.
CANVAS_PANEL_COVERAGE = 0.90

#: Every shape `_apply_frame` stamps is named with this prefix. The geometry gate reads
#: it to tell brand furniture from the program's own output: the furniture sits in the
#: footer band by design, which rule 1 (0.30in margins) and rule 5 (keep out of the
#: reserved zones) would otherwise report as violations on every single slide. It is
#: exported rather than duplicated so the two modules cannot drift.
FRAME_SHAPE_PREFIX = "frame:"


def logo_variant_for(background_luminance: float) -> str:
    """Which logo variant reads on this background. Mechanical, not the model's guess.

    Args:
        background_luminance: 0.0 (black) to 1.0 (white), from the catalog.

    Returns:
        "white" on a dark background, "color" otherwise.
    """
    return "white" if background_luminance < 0.5 else "color"


def _image_aspect(path: Optional[str]) -> float:
    """width / height of a PNG, read from its IHDR chunk. 1.0 when unreadable.

    Done by hand rather than with Pillow: this module is copied into the sandbox and
    may import nothing but the stdlib, python-pptx and lxml.

    Args:
        path: The PNG to measure, or None.

    Returns:
        The aspect ratio, or 1.0 when the file is missing, truncated or not a PNG.
    """
    if not path:
        return 1.0
    try:
        with open(str(path), "rb") as handle:
            header = handle.read(24)
    except OSError as exc:
        log.warning("cannot read %s to measure it (%s); assuming square, which "
                    "over-reserves the corner", path, exc)
        return 1.0
    if len(header) < 24 or header[:8] != _PNG_MAGIC:
        log.warning("%s is not a readable PNG header; assuming square", path)
        return 1.0
    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    if not (width and height):
        log.warning("%s reports a zero dimension (%dx%d); assuming square",
                    path, width, height)
        return 1.0
    log.debug("%s is %dx%d px, aspect %.2f", Path(str(path)).name, width, height,
              width / height)
    return width / height


def frame_spec(slide_width: float, slide_height: float, *, label: str,
               symbol_path: Optional[str] = None, logo_path: Optional[str] = None,
               logo_variant: str = "color",
               slide_number: Optional[int] = None) -> Dict[str, Any]:
    """The brand furniture every slide carries, as rectangles.

    Computed, not drawn, so the geometry gate can protect exactly the rectangles
    `finalize()` later fills (Decision 3). Everything is in inches.

    Args:
        slide_width: Canvas width.
        slide_height: Canvas height.
        label: The classification as rendered, e.g. "Internal".
        symbol_path: Policy symbol PNG, or None when the brand pack has none.
        logo_path: The brand mark to place, or None to place none.
        logo_variant: "color" or "white"; recorded so the gate can report mismatches.
        slide_number: Printed at the right of the footer band.

    Returns:
        The frame block: the footer band, the classification symbol and label, the logo
        and the slide number, each with its [left, top, width, height].
    """
    band_h = FOOTER_BAND_HEIGHT_IN
    side = FRAME_SIDE_MARGIN_IN
    sym = CLASSIFICATION_SYMBOL_IN
    logo_w = LOGO_WIDTH_IN
    band_top = slide_height - band_h
    sym_top = band_top + (band_h - sym) / 2
    # The logo is placed by width alone, so its height follows from the file's own
    # aspect ratio. Reserving a square here would block a half-inch strip of slide that
    # is actually free - the brand marks are wide (d2r_mark is 2.63:1, the full RA
    # lockup 3.42:1), so the real mark is about 0.30in tall, not 0.79in.
    logo_h = logo_w / _image_aspect(logo_path) if logo_path else logo_w
    log.debug("frame for %r on a %.2fx%.2fin canvas: band at y=%.2f, slide number %s, "
              "symbol=%s, logo=%s (%.2fx%.2fin)", label, slide_width, slide_height,
              band_top, slide_number, bool(symbol_path), bool(logo_path), logo_w,
              logo_h)
    return {
        "footer_band": [0.0, band_top, slide_width, band_h],
        "classification": {
            "label_text": label,
            "symbol_path": symbol_path,
            "symbol": [side, sym_top, sym, sym],
            "label": [side + sym + 0.10, band_top, CLASSIFICATION_LABEL_WIDTH_IN,
                      band_h],
        },
        "logo": None if not logo_path else {
            "path": str(logo_path), "variant": logo_variant,
            "rect": [side, _LOGO_TOP_IN, logo_w, round(logo_h, 3)],
        },
        "slide_number": {
            "value": slide_number,
            "rect": [slide_width - side - _SLIDE_NUMBER_WIDTH_IN, band_top,
                     _SLIDE_NUMBER_WIDTH_IN, band_h],
        },
    }


def rect_of(value: Any) -> Optional[List[float]]:
    """[left, top, width, height] from a sequence, a mapping or an object.

    The same rectangle arrives in three shapes depending on who is asking: the catalog's
    `Rect` dataclass in-process, a plain list out of `to_dict()`, and a placeholder dict
    inside the sandbox. Normalising here is what lets `reserved_rects` take a
    `LayoutInfo` and a `LayoutView` interchangeably.

    Args:
        value: Whatever is holding the rectangle.

    Returns:
        The four floats, or None when `value` is not a rectangle at all.
    """
    if value is None:
        return None
    if hasattr(value, "as_tuple"):
        return [float(v) for v in value.as_tuple()]
    if isinstance(value, dict):
        try:
            return [float(value["left"]), float(value["top"]),
                    float(value["width"]), float(value["height"])]
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(value, (list, tuple)) and len(value) >= 4:
        try:
            return [float(v) for v in value[:4]]
        except (TypeError, ValueError):
            return None
    if all(hasattr(value, attr) for attr in ("left", "top", "width", "height")):
        try:
            return [float(value.left), float(value.top),
                    float(value.width), float(value.height)]
        except (TypeError, ValueError):
            return None
    return None


def _rects_overlap(a: Sequence[float], b: Sequence[float],
                   tolerance: float = 0.02) -> bool:
    """True when two [left, top, width, height] rectangles overlap on both axes."""
    return (min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]) > tolerance
            and min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]) > tolerance)


def _covers(rect: Sequence[float], region: Sequence[float]) -> float:
    """How much of `region` lies inside `rect`, as a fraction of `region`'s area.

    Mirrors `catalog.covers`; see `CANVAS_PANEL_COVERAGE` for why it is duplicated
    rather than imported.

    Args:
        rect: The covering [left, top, width, height].
        region: The [left, top, width, height] being covered.

    Returns:
        0.0 when they do not overlap, 1.0 when `region` sits entirely inside `rect`.
    """
    area = region[2] * region[3]
    if area <= 0:
        return 0.0
    wide = min(rect[0] + rect[2], region[0] + region[2]) - max(rect[0], region[0])
    tall = min(rect[1] + rect[3], region[1] + region[3]) - max(rect[1], region[1])
    return (wide * tall) / area if wide > 0 and tall > 0 else 0.0


def logo_is_placeable(frame: Optional[Dict[str, Any]], layout: Any) -> bool:
    """Whether the frame should stamp its brand mark on this layout.

    Two ways the answer is no, and both are mistakes the brand guide names:

    * the layout already draws a mark of its own (`decor_rects`), so a second one is
      the duplicate-logo error;
    * the logo's corner is where the layout puts its own title or another placeholder.
      23 of this template's 34 decor-free layouts run their title across the top of the
      slide, and stamping the mark at (0.42, 0.28) lands it on the first words of that
      title.

    `_apply_frame` and `reserved_rects` both ask this one question, which is what keeps
    the rectangles the gate reserves and the rectangles that actually get filled the
    same rectangles.

    Args:
        frame: A `frame_spec` block, or None/empty.
        layout: A `LayoutInfo` or a `LayoutView`.

    Returns:
        True when the mark may be placed.
    """
    key = getattr(layout, "key", "?")
    logo = (frame or {}).get("logo")
    if not logo:
        return False
    if getattr(layout, "decor_rects", None):
        log.debug("%s already draws its own mark; the frame adds none", key)
        return False
    rect = rect_of(logo.get("rect"))
    if rect is None:
        log.warning("the frame's logo has no readable rectangle (%r); no mark is "
                    "placed", logo.get("rect"))
        return False
    for placeholder in (getattr(layout, "placeholders", ()) or ()):
        other = rect_of(placeholder)
        if other and _rects_overlap(rect, other):
            log.debug("%s puts a placeholder at (%.2f, %.2f) %.2fx%.2fin, over the "
                      "logo corner; the frame adds no mark", key, *other)
            return False
    log.debug("%s leaves the logo corner free", key)
    return True


def reserved_rects(frame: Optional[Dict[str, Any]], layout: Any,
                   slide_width: Optional[float] = None,
                   slide_height: Optional[float] = None) -> List[List[float]]:
    """Every rectangle an added shape must not intersect (gate rule 5).

    That is the frame's own furniture plus the layout's inherited decorative graphics -
    the shapes a program cannot see in its own output but which land on the slide all
    the same.

    Four things are deliberately NOT reserved:

    * the logo, unless it will actually be placed. Reserving a rectangle nothing fills
      would block a corner of the slide that is genuinely free.
    * a decor shape that covers the whole slide. Ten of this template's layouts open
      with a full-bleed background picture, and a background is something you compose
      ON TOP OF, not around - reserving it would make the layout unusable for anything
      at all, including its own title.
    * the panel a canvas layout's free region sits on - same reasoning, one scale
      down. A card or tinted content band covering the composable area is a surface,
      not an obstacle; there is nowhere to route around it, because it IS where the
      composing happens. Reserving these while `_canvas_region` still advertised the
      area they cover is what made canvas slides ship blank: every shape drawn was a
      blocking rule 5 violation, so the only slide that passed the gate was an empty
      one. `catalog._canvas_region` applies the same `CANVAS_PANEL_COVERAGE` test and
      guarantees the free region clears everything this function does reserve.
    * a degenerate rectangle with no width or height. Nothing can intrude into it, and
      it only adds noise to the violation messages.

    Args:
        frame: A `frame_spec` block, or None/empty for no frame.
        layout: A `LayoutInfo` or a `LayoutView`.
        slide_width: Canvas width; taken from the frame, else the 16:9 default.
        slide_height: Canvas height; taken from the frame, else the 16:9 default.

    Returns:
        The reserved rectangles, each [left, top, width, height].
    """
    band = (frame or {}).get("footer_band")
    if slide_width is None:
        slide_width = float(band[2]) if band else DEFAULT_SLIDE_WIDTH_IN
    if slide_height is None:
        slide_height = (float(band[1]) + float(band[3])) if band \
            else DEFAULT_SLIDE_HEIGHT_IN
    canvas_area = max(slide_width * slide_height, 1e-6)

    rects: List[List[float]] = []
    if frame:
        rects.append(list(frame["footer_band"]))
        rects.append(list(frame["classification"]["symbol"]))
        rects.append(list(frame["classification"]["label"]))
        rects.append(list(frame["slide_number"]["rect"]))
        if logo_is_placeable(frame, layout):
            rects.append(list(frame["logo"]["rect"]))
    from_frame = len(rects)
    backgrounds = panels = degenerate = 0
    free = rect_of(getattr(layout, "free_region", None))
    for decor in (getattr(layout, "decor_rects", ()) or ()):
        rect = rect_of(decor)
        if rect is None:
            continue
        if rect[2] <= 0 or rect[3] <= 0:
            degenerate += 1
            continue
        if (rect[2] * rect[3]) / canvas_area >= BACKGROUND_DECOR_AREA_RATIO:
            backgrounds += 1
            continue
        if free is not None and _covers(rect, free) >= CANVAS_PANEL_COVERAGE:
            panels += 1
            continue
        rects.append(rect)
    log.debug("%s reserves %d zone(s): %d from the frame, %d decor; ignored %d "
              "full-bleed background(s), %d canvas panel(s), %d degenerate rect(s)",
              getattr(layout, "key", "?"), len(rects), from_frame,
              len(rects) - from_frame, backgrounds, panels, degenerate)
    return rects


# --------------------------------------------------------------------------------- #
# The catalog, as the sandbox sees it
# --------------------------------------------------------------------------------- #
_DRAWING_NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}


def apply_high_creativity_overrides(prs: Any, master_index: int, brand_master_index: int,
                                    font: str,
                                    prompt_shapes: Dict[int, List[str]]) -> bool:
    """Put the High Creativity master on brand, in this presentation only.

    SAMPLE.pptx ships that master with the Office colour scheme, Aptos and a baked
    "Click to edit title" text box. Nothing on disk is changed: this rewrites the
    in-memory copy that a generated slide (here) or the stitched deck (stitcher) is
    saved from, so the renderer, the judge and the shipped deck all see the same thing.

    * the master's theme colour scheme becomes the brand master's ("RA Brand");
    * its theme fonts (major and minor latin) become `font`;
    * the named sample shapes are removed from its layouts - the program draws the real
      title in their place with `set_title`.

    Args:
        prs: A python-pptx `Presentation` opened from the brand template.
        master_index: The High Creativity master.
        brand_master_index: The master whose colour scheme is the brand's.
        font: The brand font, e.g. "Barlow".
        prompt_shapes: Layout index -> names of the sample shapes to remove.

    Returns:
        True when the overrides were applied.
    """
    import copy

    from pptx.opc.constants import RELATIONSHIP_TYPE as RT

    masters = list(prs.slide_masters)
    if not (0 <= master_index < len(masters) and 0 <= brand_master_index < len(masters)):
        log.warning("High Creativity overrides skipped: master %d / brand master %d not "
                    "in a %d-master deck", master_index, brand_master_index, len(masters))
        return False
    target_part = masters[master_index].part.part_related_by(RT.THEME)
    brand_part = masters[brand_master_index].part.part_related_by(RT.THEME)
    target = etree.fromstring(target_part.blob)
    brand = etree.fromstring(brand_part.blob)
    scheme = brand.find(".//a:themeElements/a:clrScheme", _DRAWING_NS)
    old = target.find(".//a:themeElements/a:clrScheme", _DRAWING_NS)
    if scheme is not None and old is not None:
        old.getparent().replace(old, copy.deepcopy(scheme))
    else:
        log.warning("High Creativity overrides: no colour scheme to copy (brand %s, "
                    "target %s)", scheme is not None, old is not None)
    fonts = 0
    for tag in ("a:majorFont", "a:minorFont"):
        latin = target.find(f".//a:fontScheme/{tag}/a:latin", _DRAWING_NS)
        if latin is not None:
            latin.set("typeface", font)
            fonts += 1
    target_part.blob = etree.tostring(target, xml_declaration=True, encoding="UTF-8",
                                      standalone=True)
    removed = 0
    layouts = masters[master_index].slide_layouts
    for index, names in prompt_shapes.items():
        if not 0 <= index < len(layouts):
            continue
        for shape in list(layouts[index].shapes):
            if shape.name in names:
                shape._element.getparent().remove(shape._element)
                removed += 1
    log.info("High Creativity master M%d put on brand in memory: colour scheme from M%d "
             "(%s), %d theme font(s) -> %s, %d sample shape(s) removed", master_index,
             brand_master_index, scheme.get("name") if scheme is not None else "-",
             fonts, font, removed)
    return True


class LayoutView:
    """One layout, read from the serialized catalog.

    This is the read-only face of `TemplateCatalog`'s `LayoutInfo`. It takes a plain
    dict because the sandbox has no access to the catalog module.
    """

    def __init__(self, data: Dict[str, Any]) -> None:
        """
        Args:
            data: One entry of the catalog dict's `layouts` list.
        """
        self._data = data
        self.key: str = data["key"]
        self.name: str = data["name"]
        self.master_index: int = data["master_index"]
        self.layout_index: int = data["layout_index"]
        self.tags: List[str] = list(data.get("tags") or [])
        self.columns: int = int(data.get("columns", 1) or 1)
        self.background: str = data.get("background", "light")
        self.background_luminance: float = float(data.get("background_luminance", 1.0))
        self.canvas_capable: bool = bool(data.get("canvas_capable"))
        self.recommended_use: str = data.get("recommended_use", "")
        self.free_region: Optional[List[float]] = (
            list(data["free_region"]) if data.get("free_region") else None)
        self.decor_rects: List[List[float]] = [
            list(r) for r in (data.get("decor_rects") or [])]
        self.placeholders: List[Dict[str, Any]] = list(data.get("placeholders") or [])
        # High Creativity master only (absent everywhere else): where the title is drawn
        # and which baked sample shapes are stripped from generated files.
        self.high_creativity: bool = bool(data.get("high_creativity"))
        self.title_rect: Optional[List[float]] = (
            list(data["title_rect"]) if data.get("title_rect") else None)
        self.title_size: Optional[float] = data.get("title_size")
        self.prompt_shapes: List[str] = list(data.get("prompt_shapes") or [])

    def __repr__(self) -> str:  # pragma: no cover - debugging affordance
        return f"<LayoutView {self.key} {self.name!r}>"

    def find(self, target: Union[int, str]) -> Optional[Dict[str, Any]]:
        """Resolve a placeholder by idx, numeric string, slot name or role.

        Args:
            target: `0`, `"10"`, a slot name like `"body_left"`, or a role like
                `"title"` / `"subtitle"`.

        Returns:
            The placeholder dict, or None when the layout has no such placeholder.
        """
        if isinstance(target, bool):
            return None
        if isinstance(target, int) or (
                isinstance(target, str) and target.strip().isdigit()):
            idx = int(target)
            return next((p for p in self.placeholders if p["idx"] == idx), None)
        key = str(target).strip().lower().replace(" ", "").replace("_", "")
        for placeholder in self.placeholders:
            if str(placeholder.get("slot", "")).lower() == key:
                return placeholder
        found = next((p for p in self.placeholders if p.get("role") == key), None)
        if found is None:
            log.debug("%s has no placeholder matching %r; it offers %s", self.key,
                      target, [p.get("slot") for p in self.placeholders] or "none")
        return found


class CatalogView:
    """The serialized catalog, as the sandbox sees it.

    Replaces `TemplateCatalog` for everything this module needs. The builder never
    imports the catalog package; it only ever reads the dict that package produced.
    """

    def __init__(self, data: Union[Dict[str, Any], str, Path]) -> None:
        """
        Args:
            data: `TemplateCatalog.to_dict()`, or a path to that JSON on disk.
        """
        if not isinstance(data, dict):
            log.debug("reading the catalog from %s", data)
            data = json.loads(Path(data).read_text(encoding="utf-8"))
        self._data = data
        self.slide_width: float = float(data["slide_width"])
        self.slide_height: float = float(data["slide_height"])
        self.preferred_master: int = int(data.get("preferred_master", 0))
        self.cover_master: int = int(data.get("cover_master", 0))
        self.themes: List[Dict[str, Any]] = list(data.get("themes") or [])
        self.layouts: List[LayoutView] = [LayoutView(l) for l in data["layouts"]]
        self._by_key = {l.key.lower(): l for l in self.layouts}
        log.debug("catalog view: %d layout(s), %d theme(s), %.2fx%.2fin, preferred "
                  "master M%d", len(self.layouts), len(self.themes),
                  self.slide_width, self.slide_height, self.preferred_master)

    @property
    def theme(self) -> Dict[str, Any]:
        """The theme of the preferred master - the one new content slides use."""
        if not self.themes:
            log.warning("the catalog carries no theme; falling back to Barlow and an "
                        "empty colour map, so every token keeps its default")
            return {"major_font": "Barlow", "minor_font": "Barlow", "colors": {}}
        return self.themes[min(self.preferred_master, len(self.themes) - 1)]

    def get(self, key: str) -> Optional[LayoutView]:
        """The layout with this catalog key (`"M1-L10"`), or None."""
        return self._by_key.get(str(key).strip().lower())

    def resolve(self, ref: Union[str, LayoutView]) -> LayoutView:
        """Resolve a catalog key ('M1-L10') or an exact layout name.

        Args:
            ref: A catalog key, an exact layout name, or an already-resolved view.

        Returns:
            The matching `LayoutView`.

        Raises:
            LookupError: When neither matches. No fuzzy fallback: a program that names a
                layout that does not exist has a bug the repair loop should see. Close
                names are offered in the message as a hint, never silently substituted.
        """
        if isinstance(ref, LayoutView):
            return ref
        found = self.get(str(ref))
        if found is not None:
            return found
        wanted = re.sub(r"\s+", " ", str(ref).strip().lower())
        exact = [l for l in self.layouts
                 if re.sub(r"\s+", " ", l.name.lower()) == wanted]
        if exact:
            return exact[0]
        import difflib
        names = {re.sub(r"\s+", " ", l.name.lower()): l for l in self.layouts}
        close = difflib.get_close_matches(wanted, list(names), n=3, cutoff=0.75)
        hint = ", ".join(f"{names[c].key} '{names[c].name}'" for c in close)
        log.error("unknown layout %r; %s", ref,
                  f"closest: {hint}" if hint else "nothing close in the catalog")
        raise LookupError(
            f"unknown layout {ref!r}." + (f" Did you mean: {hint}" if hint else ""))


# --------------------------------------------------------------------------------- #
# One slide
# --------------------------------------------------------------------------------- #
class SlideHandle:
    """One generated slide.

    Fills template placeholders first, then adds optional extra shapes. Every method is
    safe to call on any layout: a placeholder that does not exist produces a warning and
    a skip, never an exception, because the repair loop reads warnings but a traceback
    kills the whole program.

    Geometry arguments are inches from the top-left of the slide. Nothing is clamped -
    see the module docstring.
    """

    def __init__(self, deck: "DeckBuilder", slide: Any, layout: LayoutView) -> None:
        """
        Args:
            deck: The owning `DeckBuilder` (for the palette, fonts and canvas size).
            slide: The python-pptx slide this handle writes to.
            layout: The catalog view of the layout the slide sits on.
        """
        self.deck = deck
        self.slide = slide
        self.layout = layout
        self.used_idx: Set[int] = set()
        self.warnings: List[str] = []
        #: Quiz choices drawn with `add_choice`, in drawing order.
        self.choices: List[Dict[str, Any]] = []
        #: One entry per component call: {"kind", "content"}. Parts drawn inside a
        #: composite (a card's accent bar) are not counted separately.
        self.components: List[Dict[str, Any]] = []
        self._nesting = 0
        self._finalized = False

    # ---- helpers ---------------------------------------------------------------- #
    def _warn(self, message: str) -> None:
        """Record a recoverable problem.

        A warning is data first: it surfaces through `DeckBuilder.warnings` and reaches
        the repair loop as text, which is the only channel that exists inside the
        sandbox. It is also logged, which is the channel that exists in-process.
        """
        self.warnings.append(f"slide {self.index}: {message}")
        log.warning("slide %d on %s: %s", self.index, self.layout.key, message)

    def _record(self, kind: str, content: bool) -> None:
        """Ledger one component call, unless it is a part of a composite."""
        if self._nesting:
            return
        self.components.append({"kind": kind, "content": bool(content)})

    def _fit_component(self, items: Sequence[str], width: float, height: float,
                       size: int, bold: bool, bullets: bool, what: str) -> int:
        """The largest type-scale size, at most `size`, at which `items` fit the box.

        Never goes below `COMPONENT_FLOOR_PT` (or the caller's own smaller size). At
        the floor, a copy that still does not fit is a warning for the repair loop,
        and the gate's rule 10 measures what remains.

        Args:
            items: The paragraphs, as written.
            width: Box width in inches.
            height: Box height in inches.
            size: The requested size, already on the type scale.
            bold: Whether the text is bold.
            bullets: Whether the paragraphs are bulleted.
            what: The component, for the warning.

        Returns:
            The size to write the text at.
        """
        if not [i for i in items if str(i).strip()] or _text_fits(
                items, width, height, size, bullets, bold):
            return size
        floor = min(size, COMPONENT_FLOOR_PT)
        for candidate in sorted((s for s in TYPE_SCALE if floor <= s < size),
                                reverse=True):
            if _text_fits(items, width, height, candidate, bullets, bold):
                log.info("slide %d: %s text shrunk from %dpt to %dpt to fit its "
                         "%.2fx%.2fin box", self.index, what, size, candidate,
                         width, height)
                return candidate
        self._warn(f"{what} text does not fit its {width:.2f}x{height:.2f}in box even at "
                   f"{floor}pt; shorten it, enlarge the box, or move the detail to the "
                   f"speaker notes")
        return floor

    # ---- composition helpers (pure arithmetic, safe to call anywhere) ----------- #
    def text_height(self, text: Union[str, Sequence[Any]], width: float,
                    size: float = 12, bullets: bool = False, bold: bool = False) -> float:
        """Inches of box height `text` needs at `size` in a box `width` inches wide.

        Args:
            text: A string (newlines split paragraphs) or a list of paragraphs.
            width: Box width in inches.
            size: Point size.
            bullets: Whether the paragraphs are bulleted.
            bold: Whether the text is bold.

        Returns:
            The height in inches, insets included. Size boxes with it instead of
            guessing, and text never spills onto its neighbours.
        """
        items = list(text) if isinstance(text, (list, tuple)) else str(text).split("\n")
        return _text_height([str(i) for i in items], width, size, bullets, bold)

    def fits(self, text: Union[str, Sequence[Any]], width: float, height: float,
             size: float = 12, bullets: bool = False, bold: bool = False) -> bool:
        """True when `text` fits a `width` x `height` box at `size` without shrinking."""
        items = list(text) if isinstance(text, (list, tuple)) else str(text).split("\n")
        return _text_fits([str(i) for i in items], width, height, size, bullets, bold)

    def split(self, rect: Sequence[float], parts: Union[int, Sequence[float]],
              axis: str = "row", gap: float = 0.2) -> List[Tuple[float, float, float, float]]:
        """Divide a rectangle into non-overlapping parts with fixed gaps.

        Args:
            rect: (left, top, width, height) in inches, e.g. from `grid(...)`.
            parts: How many equal parts, or relative weights such as [2, 1].
            axis: "row" lays the parts left to right; "column" stacks them top to
                bottom.
            gap: Inches between neighbouring parts.

        Returns:
            One (left, top, width, height) per part, in order. Neighbours never
            overlap, so components placed in them satisfy rule 2 by construction.
        """
        left, top, width, height = (float(v) for v in rect)
        weights = ([1.0] * int(parts) if isinstance(parts, (int, float))
                   else [float(w) for w in parts]) or [1.0]
        total = sum(weights) or 1.0
        along = width if axis == "row" else height
        room = max(0.0, along - gap * (len(weights) - 1))
        out, cursor = [], left if axis == "row" else top
        for weight in weights:
            span = room * weight / total
            out.append((cursor, top, span, height) if axis == "row"
                       else (left, cursor, width, span))
            cursor += span + gap
        log.debug("slide %d: split %s into %d %s part(s)", self.index,
                  tuple(round(v, 2) for v in (left, top, width, height)),
                  len(out), axis)
        return out

    @property
    def index(self) -> int:
        """This slide's 1-based position in the deck."""
        return (self.deck.slides.index(self) + 1 if self in self.deck.slides
                else len(self.deck.slides) + 1)

    def _placeholder_shapes(self) -> Dict[int, Any]:
        return {ph.placeholder_format.idx: ph for ph in self.slide.placeholders}

    def _resolve(self, target: Union[int, str]
                 ) -> Tuple[Optional[Dict[str, Any]], Any]:
        info = self.layout.find(target)
        if info is None:
            self._warn(f"placeholder '{target}' does not exist on layout "
                       f"'{self.layout.name}' - skipped")
            return None, None
        shape = self._placeholder_shapes().get(info["idx"])
        if shape is None:
            self._warn(f"placeholder {info.get('slot')} (idx {info['idx']}) is no "
                       f"longer on the slide - skipped")
            return info, None
        log.debug("slide %d: %r resolved to idx %d (%s)", self.index, target,
                  info["idx"], info.get("slot"))
        return info, shape

    @staticmethod
    def _inherited_color(info: Dict[str, Any]) -> Optional[RGBColor]:
        """The placeholder's own colour, as an already-resolved `RGBColor`.

        `to_rgb` passes an `RGBColor` straight through, which is what keeps the
        template's hex out of the token check.

        Args:
            info: The placeholder dict from the catalog.

        Returns:
            The colour, or None when the template set none (or set something the
            catalog could not reduce to 6 hex digits).
        """
        value = str(info.get("color") or "").strip().lstrip("#")
        if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
            return None
        return RGBColor.from_string(value.upper())

    def placeholder_bounds(self, target: Union[int, str]
                           ) -> Optional[Tuple[float, float, float, float]]:
        """The template's own geometry for a placeholder.

        Args:
            target: Placeholder idx, slot name or role.

        Returns:
            `(left, top, width, height)` in inches, or None when the layout has no such
            placeholder. These are the template's numbers, so a component aligned to
            them is aligned to the brand grid by construction.
        """
        info = self.layout.find(target)
        if info is None:
            return None
        return info["left"], info["top"], info["width"], info["height"]

    def remove_placeholder(self, target: Union[int, str]) -> bool:
        """Delete a placeholder outright.

        Args:
            target: Placeholder idx, slot name or role.

        Returns:
            True when something was removed.
        """
        info = self.layout.find(target)
        shape = self._placeholder_shapes().get(info["idx"]) if info else None
        if shape is None:
            log.debug("slide %d: nothing to remove for %r", self.index, target)
            return False
        shape._element.getparent().remove(shape._element)
        self.used_idx.add(info["idx"])
        log.debug("slide %d: removed placeholder %s (idx %d)", self.index,
                  info.get("slot"), info["idx"])
        return True

    def _fit_size(self, info: Dict[str, Any], items: Sequence[str],
                  requested: Optional[float]) -> Optional[float]:
        """An explicit font size, only when the inherited one would overflow.

        Returning None means "leave the template's inherited size alone", which is the
        preferred outcome: the template's choice is correct by definition and is exempt
        from the type scale.

        Args:
            info: The placeholder dict from the catalog.
            items: The paragraphs about to be written.
            requested: A caller-supplied size, which always wins.

        Returns:
            A point size, or None to inherit.
        """
        if requested:
            log.debug("slide %d: caller set %gpt on %s explicitly", self.index,
                      requested, info.get("slot"))
            return requested
        base = info.get("font_size") or 18.0
        minimum = 22.0 if info.get("role") == "title" else 10.0
        bullets = bool(info.get("bullets")) and len(items) > 1
        size = base
        while size > minimum and not _text_fits(
                items, info["width"], info["height"], size, bullets):
            size -= 1.0
        if size >= base:
            return None
        fitted = max(size, minimum)
        if fitted <= minimum and not _text_fits(items, info["width"], info["height"],
                                                fitted, bullets):
            # Shrink-to-fit bottomed out. The copy is still too long for the box and
            # the gate's rule 10 is where it will surface.
            self._warn(f"{info.get('slot')} is at the {minimum:g}pt floor and the copy "
                       f"still does not fit; shorten it")
        else:
            log.info("slide %d: shrank %s from %gpt to %gpt to fit %d item(s)",
                     self.index, info.get("slot"), base, fitted, len(items))
        return fitted

    def _write_paragraph(self, paragraph: Any, text: str, size: Optional[float] = None,
                         bold: Optional[bool] = None,
                         color: Union[str, RGBColor, None] = None,
                         font: Optional[str] = None,
                         italic: Optional[bool] = None) -> None:
        """Replace a paragraph's runs, honouring `**bold**` segments in `text`."""
        for run in list(paragraph.runs):
            run._r.getparent().remove(run._r)
        segments: List[Tuple[str, bool]] = []
        pos = 0
        for m in _BOLD_RE.finditer(text):
            if m.start() > pos:
                segments.append((text[pos:m.start()], False))
            segments.append((m.group(1), True))
            pos = m.end()
        if pos < len(text) or not segments:
            segments.append((text[pos:], False))
        for seg_text, seg_bold in segments:
            run = paragraph.add_run()
            run.text = seg_text
            if size:
                run.font.size = Pt(size)
            if bold or seg_bold:
                run.font.bold = True
            elif bold is False:
                run.font.bold = False
            if italic:
                run.font.italic = True
            if color:
                run.font.color.rgb = to_rgb(color, self.deck.brand)
            if font:
                run.font.name = font

    def _fill_text_frame(self, tf: Any, items: Sequence[Any],
                         info: Optional[Dict[str, Any]] = None,
                         size: Optional[float] = None, bold: Optional[bool] = None,
                         color: Optional[str] = None, align: Optional[str] = None,
                         font: Optional[str] = None) -> None:
        """Write paragraphs into a text frame, shrinking to fit when `info` is given."""
        normalised: List[Tuple[str, int, Optional[bool]]] = []
        for item in items:
            if isinstance(item, dict):
                normalised.append((str(item.get("text", "")),
                                   int(item.get("level", 0) or 0), item.get("bold")))
            else:
                normalised.append((str(item), 0, None))
        fit = (self._fit_size(info, [t for t, _, _ in normalised], size)
               if info else size)
        tf.clear()
        tf.word_wrap = True
        for i, (text, level, item_bold) in enumerate(normalised):
            paragraph = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            if level:
                paragraph.level = min(level, 4)
            if align:
                paragraph.alignment = _ALIGN.get(align.lower(), PP_ALIGN.LEFT)
            self._write_paragraph(
                paragraph, text, fit,
                item_bold if item_bold is not None else bold, color, font)

    # ---- the grid --------------------------------------------------------------- #
    def grid(self, col: int, row: int, cols: int = 1, rows: int = 1
             ) -> Tuple[float, float, float, float]:
        """A cell of the deck's 12x6 grid, as (left, top, width, height) in inches.

        The grid is laid over the layout's free region when it has one, and over the
        canvas minus the frame margins otherwise. Fixed gutters mean two adjacent cells
        never overlap, which is what keeps rule 2 satisfiable by construction.

        Args:
            col: Zero-based column of the top-left cell.
            row: Zero-based row of the top-left cell.
            cols: How many columns the block spans.
            rows: How many rows the block spans.

        Returns:
            `(left, top, width, height)` in inches. Cell indices are clamped to the
            grid - that is addressing arithmetic, not geometry: a block is never moved
            off the position its (clamped) indices name, so nothing is hidden from the
            gate.
        """
        region = self.layout.free_region
        if region is None:
            region = [
                _FALLBACK_SIDE_MARGIN_IN,
                _FALLBACK_TOP_IN,
                self.deck.slide_width - 2 * _FALLBACK_SIDE_MARGIN_IN,
                self.deck.slide_height - _FALLBACK_VERTICAL_TRIM_IN,
            ]
            log.debug("slide %d: %s has no measured free region; the grid is laid over "
                      "the default band", self.index, self.layout.key)
        left0, top0, width0, height0 = region
        gutter = GRID_GUTTER_IN
        col_w = (width0 - gutter * (GRID_COLUMNS - 1)) / GRID_COLUMNS
        row_h = (height0 - gutter * (GRID_ROWS - 1)) / GRID_ROWS
        col = max(0, min(col, GRID_COLUMNS - 1))
        row = max(0, min(row, GRID_ROWS - 1))
        fitted_cols = max(1, min(cols, GRID_COLUMNS - col))
        fitted_rows = max(1, min(rows, GRID_ROWS - row))
        # Clamping the *indices* above is addressing arithmetic. Clamping the *span* is
        # not: it hands back a smaller block than was asked for, and because that block
        # is legal the gate never sees the caller's arithmetic error. Warn so the
        # mistake reaches the repair loop instead of dying in the geometry.
        if (fitted_cols, fitted_rows) != (cols, rows):
            self._warn(
                f"grid({col}, {row}, cols={cols}, rows={rows}) runs off the "
                f"{GRID_COLUMNS}x{GRID_ROWS} grid; clamped to cols={fitted_cols}, "
                f"rows={fitted_rows}")
        cols, rows = fitted_cols, fitted_rows
        block = (
            left0 + col * (col_w + gutter),
            top0 + row * (row_h + gutter),
            cols * col_w + (cols - 1) * gutter,
            rows * row_h + (rows - 1) * gutter,
        )
        log.debug("slide %d: grid(%d, %d, %dx%d) -> (%.2f, %.2f) %.2fx%.2fin",
                  self.index, col, row, cols, rows, *block)
        return block

    # ---- placeholder population ------------------------------------------------- #
    def set_title(self, text: str, size: Optional[float] = None) -> bool:
        """Fill the title placeholder.

        Args:
            text: The title.
            size: An explicit point size; omit it so the template's own size is used.

        Returns:
            True when a real placeholder was filled. False means the layout has no
            title and one was drawn as a text box at the template title position, which
            is recorded as a warning.
        """
        info = self.layout.find("title")
        if info is None and getattr(self.layout, "title_rect", None):
            # A High Creativity layout: its sample title was stripped, and this is the
            # intended way to title it, so no warning.
            left, top, width, height = self.layout.title_rect
            log.debug("slide %d: title drawn at the High Creativity title box %s",
                      self.index, self.layout.title_rect)
            self.add_textbox(text, left, top, width, height,
                             size=size or self.layout.title_size or 28, color="navy",
                             font=self.deck.heading_font)
            return False
        if info is None:
            left, width, height = 1.14, self.deck.slide_width - 1.6, 0.6
            top = self._first_clear_top(left, width, height, start=0.38)
            self._warn("layout has no title placeholder; drawing the title as a "
                       f"text box at {top:.2f}in")
            self.add_textbox(text, left, top, width, height,
                             size=size or 28, color="navy",
                             font=self.deck.heading_font)
            return False
        return self.set_text(info["idx"], text, size=size)

    def _first_clear_top(self, left: float, width: float, height: float,
                         start: float) -> float:
        """The highest position at or below `start` where a band clears the decor.

        The template title position is not free on every layout: 18 of the layouts
        without a title placeholder draw a graphic straight through it, and dropping a
        text box there is a guaranteed rule 5 violation the model cannot diagnose.
        Full-bleed backgrounds are skipped - you compose on top of those.

        Args:
            left: The band's left edge.
            width: The band's width.
            height: The band's height.
            start: The preferred top, tried first.

        Returns:
            A clear top, or `start` when the layout leaves no clear band at all - the
            title is drawn either way, because losing it silently is worse.
        """
        canvas = max(self.deck.slide_width * self.deck.slide_height, 1e-6)
        obstacles = [r for r in (rect_of(d) for d in (self.layout.decor_rects or ()))
                     if r and (r[2] * r[3]) / canvas < BACKGROUND_DECOR_AREA_RATIO]
        if not obstacles:
            return start
        limit = self.deck.slide_height - FOOTER_BAND_HEIGHT_IN - height
        top = start
        while top <= limit:
            if not any(_rects_overlap([left, top, width, height], obstacle,
                                      GRID_GUTTER_IN) for obstacle in obstacles):
                return top
            top += 0.1
        return start

    def set_subtitle(self, text: str, size: Optional[float] = None) -> bool:
        """Fill the subtitle placeholder, or draw one under the title.

        Args:
            text: The subtitle.
            size: An explicit point size; omit it to inherit the template's.

        Returns:
            True when a real placeholder was filled.
        """
        info = self.layout.find("subtitle")
        box = getattr(self.layout, "title_rect", None)
        if info is None and box and self.layout.find("title") is None:
            # A High Creativity layout: the subtitle sits under the drawn title box.
            self.add_textbox(text, box[0], box[1] + box[3] + 0.05, box[2], 0.4,
                             size=size or 14, color="body")
            return False
        if info is None:
            title = self.layout.find("title")
            top = (title["top"] + title["height"] + 0.05) if title else 1.0
            left = title["left"] if title else 1.14
            width = title["width"] if title else self.deck.slide_width - 1.6
            self._warn("layout has no subtitle placeholder; drawing the subtitle as "
                       "a text box")
            self.add_textbox(text, left, top, width, 0.4, size=size or 14,
                             color="body")
            return False
        return self.set_text(info["idx"], text, size=size)

    def set_text(self, target: Union[int, str], text: Union[str, Sequence[Any]],
                 size: Optional[float] = None, bold: Optional[bool] = None,
                 color: Optional[str] = None, align: Optional[str] = None) -> bool:
        """Put text into a placeholder addressed by idx, slot name or role.

        Args:
            target: Placeholder idx, slot name or role.
            text: A string, or a list of paragraphs. A paragraph may be a string or a
                `{"text": ..., "level": ..., "bold": ...}` dict.
            size: Explicit point size. Omit it and the size is inherited, then shrunk
                only if the copy would overflow (floor 10pt, or 22pt for a title).
            bold: Force bold on or off.
            color: A brand token.
            align: `"left"`, `"center"`, `"right"` or `"justify"`.

        Returns:
            True when the placeholder was filled; False (with a warning) when it does
            not exist.
        """
        info, shape = self._resolve(target)
        if shape is None:
            return False
        items = list(text) if isinstance(text, (list, tuple)) else [text]
        if (shape.placeholder_format.type == PP_PLACEHOLDER.PICTURE
                or not shape.has_text_frame):
            # Picture slots cannot reliably display typed text: swap in a text box with
            # the slot's own formatting so the result still reads as the template's.
            label = " ".join(
                str(i.get("text", i) if isinstance(i, dict) else i) for i in items)
            short = len(label) <= 4
            log.info("slide %d: %s is a picture slot, so %d char(s) of text are drawn "
                     "as a caption box instead", self.index, info.get("slot"),
                     len(label))
            self.remove_placeholder(info["idx"])
            base = info.get("font_size") or 14
            # The catalog reports a placeholder's own colour as raw hex, read straight
            # out of the template's `a:srgbClr/@val`. Resolve it to an RGBColor here
            # rather than letting it reach `to_rgb`, which rejects hex by design: that
            # rule is about the model's literals, and the template's own colours are
            # not that. 73 of this template's picture slots carry one, so forwarding
            # the string would turn a legal caption call into a fatal ValueError.
            inherited = self._inherited_color(info)
            self.add_textbox(
                label, info["left"], info["top"], info["width"], info["height"],
                size=size or (max(base, 24) if short else base),
                bold=bool(bold) or short,
                color=color or inherited or "white",
                align=align or info.get("align") or "center", anchor="middle")
            return True
        self._fill_text_frame(shape.text_frame, items, info, size, bold, color, align)
        self.used_idx.add(info["idx"])
        log.debug("slide %d: filled %s (idx %d) with %d paragraph(s)",
                  self.index, info.get("slot"), info["idx"], len(items))
        return True

    def set_bullets(self, target: Union[int, str], items: Sequence[Any],
                    size: Optional[float] = None,
                    color: Optional[str] = None) -> bool:
        """Fill a placeholder with bullet items.

        Args:
            target: Placeholder idx, slot name or role.
            items: Strings, or `{"text", "level", "bold"}` dicts for nested bullets.
            size: Explicit point size; omit it to inherit and shrink to fit.
            color: A brand token.

        Returns:
            True when the placeholder was filled.
        """
        return self.set_text(target, list(items), size=size, color=color)

    def set_picture(self, target: Union[int, str],
                    image_path: Union[str, Path]) -> bool:
        """Insert an image into a picture placeholder, or at another slot's bounds.

        Args:
            target: Placeholder idx, slot name or role.
            image_path: Absolute, or relative to the deck's `asset_root`.

        Returns:
            True when the image landed. A missing file is a warning and the placeholder
            is removed, so no empty prompt box ships.
        """
        info, shape = self._resolve(target)
        if shape is None:
            return False
        path = self.deck.resolve_asset(image_path)
        if path is None:
            self._warn(f"image '{image_path}' not found; placeholder "
                       f"{info.get('slot')} removed")
            self.remove_placeholder(info["idx"])
            return False
        crop = _crop_fraction(path, info["width"], info["height"])
        if shape.placeholder_format.type == PP_PLACEHOLDER.PICTURE and \
                crop <= _MAX_CROP_FRACTION:
            shape.insert_picture(str(path))
            log.debug("slide %d: %s took %s directly (%.0f%% cropped)", self.index,
                      info.get("slot"), path.name, crop * 100)
        elif shape.placeholder_format.type == PP_PLACEHOLDER.PICTURE:
            # Cropping to fill would cut away a large part of the image (a diagram's
            # labels, a screenshot's edge). Show it whole inside the slot instead.
            log.info("slide %d: %s would crop %.0f%% of %s; placing it whole at its own "
                     "aspect ratio inside the slot", self.index, info.get("slot"),
                     crop * 100, path.name)
            self.remove_placeholder(info["idx"])
            self.add_image(str(path), info["left"], info["top"],
                           info["width"], info["height"])
        else:
            log.debug("slide %d: %s is not a picture slot, placing %s at its bounds "
                      "instead", self.index, info.get("slot"), path.name)
            self.remove_placeholder(info["idx"])
            self.add_image(str(path), info["left"], info["top"],
                           info["width"], info["height"])
        self.used_idx.add(info["idx"])
        return True

    def set_table(self, target: Union[int, str], columns: Sequence[str],
                  rows: Sequence[Sequence[Any]], font_size: float = 11,
                  col_widths: Optional[Sequence[float]] = None) -> Any:
        """Replace a placeholder with a branded table occupying the same area.

        Args:
            target: Placeholder idx, slot name or role.
            columns: Header labels; pass `[]` for a headerless table.
            rows: Body rows.
            font_size: Body point size, snapped to the type scale.
            col_widths: Relative column widths; rescaled to the placeholder width.

        Returns:
            The graphic frame, or None when the placeholder does not exist.
        """
        info, shape = self._resolve(target)
        if shape is None:
            return None
        log.info("slide %d: replacing %s with a %dx%d table", self.index,
                 info.get("slot"), len(rows), len(columns))
        self.remove_placeholder(info["idx"])
        return self.add_table(columns, rows, info["left"], info["top"],
                              info["width"], info["height"], font_size, col_widths)

    def set_chart(self, target: Union[int, str], chart_type: str,
                  categories: Sequence[str], series: Sequence[Dict[str, Any]],
                  title: Optional[str] = None, show_legend: Optional[bool] = None,
                  data_labels: bool = False,
                  number_format: Optional[str] = None) -> Any:
        """Replace a placeholder with a branded chart occupying the same area.

        Args:
            target: Placeholder idx, slot name or role.
            chart_type: One of `_CHART_TYPES` (`"column"`, `"line"`, `"pie"`, ...).
            categories: Category axis labels.
            series: `[{"name": ..., "values": [...]}, ...]`.
            title: Chart title, or None for no title.
            show_legend: Force the legend on or off; None means auto.
            data_labels: Draw value labels on the points.
            number_format: Excel number format for the data labels.

        Returns:
            The graphic frame, or None when the placeholder does not exist.
        """
        info, shape = self._resolve(target)
        if shape is None:
            return None
        log.info("slide %d: replacing %s with a %s chart, %d series over %d category",
                 self.index, info.get("slot"), chart_type, len(series),
                 len(categories))
        self.remove_placeholder(info["idx"])
        return self.add_chart(chart_type, categories, series, info["left"],
                              info["top"], info["width"], info["height"],
                              title, show_legend, data_labels, number_format)

    def set_notes(self, text: str) -> None:
        """Set the speaker notes for this slide.

        Args:
            text: The notes body.
        """
        self.slide.notes_slide.notes_text_frame.text = text
        log.debug("slide %d: %d chars of speaker notes set", self.index,
                  len(text or ""))

    def _placed(self, kind: str, left: float, top: float, width: float,
                height: float, **detail: Any) -> None:
        """Record where a component landed, and flag it if it landed off the canvas.

        The `add_*` methods clamp nothing by design - the gate is what reports bad
        geometry - but by the time the gate speaks the program has already run. This
        is the same fact, at the moment it happens, with the call that caused it.

        Args:
            kind: The component name, for the log line.
            left: Inches from the left edge.
            top: Inches from the top edge.
            width: Width in inches.
            height: Height in inches.
            **detail: Anything else worth naming, e.g. `fill="red"`.
        """
        extra = ", ".join(f"{k}={v!r}" for k, v in detail.items() if v is not None)
        log.debug("slide %d: %s at (%.2f, %.2f) %.2fx%.2fin%s", self.index, kind,
                  left, top, width, height, f" [{extra}]" if extra else "")
        if (left < 0 or top < 0 or width <= 0 or height <= 0
                or left + width > self.deck.slide_width
                or top + height > self.deck.slide_height):
            log.warning("slide %d: %s at (%.2f, %.2f) %.2fx%.2fin does not fit the "
                        "%.2fx%.2fin canvas; it is placed as asked and rule 1 will "
                        "report it", self.index, kind, left, top, width, height,
                        self.deck.slide_width, self.deck.slide_height)

    # ---- extra shapes (brand styled) -------------------------------------------- #
    def add_textbox(self, text: Union[str, Sequence[Any]], left: float, top: float,
                    width: float, height: float, size: float = 12, bold: bool = False,
                    color: Union[str, RGBColor] = "body", align: str = "left",
                    fill: Optional[str] = None, italic: bool = False,
                    font: Optional[str] = None, anchor: str = "top",
                    line_spacing: Optional[float] = None) -> Any:
        """Free text box.

        Args:
            text: A string (newlines split paragraphs) or a list of paragraphs. Any
                `**bold**` segment is rendered bold.
            left: Inches from the left edge.
            top: Inches from the top edge.
            width: Width in inches.
            height: Height in inches.
            size: Point size, snapped to the type scale.
            bold: Bold the whole box.
            color: A brand token.
            align: `"left"`, `"center"`, `"right"` or `"justify"`.
            fill: A brand token for the box fill, or None for no fill.
            italic: Italicise the whole box.
            font: Font name; defaults to the theme body font.
            anchor: Vertical anchor - `"top"`, `"middle"` or `"bottom"`.
            line_spacing: Multiple of single spacing.

        Returns:
            The created text box shape.
        """
        size = _snap_size(size)
        items = (list(text) if isinstance(text, (list, tuple))
                 else str(text).split("\n"))
        raws = [str(i.get("text", "")) if isinstance(i, dict) else str(i) for i in items]
        bulleted = any(r.startswith(("- ", "* ", "\u2022")) for r in raws)
        size = self._fit_component(raws, width, height, size, bool(bold), bulleted,
                                   "text box")
        self._record("textbox", any(r.strip() for r in raws))
        box = self.slide.shapes.add_textbox(
            Inches(left), Inches(top), Inches(width), Inches(height))
        tf = box.text_frame
        tf.word_wrap = True
        # The drawn box is the declared box: no grow-to-fit, so what the gate measures
        # is what the audience sees.
        tf.auto_size = MSO_AUTO_SIZE.NONE
        tf.margin_left = tf.margin_right = Inches(0.05)
        tf.margin_top = tf.margin_bottom = Inches(0.03)
        tf.vertical_anchor = {"top": MSO_ANCHOR.TOP, "middle": MSO_ANCHOR.MIDDLE,
                              "bottom": MSO_ANCHOR.BOTTOM}.get(anchor, MSO_ANCHOR.TOP)
        if fill:
            box.fill.solid()
            box.fill.fore_color.rgb = to_rgb(fill, self.deck.brand)
        for i, item in enumerate(items):
            paragraph = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            paragraph.alignment = _ALIGN.get(str(align).lower(), PP_ALIGN.LEFT)
            if line_spacing:
                paragraph.line_spacing = line_spacing
            raw = str(item.get("text", "")) if isinstance(item, dict) else str(item)
            if raw.startswith(("- ", "* ")):
                raw = "• " + raw[2:]
            self._write_paragraph(paragraph, raw, size, bold, color,
                                  font or self.deck.body_font, italic)
        self._placed("textbox", left, top, width, height, size=size, fill=fill,
                     paragraphs=len(items))
        return box

    def add_shape(self, shape: str, left: float, top: float, width: float,
                  height: float, fill: Optional[str] = "red",
                  line: Optional[str] = None, text: str = "", size: float = 12,
                  color: str = "white", bold: bool = True, align: str = "center",
                  corner_radius: Optional[float] = None) -> Any:
        """Auto shape in brand colours.

        Args:
            shape: One of `_SHAPES` - `"rectangle"`, `"rounded_rectangle"`, `"oval"`,
                `"chevron"`, `"right_arrow"`, `"pentagon"`, `"diamond"`, ...
            left: Inches from the left edge.
            top: Inches from the top edge.
            width: Width in inches.
            height: Height in inches.
            fill: A brand token, or None for no fill.
            line: A brand token for a 1pt outline, or None for none.
            text: Optional label drawn inside the shape.
            size: Label point size, snapped to the type scale.
            color: Label colour token.
            bold: Bold the label.
            align: Label alignment.
            corner_radius: Adjustment value, rounded rectangles only.

        Returns:
            The created shape.

        Raises:
            ValueError: For an unknown shape name. Failing loudly is the point: a
                silent fallback would ship a rectangle where a chevron was meant.
        """
        kind = _SHAPES.get(shape.lower().replace("-", "_").replace(" ", "_"))
        if kind is None:
            raise ValueError(
                f"unknown shape '{shape}'. Allowed: {', '.join(sorted(_SHAPES))}")
        size = _snap_size(size)
        if text:
            size = self._fit_component(str(text).split("\n"), width, height, size,
                                       bool(bold), False, f"{shape} label")
        self._record("shape", bool(str(text).strip()))
        shp = self.slide.shapes.add_shape(
            kind, Inches(left), Inches(top), Inches(width), Inches(height))
        shp.shadow.inherit = False
        if fill:
            shp.fill.solid()
            shp.fill.fore_color.rgb = to_rgb(fill, self.deck.brand)
        else:
            shp.fill.background()
        if line:
            shp.line.color.rgb = to_rgb(line, self.deck.brand)
            shp.line.width = Pt(1)
        else:
            shp.line.fill.background()
        if corner_radius is not None and kind == MSO_SHAPE.ROUNDED_RECTANGLE:
            shp.adjustments[0] = corner_radius
        if text:
            tf = shp.text_frame
            tf.word_wrap = True
            tf.margin_left = tf.margin_right = Inches(0.08)
            tf.vertical_anchor = MSO_ANCHOR.MIDDLE
            for i, line_text in enumerate(str(text).split("\n")):
                paragraph = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
                paragraph.alignment = _ALIGN.get(str(align).lower(), PP_ALIGN.CENTER)
                self._write_paragraph(paragraph, line_text, size, bold, color,
                                      self.deck.body_font)
        self._placed(f"shape:{shape}", left, top, width, height, fill=fill,
                     text=bool(text))
        return shp

    def add_line(self, left: float, top: float, width: float, height: float,
                 color: str = "border", weight: float = 1.0) -> Any:
        """Straight connector from (left, top) to (left+width, top+height).

        Args:
            left: Inches from the left edge.
            top: Inches from the top edge.
            width: Horizontal run in inches (may be 0 for a vertical rule).
            height: Vertical run in inches (may be 0 for a horizontal rule).
            color: A brand token.
            weight: Line weight in points.

        Returns:
            The created connector.
        """
        self._record("line", False)
        connector = self.slide.shapes.add_connector(
            MSO_CONNECTOR.STRAIGHT, Inches(left), Inches(top),
            Inches(left + width), Inches(top + height))
        connector.line.color.rgb = to_rgb(color, self.deck.brand)
        connector.line.width = Pt(weight)
        self._placed("line", left, top, width, height, color=color, weight=weight)
        return connector

    def add_card(self, title: str, body: Union[str, Sequence[Any]], left: float,
                 top: float, width: float, height: float,
                 accent: Optional[str] = "red", title_size: float = 14,
                 body_size: float = 11, fill: str = "white",
                 title_color: str = "navy") -> Any:
        """Rounded card with an accent bar, a bold title and body copy.

        Args:
            title: Card heading.
            body: A string, or a list of lines rendered as bullets.
            left: Inches from the left edge.
            top: Inches from the top edge.
            width: Width in inches.
            height: Height in inches.
            accent: Brand token for the short accent bar, or None to omit it.
            title_size: Heading point size, snapped to the type scale.
            body_size: Body point size, snapped to the type scale.
            fill: Brand token for the card fill.
            title_color: Brand token for the heading.

        Returns:
            The card's background shape.
        """
        log.debug("slide %d: card %r at (%.2f, %.2f) %.2fx%.2fin", self.index,
                  title, left, top, width, height)
        if height < 0.9:
            # The title sits at +0.22 and the body at +0.68, so a card shorter than
            # this has its body hanging off the bottom - rule 3, and a puzzling one.
            log.warning("slide %d: card %r is only %.2fin tall; its body starts at "
                        "+0.68in and will overhang", self.index, title, height)
        self._record("card", True)
        self._nesting += 1
        try:
            card = self.add_shape("rounded_rectangle", left, top, width, height,
                                  fill=fill, line="border", corner_radius=0.06)
            if accent:
                self.add_shape("rectangle", left + 0.15, top + 0.12, 0.5, 0.06,
                               fill=accent)
            # The title box is as tall as its wrapped title, so a two-line title
            # pushes the body down instead of drawing over it.
            inner_w = width - 0.24
            title_size = _snap_size(title_size)
            has_title = bool(str(title or "").strip())
            title_h = (min(max(0.45, _text_height([str(title)], inner_w, title_size,
                                                  bold=True)), max(0.45, height * 0.5))
                       if has_title else 0.0)
            # An untitled card gets no empty title box; its body starts at the top.
            body_top = top + 0.22 + (title_h + 0.01 if has_title else 0.0)
            body_h = top + height - 0.12 - body_top
            if body_h < 0.3:
                self._warn(f"card {title!r} is {height:.2f}in tall; its body gets only "
                           f"{max(body_h, 0):.2f}in under a {title_h:.2f}in title - "
                           f"make the card taller or the title shorter")
            if has_title:
                self.add_textbox(title, left + 0.12, top + 0.22, inner_w, title_h,
                                 size=title_size, bold=True, color=title_color)
            body_items = list(body) if isinstance(body, (list, tuple)) else [body]
            bulleted = isinstance(body, (list, tuple)) and len(body_items) > 1
            self.add_textbox(
                ["• " + str(b) if bulleted else str(b) for b in body_items],
                left + 0.12, body_top, inner_w, max(body_h, 0.3),
                size=body_size, color="body")
        finally:
            self._nesting -= 1
        return card

    def add_stat(self, value: str, label: str, left: float, top: float, width: float,
                 height: float, color: str = "red", value_size: float = 32,
                 label_size: float = 11, fill: Optional[str] = None,
                 align: str = "left") -> Any:
        """Big number with a caption - for KPIs and key figures.

        Args:
            value: The figure, e.g. `"98%"`.
            label: The caption under it.
            left: Inches from the left edge.
            top: Inches from the top edge.
            width: Width in inches.
            height: Height in inches.
            color: Brand token for the figure.
            value_size: Figure point size, snapped to the type scale.
            label_size: Caption point size, snapped to the type scale.
            fill: Brand token for an optional rounded backing panel.
            align: Alignment of both lines.

        Returns:
            The caption text box.
        """
        log.debug("slide %d: stat %r / %r at (%.2f, %.2f) %.2fx%.2fin", self.index,
                  value, label, left, top, width, height)
        self._record("stat", True)
        self._nesting += 1
        try:
            if fill:
                self.add_shape("rounded_rectangle", left, top, width, height, fill=fill,
                               line=None, corner_radius=0.08)
            self.add_textbox(value, left + 0.1, top + 0.05, width - 0.2, height * 0.6,
                             size=value_size, bold=True, color=color, align=align)
            return self.add_textbox(label, left + 0.1, top + height * 0.6, width - 0.2,
                                    height * 0.4 - 0.05, size=label_size,
                                    color="body", align=align)
        finally:
            self._nesting -= 1

    def add_icon_circle(self, text: str, left: float, top: float,
                        diameter: float = 0.8, fill: str = "red",
                        color: str = "white", size: Optional[float] = None) -> Any:
        """Filled circle with a short label or number - a brand-style icon substitute.

        Args:
            text: A digit, a pair of digits or a very short label.
            left: Inches from the left edge.
            top: Inches from the top edge.
            diameter: Circle diameter in inches.
            fill: Brand token for the circle.
            color: Brand token for the label.
            size: Label point size. Omit it and one is derived from the diameter, then
                snapped to the type scale - the reference's raw `diameter * 22` is
                off-scale for most diameters.

        Returns:
            The created oval.
        """
        if len(str(text)) > 4:
            log.warning("slide %d: icon circle carries %d characters (%r); it is meant "
                        "for a digit or two and longer text will not fit",
                        self.index, len(str(text)), text)
        log.debug("slide %d: icon circle %r at (%.2f, %.2f) d=%.2fin", self.index,
                  text, left, top, diameter)
        self._record("icon_circle", bool(str(text).strip()))
        self._nesting += 1
        try:
            return self.add_shape("oval", left, top, diameter, diameter, fill=fill,
                                  text=text, size=_snap_size(size or diameter * 22),
                                  color=color, bold=True)
        finally:
            self._nesting -= 1

    def add_choice(self, label: str, text: str, left: float, top: float, width: float,
                   height: float, correct: bool = False, accent: Optional[str] = None,
                   label_size: float = 32, text_size: float = 15,
                   label_color: str = "blue", text_color: str = "navy") -> Any:
        """One answer choice of a quiz question: a card, a letter badge and its text.

        Draw EVERY option of a quiz with this component and pass `correct=True` on the
        right one. The question slide shows all choices identically; the answer slide
        is the same program re-run with the reveal switched on, which is the only
        thing that highlights the correct card. Never highlight it yourself.

        Args:
            label: The option letter, e.g. "A".
            text: The option's wording.
            left: Inches from the left edge.
            top: Inches from the top edge.
            width: Width in inches.
            height: Height in inches.
            correct: True for the correct option.
            accent: Brand token for a thin bar along the card's top edge, or None.
            label_size: Letter point size, snapped to the type scale.
            text_size: Option point size, snapped to the type scale.
            label_color: Brand token for the letter.
            text_color: Brand token for the option text.

        Returns:
            The card shape.
        """
        label = str(label).strip()[:2] or "?"
        self._record("choice", True)
        self._nesting += 1
        try:
            return self._draw_choice(label, text, left, top, width, height, correct,
                                     accent, label_size, text_size, label_color,
                                     text_color)
        finally:
            self._nesting -= 1

    def _draw_choice(self, label: str, text: str, left: float, top: float,
                     width: float, height: float, correct: bool,
                     accent: Optional[str], label_size: float, text_size: float,
                     label_color: str, text_color: str) -> Any:
        """`add_choice`'s drawing, as one ledger entry (see `add_choice`)."""
        card = self.add_shape("rounded_rectangle", left, top, width, height,
                              fill="surface", line="border", corner_radius=0.06)
        card.name = f"{CHOICE_SHAPE_PREFIX}{label}-card"
        if accent:
            bar = self.add_shape("rectangle", left, top, width, min(0.1, height / 10),
                                 fill=accent)
            bar.name = f"{CHOICE_SHAPE_PREFIX}{label}-accent"
        badge_w = min(0.9, width * 0.25)
        badge = self.add_textbox(label, left + 0.18, top + 0.15, badge_w,
                                 max(height - 0.3, 0.3), size=label_size, bold=True,
                                 color=label_color, anchor="middle")
        badge.name = f"{CHOICE_SHAPE_PREFIX}{label}-label"
        body = self.add_textbox(text, left + 0.18 + badge_w, top + 0.15,
                                max(width - badge_w - 0.36, 0.4),
                                max(height - 0.3, 0.3), size=text_size,
                                color=text_color, anchor="middle")
        body.name = f"{CHOICE_SHAPE_PREFIX}{label}-text"
        self.choices.append({"label": label, "text": str(text), "correct": bool(correct),
                             "card": card})
        log.debug("slide %d: choice %s (%s) at (%.2f, %.2f) %.2fx%.2fin", self.index,
                  label, "correct" if correct else "distractor", left, top, width,
                  height)
        return card

    def _reveal_choices(self) -> int:
        """Restyle the correct choice card(s) as the template's answered state.

        Returns:
            How many cards were restyled.
        """
        revealed = 0
        for choice in self.choices:
            if not choice["correct"]:
                continue
            card = choice["card"]
            card.fill.solid()
            card.fill.fore_color.rgb = to_rgb(ANSWER_FILL_TOKEN, self.deck.brand)
            card.line.color.rgb = to_rgb(ANSWER_LINE_TOKEN, self.deck.brand)
            card.line.width = Pt(2)
            revealed += 1
        if self.choices and not revealed:
            self._warn("reveal requested but no choice was drawn with correct=True")
        return revealed

    def add_image(self, path: Union[str, Path], left: float, top: float,
                  width: Optional[float] = None,
                  height: Optional[float] = None) -> Any:
        """Picture fitted inside the given box, aspect ratio preserved.

        Args:
            path: Absolute, or relative to the deck's `asset_root`.
            left: Inches from the left edge.
            top: Inches from the top edge.
            width: Box width in inches.
            height: Box height in inches.

        Returns:
            The created picture, or None when the file does not exist - a missing
            asset is a warning, never a crash, because one bad path must not lose the
            whole deck.
        """
        file_path = self.deck.resolve_asset(path)
        if file_path is None:
            self._warn(f"image '{path}' not found - skipped")
            return None
        self._record("image", True)
        pic = self.slide.shapes.add_picture(
            str(file_path), Inches(left), Inches(top),
            width=Inches(width) if width else None,
            height=None if width else (Inches(height) if height else None))
        if width and height and pic.height > Inches(height):
            ratio = pic.width / pic.height
            pic.height = Inches(height)
            pic.width = int(Inches(height) * ratio)
        if width and height:  # centre inside the box
            pic.left = Inches(left) + int((Inches(width) - pic.width) / 2)
            pic.top = Inches(top) + int((Inches(height) - pic.height) / 2)
        # The picture keeps its aspect ratio, so what LANDED is rarely what was asked
        # for. That difference is exactly what the gate measures, so record it here.
        self._placed("image", Emu(pic.left).inches, Emu(pic.top).inches,
                     Emu(pic.width).inches, Emu(pic.height).inches,
                     file=Path(str(file_path)).name,
                     asked=f"{width or 0:.2f}x{height or 0:.2f}in")
        return pic

    def add_table(self, columns: Sequence[str], rows: Sequence[Sequence[Any]],
                  left: float, top: float, width: float, height: float,
                  font_size: float = 11,
                  col_widths: Optional[Sequence[float]] = None,
                  header_fill: str = "navy") -> Any:
        """Brand table: navy header, white uppercase header text, thin bottom rules.

        The branding is not optional and not a parameter set the caller has to get
        right - a table built through this method is on-brand by construction.

        Args:
            columns: Header labels; pass `[]` for a headerless table.
            rows: Body rows; short rows are padded with empty cells.
            left: Inches from the left edge.
            top: Inches from the top edge.
            width: Width in inches.
            height: Maximum height in inches; rows are capped at 0.55in each and the
                table is top-aligned inside the box.
            font_size: Point size, snapped to the type scale.
            col_widths: Relative column widths; rescaled to `width`.
            header_fill: Brand token for the header row.

        Returns:
            The created graphic frame.

        Raises:
            ValueError: When there is neither a header nor a body row.
        """
        columns = [str(c) for c in (columns or [])]
        rows = [[str(c) for c in r] for r in (rows or [])]
        n_cols = len(columns) or max((len(r) for r in rows), default=1)
        n_rows = len(rows) + (1 if columns else 0)
        if n_rows == 0:
            log.error("slide %d: add_table called with neither a header nor a row",
                      self.index)
            raise ValueError("table needs at least one row")
        ragged = [i for i, r in enumerate(rows) if len(r) != n_cols]
        if ragged:
            log.warning("slide %d: %d row(s) do not have %d cell(s) and are padded: "
                        "rows %s", self.index, len(ragged), n_cols, ragged[:6])
        font_size = _snap_size(font_size)
        self._record("table", True)
        # Compact rows, top-aligned inside the given area: a table that is shorter than
        # its box reads better than one stretched to fill it.
        row_height = Inches(min(height / n_rows, 0.55))
        frame = self.slide.shapes.add_table(
            n_rows, n_cols, Inches(left), Inches(top), Inches(width),
            row_height * n_rows)
        table = frame.table
        tbl_pr = frame._element.graphic.graphicData.tbl.tblPr
        tbl_pr.set("bandRow", "0")
        tbl_pr.set("firstRow", "1" if columns else "0")
        widths = (list(col_widths) if col_widths and len(col_widths) == n_cols
                  else [width / n_cols] * n_cols)
        # A caller that passes all-zero widths gets even columns, not a ZeroDivisionError.
        scale = width / (sum(widths) or 1)
        for i, w in enumerate(widths):
            table.columns[i].width = Inches(w * scale)
        brand = self.deck.brand
        for r in range(n_rows):
            table.rows[r].height = row_height
            is_header = bool(columns) and r == 0
            values = columns if is_header else rows[r - (1 if columns else 0)]
            for c in range(n_cols):
                cell = table.cell(r, c)
                value = values[c] if c < len(values) else ""
                cell.margin_left = cell.margin_right = Inches(0.08)
                cell.margin_top = cell.margin_bottom = Inches(0.04)
                cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                cell.fill.solid()
                cell.fill.fore_color.rgb = to_rgb(
                    header_fill if is_header else "white", brand)
                paragraph = cell.text_frame.paragraphs[0]
                self._write_paragraph(
                    paragraph, value.upper() if is_header else value, font_size,
                    is_header, "white" if is_header else "body", self.deck.body_font)
                _set_cell_borders(
                    cell, bottom=brand["border"] if not is_header else None)
        drawn_height = Emu(row_height * n_rows).inches
        self._placed("table", left, top, width, drawn_height, rows=n_rows,
                     cols=n_cols, size=font_size)
        if drawn_height < height - 0.05:
            log.debug("slide %d: table is %.2fin tall in a %.2fin box; rows are capped "
                      "at 0.55in and the table is top-aligned", self.index,
                      drawn_height, height)
        return frame

    def add_chart(self, chart_type: str, categories: Sequence[str],
                  series: Sequence[Dict[str, Any]], left: float, top: float,
                  width: float, height: float, title: Optional[str] = None,
                  show_legend: Optional[bool] = None, data_labels: bool = False,
                  number_format: Optional[str] = None) -> Any:
        """Native chart in brand colours.

        Args:
            chart_type: `"column"`, `"stacked_column"`, `"bar"`, `"stacked_bar"`,
                `"line"`, `"pie"`, `"doughnut"` or `"area"`.
            categories: Category axis labels.
            series: `[{"name": ..., "values": [...]}, ...]`. Series are coloured from
                the brand palette in order, so the caller never picks a colour.
            left: Inches from the left edge.
            top: Inches from the top edge.
            width: Width in inches.
            height: Height in inches.
            title: Chart title, or None for none.
            show_legend: Force the legend on or off; None shows one for multi-series
                and pie charts.
            data_labels: Draw value labels on the points.
            number_format: Excel number format for the data labels.

        Returns:
            The created graphic frame.

        Raises:
            ValueError: For an unknown chart type.
        """
        xl_type = _CHART_TYPES.get(str(chart_type).lower())
        if xl_type is None:
            log.error("slide %d: unknown chart_type %r; allowed: %s", self.index,
                      chart_type, ", ".join(_CHART_TYPES))
            raise ValueError(f"unknown chart_type '{chart_type}'. "
                             f"Allowed: {', '.join(_CHART_TYPES)}")
        data = CategoryChartData()
        data.categories = [str(c) for c in categories]
        for s in series:
            data.add_series(str(s.get("name", "Series")),
                            [float(v) for v in s["values"]])
        frame = self.slide.shapes.add_chart(
            xl_type, Inches(left), Inches(top), Inches(width), Inches(height), data)
        chart = frame.chart
        brand = self.deck.brand
        palette = [brand[k] for k in ("red", "blue", "orange", "light_blue", "green",
                                      "yellow", "navy", "caption")]
        chart.font.name = self.deck.body_font
        chart.font.size = Pt(11)
        chart.font.color.rgb = to_rgb("body", brand)
        chart.has_title = bool(title)
        if title:
            chart.chart_title.text_frame.text = title
            run = chart.chart_title.text_frame.paragraphs[0].runs[0]
            run.font.size = Pt(14)
            run.font.bold = True
            run.font.color.rgb = to_rgb("navy", brand)
        is_pie = xl_type in (XL_CHART_TYPE.PIE, XL_CHART_TYPE.DOUGHNUT)
        legend = show_legend if show_legend is not None else (len(series) > 1 or is_pie)
        chart.has_legend = legend
        if legend:
            chart.legend.position = XL_LEGEND_POSITION.BOTTOM
            chart.legend.include_in_layout = False
            chart.legend.font.size = Pt(10)
        plot = chart.plots[0]
        if is_pie:
            for i, point in enumerate(plot.series[0].points):
                point.format.fill.solid()
                point.format.fill.fore_color.rgb = RGBColor.from_string(
                    palette[i % len(palette)])
        else:
            for i, s in enumerate(plot.series):
                s.format.fill.solid()
                s.format.fill.fore_color.rgb = RGBColor.from_string(
                    palette[i % len(palette)])
                if xl_type == XL_CHART_TYPE.LINE_MARKERS:
                    s.format.line.color.rgb = RGBColor.from_string(
                        palette[i % len(palette)])
                    s.format.line.width = Pt(2.25)
                    s.smooth = False
            # Axis styling is best-effort: some chart types expose no value axis, and a
            # missing gridline is not worth losing the chart over.
            try:
                chart.value_axis.has_major_gridlines = True
                chart.value_axis.major_gridlines.format.line.color.rgb = to_rgb(
                    "border", brand)
                chart.value_axis.format.line.fill.background()
                chart.value_axis.tick_labels.font.size = Pt(10)
                chart.category_axis.tick_labels.font.size = Pt(10)
                chart.category_axis.format.line.color.rgb = to_rgb("border", brand)
            except Exception as exc:  # noqa: BLE001 - see comment above
                self._warn(f"chart axis styling skipped: {exc}")
        if data_labels:
            plot.has_data_labels = True
            plot.data_labels.font.size = Pt(10)
            if number_format:
                plot.data_labels.number_format = number_format
                plot.data_labels.number_format_is_linked = False
        self._placed(f"chart:{chart_type}", left, top, width, height,
                     series=len(series), categories=len(categories), legend=legend)
        self._record("chart", True)
        return frame

    # ---- finalisation ----------------------------------------------------------- #
    def finalize(self) -> None:
        """Drop unfilled placeholders, then stamp the frame.

        Order matters. The frame is applied last so nothing the program wrote can sit
        on top of the classification mark, and placeholder removal happens first so a
        never-filled body cannot leave "Click to edit" under the footer.

        Footer, date and slide-number placeholders are kept: they are template
        furniture, not content, and the template fills them itself.

        Idempotent: a program that calls `deck.save()` twice would otherwise stamp a
        second classification mark exactly on top of the first, and because frame
        shapes are exempt from the gate's rules nothing would report it.
        """
        if self._finalized:
            log.debug("slide %d already finalized; not stamping the frame again",
                      self.index)
            return
        self._finalized = True
        dropped = []
        if self.deck.remove_unused_placeholders:
            for idx, shape in self._placeholder_shapes().items():
                if idx in self.used_idx:
                    continue
                if shape.placeholder_format.type in (PP_PLACEHOLDER.SLIDE_NUMBER,
                                                     PP_PLACEHOLDER.FOOTER,
                                                     PP_PLACEHOLDER.DATE):
                    continue
                shape._element.getparent().remove(shape._element)
                dropped.append(idx)
        log.info("slide %d on %s finalized: %d placeholder(s) filled, %d unfilled "
                 "dropped%s", self.index, self.layout.key, len(self.used_idx),
                 len(dropped), f" (idx {dropped})" if dropped else "")
        self._apply_frame()

    def _apply_frame(self) -> None:
        """Stamp classification, logo and slide number. Never the model's to draw.

        `save()` finalises each handle exactly once and this is the only path that
        writes the frame, which is what keeps the rectangles the gate reserves and the
        rectangles that actually get filled the same rectangles.

        Every shape stamped here is named with `FRAME_SHAPE_PREFIX`. The furniture
        deliberately sits in the footer band, flush to the bottom of the canvas, so
        without that marker the gate would report the classification mark it placed
        itself as a rule 1 and rule 5 violation on every slide in the deck.
        """
        frame = self.deck.frame
        if not frame:
            log.debug("slide %d: no frame on this deck, nothing stamped", self.index)
            return
        stamped = []
        classification = frame.get("classification") or {}
        symbol_path = classification.get("symbol_path")
        label_text = classification.get("label_text")
        if symbol_path and Path(str(symbol_path)).is_file():
            left, top, width, height = classification["symbol"]
            picture = self.slide.shapes.add_picture(
                str(symbol_path), Inches(left), Inches(top),
                width=Inches(width), height=Inches(height))
            picture.name = f"{FRAME_SHAPE_PREFIX}classification-symbol"
            stamped.append("symbol")
        elif label_text:
            self._warn(f"no policy symbol for {label_text}; the label is stamped "
                       f"without it")
        if label_text:
            shape = self.add_textbox(label_text, *classification["label"],
                                     size=10, color="caption", align="left",
                                     anchor="middle")
            shape.name = f"{FRAME_SHAPE_PREFIX}classification-label"
            stamped.append(f"label {label_text!r}")

        logo = frame.get("logo")
        # `logo_is_placeable` is the single answer both this and `reserved_rects` use,
        # so the mark is reserved exactly where it is stamped and nowhere else.
        if (logo_is_placeable(frame, self.layout)
                and Path(str(logo.get("path") or "")).is_file()):
            left, top, width, _ = logo["rect"]
            mark = self.slide.shapes.add_picture(str(logo["path"]), Inches(left),
                                                 Inches(top), width=Inches(width))
            mark.name = f"{FRAME_SHAPE_PREFIX}logo"
            stamped.append(f"logo {logo.get('variant')}")

        number = frame.get("slide_number") or {}
        if number.get("value") is not None:
            shape = self.add_textbox(str(number["value"]), *number["rect"],
                                     size=10, color="caption", align="right",
                                     anchor="middle")
            shape.name = f"{FRAME_SHAPE_PREFIX}slide-number"
            stamped.append(f"number {number['value']}")
        log.info("slide %d frame stamped: %s", self.index,
                 ", ".join(stamped) or "nothing")


def _set_cell_borders(cell: Any, bottom: Optional[str] = None,
                      others_hidden: bool = True) -> None:
    """Hide a table cell's side borders and optionally draw a thin bottom rule.

    The four `a:ln*` elements have to be inserted in schema order at the head of
    `tcPr`, which is why they are built as a list and inserted by position rather than
    appended.

    Args:
        cell: A python-pptx table cell.
        bottom: Hex for the bottom rule, or None for no rule.
        others_hidden: Explicitly no-fill the left, right and top borders.
    """
    tc_pr = cell._tc.get_or_add_tcPr()
    for tag in ("a:lnL", "a:lnR", "a:lnT", "a:lnB"):
        for existing in tc_pr.findall(qn(tag)):
            tc_pr.remove(existing)
    elements = []
    for tag in ("a:lnL", "a:lnR", "a:lnT", "a:lnB"):
        ln = etree.Element(qn(tag))
        if tag == "a:lnB" and bottom:
            ln.set("w", str(int(0.75 * 12700)))
            solid = etree.SubElement(ln, qn("a:solidFill"))
            etree.SubElement(solid, qn("a:srgbClr")).set("val", bottom)
        elif others_hidden or tag != "a:lnB":
            ln.set("w", "0")
            etree.SubElement(ln, qn("a:noFill"))
        elements.append(ln)
    for i, ln in enumerate(elements):
        tc_pr.insert(i, ln)


# --------------------------------------------------------------------------------- #
# The deck
# --------------------------------------------------------------------------------- #
class DeckBuilder:
    """Opens the brand template, drops its sample slides and adds new ones.

    Every slide sits on a real template layout, so masters, theme colours and theme
    fonts are inherited rather than reproduced.
    """

    def __init__(self, template_path: Union[str, Path],
                 catalog: Union[Dict[str, Any], str, Path, CatalogView],
                 frame: Optional[Dict[str, Any]] = None,
                 asset_root: Optional[Union[str, Path]] = None,
                 remove_unused_placeholders: bool = True,
                 reveal_answers: Optional[bool] = None) -> None:
        """
        Args:
            template_path: The brand template. Every slide sits on one of its layouts.
            catalog: `TemplateCatalog.to_dict()`, or a path to that JSON.
            frame: The deck design contract's frame block, applied by `finalize()`.
                None means no frame - used by tests and by single-slide previews.
            asset_root: Directory relative image paths resolve against. The reference
                used a module-level `BASE_DIR`; there is no such thing inside the
                sandbox, so the caller supplies it.
            remove_unused_placeholders: Drop placeholders nothing filled, so no
                "Click to edit" prompt ever ships.
            reveal_answers: Highlight the correct quiz choice on save. None (the
                default for generated programs) reads the sandbox's reveal marker
                beside the output file instead.
        """
        self.template_path = Path(template_path)
        self.prs = Presentation(str(self.template_path))
        self.catalog = (catalog if isinstance(catalog, CatalogView)
                        else CatalogView(catalog))
        self.frame = frame or {}
        self.asset_root = Path(asset_root) if asset_root else None
        self.brand = brand_palette(self.catalog.theme.get("colors") or {})
        self.heading_font = self.catalog.theme.get("major_font") or "Barlow"
        self.body_font = self.catalog.theme.get("minor_font") or "Barlow"
        self.remove_unused_placeholders = remove_unused_placeholders
        self.reveal_answers = reveal_answers
        self.slide_width = self.catalog.slide_width
        self.slide_height = self.catalog.slide_height
        self.slides: List[SlideHandle] = []
        self._overridden_masters: Set[int] = set()
        log.info("deck opened from %s: %.2fx%.2fin, %d layout(s), fonts %s/%s, "
                 "frame=%s, asset_root=%s", self.template_path.name,
                 self.slide_width, self.slide_height, len(self.catalog.layouts),
                 self.heading_font, self.body_font, "yes" if self.frame else "no",
                 self.asset_root or "none")
        self._clear_slides()

    def _clear_slides(self) -> None:
        """Drop the template's own sample slides, keeping its masters and layouts."""
        sld_id_lst = self.prs.slides._sldIdLst
        dropped = 0
        for sld_id in list(sld_id_lst):
            r_id = sld_id.rId
            sld_id_lst.remove(sld_id)
            self.prs.part.drop_rel(r_id)
            dropped += 1
        log.debug("dropped %d sample slide(s) from the template", dropped)

    def _layout_object(self, view: LayoutView) -> Any:
        log.debug("resolving %s to master %d layout %d", view.key, view.master_index,
                  view.layout_index)
        return self.prs.slide_masters[view.master_index].slide_layouts[
            view.layout_index]

    def resolve_asset(self, path: Union[str, Path]) -> Optional[Path]:
        """Locate an image, absolutely or relative to `asset_root`.

        Args:
            path: The path a generated program asked for.

        Returns:
            An existing `Path`, or None when nothing matches. Resolution never raises:
            callers turn a None into a warning.
        """
        candidate = Path(str(path))
        if candidate.exists():
            return candidate
        if self.asset_root is not None:
            rooted = self.asset_root / str(path)
            if rooted.exists():
                log.debug("asset %s resolved under asset_root -> %s", path, rooted)
                return rooted
        log.warning("asset %r is on neither the given path nor under asset_root (%s)",
                    path, self.asset_root or "unset")
        return None

    def add_slide(self, layout: Union[str, LayoutView]) -> SlideHandle:
        """Add a slide on a template layout.

        Args:
            layout: A catalog key (`"M1-L10"`), an exact layout name, or a
                `LayoutView`.

        Returns:
            The `SlideHandle` for the new slide.

        Raises:
            LookupError: When no layout matches.
        """
        view = layout if isinstance(layout, LayoutView) else self.catalog.resolve(layout)
        if view.high_creativity and view.master_index not in self._overridden_masters:
            # Only a slide on the High Creativity master pays for this, once per deck:
            # every other slide's file keeps the template exactly as it is.
            apply_high_creativity_overrides(
                self.prs, view.master_index, self.catalog.preferred_master,
                self.heading_font,
                {l.layout_index: l.prompt_shapes for l in self.catalog.layouts
                 if l.master_index == view.master_index and l.prompt_shapes})
            self._overridden_masters.add(view.master_index)
        slide = self.prs.slides.add_slide(self._layout_object(view))
        handle = SlideHandle(self, slide, view)
        self.slides.append(handle)
        log.info("slide %d added on %s (%r): %d placeholder(s), %d decor rect(s)",
                 handle.index, view.key, view.name, len(view.placeholders),
                 len(view.decor_rects or ()))
        return handle

    def save(self, path: Union[str, Path]) -> Path:
        """Finalise every slide and write the deck.

        Args:
            path: Destination `.pptx`. Parent directories are created.

        Returns:
            The path written.
        """
        log.info("saving %d slide(s) to %s", len(self.slides), path)
        out = Path(path)
        reveal = (self.reveal_answers if self.reveal_answers is not None
                  else (out.parent / REVEAL_MARKER_FILENAME).is_file())
        if reveal:
            revealed = sum(handle._reveal_choices() for handle in self.slides)
            log.info("answer reveal: %d correct choice(s) highlighted", revealed)
        for handle in self.slides:
            handle.finalize()
        out.parent.mkdir(parents=True, exist_ok=True)
        self.prs.save(str(out))
        self._write_choices(out.parent, reveal)
        self._write_report(out.parent)
        warnings = self.warnings
        log.info("wrote %s (%d bytes, %d slide(s), %d warning(s))", out,
                 out.stat().st_size, len(self.slides), len(warnings))
        for warning in warnings:
            log.warning("deck warning: %s", warning)
        return out

    @property
    def warnings(self) -> List[str]:
        """Every slide's warnings, in slide order. The repair loop reads these."""
        return [w for handle in self.slides for w in handle.warnings]

    def _write_report(self, directory: Path) -> None:
        """Write the builder report sidecar: warnings and the component ledger.

        The repair loop reads it, because inside the sandbox it is the only channel
        a warning has back to the model.

        Args:
            directory: The output file's directory.
        """
        report = {"warnings": self.warnings,
                  "components": [dict(c, slide=handle.index)
                                 for handle in self.slides for c in handle.components],
                  "placeholders_filled": sum(len(h.used_idx) for h in self.slides)}
        (directory / REPORT_FILENAME).write_text(json.dumps(report, indent=1),
                                                 encoding="utf-8")
        log.debug("builder report: %d warning(s), %d component(s)",
                  len(report["warnings"]), len(report["components"]))

    def _write_choices(self, directory: Path, revealed: bool) -> None:
        """Write the quiz-choice sidecar, so the caller can verify the options drawn.

        Nothing is written when no choice was drawn.

        Args:
            directory: The output file's directory.
            revealed: Whether the correct choice was highlighted.
        """
        rows = [{"slide": handle.index, "label": c["label"], "text": c["text"],
                 "correct": c["correct"]}
                for handle in self.slides for c in handle.choices]
        if not rows:
            return
        (directory / CHOICES_FILENAME).write_text(
            json.dumps({"revealed": revealed, "choices": rows}, indent=1),
            encoding="utf-8")

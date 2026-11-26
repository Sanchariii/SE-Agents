"""The geometry gate: what actually landed on the slide, measured.

Rules 1-6 are decided by rectangle arithmetic alone, which is what lets the design
promise "no overlapping text" rather than estimate it. The one subtlety is that naive
pairwise overlap detection rejects every good design: a label inside a card, an accent
bar on a card's edge and a heading over a gradient panel all overlap something, and all
three are correct. The distinction this module draws is CONTAINMENT - overlap is a
collision, full containment is layering.

Rules 7-9 cannot be decided from declared rectangles: leftover prompt text, off-palette
colours and off-scale fonts are properties of the XML that was actually produced. That
is why `check_slide` reopens the file, and why it - not the `check_geometry` tool - is
authoritative. Rule 10 is the calibrated overflow estimate, advisory until the margin is
wide, because a heuristic should cost tokens rather than correctness.

Rules 11-14 keep a slide lean: no empty text boxes, no text repeated in two shapes, no
image placed twice and no image shown at the wrong aspect ratio. Rule 15 (advisory) is
the clutter check, decided from the builder's component ledger by `check_clutter`.

Violations are written as instructions, not assertions. They go straight back to the
model, and "card 3 overlaps card 2" is a fact it has to diagnose, while "shift card 3
right by 0.45in" is a fix it can apply.
"""

from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

from app.config.constants import GeometryConstants as GC
from app.utilities.logger_util import get_logger

#: Mirrors `deck_builder.ICON_SHAPE_PREFIX`: pre-built icons, which may repeat.
_ICON_SHAPE_PREFIX = "icon:"

log = get_logger(__name__)

#: Text the template leaves in an untouched placeholder. Rule 7.
PROMPT_PATTERNS: tuple[str, ...] = (
    "click to edit", "click to add", "second level", "third level", "fourth level",
    "fifth level", "master title style", "master text styles", "lorem ipsum",
)


@dataclass
class Box:
    """One shape as the gate sees it, in inches.

    Attributes:
        has_text: True when the shape has a text frame whose text is non-empty after
            stripping. This is the spec's definition of "text-bearing"; an empty text
            frame is not text.
        filled: True when the shape has a solid or gradient fill.
        is_picture: True for an image. Together with `filled` this is what makes a
            shape VISIBLE, which is what rule 3 is about.
        inherited_size: True for placeholder text, which rule 9 exempts entirely -
            including a size shrink-to-fit set explicitly.
        is_frame: True for the brand furniture `DeckBuilder.finalize()` stamps. It is
            not the program's output and is not measured; see `check_slide`.
    """
    id: str
    left: float
    top: float
    width: float
    height: float
    has_text: bool = False
    filled: bool = False
    text: str = ""
    font_size: Optional[float] = None
    font_name: Optional[str] = None
    colors: tuple[str, ...] = ()
    is_placeholder: bool = False
    inherited_size: bool = False
    is_picture: bool = False
    is_frame: bool = False
    #: A table or chart frame: opaque, and never a surface text may sit on.
    is_graphic: bool = False
    #: An added text box (not a placeholder, not an autoshape).
    is_textbox: bool = False
    #: Bulleted paragraphs, which lose a hanging indent of width.
    bullets: bool = False
    #: Most of the text is bold, which is wider.
    bold: bool = False
    #: The size rule 10 measures at: the explicit size, else the placeholder's own.
    measure_size: Optional[float] = None
    #: Rendered table height over the frame's height (0.0 when not a table).
    table_ratio: float = 0.0
    #: The image's content hash, for pictures.
    image_key: str = ""
    #: How far the shown aspect ratio is from the image's own (0.0 = exact).
    aspect_error: float = 0.0
    #: The text's paragraphs, for rule 12.
    paragraphs: tuple[str, ...] = ()

    @property
    def is_visible_surface(self) -> bool:
        """True when this shape paints something text could land on top of.

        An unfilled, textless box paints nothing - an emptied placeholder, a spacer,
        a group frame - so text crossing it obscures nothing and rule 3 must not fire.
        A fill, an image, a table or a chart is a surface, and half-covering one is a
        real design error.
        """
        return self.filled or self.is_picture or self.is_graphic

    @property
    def right(self) -> float:
        return self.left + self.width

    @property
    def bottom(self) -> float:
        return self.top + self.height

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.left, self.top, self.width, self.height)

    def describe(self) -> str:
        """The shape as a violation message names it, with its first words of text."""
        snippet = " ".join((self.text or "").split())
        label = f' "{snippet[:40]}{"..." if len(snippet) > 40 else ""}"' if snippet else ""
        return (f"{self.id}{label} at ({self.left:.2f}, {self.top:.2f}) "
                f"{self.width:.2f}x{self.height:.2f}in")


@dataclass
class Violation:
    """One thing wrong with the slide, phrased as an instruction."""
    rule: int
    kind: str
    message: str
    blocking: bool = True


def intersect(a: Box, b: Box) -> tuple[float, float]:
    """Overlap of two boxes on each axis, in inches. Negative means separated."""
    return (min(a.right, b.right) - max(a.left, b.left),
            min(a.bottom, b.bottom) - max(a.top, b.top))


def overlaps(a: Box, b: Box, tolerance: float = GC.OVERLAP_TOLERANCE_IN) -> bool:
    """True when two boxes overlap by more than a hairline on BOTH axes."""
    dx, dy = intersect(a, b)
    return dx > tolerance and dy > tolerance


def contains(outer: Box, inner: Box,
             tolerance: float = GC.CONTAINMENT_TOLERANCE_IN) -> bool:
    """True when `inner` sits wholly inside `outer`, allowing a hairline of bleed."""
    return (inner.left >= outer.left - tolerance
            and inner.top >= outer.top - tolerance
            and inner.right <= outer.right + tolerance
            and inner.bottom <= outer.bottom + tolerance)


# --------------------------------------------------------------------------- #
# rules 1-7, decidable from rectangles                                         #
#
# One principle runs through rules 1 to 5: a violation is only worth reporting when at
# least one side of it is something THE PROGRAM ADDED. A placeholder's rectangle is the
# template's, not the program's - the component API fills placeholders, it never moves
# them - so a placeholder that sits outside the margin, or over the layout's own decor,
# or over another placeholder, is the template's arrangement and the model has no way
# to change it. On this template that is not a rare edge: 63 of 181 layouts have a
# placeholder outside the 0.30in margin and 29 have two of their own placeholders
# overlapping. Reporting those would hand the model violations it can only lose repair
# rounds to, and would fail slides that are exactly what the template intended.
#
# Rules 7 to 10 are different and are NOT exempt: leaving prompt text in a placeholder,
# recolouring it, restyling it or overfilling it are all things the program did.
# --------------------------------------------------------------------------- #
def check_rects(boxes: Sequence[Box], *, slide_width: float, slide_height: float,
                reserved: Iterable[Sequence[float]] = ()) -> list[Violation]:
    """Rules 1 to 6, decided from rectangles alone.

    Args:
        boxes: Every shape on the slide.
        slide_width: Canvas width in inches.
        slide_height: Canvas height in inches.
        reserved: Frame rectangles and the layout's inherited decor, as
            (left, top, width, height) - the zones rule 5 protects.

    Returns:
        Every violation found, in rule order. Empty means the slide is clean.
    """
    zones = list(reserved or ())
    log.debug("checking rules 1-6 over %d box(es) (%d placeholder, %d text-bearing, "
              "%d filled) on a %.2fx%.2fin canvas with %d reserved zone(s)",
              len(boxes), sum(1 for b in boxes if b.is_placeholder),
              sum(1 for b in boxes if b.has_text),
              sum(1 for b in boxes if b.filled), slide_width, slide_height, len(zones))
    violations: list[Violation] = []
    for rule, found in (
            (1, _rule_1_inside_canvas(boxes, slide_width, slide_height)),
            (2, _rule_2_text_over_text(boxes)),
            (3, _rule_3_text_over_shape(boxes)),
            (4, _rule_4_filled_over_filled(boxes)),
            (5, _rule_5_reserved_zones(boxes, zones)),
            (6, _rule_6_filled_placeholders(boxes))):
        if found:
            log.debug("rule %d: %d violation(s)", rule, len(found))
        violations += found
    return violations


def _both_from_the_template(a: Box, b: Box) -> bool:
    """True when neither side of a pair is something the program added."""
    return a.is_placeholder and b.is_placeholder


def _rule_1_inside_canvas(boxes: Sequence[Box], slide_width: float,
                          slide_height: float) -> list[Violation]:
    margin, slack = GC.CANVAS_MARGIN_IN, GC.CANVAS_TOLERANCE_IN
    out: list[Violation] = []
    for shape in (b for b in boxes if not b.is_placeholder):
        overhang = {
            "left": (margin - slack) - shape.left,
            "top": (margin - slack) - shape.top,
            "right": shape.right - (slide_width - margin + slack),
            "bottom": shape.bottom - (slide_height - margin + slack),
        }
        worst = max(overhang.items(), key=lambda item: item[1])
        if worst[1] > 0:
            out.append(Violation(1, "canvas", (
                f"{shape.describe()} runs {worst[1]:.2f}in past the {worst[0]} edge of "
                f"the {slide_width:.2f}x{slide_height:.2f}in canvas; move it "
                f"{worst[1] + 0.05:.2f}in towards the centre or shrink it to fit inside "
                f"the {margin:.2f}in margin")))
    return out


def _rule_2_text_over_text(boxes: Sequence[Box]) -> list[Violation]:
    out: list[Violation] = []
    text_boxes = [b for b in boxes if b.has_text]
    for index, a in enumerate(text_boxes):
        for b in text_boxes[index + 1:]:
            if _both_from_the_template(a, b) or not overlaps(a, b):
                continue
            dx, dy = intersect(a, b)
            out.append(Violation(2, "text-over-text", (
                f"{a.describe()} overlaps {b.describe()} by {dx:.2f}x{dy:.2f}in. Text "
                f"may never overlap text. Shift {b.id} right by {dx + 0.05:.2f}in, or "
                f"narrow both to {(a.width - dx / 2):.2f}in, or move {b.id} down by "
                f"{dy + 0.05:.2f}in")))
    return out


def _rule_3_text_over_shape(boxes: Sequence[Box]) -> list[Violation]:
    out: list[Violation] = []
    # Only shapes that actually paint a surface count. See Box.is_visible_surface.
    shapes = [b for b in boxes if not b.has_text and b.is_visible_surface]
    for text in (b for b in boxes if b.has_text):
        for shape in shapes:
            # A table or chart is never a surface text may be layered on.
            if (_both_from_the_template(text, shape) or not overlaps(text, shape)
                    or (contains(shape, text) and not shape.is_graphic)):
                continue
            out.append(Violation(3, "text-over-shape", (
                f"{text.describe()} partly covers {shape.describe()}. Text over a shape "
                f"must sit wholly inside it: either move {text.id} fully within "
                f"({shape.left:.2f}, {shape.top:.2f}) to "
                f"({shape.right:.2f}, {shape.bottom:.2f}), or move it clear of "
                f"{shape.id} altogether")))
    return out


def _rule_4_filled_over_filled(boxes: Sequence[Box]) -> list[Violation]:
    out: list[Violation] = []
    # Tables and charts are opaque: they collide with fills like a fill does.
    filled = [b for b in boxes if b.filled or b.is_graphic]
    for index, a in enumerate(filled):
        for b in filled[index + 1:]:
            if (_both_from_the_template(a, b) or not overlaps(a, b)
                    or contains(a, b) or contains(b, a)):
                continue
            dx, dy = intersect(a, b)
            out.append(Violation(4, "fill-over-fill", (
                f"{a.describe()} and {b.describe()} overlap by {dx:.2f}x{dy:.2f}in "
                f"without one containing the other. Two filled shapes may only nest or "
                f"stand apart: separate them by {dx + 0.05:.2f}in, or place {b.id} "
                f"entirely inside {a.id}")))
    return out


def _rule_5_reserved_zones(boxes: Sequence[Box],
                           reserved: Iterable[Sequence[float]]) -> list[Violation]:
    out: list[Violation] = []
    zones = [Box(id=f"reserved zone {i + 1}", left=float(r[0]), top=float(r[1]),
                 width=float(r[2]), height=float(r[3]))
             for i, r in enumerate(reserved or ())]
    # Only what the program ADDED. A placeholder sits where the template put it, and
    # the program cannot move it - the component API fills placeholders, it does not
    # reposition them. On 89 of this template's 181 layouts a placeholder overlaps the
    # layout's own decor, so measuring them here would make half the template
    # unusable and hand the model a violation it has no way to fix.
    for shape in (b for b in boxes if not b.is_placeholder):
        for zone in zones:
            if not overlaps(shape, zone):
                continue
            dx, dy = intersect(shape, zone)
            out.append(Violation(5, "reserved", (
                f"{shape.describe()} intrudes {dx:.2f}x{dy:.2f}in into "
                f"{zone.describe()}, which carries brand furniture (the footer band, "
                f"the classification mark, or a graphic the layout already draws). "
                f"Keep every element clear of it: move {shape.id} up by "
                f"{dy + 0.05:.2f}in or shorten it to {shape.height - dy - 0.05:.2f}in")))
    return out


def _rule_6_filled_placeholders(boxes: Sequence[Box]) -> list[Violation]:
    out: list[Violation] = []
    placeholders = [b for b in boxes if b.is_placeholder and b.filled]
    for shape in (b for b in boxes if not b.is_placeholder):
        for placeholder in placeholders:
            if not overlaps(shape, placeholder):
                continue
            dx, dy = intersect(shape, placeholder)
            out.append(Violation(6, "over-placeholder", (
                f"{shape.describe()} covers the filled placeholder "
                f"{placeholder.describe()} by {dx:.2f}x{dy:.2f}in. Either place "
                f"{shape.id} in free space, or fill that placeholder through the "
                f"placeholder API instead of drawing over it")))
    return out


def check_prompt_text(boxes: Sequence[Box]) -> list[Violation]:
    """Rule 7: no shape may still carry the template's own prompt copy."""
    out: list[Violation] = []
    for shape in boxes:
        lowered = (shape.text or "").strip().lower()
        if not lowered:
            continue
        hit = next((pattern for pattern in PROMPT_PATTERNS if pattern in lowered), None)
        if hit:
            log.debug("rule 7: %s still carries prompt text %r", shape.id, hit)
            out.append(Violation(7, "prompt-text", (
                f"{shape.describe()} still shows the template's prompt text "
                f"({hit!r}). Fill it with real content, or remove the placeholder so "
                f"nothing ships with a 'Click to edit' prompt on it")))
    return out


# --------------------------------------------------------------------------- #
# reading a real slide, and rules 8-10                                         #
# --------------------------------------------------------------------------- #
def boxes_from_slide(slide: Any, layout: Any) -> list[Box]:
    """Read a rendered slide into the boxes the rules are decided over.

    Measures what LANDED, not what was declared: a component that python-pptx resized,
    a picture that kept its aspect ratio, a table that grew a row. That is the whole
    point of reopening the file rather than trusting the program's own arithmetic.

    Args:
        slide: A python-pptx slide from the reopened file.
        layout: The `LayoutInfo` it was built on. Carried for symmetry with
            `check_slide`; the decor it contributes is handled as reserved zones.

    Returns:
        One `Box` per shape, in the slide's own shape order.
    """
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    from pptx.util import Emu

    boxes: list[Box] = []
    log.debug("reading slide on layout %s", getattr(layout, "key", "?"))
    # The template's own sizes, so inherited placeholder text can be measured too.
    inherited = {p.idx: p for p in (getattr(layout, "placeholders", None) or [])}
    for index, shape in enumerate(slide.shapes):
        text = ""
        paragraphs: list[str] = []
        if shape.has_text_frame:
            text = shape.text_frame.text or ""
            paragraphs = [p.text for p in shape.text_frame.paragraphs if p.text.strip()]
        info = None
        if shape.is_placeholder:
            try:
                info = inherited.get(shape.placeholder_format.idx)
            except Exception:                                # noqa: BLE001
                info = None
        explicit = _font_size(shape)
        picture = _is_picture(shape)
        image_key, aspect_error = _image_facts(shape) if picture else ("", 0.0)
        if str(getattr(shape, "name", "")).startswith(_ICON_SHAPE_PREFIX):
            # A pre-built icon may repeat (one per card), so rule 13 never counts it.
            image_key = ""
        try:
            is_textbox = shape.shape_type == MSO_SHAPE_TYPE.TEXT_BOX
        except Exception:                                    # noqa: BLE001
            is_textbox = False
        boxes.append(Box(
            id=_shape_label(shape, index),
            left=Emu(shape.left or 0).inches, top=Emu(shape.top or 0).inches,
            width=Emu(shape.width or 0).inches, height=Emu(shape.height or 0).inches,
            has_text=bool(text.strip()),
            filled=_is_filled(shape),
            text=text,
            font_size=_font_size(shape),
            font_name=_font_name(shape),
            colors=tuple(_colors(shape)),
            is_placeholder=bool(shape.is_placeholder),
            is_picture=picture,
            is_frame=_is_frame(shape),
            is_graphic=bool(getattr(shape, "has_table", False)
                            or getattr(shape, "has_chart", False)),
            is_textbox=is_textbox,
            bullets=_is_bulleted(paragraphs, info),
            bold=_is_bold(shape),
            measure_size=explicit or (getattr(info, "font_size", None) if info else None),
            table_ratio=_table_ratio(shape),
            image_key=image_key,
            aspect_error=aspect_error,
            paragraphs=tuple(paragraphs),
            # ALL placeholder text is exempt from rule 9, not merely text whose size
            # was inherited. The spec is explicit: "Placeholder text is exempt. Sizes
            # inherited from the template's own placeholders are correct by definition,
            # and shrink-to-fit may reduce them as far as 10pt." Shrink-to-fit
            # decrements by 1pt from the placeholder's own base, so it routinely writes
            # an explicit 13pt or 17pt - off the type scale, and correct. Keying the
            # exemption on "no explicit size" would make rule 9 fire on every
            # placeholder the builder shrank to make the copy fit.
            inherited_size=bool(shape.is_placeholder),
        ))
    zero_sized = [b.id for b in boxes if b.width <= 0 or b.height <= 0]
    if zero_sized:
        # python-pptx reports None for a dimension the layout never set, which becomes
        # a 0x0 box the rules can never fault. Worth seeing when a slide passes the
        # gate but looks wrong.
        log.warning("%d shape(s) have no resolvable size and cannot be measured: %s",
                    len(zero_sized), ", ".join(zero_sized[:6]))
    log.debug("read %d shape(s): %d frame, %d placeholder, %d text-bearing, %d picture",
              len(boxes), sum(1 for b in boxes if b.is_frame),
              sum(1 for b in boxes if b.is_placeholder),
              sum(1 for b in boxes if b.has_text),
              sum(1 for b in boxes if b.is_picture))
    return boxes


def _shape_label(shape: Any, index: int) -> str:
    """A name the model will recognise in a violation message."""
    name = (getattr(shape, "name", "") or "").strip()
    if shape.is_placeholder:
        try:
            return f"placeholder idx {shape.placeholder_format.idx} ({name})"
        except Exception:                                    # noqa: BLE001
            pass
    return name or f"shape {index + 1}"


def _is_filled(shape: Any) -> bool:
    """Solid or gradient fill, per the spec's definition of 'filled'."""
    from pptx.oxml.ns import qn

    element = shape._element.find(qn("p:spPr"))
    if element is None:
        return False
    return (element.find(qn("a:solidFill")) is not None
            or element.find(qn("a:gradFill")) is not None)


def _is_frame(shape: Any) -> bool:
    """True for a shape `DeckBuilder._apply_frame` stamped, by its name.

    The prefix is imported from `deck_builder` rather than duplicated, so renaming it
    there cannot silently turn the whole frame back into violations.
    """
    from app.genai.ppt.deck_builder import FRAME_SHAPE_PREFIX

    return (getattr(shape, "name", "") or "").startswith(FRAME_SHAPE_PREFIX)


def _is_picture(shape: Any) -> bool:
    """True for an image, which paints a surface even with no fill of its own.

    A filled picture placeholder reports itself as a PLACEHOLDER, so the element tag
    (`p:pic`) is checked too.
    """
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    from pptx.oxml.ns import qn

    if getattr(shape, "_element", None) is not None and shape._element.tag == qn("p:pic"):
        return True
    try:
        return shape.shape_type in (MSO_SHAPE_TYPE.PICTURE,
                                    MSO_SHAPE_TYPE.LINKED_PICTURE)
    except Exception as exc:                                 # noqa: BLE001
        # shape_type raises on a few exotic shapes; an unknown shape is not a picture.
        log.debug("shape %r has no readable shape_type (%s); treating it as not a "
                  "picture, so rule 3 will not guard it", getattr(shape, "name", "?"),
                  exc)
        return False


def _image_facts(shape: Any) -> tuple[str, float]:
    """A picture's content hash, and how far its shown aspect is from its own.

    Returns:
        (sha1 of the image bytes, relative aspect error); ("", 0.0) when unreadable.
    """
    try:
        image = shape.image
        px_w, px_h = image.size
        crop_w = 1.0 - (shape.crop_left or 0.0) - (shape.crop_right or 0.0)
        crop_h = 1.0 - (shape.crop_top or 0.0) - (shape.crop_bottom or 0.0)
        native = (px_w * crop_w) / max(px_h * crop_h, 1e-6)
        shown = (shape.width or 0) / max(shape.height or 1, 1)
        error = abs(shown / native - 1.0) if native > 0 and shown > 0 else 0.0
        return image.sha1, error
    except Exception as exc:                                 # noqa: BLE001
        log.debug("picture %r could not be read (%s); rules 13-14 skip it",
                  getattr(shape, "name", "?"), exc)
        return "", 0.0


def _is_bulleted(paragraphs: Sequence[str], info: Any) -> bool:
    """True when the text reads as bullets (a glyph prefix, or a bulleted placeholder)."""
    if any(p.lstrip().startswith(("\u2022", "- ", "* ")) for p in paragraphs):
        return True
    return bool(info is not None and getattr(info, "bullets", False) and len(paragraphs) > 1)


def _is_bold(shape: Any) -> bool:
    """True when most of the shape's characters are explicitly bold."""
    runs = [r for r in _runs(shape) if r.text.strip()]
    total = sum(len(r.text) for r in runs)
    bold = sum(len(r.text) for r in runs if r.font.bold)
    return bool(total) and bold * 2 > total


def _table_ratio(shape: Any) -> float:
    """A table's estimated rendered height over its frame height; 0.0 for non-tables.

    PowerPoint grows a row to fit its wrapped text, so a table that fits its frame on
    paper can run into the footer. Each row is as tall as its tallest cell.
    """
    if not getattr(shape, "has_table", False):
        return 0.0
    from pptx.util import Emu

    from app.genai.ppt import measure

    try:
        table = shape.table
        widths = [Emu(c.width).inches for c in table.columns]
        needed = 0.0
        for row in table.rows:
            tallest = Emu(row.height).inches
            for cell, width in zip(row.cells, widths):
                lines = [p.text for p in cell.text_frame.paragraphs if p.text.strip()]
                sizes = [r.font.size.pt for p in cell.text_frame.paragraphs
                         for r in p.runs if r.font.size is not None]
                size = max(sizes) if sizes else 11.0
                count = measure.rendered_lines(lines, width + 0.04, size)
                tallest = max(tallest, count * size * measure.LINE_HEIGHT_FACTOR / 72.0
                              + 0.08)
            needed += tallest
        frame = Emu(shape.height or 0).inches
        return needed / frame if frame > 0 else 0.0
    except Exception as exc:                                 # noqa: BLE001
        log.debug("table %r could not be measured (%s)", getattr(shape, "name", "?"), exc)
        return 0.0


def _runs(shape: Any) -> list:
    if not shape.has_text_frame:
        return []
    return [run for paragraph in shape.text_frame.paragraphs for run in paragraph.runs]


def _font_size(shape: Any) -> Optional[float]:
    """The largest explicitly set size on the shape, or None when all are inherited."""
    sizes = [run.font.size.pt for run in _runs(shape) if run.font.size is not None]
    return max(sizes) if sizes else None


def _font_name(shape: Any) -> Optional[str]:
    names = [run.font.name for run in _runs(shape) if run.font.name]
    return names[0] if names else None


def _colors(shape: Any) -> list[str]:
    """Every explicit RGB in the shape's fill, line and runs, as uppercase hex."""
    from pptx.oxml.ns import qn

    found: list[str] = []
    for node in shape._element.iter(qn("a:srgbClr")):
        value = node.get("val")
        if value:
            found.append(value.upper())
    return found


def check_colors(boxes: Sequence[Box], palette: dict) -> list[Violation]:
    """Rule 8: every explicit colour resolves to a theme or brand token.

    Args:
        boxes: The slide's shapes, carrying the hex values found in their XML.
        palette: `deck_builder.brand_palette(theme_colors)`.

    Returns:
        One violation per shape that used a colour outside the palette.
    """
    allowed = {str(value).upper() for value in palette.values()}
    if not allowed:
        # An empty palette would fault every colour on the slide. Say so once rather
        # than emitting one violation per shape for a theme that never loaded.
        log.warning("rule 8 has an empty palette; every explicit colour will be "
                    "reported as off-theme")
    out: list[Violation] = []
    for shape in boxes:
        stray = sorted({c for c in shape.colors if c.upper() not in allowed})
        if stray:
            log.debug("rule 8: %s uses %s, outside the %d-token palette",
                      shape.id, ", ".join(stray), len(allowed))
            out.append(Violation(8, "off-palette", (
                f"{shape.describe()} uses {', '.join(stray)}, which is not a template "
                f"colour. Every fill, line and font colour must come from a brand token "
                f"({', '.join(sorted(palette))}) so the deck stays on the template's "
                f"theme")))
    return out


def check_typography(boxes: Sequence[Box],
                     theme_font: str = "Barlow") -> list[Violation]:
    """Rule 9: the theme font everywhere, and a type-scale size on component text.

    Placeholder text is exempt from the size rule entirely: the template's own choices
    are correct by definition, and the builder's shrink-to-fit writes explicit
    off-scale sizes down to 10pt while doing exactly what it should.

    Args:
        boxes: The slide's shapes.
        theme_font: The template theme's body font.

    Returns:
        One violation per offending shape, per problem.
    """
    out: list[Violation] = []
    for shape in boxes:
        if not shape.has_text:
            continue
        if shape.font_name and shape.font_name != theme_font:
            log.debug("rule 9: %s is set in %r, not the theme font %r",
                      shape.id, shape.font_name, theme_font)
            out.append(Violation(9, "font", (
                f"{shape.describe()} is set in {shape.font_name!r}. Every run must use "
                f"the template theme font {theme_font!r} - do not set `font.name` at "
                f"all and it is inherited correctly")))
        if shape.inherited_size or shape.font_size is None:
            continue
        if int(round(shape.font_size)) not in GC.TYPE_SCALE:
            nearest = min(GC.TYPE_SCALE, key=lambda s: abs(s - shape.font_size))
            log.debug("rule 9: %s uses %gpt, off the type scale (nearest %dpt)",
                      shape.id, shape.font_size, nearest)
            out.append(Violation(9, "type-scale", (
                f"{shape.describe()} uses {shape.font_size:g}pt, which is not on the "
                f"type scale. Use one of "
                f"{', '.join(f'{s}pt' for s in GC.TYPE_SCALE)} - nearest is "
                f"{nearest}pt")))
    return out


def check_overflow(boxes: Sequence[Box]) -> list[Violation]:
    """Rule 10: estimated lines against the box's capacity.

    Advisory by default and blocking only past a wide margin, because the estimate is a
    heuristic biased to under-estimate capacity (Decision 7). A false positive here
    should cost tokens, not correctness.

    Args:
        boxes: The slide's shapes. Only those with text and an explicit size count -
            without a size there is nothing to measure against.

    Returns:
        One violation per overflowing shape, `blocking` only past
        `OVERFLOW_BLOCKING_RATIO`.
    """
    from app.genai.ppt import measure

    out: list[Violation] = []
    for shape in boxes:
        if shape.table_ratio > GC.OVERFLOW_ADVISORY_RATIO:
            blocking = shape.table_ratio >= GC.OVERFLOW_BLOCKING_RATIO
            out.append(Violation(10, "table-overflow", (
                f"{shape.describe()} needs about "
                f"{shape.height * shape.table_ratio:.2f}in once its rows grow to fit "
                f"their text, {int(round((shape.table_ratio - 1) * 100))}% more than "
                f"its frame. Shorten the cell text, drop a column, or move the detail "
                f"to the speaker notes"), blocking=blocking))
        size = shape.measure_size or shape.font_size
        if not shape.has_text or not size:
            continue
        lines = [line for line in shape.text.splitlines() if line.strip()]
        if shape.font_size is None and measure.rendered_lines(
                lines, shape.width, size, shape.bullets, shape.bold) <= measure.line_capacity(
                    shape.height, size):
            # Inherited template text is judged in whole lines, as the builder's
            # shrink-to-fit judges it: the template sized a one-line title box for one
            # line of its own type, which a fractional-height ratio would fault.
            continue
        ratio = measure.overflow_ratio(lines, shape.width, shape.height, size,
                                       bullets=shape.bullets, bold=shape.bold)
        if ratio <= GC.OVERFLOW_ADVISORY_RATIO:
            continue
        blocking = ratio >= GC.OVERFLOW_BLOCKING_RATIO
        excess = int(round((ratio - 1.0) * 100))
        target = max(10, int(len(" ".join(lines)) / ratio))
        log.debug("rule 10: %s is %d%% over at %gpt (ratio %.2f) - %s",
                  shape.id, excess, size, ratio,
                  "blocking" if blocking else "advisory")
        out.append(Violation(10, "overflow", (
            f"{shape.describe()} holds about {excess}% more text than fits at "
            f"{size:g}pt. Shorten the copy to about {target} characters, move the "
            f"detail to the speaker notes, or give the box "
            f"{shape.height * ratio - shape.height + 0.05:.2f}in more height"
        ), blocking=blocking))
    return out


def _normalized(text: str) -> str:
    """Text reduced for comparison: case, whitespace and bullet glyphs ignored."""
    return " ".join(text.replace("\u2022", " ").casefold().split())


def check_content(boxes: Sequence[Box]) -> list[Violation]:
    """Rules 11-12: no empty text boxes, and no text shown twice.

    Args:
        boxes: The slide's shapes (brand furniture already excluded).

    Returns:
        One violation per empty text box and per repeated paragraph.
    """
    out: list[Violation] = []
    for shape in boxes:
        if (shape.is_textbox and not shape.is_placeholder and not shape.has_text
                and not shape.filled):
            out.append(Violation(11, "empty-text", (
                f"{shape.describe()} is an empty text box. Remove it - an element that "
                f"carries nothing only adds clutter")))
    seen: dict[str, Box] = {}
    for shape in boxes:
        if not shape.has_text:
            continue
        for paragraph in dict.fromkeys(_normalized(p) for p in shape.paragraphs):
            if len(paragraph) < GC.DUPLICATE_TEXT_MIN_CHARS:
                continue
            first = seen.get(paragraph)
            if first is not None and first is not shape:
                out.append(Violation(12, "duplicate-text", (
                    f"{shape.describe()} repeats text already shown in {first.id} "
                    f"(\"{paragraph[:50]}\"). Show each line once: remove the repeat, "
                    f"or replace it with what it was meant to add")))
            else:
                seen.setdefault(paragraph, shape)
    return out


def check_images(boxes: Sequence[Box]) -> list[Violation]:
    """Rules 13-14: each image once per slide, and never distorted.

    Args:
        boxes: The slide's shapes.

    Returns:
        One violation per repeated image and per stretched one.
    """
    out: list[Violation] = []
    seen: dict[str, Box] = {}
    for shape in (b for b in boxes if b.is_picture and b.image_key):
        first = seen.get(shape.image_key)
        if first is not None:
            out.append(Violation(13, "duplicate-image", (
                f"{shape.describe()} shows the same image as {first.id}. Place each "
                f"image once; remove the repeat")))
        else:
            seen[shape.image_key] = shape
        if shape.aspect_error > GC.IMAGE_ASPECT_TOLERANCE:
            out.append(Violation(14, "distorted-image", (
                f"{shape.describe()} shows its image {shape.aspect_error:.0%} off its own "
                f"aspect ratio, so it looks stretched. Place it with add_image, which "
                f"keeps the aspect ratio, or in a slot of a matching shape")))
    return out


def check_clutter(components: Sequence[dict], placeholders_filled: int = 0
                  ) -> list[Violation]:
    """Rule 15 (advisory): decorative elements outnumbering the content they frame.

    Args:
        components: The builder's ledger, one {"kind", "content"} per component call.
        placeholders_filled: Template placeholders the program filled with content.

    Returns:
        One advisory violation when the slide carries more decorative components than
        `max(CLUTTER_MIN_DECORATIVE, content components)`, else nothing.
    """
    decorative = [c for c in components if not c.get("content")]
    content = len(components) - len(decorative) + placeholders_filled
    allowed = max(GC.CLUTTER_MIN_DECORATIVE, content)
    if len(decorative) <= allowed:
        return []
    kinds = ", ".join(sorted({str(c.get("kind")) for c in decorative}))
    log.debug("rule 15: %d decorative component(s) (%s) against %d content",
              len(decorative), kinds, content)
    return [Violation(15, "clutter", (
        f"The slide draws {len(decorative)} decorative element(s) ({kinds}) that carry no "
        f"content, against {content} that do. Simplify: remove "
        f"{len(decorative) - allowed} or more of them, keeping only what guides the eye"),
        blocking=False)]


# --------------------------------------------------------------------------- #
# the authoritative entry point                                                #
# --------------------------------------------------------------------------- #
def check_slide(pptx_path, layout, *, frame: Optional[dict] = None,
                theme_colors: Optional[dict] = None, theme_font: str = "Barlow",
                slide_width: float = 13.333,
                slide_height: float = 7.5) -> list[Violation]:
    """Every rule, against the file that was actually produced.

    This is authoritative. `check_geometry` (the design tool) evaluates rules 1-6 and
    10 from declared rectangles as a fast path, but rules 7-9 depend on what the XML
    really contains, so only this function can clear a slide.

    Args:
        pptx_path: The single-slide file the program wrote.
        layout: The `LayoutInfo` the slide was built on, for its decor rectangles.
        frame: The deck contract's frame block, whose rectangles are reserved.
        theme_colors: The template theme's colour map, for rule 8.
        theme_font: The template theme's body font, for rule 9.
        slide_width: Canvas width in inches.
        slide_height: Canvas height in inches.

    Returns:
        Every violation, in rule order. `is_clean()` decides whether it may ship.
    """
    from pptx import Presentation

    from app.genai.ppt import deck_builder as builder

    layout_key = getattr(layout, "key", "?")
    log.info("gating %s on layout %s (frame=%s, theme_font=%r, canvas %.2fx%.2fin)",
             getattr(pptx_path, "name", pptx_path), layout_key,
             "yes" if frame else "no", theme_font, slide_width, slide_height)

    presentation = Presentation(str(pptx_path))
    slides = list(presentation.slides)
    if len(slides) > 1:
        # One generated file is meant to hold exactly one slide. Only the first is
        # measured, so extras would ship past the gate unchecked.
        log.warning("%s holds %d slides; only the first is gated",
                    getattr(pptx_path, "name", pptx_path), len(slides))
    if not slides:
        log.error("%s has no slide in it; nothing to gate", pptx_path)
        return [Violation(0, "empty", f"{pptx_path} has no slide in it; the program "
                                      f"must add exactly one slide and save it")]

    # The frame's own furniture is excluded before any rule runs. It sits in the footer
    # band flush to the bottom edge - by design, and from one code path nobody else can
    # reach - so measuring it would report a rule 1 and a rule 5 violation on every
    # slide in every deck. What protects that band is rule 5, which still applies to
    # everything the program drew.
    every_box = boxes_from_slide(slides[0], layout)
    boxes = [b for b in every_box if not b.is_frame]
    reserved = builder.reserved_rects(frame or {}, layout, slide_width, slide_height)
    palette = builder.brand_palette(theme_colors or {})
    log.debug("gating %d of %d shape(s) (%d are brand furniture) against %d reserved "
              "zone(s) and a %d-token palette",
              len(boxes), len(every_box), len(every_box) - len(boxes), len(reserved),
              len(palette))

    violations = check_rects(boxes, slide_width=slide_width, slide_height=slide_height,
                             reserved=reserved)
    violations += check_prompt_text(boxes)
    violations += check_colors(boxes, palette)
    violations += check_typography(boxes, theme_font)
    violations += check_overflow(boxes)
    violations += check_content(boxes)
    violations += check_images(boxes)
    violations.sort(key=lambda v: (v.rule, v.message))
    if violations:
        blocking = [v for v in violations if v.blocking]
        counts = {}
        for violation in violations:
            counts[violation.rule] = counts.get(violation.rule, 0) + 1
        log.info("gate on %s (%s): %d violation(s), %d blocking, by rule %s",
                 getattr(pptx_path, "name", pptx_path), layout_key, len(violations),
                 len(blocking), dict(sorted(counts.items())))
        for violation in blocking:
            log.info("  blocking [rule %d/%s] %s", violation.rule, violation.kind,
                     violation.message)
    else:
        log.info("gate on %s (%s): clean",
                 getattr(pptx_path, "name", pptx_path), layout_key)
    return violations


def is_clean(violations: Sequence[Violation]) -> bool:
    """True when nothing blocking remains. Advisory notes (rules 10, 15) do not block."""
    blocking = [v for v in violations if v.blocking]
    if violations and not blocking:
        log.info("slide accepted with %d advisory note(s) and nothing blocking",
                 len(violations))
    return not blocking


def format_violations(violations: Sequence[Violation]) -> str:
    """The violation list as the model receives it: numbered instructions, rule order."""
    if not violations:
        log.debug("formatting an empty violation list as the pass message")
        return "The slide passed every geometry and brand check."
    ordered = sorted(violations, key=lambda v: (v.rule, v.message))
    lines = ["The slide was built but did not pass the geometry gate. Fix every point "
             "below and resend the whole program.", ""]
    for index, violation in enumerate(ordered, start=1):
        flag = "" if violation.blocking else "  (advisory)"
        lines.append(f"{index}. [rule {violation.rule}]{flag} {violation.message}")
    report = "\n".join(lines)
    log.debug("formatted %d violation(s) into a %d-char repair message",
              len(ordered), len(report))
    return report

"""Layout check: finds layout mistakes in the deck the model's program wrote.

It runs on every version of the deck and only measures shape boxes and text lengths
(no rendering, no LibreOffice). Each problem is a short sentence the model can
act on; the graph sends them back once so the model can move or shrink the shapes.

What it catches:

* a shape that runs off the slide or past the side margins;
* content in the bottom strip, where the classification label goes;
* content in the logo zones (slide 1 top-left, other slides top-right);
* text that is clearly too long for its box (estimated from character counts);
* cards that run into each other (filled shapes that partly overlap);
* text that runs into other text, including text that grows past its own box
  (auto-fit boxes grow downwards, so a long title can push into the subtitle).

``universal=True`` keeps only the defects that are wrong in ANY deck, whichever engine
built it: off-slide shapes, text too long for its box, overlapping cards or text, and
content covering a logo or label. The scorecard uses it so the classic designer and the
skill deck agent are measured by the same rules; margins, the bottom strip and the logo
zones are this agent's own layout contract and stay out of it.
"""
from __future__ import annotations

import math
import threading
from contextlib import contextmanager
from pathlib import Path

from pptx.oxml.ns import qn

from app.config.constants import SkillDeckConstants as C

_EMU = 914400
_OURS = (C.WORDMARK_SHAPE_NAME, C.BADGE_SHAPE_NAME, C.LABEL_SHAPE_NAME)
#: Shapes placed by the branding component or the template, not by the content.
FURNITURE = _OURS + C.FRAME_SHAPE_PREFIXES


def _inches(emu: int | None) -> float:
    return (emu or 0) / _EMU


# --- reading a deck quickly -------------------------------------------------------- #
# A placeholder that inherits its position looks it up in its layout every time it is
# asked (python-pptx searches the layout's shape tree), and the checks ask thousands of
# times per deck. Inside ``measuring()`` each shape's box and each paragraph's inherited
# text size are worked out once. Nothing may move, resize or restyle shapes inside it.
_local = threading.local()


@contextmanager
def measuring():
    """Remember boxes and inherited text sizes while a deck is only being read."""
    outer = getattr(_local, "cache", None)
    if outer is None:
        _local.cache = {}
    try:
        yield
    finally:
        if outer is None:
            _local.cache = None


def _memo(kind, element, compute):
    """``compute()`` once per element inside ``measuring()``; every time outside it."""
    cache = getattr(_local, "cache", None)
    if cache is None:
        return compute()
    key = (kind, id(element))
    hit = cache.get(key)
    if hit is not None and hit[0] is element:      # the element itself is kept alive
        return hit[1]
    value = compute()
    cache[key] = (element, value)
    return value


def forget(shape) -> None:
    """Drop what ``measuring()`` remembers about a shape that was just moved or resized."""
    cache = getattr(_local, "cache", None)
    if cache is not None:
        cache.pop(("geometry", id(shape._element)), None)


def geometry(shape) -> tuple[int, int, int, int] | None:
    """(left, top, width, height) in EMU, inherited from the layout when the shape has
    none of its own; None for a shape without a position."""
    def read():
        left, width = shape.left, shape.width
        if left is None or width is None:
            return None
        return left, shape.top or 0, width, shape.height or 0
    return _memo("geometry", shape._element, read)


# --- text settings the template gives --------------------------------------------- #
_TITLE_PH = ("title", "ctrTitle")


def _read_size(ppr) -> float | None:
    """Size (pt) a paragraph-properties element sets on its default run."""
    rpr = ppr.find(qn("a:defRPr"))
    size = rpr.get("sz") if rpr is not None else None
    return int(size) / 100 if size and size.isdigit() else None


def _read_spacing(ppr) -> tuple[str, float] | None:
    """Line spacing a paragraph-properties element sets: ("pt", 33.0) for an exact
    height or ("x", 0.9) for a multiple of single spacing."""
    spacing = ppr.find(qn("a:lnSpc"))
    if spacing is None:
        return None
    exact, share = spacing.find(qn("a:spcPts")), spacing.find(qn("a:spcPct"))
    if exact is not None and exact.get("val", "").isdigit():
        return "pt", int(exact.get("val")) / 100
    if share is not None and share.get("val", "").isdigit():
        return "x", int(share.get("val")) / 100000
    return None


def _from_style(style, level: int, read):
    """What a list style (lstStyle, titleStyle, bodyStyle...) sets for a level."""
    if style is None:
        return None
    ppr = style.find(qn(f"a:lvl{min(level, 8) + 1}pPr"))
    return read(ppr) if ppr is not None else None


def _ph(element):
    """The placeholder (``p:ph``) element of a shape element, or None."""
    nv = element.find(".//" + qn("p:nvPr"))
    return nv.find(qn("p:ph")) if nv is not None else None


def _placeholders(owner) -> dict:
    """A layout's or master's placeholders by ``("idx", n)`` and ``("type", t)``."""
    def build():
        found = {}
        for element in owner.shapes._spTree.iterchildren():
            ph = _ph(element)
            if ph is None:
                continue
            found.setdefault(("idx", ph.get("idx", "0")), element)
            found.setdefault(("type", ph.get("type", "obj")), element)
        return found
    return _memo("placeholders", owner._element, build)


def _own(element, level: int, read):
    body = element.find(qn("p:txBody"))
    return _from_style(body.find(qn("a:lstStyle")), level, read) if body is not None else None


def _layout_placeholder(shape, ph):
    """The layout placeholder a slide placeholder inherits from, or None."""
    try:
        layout = shape.part.slide.slide_layout
    except AttributeError:                        # a shape on a layout or master
        return None, None
    on_layout = _placeholders(layout)
    base = on_layout.get(("idx", ph.get("idx", "0")))
    if base is None and ph.get("type", "obj") in _TITLE_PH:
        base = on_layout.get(("type", ph.get("type", "obj")))
    return layout, base


def _inherited(shape, level: int, read, kind: str):
    """A paragraph setting from the shape's own list style, its layout placeholder, its
    master placeholder and the master's text styles, in that order (how PowerPoint
    resolves it); None when nothing sets it."""
    def resolve():
        element = shape._element
        value = _own(element, level, read)
        ph = _ph(element)
        if value is not None or ph is None:
            return value
        ph_type = ph.get("type", "obj")
        layout, base = _layout_placeholder(shape, ph)
        if layout is None:
            return None
        if base is not None:
            value = _own(base, level, read)
            if value is not None:
                return value
            base_ph = _ph(base)
            ph_type = base_ph.get("type", "obj") if base_ph is not None else ph_type
        master = layout.slide_master
        master_type = "title" if ph_type in _TITLE_PH else (
            ph_type if ph_type in ("dt", "ftr", "sldNum") else "body")
        on_master = _placeholders(master).get(("type", master_type))
        if on_master is not None:
            value = _own(on_master, level, read)
            if value is not None:
                return value
        styles = master._element.find(qn("p:txStyles"))
        if styles is None:
            return None
        style = ("p:titleStyle" if ph_type in _TITLE_PH
                 else "p:bodyStyle" if master_type == "body" else "p:otherStyle")
        return _from_style(styles.find(qn(style)), level, read)
    return _memo((kind, level), shape._element, resolve)


def inherited_pt(shape, level: int = 0) -> float | None:
    """The text size (pt) a paragraph at ``level`` gets from the template."""
    return _inherited(shape, level, _read_size, "size")


def _line_height(shape, paragraph, size: float) -> float:
    """Inches one line of this paragraph takes: its own line spacing, else the one the
    template gives, else single spacing (about 1.2 x the font size)."""
    ppr = paragraph._p.find(qn("a:pPr"))
    spacing = (_read_spacing(ppr) if ppr is not None else None) or _inherited(
        shape, paragraph.level, _read_spacing, "spacing")
    if spacing and spacing[0] == "pt":
        return spacing[1] / 72
    share = spacing[1] if spacing else 1.0
    return size / 72 * C.LAYOUT_LINE_SPACING * share


def _autofit(shape) -> str | None:
    """'normAutofit', 'spAutoFit' or 'noAutofit' from the shape, else its layout
    placeholder (a placeholder inherits the setting); None when nothing sets it."""
    def own(element):
        body = element.find(qn("p:txBody"))
        pr = body.find(qn("a:bodyPr")) if body is not None else None
        if pr is None:
            return None
        for tag in ("normAutofit", "spAutoFit", "noAutofit"):
            if pr.find(qn(f"a:{tag}")) is not None:
                return tag
        return None

    def resolve():
        found = own(shape._element)
        ph = _ph(shape._element)
        if found is not None or ph is None:
            return found
        _, base = _layout_placeholder(shape, ph)
        return own(base) if base is not None else None
    return _memo("autofit", shape._element, resolve)


def _is_background(shape, width: float, height: float) -> bool:
    """Full-height panels and full-slide backgrounds may touch every edge."""
    box = geometry(shape)
    return _inches(box[3] if box else shape.height) >= height * 0.95


def _font_pt(paragraph, default: float) -> tuple[float, bool]:
    """Largest run size in the paragraph (pt) and whether it is bold.

    Programs often set the size on the paragraph (``paragraph.font.size``) instead of
    the runs, so that is the fallback before the default (the size the template gives).
    """
    sizes = [r.font.size.pt for r in paragraph.runs if r.font.size is not None]
    if not sizes and paragraph.font.size is not None:
        sizes = [paragraph.font.size.pt]
    bold = any(r.font.bold for r in paragraph.runs) or bool(paragraph.font.bold)
    return (max(sizes) if sizes else default), bold


def _needed_height(shape) -> float | None:
    """Estimated height (in) the text needs at its set size, or None without text."""
    if not shape.has_text_frame or not shape.text_frame.text.strip():
        return None
    frame = shape.text_frame
    box = geometry(shape)
    usable = _inches(box[2] if box else shape.width) - 0.2   # default left + right inset
    if usable <= 0.2:
        return None
    total = 0.1                                      # default top + bottom inset
    for paragraph in frame.paragraphs:
        template_pt = inherited_pt(shape, paragraph.level)
        size, bold = _font_pt(paragraph, template_pt or C.LAYOUT_DEFAULT_FONT_PT)
        text = "".join(r.text for r in paragraph.runs) or " "
        char_w = size / 72 * (C.LAYOUT_BOLD_CHAR_EM if bold else C.LAYOUT_CHAR_EM)
        lines = (max(1, math.ceil(len(text) * char_w / usable - C.LAYOUT_WRAP_SLACK))
                 if frame.word_wrap is not False else 1)
        total += lines * _line_height(shape, paragraph, size)
    return total


Box = tuple[float, float, float, float]          # left, top, right, bottom (in)


def _box(shape) -> Box:
    left, top, width, height = geometry(shape) or (0, 0, 0, 0)
    left, top = _inches(left), _inches(top)
    return left, top, left + _inches(width), top + _inches(height)


def _overlap(a: Box, b: Box) -> float:
    """Area (sq in) the two boxes share."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def _area(a: Box) -> float:
    return max(a[2] - a[0], 0) * max(a[3] - a[1], 0)


def _contains(outer: Box, inner: Box) -> bool:
    tol = C.LAYOUT_TOLERANCE_IN
    return (inner[0] >= outer[0] - tol and inner[1] >= outer[1] - tol
            and inner[2] <= outer[2] + tol and inner[3] <= outer[3] + tol)


def _is_card(shape, box: Box) -> bool:
    """A filled autoshape big enough to be a card or panel (not a thin bar or line)."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    if shape.shape_type != MSO_SHAPE_TYPE.AUTO_SHAPE:
        return False
    if min(box[2] - box[0], box[3] - box[1]) < C.LAYOUT_MIN_CARD_IN:
        return False
    try:
        return shape.fill.type is not None and shape.fill.type != 5   # 5 = background
    except (AttributeError, TypeError, NotImplementedError):
        return False


def _text_box(shape, box: Box) -> Box | None:
    """Where the text really ends up: its box, grown downwards when it needs more room."""
    needed = _needed_height(shape)
    if not needed:
        return None
    return box[0], box[1], box[2], max(box[3], box[1] + needed)


def _collisions(slide) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(card pairs that partly overlap, text pairs that run into each other)."""
    shapes = [s for s in slide.shapes if geometry(s) is not None
              and not s.name.startswith(FURNITURE)]
    boxes = {id(s): _box(s) for s in shapes}
    cards = [s for s in shapes if _is_card(s, boxes[id(s)])]
    card_pairs = []
    for i, a in enumerate(cards):
        for b in cards[i + 1:]:
            ba, bb = boxes[id(a)], boxes[id(b)]
            if (_overlap(ba, bb) > C.LAYOUT_MIN_OVERLAP_SQIN
                    and not _contains(ba, bb) and not _contains(bb, ba)):
                card_pairs.append((a.name, b.name))
    texts = [(s, t) for s in shapes if (t := _text_box(s, boxes[id(s)]))]
    text_pairs = []
    for i, (a, ta) in enumerate(texts):
        for b, tb in texts[i + 1:]:
            shared = _overlap(ta, tb)
            if shared <= C.LAYOUT_MIN_OVERLAP_SQIN:
                continue
            if _contains(boxes[id(a)], boxes[id(b)]) or _contains(boxes[id(b)], boxes[id(a)]):
                continue                    # e.g. a label written on its own card
            if shared >= C.LAYOUT_TEXT_OVERLAP_SHARE * min(_area(ta), _area(tb)):
                text_pairs.append((a.name, b.name))
    return card_pairs, text_pairs


def _bleeds(edge: float, limit: float) -> bool:
    """True when a shape runs exactly to the slide edge on purpose (a bleed)."""
    return abs(edge - limit) <= C.LAYOUT_TOLERANCE_IN


def _shrinks(shape) -> bool:
    """True when the box shrinks its text on overflow (PowerPoint fits it), set on the
    shape or inherited from its layout placeholder."""
    if not shape.has_text_frame:
        return False
    return _autofit(shape) == "normAutofit"


def _furniture_boxes(slide, width: float, height: float) -> list[tuple[str, Box]]:
    """Logos and labels: stamped on the slide, or pictures in its layout and master."""
    boxes = [(s.name, _box(s)) for s in slide.shapes
             if s.name.startswith(FURNITURE) and geometry(s) is not None]
    for owner in (slide.slide_layout, slide.slide_layout.slide_master):
        for s in owner.shapes:
            if (geometry(s) is not None and "PICTURE" in str(s.shape_type)
                    and _area(_box(s)) < C.SCORE_LOGO_MAX_SHARE * width * height):
                boxes.append((s.name, _box(s)))
    return boxes


def _covering(slide, width: float, height: float) -> list[tuple[str, str]]:
    """(content shape, logo/label) pairs where content sits on top of the brand mark."""
    marks = _furniture_boxes(slide, width, height)
    pairs = []
    for shape in slide.shapes:
        if (geometry(shape) is None or shape.name.startswith(FURNITURE)
                or _is_background(shape, width, height)):
            continue
        box = _box(shape)
        pairs += [(shape.name, name) for name, mark in marks
                  if _overlap(box, mark) > C.LAYOUT_MIN_OVERLAP_SQIN]
    return pairs


def layout_problems(deck: Path, universal: bool = False) -> list[str]:
    """Check the raw deck and describe every layout mistake found.

    Shapes with the same mistake on one slide are reported together, so the model gets
    one sentence per cause, e.g. "slide 3: 'Rectangle 28', 'TextBox 31' run past the
    right margin (limit 12.83 in)".

    Args:
        deck: The .pptx the model's program saved.
        universal: Only defects that are wrong in any deck (see the module docstring).

    Returns:
        Problem sentences; empty when the layout is clean.
    """
    from pptx import Presentation
    prs = Presentation(str(deck))
    with measuring():
        return _problems(prs, universal)


def _problems(prs, universal: bool) -> list[str]:
    width, height = _inches(prs.slide_width), _inches(prs.slide_height)
    tol = C.LAYOUT_TOLERANCE_IN
    right_limit = width - C.LAYOUT_SIDE_MARGIN_IN
    bottom_limit = height - C.LAYOUT_BOTTOM_STRIP_IN
    cover_w, cover_h = C.LAYOUT_COVER_LOGO_ZONE_IN
    logo_w, logo_h = C.LOGO_ZONE_IN
    rules = {
        "off": "run off the slide (slide is %.2f x %.2f in)" % (width, height),
        "right": f"run past the right margin (keep them left of {right_limit:.2f} in)",
        "bottom": (f"reach into the bottom strip (keep everything above "
                   f"{bottom_limit:.2f} in; the classification label goes there)"),
        "cover_logo": f"sit in the top-left logo zone ({cover_w} x {cover_h} in)",
        "logo": f"sit in the top-right logo zone ({logo_w} x {logo_h} in)",
    }
    found: dict[tuple[int, str], list[str]] = {}
    overflow: list[str] = []
    collisions: list[str] = []
    for number, slide in enumerate(prs.slides, start=1):
        card_pairs, text_pairs = _collisions(slide)
        for a, b in card_pairs:
            collisions.append(f"slide {number}: cards '{a}' and '{b}' run into each other - "
                              "line cards up in rows of equal height and keep at least "
                              f"{C.LAYOUT_CARD_GAP_IN} in between them")
        for a, b in text_pairs:
            collisions.append(f"slide {number}: text in '{a}' and '{b}' runs into each "
                              "other (long text grows its box downwards) - move the lower "
                              "one down, shorten the text or lower the font size")
        for shape in slide.shapes:
            if geometry(shape) is None or shape.name.startswith(FURNITURE):
                continue                    # logos, labels: placed by branding or template
            left, top, right, bottom = _box(shape)
            has_text = shape.has_text_frame and bool(shape.text_frame.text.strip())
            kinds = []
            if right > width + tol or bottom > height + tol or left < -tol or top < -tol:
                kinds.append("off")
            elif not universal and not _is_background(shape, width, height):
                if right > right_limit + tol and not (_bleeds(right, width) and not has_text):
                    kinds.append("right")
                if bottom > bottom_limit + tol and not (_bleeds(bottom, height) and not has_text):
                    kinds.append("bottom")
                if number == 1 and left < cover_w and top < cover_h:
                    kinds.append("cover_logo")
                if number > 1 and right > width - logo_w and top < logo_h:
                    kinds.append("logo")
            for kind in kinds:
                found.setdefault((number, kind), []).append(f"'{shape.name}'")
            needed = None if (universal and _shrinks(shape)) else _needed_height(shape)
            box_h = bottom - top
            if needed and needed > box_h * C.LAYOUT_OVERFLOW_RATIO + 0.1:
                text = shape.text_frame.text.strip().replace("\n", " ")[:40]
                overflow.append(f"slide {number}: text '{text}...' in '{shape.name}' needs "
                                f"about {needed:.2f} in but its box is {box_h:.2f} in tall - "
                                "shorten it, widen the box or lower the font size "
                                "(not below 12 pt)")
        if universal:
            collisions += [f"slide {number}: '{a}' covers the brand mark or label '{b}'"
                           for a, b in _covering(slide, width, height)]
    problems = [f"slide {number}: {', '.join(names)} {rules[kind]}"
                for (number, kind), names in sorted(found.items())]
    problems += collisions + overflow
    return problems[:C.LAYOUT_MAX_PROBLEMS]

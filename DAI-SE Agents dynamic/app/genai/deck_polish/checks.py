"""Find what is wrong with a finished deck - from either engine.

Every finding is an :class:`Issue` that says which slide, which shapes, what is wrong
and who can fix it:

* ``rule``   - fixed by code in ``rules.py``, no model call (the LOW level);
* ``ai``     - sent to the model as part of the MEDIUM / HIGH round;
* ``report`` - neither can fix it (e.g. a template layout that prints the wrong
  classification); it is reported in the chat so a person can.

Measures boxes and estimated text heights only: no rendering, no LibreOffice.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from lxml import etree
from pptx.opc.constants import RELATIONSHIP_TYPE as RT

from app.config.constants import DeckPolishConstants as C, SkillDeckConstants as SD
from app.genai.deck_polish import reader, spelling
from app.genai.skill_deck import layout_check

_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_WORD_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789"
#: Layouts that are full-bleed statements by design: no body to check for gaps.
_STATEMENT_LAYOUT_WORDS = ("cover", "section", "divider", "thank", "closing", "agenda",
                           "quote", "title only")


@dataclass
class Issue:
    """One thing wrong with one slide."""
    slide: int
    code: str
    message: str
    shapes: tuple[int, ...] = ()
    fix: str = "ai"                 # rule | ai | report
    data: dict = field(default_factory=dict)

    def line(self) -> str:
        ids = f" [shapes {', '.join(map(str, self.shapes))}]" if self.shapes else ""
        return f"slide {self.slide}: {self.message}{ids}"


# --- colours ----------------------------------------------------------------------- #
def _hex(value: str) -> str:
    return value.upper().lstrip("#")


def tint(hex_value: str, share: float) -> str:
    """The colour mixed with white (share 0.4 = 40% of the way to white)."""
    rgb = [int(hex_value[i:i + 2], 16) for i in (0, 2, 4)]
    return "".join(f"{round(c + (255 - c) * share):02X}" for c in rgb)


def theme_colors(prs) -> set[str]:
    """Colours declared in the deck's theme(s): on brand by definition."""
    found = set()
    for master in prs.slide_masters:
        try:
            theme = master.part.part_related_by(RT.THEME)
        except KeyError:
            continue
        root = etree.fromstring(theme.blob)
        for node in root.iter(f"{{{_A}}}srgbClr"):
            found.add(_hex(node.get("val", "")))
        for node in root.iter(f"{{{_A}}}sysClr"):
            if node.get("lastClr"):
                found.add(_hex(node.get("lastClr")))
    return {c for c in found if len(c) == 6}


def brand_palette(prs=None) -> set[str]:
    """Every colour that is on brand: the ppt skill's palette, accent tints, the theme."""
    allowed = set(SD.BRAND_COLORS)
    allowed |= {tint(base, step) for base in SD.BRAND_TINT_BASES
                for step in SD.BRAND_TINT_STEPS}
    if prs is not None:
        allowed |= theme_colors(prs)
    return allowed


def nearest_brand(hex_value: str) -> str:
    """The palette colour closest to this one (by RGB distance)."""
    def rgb(h: str) -> tuple[int, int, int]:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    target = rgb(hex_value)
    return min(sorted(SD.BRAND_COLORS),
               key=lambda c: sum((a - b) ** 2 for a, b in zip(rgb(c), target)))


def run_color(run) -> str:
    try:
        color = run.font.color
        return str(color.rgb) if color.type is not None and color.rgb else ""
    except (AttributeError, TypeError, ValueError):
        return ""


def fill_color(shape) -> str:
    try:
        if shape.fill.type == 1:            # MSO_FILL.SOLID
            color = shape.fill.fore_color
            return str(color.rgb) if color.type is not None and color.rgb else ""
    except (AttributeError, TypeError, ValueError, NotImplementedError):
        pass
    return ""


# --- helpers ----------------------------------------------------------------------- #
def _content_shapes(slide):
    return [s for s in slide.shapes if reader.has_box(s) and not reader.is_furniture(s)]


def _is_background(shape, height: float) -> bool:
    b = reader.box(shape)
    return b[3] - b[1] >= height * 0.95


def _needed(shape) -> float | None:
    return layout_check._needed_height(shape)          # noqa: SLF001 - shared estimate


def _shrinks(shape) -> bool:
    return layout_check._shrinks(shape)                # noqa: SLF001


def ink_box(shape) -> reader.Box:
    """Where a shape's content really is: a text box that needs less room than it has
    ends where its text ends (top-anchored), one that needs more grows downwards."""
    b = reader.box(shape)
    needed = _needed(shape) if not _shrinks(shape) else None
    if needed is None:
        return b
    return b[0], b[1], b[2], b[1] + max(needed, 0.2)


def _words(text: str) -> int:
    return sum(1 for w in text.lower().split() if any(ch in _WORD_CHARS for ch in w))


def _is_statement_layout(slide) -> bool:
    name = slide.slide_layout.name.lower()
    return any(word in name for word in _STATEMENT_LAYOUT_WORDS)


# --- checks ------------------------------------------------------------------------ #
def template_shapes(slide):
    """Non-placeholder shapes the slide's layout (and master, unless the layout hides
    the master's shapes) prints on the slide."""
    layout = slide.slide_layout
    owners = [layout]
    if layout._element.get("showMasterSp") != "0":
        owners.append(layout.slide_master)
    return [s for owner in owners for s in owner.shapes if not s.is_placeholder]


def _prints_slide_number(shape) -> bool:
    return 'type="slidenum"' in etree.tostring(shape._element).decode()


def _numbers(slide, number: int) -> list[Issue]:
    """A stamped slide number next to one the template already prints."""
    stamped = next((s for s in slide.shapes if s.name == C.FRAME_NUMBER_NAME), None)
    if stamped is None:
        return []
    if any(_prints_slide_number(s) for s in template_shapes(slide)):
        return [Issue(number, "duplicate_number", "the template already prints the slide "
                      "number, so the stamped one doubles it", (stamped.shape_id,),
                      fix="rule")]
    return []


def _labels(slide, number: int) -> list[Issue]:
    """A stamped classification label next to one the layout already prints."""
    stamped = next((s for s in slide.shapes if s.name == C.FRAME_LABEL_NAME), None)
    if stamped is None:
        return []
    mine = stamped.text_frame.text.strip().lower()
    printed = []
    for shape in template_shapes(slide):
        if not getattr(shape, "has_text_frame", False):
            continue
        text = shape.text_frame.text.strip().lower()
        if text in C.CLASSIFICATION_WORDS:
            printed.append(text)
    if not printed:
        return []
    if mine in printed:
        return [Issue(number, "duplicate_label",
                      f"the layout already prints '{mine.upper()}', so the stamped "
                      "classification label repeats it on top", (stamped.shape_id,),
                      fix="rule")]
    return [Issue(number, "conflicting_label",
                  f"the layout prints '{printed[0].upper()}' but the deck is "
                  f"'{mine.title()}' - the template layout "
                  f"'{slide.slide_layout.name}' needs fixing", (stamped.shape_id,),
                  fix="report")]


def _bounds(slide, number: int, width: float, height: float) -> list[Issue]:
    issues = []
    margin = C.EDGE_MARGIN_IN
    graphics = reader.template_graphics(slide)
    for shape in _content_shapes(slide):
        left, top, right, bottom = reader.box(shape)
        tol = SD.LAYOUT_TOLERANCE_IN
        if left < -tol or top < -tol or right > width + tol or bottom > height + tol:
            if _is_background(shape, height) or reader.kind(shape) == "picture":
                continue
            issues.append(Issue(number, "off_slide", "runs off the slide",
                                (shape.shape_id,), fix="rule"))
            continue
        if (_is_background(shape, height) or not reader.text_of(shape)
                or reader.on_template_graphic(shape, graphics)):
            continue                        # the template placed it there
        if left < margin - tol or right > width - margin + tol or top < margin - tol:
            issues.append(Issue(number, "edge",
                                f"text sits closer than {margin} in to the slide edge",
                                (shape.shape_id,)))
    return issues


def _collisions(slide, number: int, height: float) -> list[Issue]:
    shapes = _content_shapes(slide)
    issues = []
    texts = [(s, ink_box(s)) for s in shapes if reader.text_of(s)
             and not _is_background(s, height)]
    for i, (a, ta) in enumerate(texts):
        for b, tb in texts[i + 1:]:
            shared = reader.overlap(ta, tb)
            if shared <= SD.LAYOUT_MIN_OVERLAP_SQIN:
                continue
            ba, bb = reader.box(a), reader.box(b)
            if reader.contains(ba, bb) or reader.contains(bb, ba):
                continue                    # a label written on its own card
            if shared >= SD.LAYOUT_TEXT_OVERLAP_SHARE * min(reader.area(ta), reader.area(tb)):
                issues.append(Issue(number, "text_overlap", "two text blocks run into "
                                    "each other", (a.shape_id, b.shape_id)))
    frames = [(s, reader.box(s)) for s in slide.shapes
              if reader.has_box(s) and reader.is_furniture(s)]
    for shape in shapes:
        if _is_background(shape, height):
            continue
        sb = ink_box(shape) if reader.text_of(shape) else reader.box(shape)
        for frame, fb in frames:
            if reader.overlap(sb, fb) > SD.LAYOUT_MIN_OVERLAP_SQIN:
                issues.append(Issue(number, "covers_frame",
                                    f"content runs into the stamped '{frame.name}'",
                                    (shape.shape_id,)))
                break
    for shape in shapes:
        needed = None if _shrinks(shape) else _needed(shape)
        b = reader.box(shape)
        box_h = b[3] - b[1]
        if needed and needed > box_h * SD.LAYOUT_OVERFLOW_RATIO + 0.1:
            issues.append(Issue(number, "overflow",
                                f"text needs about {needed:.1f} in but its box is "
                                f"{box_h:.1f} in tall", (shape.shape_id,)))
    return issues


def _text(slide, number: int, palette: set[str]) -> list[Issue]:
    issues = []
    tiny, small, off = set(), set(), {}
    words = 0
    for shape in _content_shapes(slide):
        fill = fill_color(shape)
        if fill and fill not in palette:
            off.setdefault(fill, set()).add(shape.shape_id)
        if not getattr(shape, "has_text_frame", False):
            continue
        words += _words(shape.text_frame.text)
        for paragraph in shape.text_frame.paragraphs:
            for run in paragraph.runs:
                if not run.text.strip():
                    continue
                color = run_color(run)
                if color and color not in palette:
                    off.setdefault(color, set()).add(shape.shape_id)
                size = run.font.size or paragraph.font.size
                if size is None:
                    continue
                if size.pt < C.FLOOR_PT:
                    tiny.add(shape.shape_id)
                elif size.pt < C.READ_PT and not shape.is_placeholder:
                    small.add(shape.shape_id)
    if tiny:
        issues.append(Issue(number, "tiny_text", f"text below the {C.FLOOR_PT} pt floor",
                            tuple(sorted(tiny)), fix="rule"))
    if small:
        issues.append(Issue(number, "small_text", f"component text below {C.READ_PT} pt",
                            tuple(sorted(small)), fix="rule"))
    for color, ids in sorted(off.items()):
        issues.append(Issue(number, "off_palette", f"colour {color} is not a brand colour",
                            tuple(sorted(ids)), fix="rule", data={"color": color}))
    if words > C.MAX_WORDS:
        issues.append(Issue(number, "wordy", f"{words} words on the slide (aim for "
                            f"{C.MAX_WORDS} or fewer; detail belongs in the notes)"))
    text = "\n".join(reader.text_of(s) for s in slide.shapes)
    if slide.has_notes_slide:
        text += "\n" + slide.notes_slide.notes_text_frame.text
    found = spelling.uk_words(text)
    if found:
        issues.append(Issue(number, "uk_spelling", "UK spelling: " + ", ".join(
            dict.fromkeys(found)), fix="rule"))
    return issues


def _in_card(shape, cards) -> bool:
    b = reader.box(shape)
    return any(card is not shape and reader.contains(reader.box(card), b) for card in cards)


def _heading_like(shape) -> bool:
    words = _words(reader.text_of(shape))
    return 0 < words <= C.HEADING_MAX_WORDS and len(reader.paragraphs(shape)) == 1


def _same_column(a: reader.Box, b: reader.Box) -> bool:
    shared = min(a[2], b[2]) - max(a[0], b[0])
    return shared >= 0.5 * min(a[2] - a[0], b[2] - b[0])


def headings_below(slide) -> list[tuple]:
    """(heading, block) pairs where a short heading sits right UNDER the text block it
    labels - e.g. a two-column layout whose column titles are at the bottom.

    A pair counts only when the heading is the last thing in its column, the block has
    no heading of its own above it, and neither sits inside a card (a card's title and
    body are one designed unit). One stray caption is not a pattern: at least two
    headings in a row must sit below their blocks.
    """
    content = _content_shapes(slide)
    cards = [s for s in content if reader.kind(s) == "card"]
    graphics = reader.template_graphics(slide)
    texts = [s for s in content if reader.text_of(s) and not _in_card(s, cards)
             and reader.kind(s) not in ("title", "subtitle", "footer", "frame")]
    pairs = []
    for head in texts:
        if not _heading_like(head) or reader.on_template_graphic(head, graphics):
            continue                        # e.g. titles printed on the layout's bands
        hb = reader.box(head)
        if any(other is not head and _heading_like(other)
               and _same_column(hb, reader.box(other))
               and 0 <= hb[1] - reader.box(other)[3] <= C.HEADING_MAX_GAP_IN
               for other in texts):
            continue                        # it is the body under a heading of its own
        if any(other is not head and _same_column(hb, reader.box(other))
               and reader.box(other)[1] > hb[1] + 0.05 for other in content
               if reader.kind(other) not in ("frame", "footer")):
            continue                        # something below it: not the column's end
        for block in texts:
            if block is head or _words(reader.text_of(block)) <= _words(reader.text_of(head)):
                continue
            bb = reader.box(block)
            gap = hb[1] - bb[3]
            ratio = (hb[2] - hb[0]) / max(bb[2] - bb[0], 0.01)
            if not (_same_column(hb, bb) and -0.05 <= gap <= C.HEADING_MAX_GAP_IN
                    and 0.6 <= ratio <= 1.6):
                continue                    # a column heading spans its column
            has_own = any(other not in (head, block) and _heading_like(other)
                          and _same_column(bb, reader.box(other))
                          and 0 <= bb[1] - reader.box(other)[3] <= C.HEADING_MAX_GAP_IN
                          for other in texts)
            if not has_own:
                pairs.append((head, block))
                break
    return pairs if len(pairs) >= 2 else []


def _headings(slide, number: int) -> list[Issue]:
    pairs = headings_below(slide)
    if not pairs:
        return []
    return [Issue(number, "heading_below", "column headings sit under their columns "
                  "instead of above them", tuple(h.shape_id for h, _ in pairs),
                  fix="rule")]


def _gaps(slide, number: int, height: float) -> list[Issue]:
    if _is_statement_layout(slide):
        return []
    shapes = [s for s in _content_shapes(slide) if not _is_background(s, height)]
    heads = [reader.box(s)[3] for s in shapes if reader.kind(s) in ("title", "subtitle")]
    body = [s for s in shapes if reader.kind(s) not in ("title", "subtitle", "footer")]
    if not heads or not body:
        return []
    top, bottom = max(heads), height - C.FOOTER_BAND_IN - 0.15
    if bottom - top < 1:
        return []
    spans = sorted((max(b[1], top), min(b[3], bottom)) for b in map(ink_box, body)
                   if b[3] > top and b[1] < bottom)
    largest, cursor = 0.0, top
    for start, end in spans:
        largest = max(largest, start - cursor)
        cursor = max(cursor, end)
    largest = max(largest, bottom - cursor)
    if largest > C.GAP_SHARE * (bottom - top):
        return [Issue(number, "big_gap", f"an empty band {largest:.1f} in tall leaves the "
                      "slide looking unbalanced", data={"gap_in": round(largest, 2)})]
    return []


def find_issues(prs) -> list[Issue]:
    """Everything wrong with the deck, slide by slide."""
    width, height = reader.inches(prs.slide_width), reader.inches(prs.slide_height)
    palette = brand_palette(prs)
    issues: list[Issue] = []
    with layout_check.measuring():
        for number, slide in enumerate(prs.slides, 1):
            issues += _labels(slide, number)
            issues += _numbers(slide, number)
            issues += _bounds(slide, number, width, height)
            issues += _collisions(slide, number, height)
            issues += _text(slide, number, palette)
            issues += _headings(slide, number)
            issues += _gaps(slide, number, height)
    return issues


def count_by_fix(issues: list[Issue]) -> dict:
    out = {"rule": 0, "ai": 0, "report": 0}
    for issue in issues:
        out[issue.fix] = out.get(issue.fix, 0) + 1
    return out

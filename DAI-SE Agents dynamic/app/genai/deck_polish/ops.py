"""Apply the model's edit operations - validated, one by one, by code.

The model never writes code or colours. It answers with a JSON list of operations from
a fixed menu (``DeckPolishConstants.OPS_BY_LEVEL``); each one is checked here and either
applied or rejected with a reason. What gets drawn (cards, steps, stat tiles, dividers)
is drawn by this module in the brand palette and the theme font, so the result stays on
brand whatever the model asks for.

    move        {"op": "move", "slide": 3, "shape": 7, "left": 1.0, "top": 2.2}
    resize      {"op": "resize", "slide": 3, "shape": 7, "width": 5.0, "height": 2.4}
    font_size   {"op": "font_size", "slide": 3, "shape": 7, "pt": 16}
    shorten     {"op": "shorten", "slide": 4, "shape": 14, "text": ["...", "..."],
                 "notes": "the detail taken off the slide"}
    to_cards    {"op": "to_cards", "slide": 2, "shape": 5}          (+ optional "box")
    to_steps    {"op": "to_steps", "slide": 2, "shape": 5}          (+ optional "box")
    to_timeline {"op": "to_timeline", "slide": 2, "shape": 5}       (HIGH)
    stat        {"op": "stat", "slide": 2, "value": "6", "label": "steps, in order",
                 "box": [9.0, 4.6, 3.6, 1.6]}
    divider     {"op": "divider", "before": 5, "title": "Restarting safely"}   (HIGH)

A box is [left, top, width, height] in inches.
"""
from __future__ import annotations

import copy
import io
import math
import re

from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt

from app.config.constants import DeckPolishConstants as C, SkillDeckConstants as SD
from app.genai.deck_polish import checks, reader

P = SD.BRAND_PALETTE
_SPLIT = re.compile(r"\s*(?::|\s[-\u2013\u2014])\s+")
_NUMBER = re.compile(r"\d[\d.,]*\s*%?")


class Rejected(ValueError):
    """An operation that may not be applied; the message says why."""


# --- drawing ----------------------------------------------------------------------- #
def _shape(slide, kind, box, name, fill=None, line=None):
    left, top, width, height = box
    shape = slide.shapes.add_shape(kind, Inches(left), Inches(top), Inches(width),
                                   Inches(height))
    if fill:
        shape.fill.solid()
        shape.fill.fore_color.rgb = RGBColor.from_string(fill)
    else:
        shape.fill.background()
    if line:
        shape.line.color.rgb = RGBColor.from_string(line)
        shape.line.width = Pt(0.75)
    else:
        shape.line.fill.background()
    shape.shadow.inherit = False
    shape.name = name
    if shape.has_text_frame:
        shape.text_frame.text = ""
    return shape


def _text(slide, box, parts, name, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP):
    """A text box; ``parts`` is a list of (text, pt, bold, palette hex)."""
    left, top, width, height = box
    shape = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width),
                                     Inches(height))
    frame = shape.text_frame
    frame.word_wrap = True
    frame.margin_left = frame.margin_right = frame.margin_top = frame.margin_bottom = 0
    frame.vertical_anchor = anchor
    for index, (text, size, bold, color) in enumerate(parts):
        paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        paragraph.alignment = align
        run = paragraph.add_run()
        run.text = text
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.color.rgb = RGBColor.from_string(color)
    shape.name = name
    return shape


def text_height(text: str, width: float, size: float, bold: bool = False) -> float:
    """Inches a paragraph needs at this size and width (the checks' estimate)."""
    em = SD.LAYOUT_BOLD_CHAR_EM if bold else SD.LAYOUT_CHAR_EM
    lines = max(1, math.ceil(len(text) * size / 72 * em / max(width, 0.1)))
    return lines * size / 72 * SD.LAYOUT_LINE_SPACING


def split_item(text: str) -> tuple[str, str]:
    """'Lead: rest' -> ('Lead', 'rest'); text without a lead -> ('', text)."""
    parts = _SPLIT.split(text.strip(), maxsplit=1)
    if len(parts) == 2 and 0 < len(parts[0].split()) <= 5:
        return parts[0], parts[1]
    return "", text.strip()


def _grid(box, count: int):
    """Equal cells for ``count`` items, arranged so each card is about twice as wide
    as it is tall: a row in a wide box, a stack in a narrow one, a grid in between."""
    left, top, width, height = box
    gap = C.CARD_GAP_IN

    def cost(cols: int) -> float:
        rows = math.ceil(count / cols)
        cell_w = (width - gap * (cols - 1)) / cols
        cell_h = (height - gap * (rows - 1)) / rows
        if cell_w < C.MIN_CARD_WIDTH_IN or cell_h < 0.6:
            return math.inf
        return abs(math.log((cell_w / cell_h) / 2.2)) + 0.5 * (rows * cols - count)

    cols = min(range(1, count + 1), key=cost)
    rows = math.ceil(count / cols)
    cell_w = (width - gap * (cols - 1)) / cols
    cell_h = (height - gap * (rows - 1)) / rows
    return [(left + (i % cols) * (cell_w + gap), top + (i // cols) * (cell_h + gap),
             cell_w, cell_h) for i in range(count)]


# --- validation -------------------------------------------------------------------- #
class Deck:
    """The open deck plus what the operations need to know about it."""

    def __init__(self, prs):
        self.prs = prs
        self.width = reader.inches(prs.slide_width)
        self.height = reader.inches(prs.slide_height)
        self.text_pt: dict[int, float] = {}     # slide -> text size its groups share

    def slide(self, number):
        if not isinstance(number, int) or not 1 <= number <= len(self.prs.slides):
            raise Rejected(f"there is no slide {number}")
        return self.prs.slides[number - 1]

    def shape(self, slide, shape_id, movable=False):
        shape = reader.find_shape(slide, shape_id) if isinstance(shape_id, int) else None
        if shape is None:
            raise Rejected(f"there is no shape {shape_id} on that slide")
        if reader.is_furniture(shape):
            raise Rejected("frame shapes (classification, slide number, logo) are stamped "
                           "by the template and may not be edited")
        if movable and reader.on_template_graphic(shape, reader.template_graphics(slide)):
            raise Rejected(f"shape {shape_id} sits on a graphic the template draws; it "
                           "stays where the template put it")
        return shape

    def box(self, value, name="box"):
        if (not isinstance(value, (list, tuple)) or len(value) != 4
                or not all(isinstance(v, (int, float)) for v in value)):
            raise Rejected(f"{name} must be [left, top, width, height] in inches")
        left, top, width, height = (float(v) for v in value)
        margin, tol = C.EDGE_MARGIN_IN, SD.LAYOUT_TOLERANCE_IN
        if width <= 0.2 or height <= 0.2:
            raise Rejected(f"{name} is too small")
        if (left < margin - tol or top < margin - tol
                or left + width > self.width - margin + tol
                or top + height > self.height - C.FOOTER_BAND_IN + tol):
            raise Rejected(f"{name} {value} leaves the safe area (keep {margin} in from the "
                           f"edges and the bottom {C.FOOTER_BAND_IN} in free)")
        return left, top, width, height

    def free(self, slide, area_box, ignore=()):
        """Reject a box that would cover existing content text."""
        b = (area_box[0], area_box[1], area_box[0] + area_box[2], area_box[1] + area_box[3])
        for shape in slide.shapes:
            if (shape in ignore or not reader.has_box(shape) or reader.is_furniture(shape)
                    or not reader.text_of(shape)):
                continue
            if reader.overlap(b, checks.ink_box(shape)) > SD.LAYOUT_MIN_OVERLAP_SQIN:
                raise Rejected(f"that box covers the text of shape {shape.shape_id}")


def _items(shape) -> list[tuple[str, str]]:
    """(lead, rest) per non-empty paragraph. The lead is the paragraph's opening bold
    run(s) - how the writer marks a lead phrase - or the text before "Lead: rest"."""
    items = []
    for paragraph in shape.text_frame.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        runs = [r for r in paragraph.runs if r.text]
        bold = []
        for run in runs:
            if not (run.font.bold or (run.font.bold is None and paragraph.font.bold)):
                break
            bold.append(run.text)
        lead = "".join(bold).strip()
        if lead and lead != text and len(lead.split()) <= 6:
            items.append((lead.rstrip(":").strip(), text[len("".join(bold)):].strip(" :")))
        else:
            items.append(split_item(text))
    if not C.LIST_MIN_ITEMS <= len(items) <= C.LIST_MAX_ITEMS:
        raise Rejected(f"shape {shape.shape_id} has {len(items)} paragraph(s); cards and "
                       f"steps need {C.LIST_MIN_ITEMS} to {C.LIST_MAX_ITEMS}")
    return items


def _remove(shape):
    shape._element.getparent().remove(shape._element)


def _append_notes(slide, text: str) -> None:
    text = text.strip()
    if not text:
        return
    frame = slide.notes_slide.notes_text_frame
    if frame.text.strip():
        frame.add_paragraph().text = text
    else:
        frame.text = text


# --- operations -------------------------------------------------------------------- #
def op_move(deck, op):
    slide = deck.slide(op.get("slide"))
    shape = deck.shape(slide, op.get("shape"), movable=True)
    left, top, right, bottom = reader.box(shape)
    new = deck.box([op.get("left"), op.get("top"), right - left, bottom - top])
    reader.set_box(shape, *new)
    return f"slide {op['slide']}: moved shape {op['shape']}"


def op_resize(deck, op):
    slide = deck.slide(op.get("slide"))
    shape = deck.shape(slide, op.get("shape"), movable=True)
    left, top, _, _ = reader.box(shape)
    new = deck.box([left, top, op.get("width"), op.get("height")])
    reader.set_box(shape, *new)
    return f"slide {op['slide']}: resized shape {op['shape']}"


def op_font_size(deck, op):
    slide = deck.slide(op.get("slide"))
    shape = deck.shape(slide, op.get("shape"))
    size = op.get("pt")
    if not isinstance(size, (int, float)) or not C.FLOOR_PT <= size <= C.MAX_PT:
        raise Rejected(f"pt must be between {C.FLOOR_PT} and {C.MAX_PT}")
    if not getattr(shape, "has_text_frame", False) or not reader.text_of(shape):
        raise Rejected(f"shape {op['shape']} has no text")
    before = [(item, item.font.size) for p in shape.text_frame.paragraphs
              for item in (p, *p.runs)]
    for paragraph in shape.text_frame.paragraphs:
        for run in paragraph.runs:
            run.font.size = Pt(size)
    needed = checks._needed(shape)                       # noqa: SLF001
    if needed and needed > reader.inches(shape.height) * SD.LAYOUT_OVERFLOW_RATIO + 0.1:
        for item, old in before:
            item.font.size = old
        raise Rejected(f"at {size} pt the text no longer fits shape {op['shape']}")
    return f"slide {op['slide']}: set the text of shape {op['shape']} to {size} pt"


def op_shorten(deck, op):
    slide = deck.slide(op.get("slide"))
    shape = deck.shape(slide, op.get("shape"))
    lines = op.get("text")
    if (not isinstance(lines, list) or not lines
            or not all(isinstance(t, str) and t.strip() for t in lines)):
        raise Rejected("text must be a list of non-empty paragraphs")
    old = reader.text_of(shape)
    if not old:
        raise Rejected(f"shape {op['shape']} has no text")
    old_words, new_words = len(old.split()), sum(len(t.split()) for t in lines)
    if new_words >= old_words:
        raise Rejected("shorten must make the text shorter")
    notes = op.get("notes") or ""
    if not isinstance(notes, str) or len(notes.split()) < min(5, old_words - new_words):
        raise Rejected("put the detail taken off the slide into notes - never drop a fact")
    frame = shape.text_frame
    for index, text in enumerate(lines):
        current = list(frame.paragraphs)
        if index < len(current):
            paragraph = current[index]
        else:                               # one more paragraph, styled like the last
            current[-1]._p.addnext(copy.deepcopy(current[-1]._p))
            paragraph = frame.paragraphs[index]
        runs = paragraph.runs
        if runs:
            runs[0].text = text.strip()
            for extra in runs[1:]:
                extra._r.getparent().remove(extra._r)
        else:
            paragraph.add_run().text = text.strip()
    for paragraph in frame.paragraphs[len(lines):]:
        paragraph._p.getparent().remove(paragraph._p)
    _append_notes(slide, notes)
    return (f"slide {op['slide']}: shortened shape {op['shape']} from {old_words} to "
            f"{new_words} words; the detail moved to the speaker notes")


def _group_text(name: str) -> bool:
    """True for the text box of a card, step or timeline item drawn here."""
    return (name.startswith(C.SHAPE_PREFIX) and name.endswith("-text")
            and name[len(C.SHAPE_PREFIX):].split("-", 1)[0] in ("card", "step", "timeline"))


def _shared_pt(deck, slide, number: int, fitted: float) -> float:
    """One text size for every card, step and timeline group drawn on a slide: the
    largest size that fits all of them. A smaller size always still fits, so a group
    drawn earlier is shrunk to match when a later one needs less."""
    current = deck.text_pt.get(number)
    size = fitted if current is None else min(fitted, current)
    if current is not None and size < current:
        for shape in slide.shapes:
            if _group_text(shape.name):
                for paragraph in shape.text_frame.paragraphs:
                    for run in paragraph.runs:
                        run.font.size = Pt(size + 2 if run.font.bold else size)
    deck.text_pt[number] = size
    return size


def _next_index(slide, kind: str, number: int) -> int:
    """The highest item number already used by ``kind`` groups on the slide, so a
    second group on the same slide gets names of its own."""
    pattern = re.compile(rf"^{re.escape(C.SHAPE_PREFIX)}{kind}-{number}-(\d+)$")
    return max((int(m.group(1)) for shape in slide.shapes
                if (m := pattern.match(shape.name))), default=0)


def _list_box(deck, slide, shape, op):
    if "box" in op:
        area = deck.box(op["box"])
        deck.free(slide, area, ignore=(shape,))
        return area
    left, top, right, bottom = reader.box(shape)
    left, top = max(left, C.EDGE_MARGIN_IN), max(top, C.EDGE_MARGIN_IN)
    right = min(right, deck.width - C.EDGE_MARGIN_IN)
    bottom = min(bottom, deck.height - C.FOOTER_BAND_IN)
    return deck.box([left, top, right - left, bottom - top], "the shape's box")


def op_to_cards(deck, op):
    slide = deck.slide(op.get("slide"))
    shape = deck.shape(slide, op.get("shape"), movable=True)
    items = _items(shape)
    cells = _grid(_list_box(deck, slide, shape, op), len(items))
    inset = C.CARD_INSET_IN
    plans = [(cell, title, body) for cell, (title, body) in zip(cells, items)]

    def fits(size: float) -> bool:
        for (_, _, width, height), title, body in plans:
            inner = width - 2 * inset
            need = 2 * inset + C.CARD_ACCENT_IN + 0.12
            need += (text_height(title, inner, size + 2, True) + 0.06) if title else 0
            need += text_height(body, inner, size)
            if need > height + 0.05:
                return False
        return True

    body_pt = next((size for size in C.TEXT_LADDER if fits(size)), None)
    if body_pt is None:
        raise Rejected("the items do not fit as cards in that box even at "
                       f"{C.TEXT_LADDER[-1]} pt; give a larger box or shorten them first")
    _remove(shape)
    number = op["slide"]
    body_pt = _shared_pt(deck, slide, number, body_pt)
    offset = _next_index(slide, "card", number)
    for index, ((left, top, width, height), title, body) in enumerate(plans, 1):
        name = f"{C.SHAPE_PREFIX}card-{number}-{offset + index}"
        card = _shape(slide, MSO_SHAPE.ROUNDED_RECTANGLE, (left, top, width, height), name,
                      fill=P["white"], line=P["border"])
        card.adjustments[0] = 0.06
        _shape(slide, MSO_SHAPE.RECTANGLE, (left + inset, top + inset, 0.5, C.CARD_ACCENT_IN),
               f"{name}-accent", fill=P["red"])
        parts = ([(title, body_pt + 2, True, P["navy"])] if title else [])
        parts.append((body, body_pt, False, P["body"]))
        text_top = top + inset + C.CARD_ACCENT_IN + 0.12
        _text(slide, (left + inset, text_top, width - 2 * inset,
                      top + height - inset - text_top), parts, f"{name}-text")
    return f"slide {number}: turned a {len(items)}-item list into cards"


def _steps(deck, op, timeline: bool):
    slide = deck.slide(op.get("slide"))
    shape = deck.shape(slide, op.get("shape"), movable=True)
    items = _items(shape)
    left, top, width, height = _list_box(deck, slide, shape, op)
    count = len(items)
    col_w = (width - C.CARD_GAP_IN * (count - 1)) / count
    if col_w < C.MIN_CARD_WIDTH_IN * 0.8:
        raise Rejected(f"{count} steps do not fit side by side in {width:.1f} in")
    diameter = C.STEP_DIAMETER_IN
    text_top = top + diameter + 0.15
    plans = items

    def fits(size: float) -> bool:
        return all(text_top + ((text_height(title, col_w, size + 2, True) + 0.04)
                               if title else 0) + text_height(body, col_w, size)
                   <= top + height + 0.05 for title, body in plans)

    text_pt = next((size for size in C.TEXT_LADDER if fits(size)), None)
    if text_pt is None:
        raise Rejected("the steps do not fit below their numbers in that box; give a "
                       "taller box or shorten them first")
    _remove(shape)
    number = op["slide"]
    kind = "timeline" if timeline else "step"
    text_pt = _shared_pt(deck, slide, number, text_pt)
    offset = _next_index(slide, kind, number)
    if timeline and count > 1:
        y = top + diameter / 2
        x1 = left + diameter / 2
        x2 = left + (count - 1) * (col_w + C.CARD_GAP_IN) + diameter / 2
        line = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y),
                                          Inches(x2), Inches(y))
        line.line.color.rgb = RGBColor.from_string(P["border"])
        line.line.width = Pt(2)
        line.name = f"{C.SHAPE_PREFIX}{kind}-{number}-line" + (f"-{offset + 1}" if offset else "")
    for index, (title, body) in enumerate(plans, 1):
        x = left + (index - 1) * (col_w + C.CARD_GAP_IN)
        name = f"{C.SHAPE_PREFIX}{kind}-{number}-{offset + index}"
        circle = _shape(slide, MSO_SHAPE.OVAL, (x, top, diameter, diameter), name,
                        fill=P["red"])
        circle.text_frame.text = str(index)
        paragraph = circle.text_frame.paragraphs[0]
        paragraph.alignment = PP_ALIGN.CENTER
        circle.text_frame.vertical_anchor = MSO_ANCHOR.MIDDLE
        for margin in ("margin_left", "margin_right", "margin_top", "margin_bottom"):
            setattr(circle.text_frame, margin, 0)
        run = paragraph.runs[0]
        run.font.size, run.font.bold = Pt(C.STEP_NUMBER_PT), True
        run.font.color.rgb = RGBColor.from_string(P["white"])
        parts = ([(title, text_pt + 2, True, P["navy"])] if title else [])
        parts.append((body, text_pt, False, P["body"]))
        _text(slide, (x, text_top, col_w, top + height - text_top), parts, f"{name}-text")
    return f"slide {number}: turned a {count}-item list into a numbered {kind}"


def op_to_steps(deck, op):
    return _steps(deck, op, timeline=False)


def op_to_timeline(deck, op):
    return _steps(deck, op, timeline=True)


def _slide_text(slide) -> str:
    text = " ".join(reader.text_of(s) for s in slide.shapes)
    if slide.has_notes_slide:
        text += " " + slide.notes_slide.notes_text_frame.text
    return re.sub(r"\s+", "", text.lower())


def op_stat(deck, op):
    slide = deck.slide(op.get("slide"))
    value, label = op.get("value"), op.get("label")
    if not isinstance(value, str) or not _NUMBER.fullmatch(value.strip()):
        raise Rejected("value must be a number from the slide, e.g. '6' or '70%'")
    if not isinstance(label, str) or not 0 < len(label.split()) <= 8:
        raise Rejected("label must be 1 to 8 words")
    if re.sub(r"\s+", "", value.lower()) not in _slide_text(slide):
        raise Rejected(f"'{value}' is not on the slide or in its notes - never invent a figure")
    left, top, width, height = deck.box(op.get("box"))
    if width < 1.6 or height < 1.0:
        raise Rejected("a stat tile needs at least 1.6 x 1.0 in")
    deck.free(slide, (left, top, width, height))
    name = f"{C.SHAPE_PREFIX}stat-{op['slide']}"
    tile = _shape(slide, MSO_SHAPE.ROUNDED_RECTANGLE, (left, top, width, height), name,
                  fill=P["surface"])
    tile.adjustments[0] = 0.06
    inset = C.CARD_INSET_IN
    _text(slide, (left + inset, top + inset, width - 2 * inset, height - 2 * inset),
          [(value.strip(), C.STAT_VALUE_PT, True, P["red"]),
           (label.strip(), C.STAT_LABEL_PT, False, P["body"])], f"{name}-text",
          anchor=MSO_ANCHOR.MIDDLE)
    return f"slide {op['slide']}: added a stat tile for '{value.strip()}'"


def _section_layout(prs, near_slide):
    master = near_slide.slide_layout.slide_master
    candidates = []
    for each in prs.slide_masters:
        for layout in each.slide_layouts:
            name = layout.name.lower()
            if any(word in name for word in C.SECTION_LAYOUT_WORDS):
                candidates.append((each is not master, len(name), layout))
    return min(candidates, key=lambda c: (c[0], c[1]))[2] if candidates else None


def _copy_frame(source, target) -> None:
    """Stamp the source slide's frame (classification, number, logo) on the target."""
    for shape in source.shapes:
        if not reader.is_furniture(shape):
            continue
        if "PICTURE" in str(shape.shape_type):
            pic = target.shapes.add_picture(io.BytesIO(shape.image.blob), shape.left,
                                            shape.top, shape.width, shape.height)
            pic.name = shape.name
        else:
            target.shapes._spTree.append(copy.deepcopy(shape._element))


def renumber(prs) -> None:
    """Make every stamped slide number match the slide's position again."""
    for number, slide in enumerate(prs.slides, 1):
        for shape in slide.shapes:
            if shape.name == C.FRAME_NUMBER_NAME and shape.has_text_frame:
                runs = [r for p in shape.text_frame.paragraphs for r in p.runs]
                if runs:
                    runs[0].text = str(number)
                    for extra in runs[1:]:
                        extra.text = ""


def op_divider(deck, op):
    prs = deck.prs
    before = op.get("before")
    if not isinstance(before, int) or not 2 <= before <= len(prs.slides):
        raise Rejected("before must be the number of an existing slide after the cover")
    title = op.get("title")
    if not isinstance(title, str) or not 0 < len(title) <= 60:
        raise Rejected("title must be 1 to 60 characters")
    neighbour = prs.slides[before - 1]
    layout = _section_layout(prs, neighbour)
    if layout is None:
        raise Rejected("the template has no section layout")
    slide = prs.slides.add_slide(layout)
    titled = False
    for placeholder in list(slide.placeholders):
        kind = reader.kind(placeholder)
        if kind == "title" and not titled:
            placeholder.text_frame.text = title.strip()
            titled = True
        elif kind == "subtitle" and op.get("subtitle"):
            placeholder.text_frame.text = str(op["subtitle"]).strip()[:90]
        elif kind != "footer":
            _remove(placeholder)
    if not titled:
        _text(slide, (0.8, 2.8, deck.width - 1.6, 1.2), [(title.strip(), 36, True, P["navy"])],
              f"{C.SHAPE_PREFIX}divider-title")
    _copy_frame(neighbour, slide)
    ids = prs.slides._sldIdLst
    new = ids[-1]
    ids.remove(new)
    ids.insert(before - 1, new)
    renumber(prs)
    return f"added a section divider '{title.strip()}' before slide {before}"


OPERATIONS = {"move": op_move, "resize": op_resize, "font_size": op_font_size,
              "shorten": op_shorten, "to_cards": op_to_cards, "to_steps": op_to_steps,
              "to_timeline": op_to_timeline, "stat": op_stat, "divider": op_divider}


def apply(prs, operations, level: str) -> tuple[list[str], list[str]]:
    """Apply what is allowed at this level, in order.

    Dividers run last and from the back of the deck forwards, so every slide number the
    model wrote still points at the slide it meant.

    Returns:
        (change sentences, rejection sentences).
    """
    allowed = C.OPS_BY_LEVEL.get(level, ())
    deck = Deck(prs)
    applied, rejected = [], []
    ops = [o for o in (operations or []) if isinstance(o, dict)][:C.MAX_OPS]
    ordered = ([o for o in ops if o.get("op") != "divider"]
               + sorted((o for o in ops if o.get("op") == "divider"),
                        key=lambda o: -(o.get("before") or 0)))
    for op in ordered:
        name = op.get("op")
        try:
            if name not in allowed:
                raise Rejected(f"'{name}' is not allowed at this level")
            applied.append(OPERATIONS[name](deck, op))
        except Rejected as exc:
            rejected.append(f"{name} on slide {op.get('slide', op.get('before'))}: {exc}")
        except Exception as exc:                            # noqa: BLE001
            rejected.append(f"{name} on slide {op.get('slide', op.get('before'))}: "
                            f"failed ({type(exc).__name__}: {exc})")
    return applied, rejected

"""Fixes made by code, with no model call - everything the LOW level does.

Each rule takes the open deck and the issues ``checks.find_issues`` found, changes only
what those issues point at, and returns one plain sentence per change for the chat.

LOW keeps every slide's arrangement as the engine built it: it removes a duplicated
label, pulls a shape back onto the slide, lifts text off the type-scale floor, swaps an
off-brand colour for the nearest brand colour and fixes UK spelling. MEDIUM and HIGH
also re-arrange: they lift column headings that sit under their columns and raise small
component text to a comfortable reading size where it still fits its box.
"""
from __future__ import annotations

from pptx.dml.color import RGBColor
from pptx.util import Pt

from app.config.constants import DeckPolishConstants as C
from app.genai.deck_polish import checks, reader, spelling



def _by_code(issues, code):
    return [i for i in issues if i.code == code]


def _slide(prs, number):
    return prs.slides[number - 1]


def _text_frames(slide):
    """Every text frame on the slide: shapes, table cells and the speaker notes."""
    for shape in slide.shapes:
        if getattr(shape, "has_text_frame", False):
            yield shape.text_frame
        if getattr(shape, "has_table", False):
            for row in shape.table.rows:
                for cell in row.cells:
                    yield cell.text_frame
    if slide.has_notes_slide:
        yield slide.notes_slide.notes_text_frame


# --- LOW --------------------------------------------------------------------------- #
def dedupe_labels(prs, issues) -> list[str]:
    changes = []
    for issue in _by_code(issues, "duplicate_label"):
        slide = _slide(prs, issue.slide)
        removed = [s for s in slide.shapes
                   if s.name in (C.FRAME_LABEL_NAME, C.FRAME_SYMBOL_NAME)]
        for shape in removed:
            shape._element.getparent().remove(shape._element)
        if removed:
            changes.append(f"slide {issue.slide}: removed the stamped classification "
                           "label that sat on top of the one the layout already prints")
    return changes


def dedupe_numbers(prs, issues) -> list[str]:
    slides = []
    for issue in _by_code(issues, "duplicate_number"):
        slide = _slide(prs, issue.slide)
        for shape in [s for s in slide.shapes if s.name == C.FRAME_NUMBER_NAME]:
            shape._element.getparent().remove(shape._element)
            slides.append(issue.slide)
    if not slides:
        return []
    return [f"removed the stamped slide number on {len(slides)} slide(s); the template "
            "already prints one"]


def onto_slide(prs, issues) -> list[str]:
    width, height = prs.slide_width, prs.slide_height
    changes = []
    for issue in _by_code(issues, "off_slide"):
        slide = _slide(prs, issue.slide)
        for shape_id in issue.shapes:
            shape = reader.find_shape(slide, shape_id)
            if shape is None or shape.width > width or shape.height > height:
                continue
            shape.left = int(min(max(shape.left, 0), width - shape.width))
            shape.top = int(min(max(shape.top, 0), height - shape.height))
            changes.append(f"slide {issue.slide}: moved shape {shape_id} back onto the slide")
    return changes


def _set_min_size(shape, minimum: float, placeholders: bool) -> int:
    """Raise every sized run below ``minimum`` pt to it. Returns runs changed."""
    if not getattr(shape, "has_text_frame", False):
        return 0
    if shape.is_placeholder and not placeholders:
        return 0
    changed = 0
    for paragraph in shape.text_frame.paragraphs:
        if paragraph.font.size is not None and paragraph.font.size.pt < minimum:
            paragraph.font.size = Pt(minimum)
            changed += 1
        for run in paragraph.runs:
            if run.font.size is not None and run.font.size.pt < minimum:
                run.font.size = Pt(minimum)
                changed += 1
    return changed


def floor_text(prs, issues) -> list[str]:
    changes = []
    for issue in _by_code(issues, "tiny_text"):
        slide = _slide(prs, issue.slide)
        runs = sum(_set_min_size(reader.find_shape(slide, i), C.FLOOR_PT, True)
                   for i in issue.shapes if reader.find_shape(slide, i) is not None)
        if runs:
            changes.append(f"slide {issue.slide}: raised {runs} text run(s) to the "
                           f"{C.FLOOR_PT} pt floor")
    return changes


def _recolor(shape, old: str, new: str) -> int:
    changed = 0
    if checks.fill_color(shape) == old:
        shape.fill.fore_color.rgb = RGBColor.from_string(new)
        changed += 1
    if getattr(shape, "has_text_frame", False):
        for paragraph in shape.text_frame.paragraphs:
            for run in paragraph.runs:
                if checks.run_color(run) == old:
                    run.font.color.rgb = RGBColor.from_string(new)
                    changed += 1
    return changed


def brand_colors(prs, issues) -> list[str]:
    changes = []
    for issue in _by_code(issues, "off_palette"):
        old = issue.data["color"]
        new = checks.nearest_brand(old)
        slide = _slide(prs, issue.slide)
        count = sum(_recolor(s, old, new) for s in slide.shapes if s.shape_id in issue.shapes)
        if count:
            changes.append(f"slide {issue.slide}: changed colour {old} to the brand "
                           f"colour {new}")
    return changes


def us_spelling(prs, issues) -> list[str]:
    changes = []
    for issue in _by_code(issues, "uk_spelling"):
        fixed = []
        for frame in _text_frames(_slide(prs, issue.slide)):
            for paragraph in frame.paragraphs:
                for run in paragraph.runs:
                    new = spelling.to_us(run.text)
                    if new != run.text:
                        fixed += spelling.uk_words(run.text)
                        run.text = new
        if fixed:
            changes.append(f"slide {issue.slide}: US spelling for "
                           + ", ".join(dict.fromkeys(fixed)))
    return changes


# --- MEDIUM and HIGH --------------------------------------------------------------- #
def readable_text(prs, issues) -> list[str]:
    """Raise small component text to READ_PT where the box still holds it."""
    changes = []
    for issue in _by_code(issues, "small_text"):
        slide = _slide(prs, issue.slide)
        raised = 0
        for shape_id in issue.shapes:
            shape = reader.find_shape(slide, shape_id)
            if shape is None:
                continue
            before = [(item, item.font.size) for p in shape.text_frame.paragraphs
                      for item in (p, *p.runs)]
            if not _set_min_size(shape, C.READ_PT, False):
                continue
            needed = checks._needed(shape)                  # noqa: SLF001
            if needed and needed > reader.inches(shape.height) + 0.05:
                for item, size in before:                   # does not fit: put it back
                    item.font.size = size
                continue
            raised += 1
        if raised:
            changes.append(f"slide {issue.slide}: raised small text to {C.READ_PT} pt in "
                           f"{raised} box(es)")
    return changes


def lift_headings(prs, issues) -> list[str]:
    """Move column headings from under their columns to above them."""
    changes = []
    for issue in _by_code(issues, "heading_below"):
        slide = _slide(prs, issue.slide)
        pairs = checks.headings_below(slide)
        for head, block in pairs:
            hl, ht, hr, hb = reader.box(head)
            bl, bt, br, bb = reader.box(block)
            shift = (hb - ht) + C.HEADING_GAP_IN
            reader.set_box(head, hl, bt, hr - hl, hb - ht)
            reader.set_box(block, bl, bt + shift, br - bl, max(bb - bt - shift, 0.5))
        if pairs:
            changes.append(f"slide {issue.slide}: moved {len(pairs)} column heading(s) "
                           "above their columns")
    return changes


LOW_RULES = (dedupe_labels, dedupe_numbers, onto_slide, floor_text, brand_colors, us_spelling)
ARRANGE_RULES = (lift_headings, readable_text)


def apply(prs, issues, level: str) -> list[str]:
    """Run every rule for this level. Returns the change sentences."""
    rules = LOW_RULES + (ARRANGE_RULES if level in C.ARRANGE_RULES_FROM else ())
    changes = []
    for rule in rules:
        changes += rule(prs, issues)
    return changes

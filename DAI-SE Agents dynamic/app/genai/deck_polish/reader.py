"""Read a deck: what each shape is, where it sits and what it says.

Everything here only reads. Positions are in inches, as the ppt skill and DeckBuilder
use them, and every shape is referred to by its ``shape_id`` (unique on its slide), which
is also how the model names a shape in an edit operation.
"""
from __future__ import annotations

from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER

from app.config.constants import DeckPolishConstants as C, SkillDeckConstants as SD
from app.genai.skill_deck import layout_check

EMU = 914400
#: Shapes stamped by the template frame or an older branding component: never content.
FURNITURE = (SD.WORDMARK_SHAPE_NAME, SD.BADGE_SHAPE_NAME, SD.LABEL_SHAPE_NAME,
             *SD.FRAME_SHAPE_PREFIXES)

Box = tuple[float, float, float, float]          # left, top, right, bottom (in)

_TITLE_TYPES = (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE)
_SUBTITLE_TYPES = (PP_PLACEHOLDER.SUBTITLE,)
_FOOTER_TYPES = (PP_PLACEHOLDER.FOOTER, PP_PLACEHOLDER.SLIDE_NUMBER, PP_PLACEHOLDER.DATE)


def inches(emu) -> float:
    return (emu or 0) / EMU


def has_box(shape) -> bool:
    return layout_check.geometry(shape) is not None


def box(shape) -> Box:
    left, top, width, height = layout_check.geometry(shape) or (0, 0, 0, 0)
    left, top = inches(left), inches(top)
    return left, top, left + inches(width), top + inches(height)


def overlap(a: Box, b: Box) -> float:
    """Area (sq in) two boxes share."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def area(a: Box) -> float:
    return max(a[2] - a[0], 0) * max(a[3] - a[1], 0)


def contains(outer: Box, inner: Box, tol: float = 0.05) -> bool:
    return (inner[0] >= outer[0] - tol and inner[1] >= outer[1] - tol
            and inner[2] <= outer[2] + tol and inner[3] <= outer[3] + tol)


def is_furniture(shape) -> bool:
    return shape.name.startswith(FURNITURE)


def text_of(shape) -> str:
    if not getattr(shape, "has_text_frame", False):
        return ""
    return shape.text_frame.text.strip()


def paragraphs(shape) -> list[str]:
    """Non-empty paragraph texts of a text shape."""
    if not getattr(shape, "has_text_frame", False):
        return []
    return [p.text.strip() for p in shape.text_frame.paragraphs if p.text.strip()]


def kind(shape) -> str:
    """One word for what the shape is: title, subtitle, body, picture, table, chart,
    card, shape, textbox, line, group, footer or frame."""
    if is_furniture(shape):
        return "frame"
    if shape.is_placeholder:
        ph_type = shape.placeholder_format.type
        if ph_type in _TITLE_TYPES:
            return "title"
        if ph_type in _SUBTITLE_TYPES:
            return "subtitle"
        if ph_type in _FOOTER_TYPES:
            return "footer"
        if ph_type == PP_PLACEHOLDER.PICTURE and not text_of(shape):
            return "picture"
    shape_type = shape.shape_type
    if shape_type == MSO_SHAPE_TYPE.PICTURE or (shape.is_placeholder
                                                and hasattr(shape, "image")):
        return "picture"
    if getattr(shape, "has_table", False):
        return "table"
    if getattr(shape, "has_chart", False):
        return "chart"
    if shape_type == MSO_SHAPE_TYPE.GROUP:
        return "group"
    if shape_type in (MSO_SHAPE_TYPE.LINE,) or shape.__class__.__name__ == "Connector":
        return "line"
    if shape.is_placeholder:
        return "body"
    if shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE:
        filled = False
        try:
            filled = shape.fill.type is not None and shape.fill.type != 5
        except (AttributeError, TypeError, NotImplementedError):
            pass
        b = box(shape) if has_box(shape) else (0, 0, 0, 0)
        if filled and min(b[2] - b[0], b[3] - b[1]) >= SD.LAYOUT_MIN_CARD_IN:
            return "card"
        return "shape"
    if shape_type == MSO_SHAPE_TYPE.TEXT_BOX:
        return "textbox"
    return "shape"


def run_sizes(shape) -> list[float]:
    """Font sizes (pt) set on the shape's runs (or paragraphs) with text."""
    sizes = []
    if not getattr(shape, "has_text_frame", False):
        return sizes
    for paragraph in shape.text_frame.paragraphs:
        for run in paragraph.runs:
            if not run.text.strip():
                continue
            size = run.font.size or paragraph.font.size
            if size is not None:
                sizes.append(round(size.pt, 1))
    return sizes


def template_graphics(slide) -> list[Box]:
    """Boxes of the graphics the slide's layout and master draw (bands, panels, frames).

    A placeholder that sits on one is placed by the template's designer - e.g. column
    titles printed on coloured bands - so nothing here moves it. Full-slide backgrounds
    are left out: everything sits on those.
    """
    width = inches(slide.part.package.presentation_part.presentation.slide_width)
    height = inches(slide.part.package.presentation_part.presentation.slide_height)
    boxes = []
    for owner in (slide.slide_layout, slide.slide_layout.slide_master):
        for shape in owner.shapes:
            if shape.is_placeholder or not has_box(shape):
                continue
            b = box(shape)
            if area(b) < 0.5 * width * height and min(b[2] - b[0], b[3] - b[1]) > 0.2:
                boxes.append(b)
    return boxes


def on_template_graphic(shape, graphics: list[Box]) -> bool:
    """True when the shape sits wholly on a graphic its layout draws."""
    return has_box(shape) and any(contains(g, box(shape)) for g in graphics)


def set_box(shape, left: float, top: float, width: float, height: float) -> None:
    """Set all four of a shape's coordinates (inches).

    A placeholder that inherits its position has no geometry of its own; setting only
    ``top`` would give it a zero width, so all four are always written.
    """
    shape.left, shape.top = int(left * EMU), int(top * EMU)
    shape.width, shape.height = int(width * EMU), int(height * EMU)
    layout_check.forget(shape)


def find_shape(slide, shape_id: int):
    """The shape with this id on the slide, or None."""
    return next((s for s in slide.shapes if s.shape_id == shape_id), None)


def describe_slide(slide, number: int) -> dict:
    """The slide as compact JSON for the model: layout, and one entry per shape."""
    shapes = []
    graphics = template_graphics(slide)
    for shape in slide.shapes:
        if not has_box(shape):
            continue
        entry = {"id": shape.shape_id, "kind": kind(shape),
                 "box": [round(v, 2) for v in (inches(shape.left), inches(shape.top),
                                               inches(shape.width), inches(shape.height))]}
        text = text_of(shape)
        if text:
            entry["text"] = text[:C.MAX_TEXT_CHARS]
            entry["paragraphs"] = len(paragraphs(shape))
        sizes = run_sizes(shape)
        if sizes:
            entry["pt"] = sorted(set(sizes))
        if shape.is_placeholder:
            entry["placeholder"] = True
        if on_template_graphic(shape, graphics):
            entry["fixed"] = True               # placed on a template graphic: keep it
        shapes.append(entry)
    notes = ""
    if slide.has_notes_slide:
        notes = slide.notes_slide.notes_text_frame.text.strip()
    return {"slide": number, "layout": slide.slide_layout.name, "shapes": shapes,
            **({"notes_chars": len(notes)} if notes else {})}


def describe_deck(prs) -> dict:
    """The whole deck for the model."""
    return {"slide_size_in": [round(inches(prs.slide_width), 2),
                              round(inches(prs.slide_height), 2)],
            "slides": [describe_slide(s, n) for n, s in enumerate(prs.slides, 1)]}

"""Style match: how close the generated deck is to the reference the user picked.

The dynamic skills feature (``app/genai/skills``) inspects a reference deck, PDF, image
or website into a style-only profile and pins a skill built from it to the session.
That skill reaches the model as guidance, but nothing checked that the deck actually
followed it. This module does: it measures the same few things on the reference
profile and on the generated deck, scores each one, and words every big difference as
an instruction the model can act on in its fix round.

Measured on content slides (every slide but the cover):

* shapes      - how many shapes a slide holds (busy or sparse);
* coverage    - how much of the slide the shapes cover (white space);
* title_pt    - the largest text size on a slide (the title);
* body_pt     - the typical size of the rest of the text;
* card_share  - how much of the content sits in shapes/cards rather than plain text.

Colours and fonts are left out on purpose: the brand rules fix them, and the reference
never overrides the brand. Size targets are clamped to the brand's ranges for the same
reason.
"""
from __future__ import annotations

import statistics
from pathlib import Path

from app.config.constants import SkillDeckConstants as C

_EMU = 914400
_OURS = (C.WORDMARK_SHAPE_NAME, C.BADGE_SHAPE_NAME, C.LABEL_SHAPE_NAME)
_TEXT_KINDS = ("AUTO_SHAPE", "TEXT_BOX", "PLACEHOLDER")
MEASURES = ("shapes", "coverage", "title_pt", "body_pt", "card_share")


# --- one slide's numbers, from plain boxes ------------------------------------------ #
def _coverage(boxes) -> float:
    """Share of the slide covered by the union of the boxes (fractions of the slide)."""
    cols, rows = C.STYLE_GRID
    covered = set()
    for x, y, w, h in boxes:
        c0, c1 = max(0, int(x * cols)), min(cols, int((x + w) * cols + 0.999))
        r0, r1 = max(0, int(y * rows)), min(rows, int((y + h) * rows + 0.999))
        covered.update((c, r) for c in range(c0, c1) for r in range(r0, r1))
    return len(covered) / (cols * rows)


def _slide_numbers(shapes) -> dict:
    """Measures for one slide.

    Args:
        shapes: ``(kind, (x, y, w, h), sizes_pt)`` per shape, boxes as fractions of the
            slide. Backgrounds are dropped here.

    Returns:
        The measures this slide supports.
    """
    kept = [(kind, box, sizes) for kind, box, sizes in shapes
            if box[2] * box[3] < C.STYLE_BACKGROUND_SHARE]
    low, high = C.STYLE_READ_PT_RANGE
    sizes = sorted(s for _, _, found in kept for s in found if low <= s <= high)
    numbers = {"shapes": float(len(kept)), "coverage": _coverage([b for _, b, _ in kept])}
    if sizes:
        numbers["title_pt"] = sizes[-1]
        if len(sizes) > 1:
            numbers["body_pt"] = statistics.median(sizes[:-1])
    texty = [kind for kind, _, _ in kept if any(k in kind for k in _TEXT_KINDS)]
    if texty:
        numbers["card_share"] = sum("AUTO_SHAPE" in k for k in texty) / len(texty)
    return numbers


def _combine(slides: list[dict]) -> dict:
    """Median of each measure over the slides that have it."""
    out = {}
    for name in MEASURES:
        values = [s[name] for s in slides if name in s]
        if values:
            out[name] = round(statistics.median(values), 3)
    return out


def _content_slides(slides: list) -> list:
    """Drop the cover when there is anything else to look at."""
    return slides[1:] if len(slides) > 1 else slides


# --- the reference side ----------------------------------------------------------- #
def _profile_slides(profile: dict) -> list[dict]:
    """Per-slide measures from one inspected reference profile."""
    slides = []
    for exemplar in profile.get("exemplars") or []:
        if "shapes" in exemplar:                                  # PowerPoint
            shapes = []
            for shape in exemplar["shapes"]:
                b = shape.get("bounds") or {}
                box = (b.get("x", 0), b.get("y", 0), b.get("width", 0), b.get("height", 0))
                shapes.append((str(shape.get("kind", "")), box,
                               list(shape.get("font_sizes_pt") or [])))
            slides.append(_slide_numbers(shapes))
        elif "text_layout" in exemplar:                           # PDF: type sizes only
            sizes = [s.get("size_pt") for s in exemplar["text_layout"] if s.get("size_pt")]
            numbers = _slide_numbers([("", (0, 0, 0, 0), sizes)])
            slides.append({k: v for k, v in numbers.items() if k in ("title_pt", "body_pt")})
    return slides


def _clamp(value: float, bounds: tuple[float, float]) -> float:
    return min(max(value, bounds[0]), bounds[1])


def style_target(profiles: list[dict]) -> dict:
    """What the deck should look like, from the reference profiles of the active skill.

    Images and websites carry no slide geometry, so they add nothing here (their
    colours and fonts lose to the brand anyway).

    Args:
        profiles: Inspected reference profiles (``{"source_format", "exemplars", ...}``).

    Returns:
        Target measures; empty when no reference has anything measurable.
    """
    per_profile = [_combine(_content_slides(_profile_slides(p))) for p in profiles or []]
    target = _combine([p for p in per_profile if p])
    if "title_pt" in target:
        target["title_pt"] = _clamp(target["title_pt"], C.STYLE_TITLE_PT_RANGE)
    if "body_pt" in target:
        target["body_pt"] = _clamp(target["body_pt"], C.STYLE_BODY_PT_RANGE)
    return target


def describe(target: dict) -> str:
    """The target as one line for the model, so it aims at it from the first program."""
    parts = []
    if "shapes" in target:
        parts.append(f"about {target['shapes']:.0f} shapes per content slide")
    if "coverage" in target:
        parts.append(f"shapes covering about {target['coverage']:.0%} of the slide")
    if "title_pt" in target:
        parts.append(f"titles about {target['title_pt']:.0f} pt")
    if "body_pt" in target:
        parts.append(f"body text about {target['body_pt']:.0f} pt")
    if "card_share" in target:
        parts.append(f"{target['card_share']:.0%} of text elements inside shapes or cards")
    if not parts:
        return ""
    return ("Measured from the reference (aim for these; the brand rules still win): "
            + "; ".join(parts) + ".")


_REFERENCE_TYPES = {
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def inspect_reference(path: Path) -> dict:
    """Inspect a reference file with the dynamic skills readers (for terminal tools).

    Args:
        path: A .pptx, .pdf, .png or .jpg file.

    Returns:
        Its style-only profile, the same shape the app stores for a session.

    Raises:
        ValueError: For any other file type.
    """
    from app.genai.skills.reference_readers.dispatch import inspect_file
    mime = _REFERENCE_TYPES.get(Path(path).suffix.lower())
    if mime is None:
        raise ValueError(f"reference must be one of {', '.join(_REFERENCE_TYPES)}")
    return inspect_file(Path(path).read_bytes(), mime)


# --- the deck side ---------------------------------------------------------------- #
def _run_sizes(shape) -> list[float]:
    if not getattr(shape, "has_text_frame", False) or not shape.text_frame.text.strip():
        return []
    sizes = []
    for paragraph in shape.text_frame.paragraphs:
        found = [r.font.size.pt for r in paragraph.runs if r.font.size is not None]
        if not found and paragraph.font.size is not None and paragraph.text.strip():
            found = [paragraph.font.size.pt]
        sizes += found
    return sizes


def deck_style(deck: Path) -> dict:
    """The same measures for a .pptx (the logo component's shapes are ignored).

    Args:
        deck: The deck to measure.

    Returns:
        Measures over its content slides.
    """
    from pptx import Presentation
    prs = Presentation(str(deck))
    width, height = prs.slide_width or 1, prs.slide_height or 1
    slides = []
    for slide in prs.slides:
        shapes = []
        for shape in slide.shapes:
            if shape.name in _OURS or shape.width is None or shape.left is None:
                continue
            box = (shape.left / width, shape.top / height,
                   shape.width / width, shape.height / height)
            shapes.append((str(shape.shape_type), box, _run_sizes(shape)))
        slides.append(_slide_numbers(shapes))
    return _combine(_content_slides(slides))


# --- comparing -------------------------------------------------------------------- #
def _ratio(a: float, b: float) -> float:
    return min(a, b) / max(a, b) if max(a, b) > 0 else 1.0


def _score(name: str, want: float, have: float) -> float:
    if name == "coverage":
        return max(0.0, 1 - abs(want - have) / C.STYLE_COVERAGE_SPAN)
    if name == "card_share":
        return max(0.0, 1 - abs(want - have))
    return _ratio(want, have)


def _advice(name: str, want: float, have: float) -> str:
    more = have > want
    if name == "shapes":
        return (f"content slides hold about {have:.0f} shapes each; the reference uses about "
                f"{want:.0f} - " + ("use fewer, larger elements (merge small boxes, drop "
                                    "decoration)" if more else
                                    "add structure (cards, a numbered process row, icon "
                                    "circles or a key-number callout)"))
    if name == "coverage":
        return (f"content covers about {have:.0%} of each slide; the reference covers about "
                f"{want:.0%} - " + ("leave more empty space: fewer or smaller cards, shorter "
                                    "text, wider gaps" if more else
                                    "use larger cards and more of the content area (still "
                                    "inside the margins and above the bottom strip)"))
    if name == "title_pt":
        return (f"slide titles are about {have:.0f} pt; the reference uses about {want:.0f} pt"
                f" - use {want:.0f} pt titles and keep each on one line")
    if name == "body_pt":
        return (f"body text is about {have:.0f} pt; the reference uses about {want:.0f} pt - "
                f"use {want:.0f} pt")
    return (f"{have:.0%} of the text elements sit in shapes or cards; the reference has "
            f"{want:.0%} - " + ("use fewer cards and more plain text blocks" if more else
                                "put more of the content into cards or shapes"))


def compare(target: dict, measured: dict) -> tuple[int, list[str]]:
    """Score the deck against the target and list what to change.

    Args:
        target: From :func:`style_target`.
        measured: From :func:`deck_style`.

    Returns:
        Tuple of (match 0-100, differences worded for the model). A measure the deck
        or the reference lacks is skipped; with nothing to compare the match is 100.
    """
    scores, differences = [], []
    for name in MEASURES:
        if name not in target or name not in measured:
            continue
        score = _score(name, target[name], measured[name])
        scores.append(score)
        if score < C.STYLE_FIX_BELOW:
            differences.append(_advice(name, target[name], measured[name]))
    match = round(100 * sum(scores) / len(scores)) if scores else 100
    return match, differences

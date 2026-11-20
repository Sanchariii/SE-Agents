"""Deck scorecard: one 0-100 score per area, for any .pptx.

It works on decks from either engine (the classic per-slide designer or the skill deck
agent), so the same document can be built both ways and compared with numbers. It only
measures the file (no rendering, no model call), so it is cheap enough to run on every
deck.

Areas, and what each one checks:

* layout       - slides with no layout defect. The same rules for every deck
                 (``layout_check`` universal mode): nothing off the slide, no text too
                 long for its box, no overlapping cards or text, nothing covering a logo
                 or label - plus the frame checks of the deck polish agent (a stamped
                 label on top of one the layout prints, content running into the frame);
* brand        - built on the official template (SAMPLE.pptx layouts), a logo on every
                 slide, brand fonts, brand colours (the ppt skill's palette, tints of the
                 theme accents, the theme's own colours), the slide number printed once;
* readability  - text at 12 pt or more (the floor is 10 pt), slides that are not walls of
                 text, US spelling;
* content      - (needs the written deck) slide count, titles and wording kept,
                 speaker notes kept, nothing shown twice on a slide;
* style        - (needs a reference) ``style_match`` against the user's reference.

The overall score is the weighted mean of the areas that were measured.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from pptx.enum.dml import MSO_FILL

from app.config.constants import SkillDeckConstants as C
from app.genai.deck_polish import spelling
from app.genai.skill_deck import layout_check, style_match

_FURNITURE = layout_check.FURNITURE      # logos, labels, slide numbers: not content
_WORD = re.compile(r"[a-z0-9]+")
_SLIDE = re.compile(r"^slide (\d+):")
#: Deck polish findings that are layout defects too (the frame is outside layout_check).
_FRAME_CODES = ("duplicate_label", "conflicting_label", "covers_frame")


# --- reading the deck ------------------------------------------------------------- #
def _text_shapes(slide):
    return [s for s in slide.shapes if not s.name.startswith(_FURNITURE) and s.has_text_frame
            and s.text_frame.text.strip()]


def _runs(slide):
    for shape in _text_shapes(slide):
        for paragraph in shape.text_frame.paragraphs:
            for run in paragraph.runs:
                if run.text.strip():
                    yield paragraph, run


def _rgb(color) -> str:
    try:
        return str(color.rgb) if color.type is not None and color.rgb else ""
    except (AttributeError, TypeError, ValueError):
        return ""


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _slide_text(slide) -> str:
    return "\n".join(s.text_frame.text for s in _text_shapes(slide))


def _pct(part: float, whole: float) -> int:
    return round(100 * part / whole) if whole else 100


# --- areas ------------------------------------------------------------------------ #
def _layout(prs, deck: Path) -> dict:
    from app.genai.deck_polish import checks
    problems = layout_check.layout_problems(deck, universal=True)
    problems += [i.line() for i in checks.find_issues(prs) if i.code in _FRAME_CODES]
    bad = {int(m.group(1)) for p in problems if (m := _SLIDE.match(p))}
    total = len(prs.slides)
    return {"score": _pct(total - len(bad & set(range(1, total + 1))), total),
            "details": problems}


def _has_logo(slide, slide_area: int) -> bool:
    """A mark stamped by the branding component, or a logo picture in the template.

    The classic designer's template carries its logos as pictures in the slide layout
    (not always in a corner), so any picture there that is not a background counts.
    """
    if any(s.name.startswith((C.WORDMARK_SHAPE_NAME, C.BADGE_SHAPE_NAME))
           for s in slide.shapes):
        return True
    return any("PICTURE" in str(shape.shape_type) and shape.width is not None
               and shape.width * shape.height < C.SCORE_LOGO_MAX_SHARE * slide_area
               for owner in (slide.slide_layout, slide.slide_layout.slide_master)
               for shape in owner.shapes)


def _fill(shape) -> str:
    """A shape's solid fill colour, or "" (pictures, tables and gradients have none)."""
    try:
        return _rgb(shape.fill.fore_color) if shape.fill.type == MSO_FILL.SOLID else ""
    except (AttributeError, TypeError, ValueError, NotImplementedError):
        return ""


def _colors(slide):
    for _, run in _runs(slide):
        yield _rgb(run.font.color)
    for shape in slide.shapes:
        if not shape.name.startswith(_FURNITURE):
            yield _fill(shape)


@lru_cache(maxsize=1)
def _template_layouts() -> frozenset:
    """Layout names of the official template (empty when it cannot be read)."""
    try:
        from pptx import Presentation
        from app.genai.ppt.template import registry
        template = Presentation(str(registry.template_path()))
    except Exception:                       # noqa: BLE001 - the part is skipped
        return frozenset()
    return frozenset(layout.name for master in template.slide_masters
                     for layout in master.slide_layouts)


def _brand(prs) -> dict:
    from app.genai.deck_polish import checks
    slides = list(prs.slides)
    area = (prs.slide_width or 1) * (prs.slide_height or 1)
    no_logo = [n for n, s in enumerate(slides, 1) if not _has_logo(s, area)]
    names = [run.font.name for s in slides for _, run in _runs(s) if run.font.name]
    values = [v for s in slides for v in _colors(s) if v]
    palette = checks.brand_palette(prs)
    brand_fonts = [n for n in names if n.startswith(C.BRAND_FONT_PREFIXES)]
    off_fonts = sorted(set(names) - set(brand_fonts))
    off_colors = sorted(set(values) - palette)
    parts = [_pct(len(slides) - len(no_logo), len(slides)),
             _pct(len(brand_fonts), len(names)),
             _pct(sum(v in palette for v in values), len(values))]
    official = _template_layouts()
    details = []
    # The template prints the slide number and the frame stamps another: the two sit
    # almost on top of each other (they visibly clash from slide 10 on), so this costs
    # brand points by share of slides rather than failing each slide's layout.
    doubled = sorted({i.slide for i in checks.find_issues(prs) if i.code == "duplicate_number"})
    parts.append(_pct(len(slides) - len(doubled), len(slides)))
    if doubled:
        details.append(f"slide number printed twice (template + stamp) on {len(doubled)} "
                       "slide(s)")
    if official:
        off_template = [n for n, s in enumerate(slides, 1)
                        if s.slide_layout.name not in official]
        parts.insert(0, _pct(len(slides) - len(off_template), len(slides)))
        if off_template:
            details.append("not on an official template layout: slide(s) "
                           + ", ".join(map(str, off_template)))
    if no_logo:
        details.append(f"no logo on slide(s) {', '.join(map(str, no_logo))}")
    if off_fonts:
        details.append(f"non-brand fonts: {', '.join(off_fonts)}")
    if off_colors:
        details.append(f"non-brand colours: {', '.join(off_colors)}")
    return {"score": round(sum(parts) / len(parts)), "details": details}


def _readability(prs) -> dict:
    sized = small = 0
    wordy, spellings = [], []
    for number, slide in enumerate(prs.slides, 1):
        for paragraph, run in _runs(slide):
            size = run.font.size or paragraph.font.size
            if size is not None:
                sized += 1
                small += size.pt < C.SCORE_MIN_FONT_PT
        text = _slide_text(slide)
        if len(_words(text)) > C.SCORE_MAX_WORDS:
            wordy.append(number)
        spellings += [f"'{word}' (slide {number})" for word in spelling.uk_words(text)]
    total = len(prs.slides)
    parts = [_pct(sized - small, sized), _pct(total - len(wordy), total),
             max(0, 100 - C.SCORE_SPELLING_PENALTY * len(spellings))]
    details = []
    if small:
        details.append(f"{small} text run(s) below {C.SCORE_MIN_FONT_PT} pt (allowed down "
                       f"to {C.SCORE_FLOOR_FONT_PT} pt, but hard to read when projected)")
    if wordy:
        details.append(f"more than {C.SCORE_MAX_WORDS} words on slide(s) "
                       f"{', '.join(map(str, wordy))}")
    if spellings:
        details.append("UK spelling: " + ", ".join(spellings))
    return {"score": round(sum(parts) / len(parts)), "details": details}


def _spec_words(slide: dict) -> set[str]:
    parts = [slide.get("title", "")]
    for block in slide.get("blocks") or []:
        parts += [block.get("text", ""), *(block.get("bullets") or [])]
    quiz = slide.get("quiz") or {}
    parts += [quiz.get("question", ""), *[str(o) for o in quiz.get("options") or []]]
    return {w for part in parts for w in _words(str(part)) if len(w) >= 4}


def _notes(slide) -> str:
    if slide is None or not slide.has_notes_slide:
        return ""
    return slide.notes_slide.notes_text_frame.text.strip()


def _duplicates(slide) -> bool:
    seen = set()
    for shape in _text_shapes(slide):
        text = " ".join(_words(shape.text_frame.text))
        if len(text) < C.SCORE_DUPLICATE_MIN_CHARS:
            continue
        if text in seen:
            return True
        seen.add(text)
    return False


def _content(prs, spec: dict) -> dict:
    planned = spec.get("slides") or []
    slides = list(prs.slides)
    texts = [" ".join(_words(_slide_text(s))) for s in slides]
    deck_words = {w for t in texts for w in t.split()}
    titles_missing, notes_missing = [], []
    wanted, kept = 0, 0
    for index, item in enumerate(planned):
        title = " ".join(_words(item.get("title", "")))
        here = texts[index] if index < len(texts) else ""
        if title and title not in here and not any(title in t for t in texts):
            titles_missing.append(index + 1)
        words = _spec_words(item)
        wanted += len(words)
        kept += len(words & deck_words)
        if item.get("notes") and not _notes(slides[index] if index < len(slides) else None):
            notes_missing.append(index + 1)
    with_notes = sum(1 for item in planned if item.get("notes"))
    repeated = [n for n, s in enumerate(slides, 1) if _duplicates(s)]
    count = round(100 * min(len(slides), len(planned)) / max(len(slides), len(planned), 1))
    parts = [count, _pct(len(planned) - len(titles_missing), len(planned)),
             _pct(kept, wanted), _pct(with_notes - len(notes_missing), with_notes),
             _pct(len(slides) - len(repeated), len(slides))]
    details = []
    if len(slides) != len(planned):
        details.append(f"{len(slides)} slide(s) built for {len(planned)} written")
    if titles_missing:
        details.append(f"title missing on slide(s) {', '.join(map(str, titles_missing))}")
    if wanted and kept < wanted:
        details.append(f"{wanted - kept} of {wanted} written words not on the slides")
    if notes_missing:
        details.append(f"speaker notes missing on slide(s) "
                       f"{', '.join(map(str, notes_missing))}")
    if repeated:
        details.append(f"same text shown twice on slide(s) {', '.join(map(str, repeated))}")
    return {"score": round(sum(parts) / len(parts)), "details": details}


def _style(deck: Path, target: dict) -> dict:
    match, differences = style_match.compare(target, style_match.deck_style(deck))
    return {"score": match, "details": differences}


# --- public ----------------------------------------------------------------------- #
def score_deck(deck: Path, spec: dict | None = None, style_target: dict | None = None) -> dict:
    """Score one deck.

    Args:
        deck: The .pptx to score.
        spec: The written deck (``deck_engine.deck_spec``); enables the content area.
        style_target: From ``style_match.style_target``; enables the style area.

    Returns:
        ``{"overall": int, "areas": {name: {"score": int, "details": [str]}}}``.
    """
    from pptx import Presentation
    prs = Presentation(str(deck))
    with layout_check.measuring():
        areas = {"layout": _layout(prs, Path(deck)), "brand": _brand(prs),
                 "readability": _readability(prs)}
        if spec and spec.get("slides"):
            areas["content"] = _content(prs, spec)
    if style_target:
        areas["style"] = _style(Path(deck), style_target)
    for area in areas.values():
        area["details"] = area["details"][:C.SCORE_MAX_DETAILS]
    weights = {name: C.SCORE_WEIGHTS[name] for name in areas}
    overall = round(sum(areas[n]["score"] * w for n, w in weights.items())
                    / sum(weights.values()))
    return {"overall": overall, "areas": areas}


def summary_line(card: dict) -> str:
    """One line for the chat, e.g. ``Deck scorecard: 86/100 (layout 90, brand 100)``."""
    parts = ", ".join(f"{name} {area['score']}" for name, area in card["areas"].items())
    return f"Deck scorecard: {card['overall']}/100 ({parts})."


def format_table(named: list[tuple[str, dict]]) -> str:
    """Side-by-side table of several scorecards, then each deck's findings."""
    names = [name for name, _ in named]
    areas = list(dict.fromkeys(a for _, card in named for a in card["areas"]))
    width = max([12, *(len(n) for n in names)]) + 2
    lines = ["area".ljust(14) + "".join(n.rjust(width) for n in names),
             "overall".ljust(14) + "".join(str(c["overall"]).rjust(width) for _, c in named)]
    for area in areas:
        lines.append(area.ljust(14) + "".join(
            str(c["areas"][area]["score"] if area in c["areas"] else "-").rjust(width)
            for _, c in named))
    for name, card in named:
        found = [f"  {area}: {d}" for area, a in card["areas"].items() for d in a["details"]]
        lines += ["", f"{name}:", *(found or ["  nothing to report"])]
    return "\n".join(lines)

"""Introspect the one brand template into the catalog every later stage reads from.

Nothing here is hard-coded about Rockwell: the masters, their themes, every layout's
placeholders with their inherited formatting, the decorative shapes the master already
draws, and the background each layout sits on are all read back out of the .pptx.

Three things this catalog carries that a placeholder-only view cannot:

  - `decor_rects`: where the inherited graphics *are*, not just how many there are. The
    geometry gate needs the rectangles, because a composing agent will otherwise place a
    card straight over the brand mark the master already drew.
  - `background_luminance`: light vs dark, so text colour and which logo variant to use
    are decided from the slide rather than guessed from the layout's name.
  - `canvas_capable` + `free_region`: whether a layout leaves a usable empty area under
    its title, and exactly which rectangle that is. This is what lets `canvas` mode
    compose freely while still inheriting the master's theme and graphics (Decision 4).

Capacity hints come from `app.genai.ppt.measure` rather than being re-derived here, so
there is one text-measurement model in the codebase and not two that drift apart.
"""

import difflib
import io
import json
import re
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional, Union

from lxml import etree
from PIL import Image                       # a hard requirement of python-pptx itself
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.oxml.ns import qn

from app.config.constants import GeometryConstants
from app.genai.ppt import measure
from app.genai.ppt.template import registry
from app.genai.ppt.template.loader import open_presentation
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

EMU_PER_INCH = 914400

#: OOXML addresses the first two theme colours by their *slot* name on a slide
#: ('bg1'/'tx1') but by their *scheme* name in the theme part ('lt1'/'dk1').
_SCHEME_ALIASES = {"bg1": "lt1", "tx1": "dk1", "bg2": "lt2", "tx2": "dk2"}

#: Placeholders the frame renders itself. They are never the agent's to fill, so they
#: are dropped from the catalog rather than published with a content role.
FURNITURE_TYPES = ("DATE", "FOOTER", "SLIDE_NUMBER", "HEADER")


def _inches(emu: Optional[int]) -> float:
    return round((emu or 0) / EMU_PER_INCH, 2)


def _luminance(hex_color: str) -> float:
    """Perceived luminance of an RRGGBB hex colour, 0.0 (black) .. 1.0 (white)."""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) != 6:
        return 1.0
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


# --------------------------------------------------------------------------------- #
# value objects                                                                       #
# --------------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Rect:
    """An axis-aligned rectangle in inches on the slide canvas."""

    left: float
    top: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.left + self.width

    @property
    def bottom(self) -> float:
        return self.top + self.height

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.left, self.top, self.width, self.height)

    def to_list(self) -> list[float]:
        return [self.left, self.top, self.width, self.height]


@dataclass
class ThemeInfo:
    name: str
    major_font: str
    minor_font: str
    colors: dict[str, str]


@dataclass
class PlaceholderInfo:
    idx: int
    type: str
    role: str
    slot: str
    name: str
    prompt: str
    left: float
    top: float
    width: float
    height: float
    font_size: Optional[float] = None
    font_name: Optional[str] = None
    bold: Optional[bool] = None
    color: Optional[str] = None
    align: Optional[str] = None
    bullets: Optional[bool] = None
    all_caps: bool = False
    autofit: Optional[str] = None
    accepts_text: bool = True
    approx_chars: int = 0
    approx_lines: int = 0

    def summary(self) -> dict[str, Any]:
        data = {
            "idx": self.idx, "slot": self.slot, "role": self.role, "type": self.type,
            "position_in": [self.left, self.top, self.width, self.height],
            "font_size": self.font_size, "font": self.font_name, "bold": self.bold,
            "bullets": self.bullets, "all_caps": self.all_caps or None,
            "approx_chars": self.approx_chars, "approx_lines": self.approx_lines,
            "prompt": self.prompt[:60] if self.prompt else "",
        }
        if self.role == "picture":
            data["accepts_text"] = self.accepts_text
        return {k: v for k, v in data.items() if v not in (None, "", [])}

    def rect(self) -> Rect:
        """The placeholder's box, for the geometry gate to reason about."""
        return Rect(self.left, self.top, self.width, self.height)


@dataclass
class LayoutInfo:
    key: str
    master_index: int
    layout_index: int
    name: str
    theme_name: str
    placeholders: list[PlaceholderInfo]
    decor: dict[str, int]
    fixed_texts: list[str]
    background: str
    tags: list[str]
    columns: int
    recommended_use: str
    sample_titles: list[str] = field(default_factory=list)
    usage_count: int = 0
    canonical: bool = True
    duplicates: list[str] = field(default_factory=list)
    #: Where the master's own graphics sit. The gate treats these as occupied.
    decor_rects: tuple[Rect, ...] = ()
    #: 0.0 (black) .. 1.0 (white). Drives text colour and which logo variant is used.
    background_luminance: float = 1.0
    #: True when the layout leaves a free area worth composing into.
    canvas_capable: bool = False
    free_region: Optional[Rect] = None
    #: True for a layout of the High Creativity master, which only max mode may use.
    high_creativity: bool = False
    #: Placeholder-less High Creativity layouts only: where the program draws its title
    #: (borrowed from the master's title layout, so titles line up across the deck).
    title_rect: Optional[Rect] = None
    title_font_size: Optional[float] = None
    #: High Creativity layouts only: names of baked sample-text shapes, removed from
    #: generated files (never from SAMPLE.pptx) when a slide uses this layout.
    prompt_shapes: list[str] = field(default_factory=list)

    def by_role(self, role: str) -> list[PlaceholderInfo]:
        return [p for p in self.placeholders if p.role == role]

    def has_role(self, role: str) -> bool:
        return any(p.role == role for p in self.placeholders)

    def find(self, target: Union[int, str]) -> Optional[PlaceholderInfo]:
        """Resolve a placeholder by idx (int / numeric string) or slot name (case-insensitive)."""
        if isinstance(target, bool):
            return None
        if isinstance(target, int) or (isinstance(target, str) and target.strip().isdigit()):
            idx = int(target)
            return next((p for p in self.placeholders if p.idx == idx), None)
        if isinstance(target, str):
            key = target.strip().lower().replace(" ", "").replace("_", "")
            for p in self.placeholders:
                if p.slot.lower() == key:
                    return p
            if key in ("heading", "body", "picture", "content", "number", "caption",
                       "chart", "table"):
                return next((p for p in self.placeholders if p.role == key), None)
        return None

    def slot_summary(self) -> str:
        counts: dict[str, int] = {}
        for p in self.placeholders:
            counts[p.role] = counts.get(p.role, 0) + 1
        parts = []
        for role in ("title", "subtitle", "heading", "body", "content", "picture",
                     "number", "caption", "chart", "table"):
            if role in counts:
                extra = ""
                if role == "picture" and any(p.accepts_text for p in self.by_role("picture")):
                    extra = "(text ok)"
                parts.append(f"{counts[role]} {role}{extra}" if counts[role] > 1
                             else f"{role}{extra}")
        return ", ".join(parts) if parts else "no placeholders"

    def compact(self) -> str:
        line = f"{self.key} '{self.name}' | slots: {self.slot_summary()}"
        if self.columns >= 2:
            line += f" | {self.columns} columns"
        if self.background != "light":
            line += f" | {self.background} background"
        if self.canvas_capable and self.free_region is not None:
            line += (f" | free canvas {self.free_region.width:.1f}"
                     f"x{self.free_region.height:.1f} in")
        line += f" | tags: {', '.join(self.tags)}"
        return line

    def details(self) -> dict[str, Any]:
        return {
            "key": self.key, "name": self.name, "master": self.master_index,
            "theme": self.theme_name,
            "background": self.background,
            "background_luminance": self.background_luminance,
            "columns": self.columns, "tags": self.tags,
            "recommended_use": self.recommended_use,
            "placeholders_in_reading_order": [p.summary() for p in self.placeholders],
            "fixed_layout_shapes": self.decor,
            "fixed_shape_rects_in": [r.to_list() for r in self.decor_rects],
            "fixed_texts_on_layout": self.fixed_texts,
            "canvas_capable": self.canvas_capable,
            "free_region_in": self.free_region.to_list() if self.free_region else None,
            "sample_slide_titles_using_it": self.sample_titles,
            "duplicate_layouts_in_other_masters": self.duplicates,
        }

    # --- retired in Task 16 with ppt_creator_agent's free-form program prompt ----- #
    def to_json_contract(self) -> dict[str, Any]:
        """The template detail the old free-form code prompt handed the model."""
        return {
            "layout_name": self.name,
            "master_index": self.master_index,
            "layout_index": self.layout_index,
            "placeholders": [{"slot": p.slot, "idx": p.idx, "kind": p.role}
                             for p in self.placeholders],
        }


@dataclass
class TemplateCatalog:
    template_path: str
    slide_width: float
    slide_height: float
    themes: list[ThemeInfo]
    layouts: list[LayoutInfo]
    preferred_master: int
    cover_master: int
    sample_slide_count: int
    #: The High Creativity master's index, or None when the template has none. Its
    #: layouts are kept OUT of `layouts`, so nothing that selects layouts for LOW/MED
    #: decks (the art director, recommend, search, family lookups) can ever offer them.
    high_creativity_master: Optional[int] = None
    high_creativity_layouts: list[LayoutInfo] = field(default_factory=list)

    # ---- lookups -------------------------------------------------------------------
    @property
    def theme(self) -> ThemeInfo:
        return (self.themes[self.preferred_master] if self.themes
                else ThemeInfo("", "Barlow", "Barlow", {}))

    def canonical_layouts(self) -> list[LayoutInfo]:
        return [l for l in self.layouts if l.canonical]

    def get(self, key: str) -> Optional[LayoutInfo]:
        wanted = key.strip().lower()
        return next((l for l in (*self.layouts, *self.high_creativity_layouts)
                     if l.key.lower() == wanted), None)

    # ---- the High Creativity master (max mode only) ---------------------------------
    def high_creativity_problems(self) -> list[str]:
        """Why the High Creativity master cannot be used, or [] when it can.

        Its colour scheme and fonts are not checked: generated files force the brand
        palette and Barlow onto it (`deck_builder.apply_high_creativity_overrides`),
        and its baked sample text is stripped. What must hold is structural: the master
        exists and offers a canvas with a place for the title.

        Returns:
            One human-readable problem per failed check.
        """
        if self.high_creativity_master is None:
            return ["the template has no High Creativity master"]
        if self._high_creativity_title_candidate() is None:
            return ["no layout with a free canvas and a title box"]
        return []

    def _usable_high_creativity(self) -> list[LayoutInfo]:
        """Its layouts that can be composed on: a free region, and a title or title box."""
        return [l for l in self.high_creativity_layouts
                if l.canvas_capable and l.free_region is not None
                and (l.title_rect is not None or l.has_role("title"))]

    def _high_creativity_title_candidate(self) -> Optional[LayoutInfo]:
        """The title layout: the one the author designed a title on, else the first."""
        usable = self._usable_high_creativity()
        return next((l for l in usable if l.prompt_shapes or l.has_role("title")),
                    usable[0] if usable else None)

    def high_creativity_title(self) -> Optional[LayoutInfo]:
        """The master's title layout, or None when the master is unusable."""
        if self.high_creativity_problems():
            return None
        return self._high_creativity_title_candidate()

    def high_creativity_blank(self) -> Optional[LayoutInfo]:
        """The master's other full-canvas layout (no sample title), or None."""
        title = self.high_creativity_title()
        if title is None:
            return None
        return next((l for l in self._usable_high_creativity()
                     if l is not title and not l.placeholders
                     and l.title_rect is not None), None)

    def resolve_layout(self, ref: Union[str, int, None]) -> LayoutInfo:
        """Resolve a key ('M1-L36'), exact / normalised name, or close match.

        Args:
            ref: Layout key, layout name, or something close enough to one.

        Returns:
            The matching layout, preferring the canonical copy of a duplicated name.

        Raises:
            LookupError: When nothing matches closely enough. The message names the
                nearest candidates, because the caller is usually a model that can
                retry with one of them.
        """
        if ref is None or ref == "":
            raise LookupError("layout reference is empty")
        if isinstance(ref, int):
            raise LookupError(
                f"layout must be a key like 'M1-L{ref}' or a layout name, not a bare number")
        text = str(ref).strip()
        found = self.get(text)
        if found:
            return found
        norm = _norm_name(text)
        exact = [l for l in self.layouts if _norm_name(l.name) == norm]
        if exact:
            return next((l for l in exact if l.canonical), exact[0])
        # A max-mode slide sits on the High Creativity master; its file reports that
        # layout's name. Exact names only: fuzzy matching never offers these layouts.
        exact = [l for l in self.high_creativity_layouts if _norm_name(l.name) == norm]
        if exact:
            return exact[0]
        names = {_norm_name(l.name): l for l in self.canonical_layouts()}
        close = difflib.get_close_matches(norm, list(names.keys()), n=3, cutoff=0.75)
        if len(close) == 1 or (close and difflib.SequenceMatcher(None, norm, close[0]).ratio() > 0.9):
            return names[close[0]]
        suggestions = ([names[c].compact() for c in close]
                       or [l.compact() for l in self.search(text, limit=3)])
        raise LookupError(f"unknown layout '{ref}'. Did you mean: " + " || ".join(suggestions))

    # ---- search / recommendation ----------------------------------------------------
    def search(self, query: str = "", tags: str = "", min_text_slots: int = 0,
               needs_picture: bool = False, limit: int = 20,
               include_duplicates: bool = False) -> list[LayoutInfo]:
        """Layouts matching free text and/or tags, best first."""
        tokens = [t for t in re.split(r"[^a-z0-9]+", (query or "").lower()) if t]
        wanted_tags = [t.strip().lower() for t in (tags or "").split(",") if t.strip()]
        scored: list[tuple[float, LayoutInfo]] = []
        for layout in self.layouts:
            if not include_duplicates and not layout.canonical:
                continue
            text_slots = (len(layout.by_role("body")) + len(layout.by_role("content"))
                          + len(layout.by_role("heading")))
            if text_slots < min_text_slots:
                continue
            if needs_picture and not layout.has_role("picture"):
                continue
            if wanted_tags and not all(t in layout.tags or t in layout.name.lower()
                                       for t in wanted_tags):
                continue
            hay = f"{layout.name} {' '.join(layout.tags)} {layout.recommended_use}".lower()
            score = sum(2.0 if tok in layout.name.lower() else 1.0 for tok in tokens if tok in hay)
            if tokens and score == 0:
                continue
            score += 0.5 if layout.master_index == self.preferred_master else 0
            score += min(layout.usage_count, 4) * 0.1
            scored.append((score, layout))
        scored.sort(key=lambda s: (-s[0], s[1].master_index, s[1].layout_index))
        return [l for _, l in scored[:limit]]

    def recommend(self, intent: str, text_blocks: int = 0, needs_picture: bool = False,
                  has_table: bool = False, has_chart: bool = False,
                  limit: int = 5) -> list[tuple[LayoutInfo, float, list[str]]]:
        """Ranked (layout, score, reasons) for an intent like 'cover' or 'comparison'."""
        intent_key = (intent or "content").strip().lower().replace(" ", "_")
        wanted, avoid = INTENT_TAGS.get(intent_key, (None, None))
        tokens = [t for t in re.split(r"[^a-z0-9]+", intent_key) if t]
        if wanted is None:
            wanted, avoid = INTENT_TAGS["content"]
        results: list[tuple[LayoutInfo, float, list[str]]] = []
        for layout in self.canonical_layouts():
            score, reasons = 0.0, []
            for i, tag in enumerate(wanted):
                if tag in layout.tags:
                    score += 4.0 if i == 0 else 2.0
                    reasons.append(f"tag '{tag}'")
            for tag in avoid:
                if tag in layout.tags:
                    score -= 5.0
            for tok in tokens:
                if tok in layout.name.lower() and tok not in ("content", "slide"):
                    score += 1.5
                    reasons.append(f"name matches '{tok}'")
            bodies = len(layout.by_role("body")) + len(layout.by_role("content"))
            if text_blocks:
                if bodies == text_blocks:
                    score += 3.0
                    reasons.append(f"exactly {text_blocks} text blocks")
                elif bodies > text_blocks:
                    score += 1.0 - min(bodies - text_blocks, 4) * 0.3
                    reasons.append(f"{bodies} text blocks available")
                else:
                    score -= 2.0 * (text_blocks - bodies)
            pictures = layout.by_role("picture")
            if needs_picture:
                if pictures:
                    score += 2.5
                    reasons.append(f"{len(pictures)} picture slot(s)")
                else:
                    score -= 1.5
            elif pictures and not any(p.accepts_text for p in pictures) and "cover" not in layout.tags:
                score -= 0.8 * min(len(pictures), 3)
            if has_table or has_chart:
                big = [p for p in layout.placeholders
                       if p.role in ("content", "body") and p.width >= 4 and p.height >= 2.5]
                if big or "title-only" in layout.tags:
                    score += 2.5
                    reasons.append("room for a table/chart")
                else:
                    score -= 2.0
            if layout.master_index == self.preferred_master:
                score += 1.5
                reasons.append("preferred content master")
            elif layout.master_index == self.cover_master and "cover" in layout.tags:
                score += 1.5
            score += min(layout.usage_count, 5) * 0.3
            if layout.usage_count:
                reasons.append(f"used by {layout.usage_count} sample slide(s)")
            results.append((layout, round(score, 2), reasons))
        results.sort(key=lambda r: (-r[1], r[0].master_index, r[0].layout_index))
        return results[:limit]

    # ---- summaries -----------------------------------------------------------------
    def summary_text(self, max_per_tag: int = 60) -> str:
        """The whole template as one prompt-sized block, grouped by layout family."""
        theme = self.theme
        colors = ", ".join(f"{k} {v}" for k, v in theme.colors.items()
                           if k.startswith("accent") or k in ("dk1", "lt1"))
        lines = [
            f"Template: {Path(self.template_path).name} | slide "
            f"{self.slide_width} x {self.slide_height} in",
            f"Theme fonts: headings '{theme.major_font}', body '{theme.minor_font}' | "
            f"theme colors: {colors}",
            f"Sample slides in template: {self.sample_slide_count} "
            "(they are removed; only layouts are reused)",
            "Masters:",
        ]
        for i, th in enumerate(self.themes):
            count = sum(1 for l in self.layouts if l.master_index == i)
            used = sum(l.usage_count for l in self.layouts if l.master_index == i)
            note = " <- preferred for content slides" if i == self.preferred_master else ""
            note += " <- cover layouts" if i == self.cover_master and i != self.preferred_master else ""
            lines.append(f"  M{i} '{th.name}': {count} layouts, used by {used} sample slides{note}")
        canon = self.canonical_layouts()
        lines.append(f"Unique layouts: {len(canon)} (keys below; identical layouts in "
                     "other masters are hidden)")
        families = ["cover", "agenda", "section", "quote", "closing", "title-only", "columns",
                    "process", "dashboard", "photo", "chart", "content", "blank"]
        seen = set()
        for fam in families:
            members = [l for l in canon if fam in l.tags and l.key not in seen]
            if not members:
                continue
            lines.append(f"[{fam}]")
            for l in members[:max_per_tag]:
                seen.add(l.key)
                lines.append(f"  {l.compact()}")
        rest = [l for l in canon if l.key not in seen]
        if rest:
            lines.append("[other]")
            lines.extend(f"  {l.compact()}" for l in rest)
        lines.append("Use get_layout_details(key) for placeholder idx / slot names, geometry "
                     "and capacity before using a layout.")
        return "\n".join(lines)

    def stats(self) -> dict[str, Any]:
        return {
            "masters": len(self.themes), "layouts": len(self.layouts),
            "unique_layouts": len(self.canonical_layouts()),
            "sample_slides": self.sample_slide_count,
            "slide_size_in": [self.slide_width, self.slide_height],
            "fonts": [self.theme.major_font, self.theme.minor_font],
            "canvas_capable": sum(1 for l in self.layouts if l.canvas_capable),
        }

    def to_dict(self, include_high_creativity: bool = False) -> dict:
        """JSON-clean snapshot, for the sealed sandbox.

        `deck_builder.py` cannot import this module (it must stay free of `app.*`
        imports so it can be copied into the sandbox), so the catalog crosses that
        boundary as plain JSON rather than as objects.

        Args:
            include_high_creativity: Also serialize the High Creativity master's layouts.
                Only a slide built on that master needs them; every other slide's
                sandbox gets exactly the catalog it always did.

        Returns:
            A dict of builtins only — safe to hand straight to `json.dumps`.
        """
        return {
            "template_path": str(self.template_path),
            "slide_width": self.slide_width,
            "slide_height": self.slide_height,
            "preferred_master": self.preferred_master,
            "cover_master": self.cover_master,
            "sample_slide_count": self.sample_slide_count,
            "themes": [asdict(theme) for theme in self.themes],
            "layouts": [{
                "key": l.key, "name": l.name,
                "master_index": l.master_index, "layout_index": l.layout_index,
                "tags": list(l.tags), "columns": l.columns,
                "background": l.background,
                "background_luminance": l.background_luminance,
                "canvas_capable": l.canvas_capable,
                "free_region": l.free_region.to_list() if l.free_region else None,
                "decor_rects": [r.to_list() for r in l.decor_rects],
                "recommended_use": l.recommended_use,
                "placeholders": [asdict(p) for p in l.placeholders],
                # Only High Creativity layouts carry these keys, so the serialized
                # catalog of every other layout is unchanged.
                **({"high_creativity": True,
                    "title_rect": l.title_rect.to_list() if l.title_rect else None,
                    "title_size": l.title_font_size,
                    "prompt_shapes": list(l.prompt_shapes)}
                   if l.high_creativity else {}),
            } for l in (*self.layouts, *(self.high_creativity_layouts
                                         if include_high_creativity else ()))],
        }

    # --- compatibility shims: plan_tools and layout_tools call these unchanged ----- #
    @property
    def content_layouts(self) -> list[LayoutInfo]:
        """Canonical content layouts an agent may be offered.

        Narrower than the `content` tag alone: `_derive_tags` adds `content` to anything
        not tagged cover/agenda/section/quote/closing/blank, which sweeps in layouts with
        no title and layouts with no text slot at all. A layout with no title, or none of
        the text slots a content slide needs, is not a content layout however it is
        tagged. `plan_tools` builds its menu from this and `by_role` falls back to its
        first entry, so a full-bleed photo cover reaching this list is a live defect.
        """
        return [l for l in self.canonical_layouts()
                if "content" in l.tags and l.has_role("title")
                and (l.has_role("body") or l.has_role("content") or l.has_role("heading"))]

    def prompt_block(self) -> str:
        return self.summary_text()

    # --- compatibility with the content agents ------------------------------------ #
    # `writer.py` still calls resolve() and best_with_picture() when it repairs a
    # slide's layout hint, and plan_tools / layout_tools read content_layouts and
    # prompt_block. They are the content side of the pipeline and were never part of
    # the deterministic renderer, so they stay.
    def resolve(self, name: Union[str, None]) -> LayoutInfo:
        """Never raises: `writer.repair_slide` has nobody to ask again."""
        try:
            return self.resolve_layout(name)
        except LookupError:
            return self.default_content()

    def by_role(self, preferences) -> LayoutInfo:
        for wanted in preferences or ():
            try:
                return self.resolve_layout(wanted)
            except LookupError:
                continue
        usable = self.content_layouts or self.canonical_layouts() or self.layouts
        return usable[0]

    def default_content(self) -> LayoutInfo:
        return self.by_role(("Title + Bullets", "Two Line Title + Bullets", "Title only"))

    def best_with_picture(self, text_slots_needed: int = 1) -> Optional[LayoutInfo]:
        """Closest content layout that actually has an image slot, biggest picture wins ties."""
        candidates = [l for l in self.content_layouts
                      if l.has_role("picture") and (l.by_role("body") or l.by_role("content"))]
        if not candidates:
            return None

        def score(layout: LayoutInfo):
            biggest = max(p.width * p.height for p in layout.by_role("picture"))
            bodies = len(layout.by_role("body")) + len(layout.by_role("content"))
            return abs(bodies - text_slots_needed), -biggest

        return sorted(candidates, key=score)[0]

    # ---- construction ---------------------------------------------------------------
    @classmethod
    def from_presentation(cls, prs, template_path: Union[str, Path]) -> "TemplateCatalog":
        slide_w, slide_h = _inches(prs.slide_width), _inches(prs.slide_height)
        masters = list(prs.slide_masters)
        themes = [_read_theme(m) for m in masters]
        txstyles = [_read_master_text_styles(m) for m in masters]

        # Sample-slide usage tells us how the template author intended the layouts to be used.
        usage: dict[tuple[int, int], list[str]] = {}
        for slide in prs.slides:
            loc = _locate_layout(masters, slide.slide_layout)
            if loc is None:
                continue
            title = ""
            try:
                if slide.shapes.title is not None and slide.shapes.title.has_text_frame:
                    title = slide.shapes.title.text_frame.text.strip().replace("\n", " ")
            except Exception:                                            # noqa: BLE001
                title = ""
            if not title:
                for shape in slide.shapes:
                    if shape.has_text_frame and shape.text_frame.text.strip():
                        title = shape.text_frame.text.strip().split("\n")[0]
                        break
            usage.setdefault(loc, []).append(title[:70])

        # The High Creativity master is set aside before anything is ranked or grouped,
        # so it can never become the preferred or cover master, a canonical copy of a
        # name, or a candidate any LOW/MED selection sees.
        hc_master = _high_creativity_master(themes)
        # A prepared reference deck has had its slides stripped, so its usage comes from
        # the counts its profile took before stripping (none for the brand template).
        reference = _reference_usage(template_path) if not len(prs.slides) else {}
        reference_usage = reference.get("layout_usage", {})
        layouts: list[LayoutInfo] = []
        hc_layouts: list[LayoutInfo] = []
        for mi, master in enumerate(masters):
            for li, layout in enumerate(master.slide_layouts):
                titles = usage.get((mi, li), [])
                info = _read_layout(layout, mi, li, themes[mi], txstyles[mi],
                                    slide_w, slide_h, titles)
                if reference_usage:
                    info.usage_count = reference_usage.get(info.key, 0)
                (hc_layouts if mi == hc_master else layouts).append(info)
        if hc_master is not None:
            _prepare_high_creativity(hc_layouts, masters[hc_master], slide_w, slide_h)

        # Canonical selection: identical names across masters -> prefer the most used master.
        master_usage = {mi: sum(len(v) for (m, _), v in usage.items() if m == mi)
                        for mi in range(len(masters))}
        content_usage = cover_usage = master_usage
        if reference_usage:
            # The reference's own slides decide: its content master is the one its
            # content slides use, its cover master the one its cover uses, so a master
            # used only for dividers (or an unused default Office master) is not picked.
            master_usage = _per_master(reference_usage, len(masters))
            content_usage = _per_master(reference.get("content_usage") or reference_usage,
                                        len(masters))
            cover_usage = _per_master(reference.get("cover_usage") or reference_usage,
                                      len(masters))
            log.info("reference usage from its profile: per master %s, content %s, "
                     "cover %s", master_usage, content_usage, cover_usage)
        content_masters = [mi for mi in range(len(masters))
                           if any("content" in l.tags for l in layouts if l.master_index == mi)]
        preferred = max(content_masters or [0], key=lambda mi: (content_usage.get(mi, 0),
                                                                master_usage.get(mi, 0), -mi))
        cover_masters = [mi for mi in range(len(masters))
                         if any("cover" in l.tags for l in layouts if l.master_index == mi)]
        cover_master = max(cover_masters or [preferred],
                           key=lambda mi: (cover_usage.get(mi, 0), master_usage.get(mi, 0), -mi))
        groups: dict[str, list[LayoutInfo]] = {}
        for l in layouts:
            groups.setdefault(_norm_name(l.name), []).append(l)
        for members in groups.values():
            members.sort(key=lambda l: (-(l.master_index == preferred),
                                        -master_usage.get(l.master_index, 0), l.master_index))
            for i, l in enumerate(members):
                l.canonical = i == 0
                l.duplicates = [m.key for m in members if m is not l]
        catalog = cls(str(template_path), slide_w, slide_h, themes, layouts,
                      preferred, cover_master, len(prs.slides),
                      high_creativity_master=hc_master,
                      high_creativity_layouts=hc_layouts)
        if hc_master is not None:
            problems = catalog.high_creativity_problems()
            if problems:
                log.error("High Creativity master M%d is UNUSABLE, so max mode keeps "
                          "slides on their contract layouts: %s", hc_master,
                          "; ".join(problems))
            else:
                theme = themes[hc_master]
                log.info("High Creativity master M%d ready: %s; its own scheme (accent1 "
                         "%s) and fonts (%s/%s) are replaced by the brand palette and "
                         "Barlow in generated files", hc_master,
                         ", ".join(f"{l.key} {l.name!r}" for l in hc_layouts),
                         theme.colors.get("accent1"), theme.major_font, theme.minor_font)
        return catalog


# --------------------------------------------------------------------------------- #
# reading the package                                                                 #
# --------------------------------------------------------------------------------- #

def _norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _locate_layout(masters, layout) -> Optional[tuple[int, int]]:
    for mi, master in enumerate(masters):
        for li, candidate in enumerate(master.slide_layouts):
            if candidate.part is layout.part:
                return mi, li
    return None


def _read_theme(master) -> ThemeInfo:
    name, major, minor, colors = "", "Barlow", "Barlow", {}
    try:
        theme_part = next((rel.target_part for rel in master.part.rels.values()
                           if rel.reltype == RT.THEME), None)
        if theme_part is not None:
            root = etree.fromstring(theme_part.blob)
            name = root.get("name", "")
            maj = root.find(".//" + qn("a:majorFont") + "/" + qn("a:latin"))
            mnr = root.find(".//" + qn("a:minorFont") + "/" + qn("a:latin"))
            major = maj.get("typeface") if maj is not None and maj.get("typeface") else major
            minor = mnr.get("typeface") if mnr is not None and mnr.get("typeface") else minor
            scheme = root.find(".//" + qn("a:clrScheme"))
            if scheme is not None:
                for child in scheme:
                    tag = etree.QName(child).localname
                    if len(child):
                        val = child[0].get("val") or child[0].get("lastClr")
                        if val:
                            colors[tag] = val.upper()
    except Exception as exc:                                             # noqa: BLE001
        # Not fatal — the Barlow/empty-palette default still builds a catalog — but it
        # empties the whole brand palette, so it must never fail silently.
        log.warning("theme unreadable on master; falling back to defaults: %s", exc)
    return ThemeInfo(name, major, minor, colors)


def _read_master_text_styles(master) -> dict[str, Optional[float]]:
    """Default level-1 font sizes a layout's placeholders inherit from their master."""
    sizes: dict[str, Optional[float]] = {"title": None, "body": None, "other": None}
    try:
        tx = master.element.find(qn("p:txStyles"))
        if tx is not None:
            for style_tag, key in (("p:titleStyle", "title"), ("p:bodyStyle", "body"),
                                   ("p:otherStyle", "other")):
                style = tx.find(qn(style_tag))
                if style is None:
                    continue
                lvl1 = style.find(qn("a:lvl1pPr"))
                rpr = lvl1.find(qn("a:defRPr")) if lvl1 is not None else None
                if rpr is not None and rpr.get("sz"):
                    sizes[key] = int(rpr.get("sz")) / 100
    except Exception as exc:                                             # noqa: BLE001
        # Every placeholder that inherits its size then falls back to 18pt, which moves
        # every capacity hint on the master. Too consequential to swallow.
        log.warning("master text styles unreadable; capacity hints will use 18pt: %s", exc)
    return sizes


def _scheme_hex(val: str, theme: ThemeInfo) -> Optional[str]:
    return theme.colors.get(_SCHEME_ALIASES.get(val, val))


def _color_element_hex(clr, theme: ThemeInfo) -> Optional[str]:
    """Hex of an <a:srgbClr>/<a:schemeClr> element, applying lumMod/lumOff approximately."""
    local = etree.QName(clr).localname
    if local == "srgbClr":
        base = clr.get("val")
    elif local == "schemeClr":
        base = _scheme_hex(clr.get("val", ""), theme)
    else:
        return None
    if not base or len(base) != 6:
        return None
    lum_mod = clr.find(qn("a:lumMod"))
    lum_off = clr.find(qn("a:lumOff"))
    if lum_mod is None and lum_off is None:
        return base.upper()
    factor = int(lum_mod.get("val", "100000")) / 100000 if lum_mod is not None else 1.0
    offset = int(lum_off.get("val", "0")) / 100000 if lum_off is not None else 0.0
    channels = []
    for i in (0, 2, 4):
        value = int(base[i:i + 2], 16) / 255
        value = min(1.0, max(0.0, value * factor + offset))
        channels.append(int(round(value * 255)))
    return "".join(f"{c:02X}" for c in channels)


def _fill_luminance(shape, theme: ThemeInfo) -> Optional[float]:
    """Luminance (0 dark .. 1 light) of a shape's solid or gradient fill, None when unknown."""
    sp_pr = shape._element.find(qn("p:spPr"))
    if sp_pr is None:
        return None
    solid = sp_pr.find(qn("a:solidFill"))
    if solid is not None and len(solid):
        hex_value = _color_element_hex(solid[0], theme)
        return _luminance(hex_value) if hex_value else None
    grad = sp_pr.find(qn("a:gradFill"))
    if grad is not None:
        values = []
        for stop in grad.iter(qn("a:gs")):
            if len(stop):
                hex_value = _color_element_hex(stop[0], theme)
                if hex_value:
                    values.append(_luminance(hex_value))
        return sum(values) / len(values) if values else None
    return None


def shape_luminance(shape, theme: ThemeInfo) -> Optional[float]:
    """How light one master/layout shape looks: a picture's mean luminance, else its
    solid or gradient fill's. 0 dark .. 1 light, None when it cannot be told (an
    empty picture placeholder, no fill).

    Args:
        shape: A python-pptx shape.
        theme: The theme of the shape's master, for theme colours.
    """
    if getattr(shape, "is_placeholder", False):
        return None
    try:
        if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
            return _image_luminance(shape.image.blob)
    except Exception as exc:                 # noqa: BLE001 — a linked picture, say
        log.debug("picture %r cannot be measured: %s", shape.name, exc)
        return None
    return _fill_luminance(shape, theme)


@lru_cache(maxsize=64)
def _image_luminance(blob: bytes) -> Optional[float]:
    """Mean perceived luminance of an embedded background image, 0 dark .. 1 light.

    Downscaled hard before averaging: this runs for every layout that carries a picture
    background, and the exact value never matters — only which side of 0.5 it falls on.

    Args:
        blob: The raw bytes of the image part.

    Returns:
        The mean luminance, or None when the image cannot be decoded.
    """
    try:
        with Image.open(io.BytesIO(blob)) as image:
            thumb = image.convert("L").resize((32, 32))
            return sum(thumb.getdata()) / (255.0 * 32 * 32)
    except Exception as exc:                 # noqa: BLE001 — an unreadable image is not fatal
        log.warning("background image unreadable: %s", exc)
        return None


def _background_from_bg(element, theme: ThemeInfo, part) -> Optional[float]:
    """Luminance of a `p:cSld/p:bg` background fill, or None when there is none to read.

    This is where this template's real backgrounds live: master 2's near-white photo
    wash and the handful of layouts that override it are `<p:bg>` fills, not shapes, so
    a shape-only scan reports every one of them as plain white.

    Classification is left to the caller and must be made from the returned number, not
    from which fill type produced it — master 2's picture background measures ~0.97,
    and calling every picture background "dark" (or every one "photo") would be wrong
    for 47 layouts.

    Args:
        element: The `<p:sldLayout>` or `<p:sldMaster>` element to read.
        theme: Theme of the owning master, for resolving scheme colours.
        part: The part that owns `element`, for resolving an image relationship.

    Returns:
        Mean luminance 0.0 .. 1.0, or None when no readable background fill is present.
    """
    bg = element.find(qn("p:cSld") + "/" + qn("p:bg"))
    if bg is None:
        return None
    # <p:bgRef> points at the theme's fill style list rather than carrying a fill; it is
    # rare in this template and not worth walking the format scheme for.
    bg_pr = bg.find(qn("p:bgPr"))
    if bg_pr is None:
        return None
    solid = bg_pr.find(qn("a:solidFill"))
    if solid is not None and len(solid):
        hex_value = _color_element_hex(solid[0], theme)
        return _luminance(hex_value) if hex_value else None
    grad = bg_pr.find(qn("a:gradFill"))
    if grad is not None:
        values = []
        for stop in grad.iter(qn("a:gs")):
            if len(stop):
                hex_value = _color_element_hex(stop[0], theme)
                if hex_value:
                    values.append(_luminance(hex_value))
        return sum(values) / len(values) if values else None
    blip_fill = bg_pr.find(qn("a:blipFill"))
    if blip_fill is not None:
        blip = blip_fill.find(qn("a:blip"))
        rid = blip.get(qn("r:embed")) if blip is not None else None
        if rid:
            try:
                return _image_luminance(part.rels[rid].target_part.blob)
            except (KeyError, AttributeError) as exc:
                log.warning("background image relationship %s unresolvable: %s", rid, exc)
    return None


def _read_placeholder_format(sp, theme: ThemeInfo) -> dict[str, Any]:
    """Formatting hints declared on a layout placeholder (list style level 1 + body properties)."""
    info: dict[str, Any] = {}
    tx_body = sp.find(qn("p:txBody"))
    if tx_body is None:
        return info
    body_pr = tx_body.find(qn("a:bodyPr"))
    if body_pr is not None:
        if body_pr.find(qn("a:spAutoFit")) is not None:
            info["autofit"] = "shape"
        elif body_pr.find(qn("a:normAutofit")) is not None:
            info["autofit"] = "text"
        elif body_pr.find(qn("a:noAutofit")) is not None:
            info["autofit"] = "none"
    lst = tx_body.find(qn("a:lstStyle"))
    lvl1 = lst.find(qn("a:lvl1pPr")) if lst is not None else None
    if lvl1 is None:
        return info
    if lvl1.get("algn"):
        info["align"] = {"l": "left", "ctr": "center", "r": "right",
                         "just": "justify"}.get(lvl1.get("algn"), lvl1.get("algn"))
    if lvl1.find(qn("a:buNone")) is not None:
        info["bullets"] = False
    elif lvl1.find(qn("a:buChar")) is not None or lvl1.find(qn("a:buAutoNum")) is not None:
        info["bullets"] = True
    rpr = lvl1.find(qn("a:defRPr"))
    if rpr is not None:
        if rpr.get("sz"):
            info["font_size"] = int(rpr.get("sz")) / 100
        if rpr.get("b") is not None:
            info["bold"] = rpr.get("b") == "1"
        if rpr.get("cap") == "all":
            info["all_caps"] = True
        latin = rpr.find(qn("a:latin"))
        if latin is not None and latin.get("typeface"):
            info["font_name"] = latin.get("typeface")
        solid = rpr.find(qn("a:solidFill"))
        if solid is not None and len(solid):
            clr = solid[0]
            if etree.QName(clr).localname == "srgbClr":
                info["color"] = clr.get("val")
            elif etree.QName(clr).localname == "schemeClr":
                info["color"] = _scheme_hex(clr.get("val", ""), theme) or clr.get("val")
    return info


def _placeholder_role(ph_type: str, idx: int, prompt: str, width: float,
                      height: float) -> tuple[str, bool]:
    """Infer the semantic role of a placeholder from its type, prompt text and geometry."""
    p = (prompt or "").strip().lower()
    if ph_type in ("TITLE", "CENTER_TITLE"):
        return "title", True
    if ph_type == "SUBTITLE":
        return "subtitle", True
    if ph_type == "PICTURE":
        return "picture", "text" in p
    if ph_type == "CHART":
        return "chart", False
    if ph_type == "TABLE":
        return "table", False
    if p.isdigit():
        return "number", True
    if ph_type == "OBJECT":
        return "content", True
    if ph_type == "BODY":
        if "subhead" in p or "subtitle" in p or (idx == 10 and height <= 0.6 and width >= 6):
            return "subtitle", True
        if "name" in p or "date" in p:
            return "caption", True
        if "second level" in p or height > 0.75:
            return "body", True
        return "heading", True
    return ph_type.lower(), True


def covers(rect: Rect, region: Rect) -> float:
    """How much of `region` lies inside `rect`, as a fraction of `region`'s area.

    Args:
        rect: The covering rectangle.
        region: The rectangle being covered.

    Returns:
        0.0 when they do not overlap, 1.0 when `region` is entirely inside `rect`.
    """
    area = region.width * region.height
    if area <= 0:
        return 0.0
    wide = min(rect.right, region.right) - max(rect.left, region.left)
    tall = min(rect.bottom, region.bottom) - max(rect.top, region.top)
    if wide <= 0 or tall <= 0:
        return 0.0
    return (wide * tall) / area


def _tallest_clear_band(region: Rect, obstacles: list[Rect]) -> Optional[Rect]:
    """The tallest full-width slice of `region` no obstacle reaches into.

    Composition happens across the full width of the region, so an obstacle blocks the
    whole horizontal band it sits in even where it is narrow. That is conservative on
    purpose: it is what makes "the free region never intersects a reserved zone" true
    by construction rather than by hope.

    Args:
        region: The candidate free region.
        obstacles: Decor rectangles the gate will reserve.

    Returns:
        The tallest clear band, or None when the obstacles leave nothing.
    """
    blocked: list[tuple[float, float]] = []
    for obstacle in obstacles:
        if obstacle.right <= region.left or obstacle.left >= region.right:
            continue                       # entirely beside the region
        top = max(obstacle.top, region.top)
        bottom = min(obstacle.bottom, region.bottom)
        if bottom > top:
            blocked.append((top, bottom))

    best_top, best_height, cursor = region.top, 0.0, region.top
    for top, bottom in sorted(blocked):
        if top - cursor > best_height:
            best_top, best_height = cursor, top - cursor
        cursor = max(cursor, bottom)
    if region.bottom - cursor > best_height:
        best_top, best_height = cursor, region.bottom - cursor
    if best_height <= 0:
        return None
    return Rect(region.left, best_top, region.width, best_height)


def _canvas_region(placeholders: list[PlaceholderInfo], decor_rects: list[Rect],
                   slide_w: float, slide_h: float) -> tuple[bool, Optional[Rect]]:
    """Whether this layout can be composed on freely, and the region that is free.

    A layout qualifies when its only content placeholders are the title and an optional
    subhead — anything else means the template already designed the content area, and
    composing over it would fight the design. The free region starts just under the
    title and stops above the footer band, so `canvas` mode still inherits the master's
    theme, background and decorative graphics (Decision 4).

    The region is then reconciled with the layout's decor, which is what this function
    used to ignore entirely. `deck_builder.reserved_rects` reserves those shapes, so a
    region advertised over them is a region the gate forbids: on six of this template's
    fourteen canvas layouts the declared region was 96-100% covered by a reserved
    zone, every shape drawn there was a blocking violation, and the only slide that
    could pass the gate was an empty one. Two rules reconcile them, and
    `reserved_rects` applies the same two in reverse:

    * A decor shape covering essentially the whole region is the *panel* the region
      sits on — a card, a hero band, a tinted content area. You compose inside it, so
      the region is clipped to it and the panel is not reserved.
    * Anything else is a genuine obstacle, and the region shrinks to the tallest band
      that clears every one of them. A layout left with too little room is simply not
      canvas-capable, which is the honest answer for a layout whose content area the
      template already designed.

    Args:
        placeholders: The layout's placeholders, in reading order.
        decor_rects: The layout's decorative shapes.
        slide_w: Slide width in inches.
        slide_h: Slide height in inches.

    Returns:
        (canvas_capable, free_region); the rectangle is None whenever the flag is
        False. When it is not None it is guaranteed disjoint from every rectangle
        `reserved_rects` will reserve for this layout.
    """
    roles = {p.role for p in placeholders}
    title = next((p for p in placeholders if p.role == "title"), None)
    if title is None or not roles <= {"title", "subtitle"}:
        return False, None
    occupied = [p for p in placeholders if p.role in ("title", "subtitle")]
    # Master 2 declares no placeholders, so a layout that omits a:off/a:ext inherits
    # nothing and python-pptx reports None for every dimension. A box at the origin with
    # no size is unknown geometry, not a real box: measuring against it would hand back a
    # "free" region that starts at the top of the slide and swallows the title band.
    if any(p.width <= 0 or p.height <= 0 for p in occupied):
        return False, None
    # Clear of the subhead as well as the title — the layout still renders it, so a
    # region that merely clears the title would compose straight over it.
    top = max(p.top + p.height for p in occupied) + GeometryConstants.CANVAS_TITLE_GAP_IN
    bottom = slide_h - GeometryConstants.FOOTER_BAND_HEIGHT_IN
    margin = GeometryConstants.FRAME_SIDE_MARGIN_IN
    region = Rect(margin, top, slide_w - 2 * margin, bottom - top)
    if region.height < GeometryConstants.MIN_CANVAS_FREE_HEIGHT_IN:
        return False, None

    usable = [r for r in (decor_rects or ()) if r.width > 0 and r.height > 0]
    panels = [r for r in usable
              if covers(r, region) >= GeometryConstants.CANVAS_PANEL_COVERAGE]
    for panel in panels:
        # Compose inside the panel rather than over its edges. Clipping to the
        # smallest one keeps the region inside every panel that claims it.
        region = Rect(max(region.left, panel.left), max(region.top, panel.top),
                      min(region.right, panel.right) - max(region.left, panel.left),
                      min(region.bottom, panel.bottom) - max(region.top, panel.top))
        if region.width <= 0 or region.height <= 0:
            return False, None

    obstacles = [r for r in usable if r not in panels]
    clear = _tallest_clear_band(region, obstacles)
    if clear is None or clear.height < GeometryConstants.MIN_CANVAS_FREE_HEIGHT_IN:
        return False, None
    return True, Rect(round(clear.left, 2), round(clear.top, 2),
                      round(clear.width, 2), round(clear.height, 2))


def _high_creativity_master(themes: list[ThemeInfo]) -> Optional[int]:
    """The index of the master whose theme name marks it as the High Creativity one."""
    from app.config.constants import HighCreativityConstants as HC

    for index, theme in enumerate(themes):
        if HC.MASTER_THEME_FRAGMENT in (theme.name or "").lower():
            log.info("master M%d (%r) is the High Creativity master; it is kept out of "
                     "every LOW/MED layout choice", index, theme.name)
            return index
    log.debug("the template has no High Creativity master")
    return None


def _master_decor(master) -> tuple[list[Rect], list[str]]:
    """The master's own non-placeholder shapes (logo, footer, page number) and texts."""
    rects, texts = [], []
    for shape in master.shapes:
        if shape.is_placeholder or shape.width is None:
            continue
        rects.append(Rect(_inches(shape.left), _inches(shape.top),
                          _inches(shape.width), _inches(shape.height)))
        if shape.has_text_frame and shape.text_frame.text.strip():
            texts.append(shape.text_frame.text.strip().replace("\n", " ")[:60])
    return rects, texts


def _blank_canvas_region(decor_rects: list[Rect], slide_w: float,
                         slide_h: float) -> Optional[Rect]:
    """The free area of a placeholder-less High Creativity layout.

    It starts below the logo band (everything drawn in the top 1.5in) and stops above
    the footer band, inside the side margins, clear of any other decor.

    Args:
        decor_rects: The layout's decor, master furniture included.
        slide_w: Slide width in inches.
        slide_h: Slide height in inches.

    Returns:
        The region, or None when too little room is left.
    """
    usable = [r for r in decor_rects if r.width > 0 or r.height > 0]
    band = [r for r in usable if r.top < 1.5]
    top = (max((r.top + r.height for r in band), default=0.0)
           + _hc_constants().BLANK_TOP_GAP_IN)
    bottom = slide_h - GeometryConstants.FOOTER_BAND_HEIGHT_IN
    margin = GeometryConstants.FRAME_SIDE_MARGIN_IN
    region = Rect(margin, top, slide_w - 2 * margin, bottom - top)
    obstacles = [r for r in usable if r not in band and r.top < bottom]
    clear = _tallest_clear_band(region, obstacles)
    if clear is None or clear.height < GeometryConstants.MIN_CANVAS_FREE_HEIGHT_IN:
        return None
    return Rect(round(clear.left, 2), round(clear.top, 2), round(clear.width, 2),
                round(clear.height, 2))


def _hc_constants():
    """`HighCreativityConstants`, imported lazily like the rest of this module's uses."""
    from app.config.constants import HighCreativityConstants
    return HighCreativityConstants


def _sample_title_slot(layout) -> Optional[tuple[str, Rect, float, str]]:
    """The layout's baked sample-title text box ("Click to edit title"), if any.

    Args:
        layout: A python-pptx slide layout.

    Returns:
        (shape name, its rect, its size snapped to the type scale, its text), or None.
    """
    from app.config.constants import HighCreativityConstants as HC

    for shape in layout.shapes:
        if shape.is_placeholder or not shape.has_text_frame or shape.width is None:
            continue
        text = shape.text_frame.text.strip().replace("\n", " ")[:60]
        if not any(p in text.lower() for p in HC.PROMPT_TEXT_PATTERNS):
            continue
        size = next((run.font.size.pt for para in shape.text_frame.paragraphs
                     for run in para.runs if run.font.size is not None), 28.0)
        snapped = min(GeometryConstants.TYPE_SCALE, key=lambda s: (abs(s - size), s))
        return (shape.name, Rect(_inches(shape.left), _inches(shape.top),
                                 _inches(shape.width), _inches(shape.height)),
                float(snapped), text)
    return None


def _prepare_high_creativity(layouts: list[LayoutInfo], master, slide_w: float,
                             slide_h: float) -> None:
    """Make the High Creativity layouts composable, in place.

    * The master's own furniture is folded into every layout's decor, so the frame never
      stamps a second logo over it and the gate keeps added shapes clear of it, whether
      the template draws that furniture on the master or on each layout.
    * A title layout gets its region from `_canvas_region`, as usual.
    * A placeholder-less layout becomes a full canvas below the logo band, and borrows
      the title layout's title box so a program draws its title where every other
      slide's title sits.

    Args:
        layouts: The master's layouts, already read.
        master: The python-pptx slide master.
        slide_w: Slide width in inches.
        slide_h: Slide height in inches.
    """
    master_rects, master_texts = _master_decor(master)
    for layout in layouts:
        layout.high_creativity = True
        layout.canonical = False
        rects = list(layout.decor_rects)
        for rect in master_rects:
            if not any(all(abs(a - b) <= 0.02 for a, b in zip(rect.as_tuple(),
                                                               r.as_tuple()))
                       for r in rects):
                rects.append(rect)
        # Sample text the template author baked in ("Click to edit title") is only a
        # placeholder: it is stripped from generated files, and its box becomes the
        # place where the program draws the real title.
        slot = _sample_title_slot(master.slide_layouts[layout.layout_index])
        if slot is not None:
            name, rect, size, text = slot
            layout.prompt_shapes = [name]
            layout.title_rect, layout.title_font_size = rect, size
            rects = [r for r in rects if not all(abs(a - b) <= 0.02 for a, b in
                                                 zip(r.as_tuple(), rect.as_tuple()))]
            layout.fixed_texts = [t for t in layout.fixed_texts if t != text]
            log.info("High Creativity layout %s: sample text box %r (%r) is a title "
                     "slot at %s, %spt", layout.key, name, text, rect.to_list(), size)
        layout.decor_rects = tuple(rects)
        layout.fixed_texts = (layout.fixed_texts
                              + [t for t in master_texts if t not in layout.fixed_texts])
        if layout.placeholders:
            layout.canvas_capable, layout.free_region = _canvas_region(
                layout.placeholders, rects, slide_w, slide_h)
        else:
            band = rects + ([layout.title_rect] if layout.title_rect else [])
            region = _blank_canvas_region(band, slide_w, slide_h)
            layout.canvas_capable, layout.free_region = region is not None, region
    # Layouts without their own sample title borrow the one that has it (or a real
    # title placeholder), so every max-mode slide's title lands in the same place.
    donor = next((l for l in layouts if l.title_rect is not None), None)
    title_layout = next((l for l in layouts if l.has_role("title")), None)
    title = title_layout.by_role("title")[0] if title_layout is not None else None
    for layout in layouts:
        if not layout.placeholders and layout.title_rect is None:
            if donor is not None:
                layout.title_rect = donor.title_rect
                layout.title_font_size = donor.title_font_size
            elif title is not None:
                layout.title_rect = title.rect()
                layout.title_font_size = title.font_size
            else:
                layout.title_rect = Rect(1.14, 0.38, round(slide_w - 1.6, 2), 0.6)
                layout.title_font_size = 28
        log.info("High Creativity layout %s %r: %s, free region %s, title box %s",
                 layout.key, layout.name,
                 "title layout" if layout.placeholders else "blank canvas",
                 layout.free_region.to_list() if layout.free_region else None,
                 layout.title_rect.to_list() if layout.title_rect else None)


def _read_layout(layout, mi: int, li: int, theme: ThemeInfo,
                 txstyles: dict[str, Optional[float]], slide_w: float, slide_h: float,
                 sample_titles: list[str]) -> LayoutInfo:
    placeholders: list[PlaceholderInfo] = []
    decor: dict[str, int] = {}
    decor_rects: list[Rect] = []
    fixed_texts: list[str] = []
    # The real backgrounds in this template are <p:bg> fills, not shapes: a layout's own
    # override wins over the master's, and a large background shape (scanned below) wins
    # over both, because it is drawn on top.
    master = layout.slide_master
    background_luminance = next(
        (lum for lum in (_background_from_bg(layout.element, theme, layout.part),
                         _background_from_bg(master.element, theme, master.part))
         if lum is not None), 1.0)
    background = "dark" if background_luminance < 0.5 else "light"
    slide_area = slide_w * slide_h
    for shape in layout.shapes:
        if shape.is_placeholder:
            pf = shape.placeholder_format
            ph_type = pf.type.name if pf.type is not None else "BODY"
            # Date / footer / slide number / header are the frame's to draw (Task 5),
            # never the agent's to fill. Publishing them as content roles invites a
            # composing agent to write copy into the page-number box.
            if ph_type in FURNITURE_TYPES:
                continue
            prompt = (shape.text_frame.text.strip().replace("\n", " / ")
                      if shape.has_text_frame else "")
            left, top, width, height = (_inches(shape.left), _inches(shape.top),
                                        _inches(shape.width), _inches(shape.height))
            role, accepts_text = _placeholder_role(ph_type, pf.idx, prompt, width, height)
            fmt = _read_placeholder_format(shape._element, theme)
            size = fmt.get("font_size")
            if size is None:
                size = txstyles.get("title") if role == "title" else txstyles.get("body")
            size = size or 18.0
            # One text-measurement model for the whole codebase (Task 2), so the hint the
            # writer sees is the same arithmetic the overflow gate later judges it by.
            # A box with no resolvable geometry (master 2 declares no placeholders to
            # inherit from) gets no hint at all rather than a fictitious "1 char, 1 line".
            if width > 0 and height > 0:
                bullets = bool(fmt.get("bullets", role == "body"))
                chars_per_line = measure.chars_per_line(width, size, bullets)
                lines = measure.line_capacity(height, size)
            else:
                chars_per_line = lines = 0
            placeholders.append(PlaceholderInfo(
                idx=pf.idx, type=ph_type, role=role, slot="", name=shape.name, prompt=prompt,
                left=left, top=top, width=width, height=height, font_size=round(size, 1),
                font_name=fmt.get("font_name") or theme.minor_font, bold=fmt.get("bold"),
                color=fmt.get("color"), align=fmt.get("align"), bullets=fmt.get("bullets"),
                all_caps=fmt.get("all_caps", False), autofit=fmt.get("autofit"),
                accepts_text=accepts_text,
                approx_chars=chars_per_line * lines, approx_lines=lines,
            ))
            continue
        kind = shape.shape_type.name.lower() if shape.shape_type is not None else "shape"
        decor[kind] = decor.get(kind, 0) + 1
        # main_v3 only counted these; the gate needs where they are, because they are
        # inherited graphics the agent will otherwise draw straight over.
        decor_rects.append(Rect(_inches(shape.left), _inches(shape.top),
                                _inches(shape.width), _inches(shape.height)))
        if shape.has_text_frame and shape.text_frame.text.strip():
            fixed_texts.append(shape.text_frame.text.strip().replace("\n", " ")[:60])
        area = _inches(shape.width) * _inches(shape.height)
        if area >= 0.45 * slide_area:
            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                background, background_luminance = "photo", 0.5
            else:
                lum = _fill_luminance(shape, theme)
                if lum is not None:
                    # Classify from the measured number, never from the fill type.
                    background_luminance = lum
                    background = "dark" if lum < 0.5 else "light"

    ordered = _reading_order(placeholders)
    counters: dict[str, int] = {}
    for p in ordered:
        if p.role in ("title", "subtitle") and counters.get(p.role, 0) == 0:
            p.slot = p.role
            counters[p.role] = 1
        else:
            counters[p.role] = counters.get(p.role, 0) + 1
            p.slot = f"{p.role}{counters[p.role]}"
    columns = _column_count(ordered)
    tags = _derive_tags(layout.name, ordered, decor, columns, background)
    use = _recommended_use(layout.name, ordered, tags, columns, background, fixed_texts)
    canvas_capable, free_region = _canvas_region(ordered, decor_rects, slide_w, slide_h)
    return LayoutInfo(
        key=f"M{mi}-L{li}", master_index=mi, layout_index=li, name=layout.name.strip(),
        theme_name=theme.name, placeholders=ordered, decor=decor,
        fixed_texts=fixed_texts[:6], background=background, tags=sorted(tags),
        columns=columns, recommended_use=use, sample_titles=sample_titles[:3],
        usage_count=len(sample_titles), decor_rects=tuple(decor_rects),
        background_luminance=round(background_luminance, 3),
        canvas_capable=canvas_capable, free_region=free_region,
    )


def _reading_order(placeholders: list[PlaceholderInfo]) -> list[PlaceholderInfo]:
    rows: list[list[PlaceholderInfo]] = []
    for p in sorted(placeholders, key=lambda x: (x.top, x.left)):
        if rows and abs(p.top - rows[-1][0].top) < 0.35:
            rows[-1].append(p)
        else:
            rows.append([p])
    ordered: list[PlaceholderInfo] = []
    for row in rows:
        ordered.extend(sorted(row, key=lambda x: x.left))
    return ordered


def _column_count(ordered: list[PlaceholderInfo]) -> int:
    best = 1
    rows: dict[float, dict[str, int]] = {}
    for p in ordered:
        if p.role in ("heading", "body", "content", "picture", "number"):
            key = round(p.top / 0.4)
            rows.setdefault(key, {})
            rows[key][p.role] = rows[key].get(p.role, 0) + 1
    for counts in rows.values():
        best = max(best, max(counts.values()))
    return best if best >= 2 else (1 if ordered else 0)


INTENT_TAGS: dict[str, tuple[list[str], list[str]]] = {
    "cover": (["cover"], ["closing"]),
    "title": (["cover"], ["closing"]),
    "agenda": (["agenda"], ["cover", "closing"]),
    "section": (["section", "quote"], ["cover", "closing", "agenda"]),
    "divider": (["section"], ["cover", "closing"]),
    "quote": (["quote", "section"], ["cover", "closing"]),
    "closing": (["closing"], ["cover"]),
    "thank_you": (["closing"], ["cover"]),
    "content": (["content", "text"], ["cover", "agenda", "section", "closing", "blank", "quote"]),
    "bullets": (["text", "title-only", "content"],
                ["cover", "agenda", "section", "closing", "blank"]),
    "comparison": (["2-column", "columns", "content"],
                   ["cover", "agenda", "section", "closing"]),
    "columns": (["columns", "content"], ["cover", "agenda", "section", "closing"]),
    "process": (["process", "columns", "content"], ["cover", "agenda", "section", "closing"]),
    "timeline": (["process", "content"], ["cover", "agenda", "section", "closing"]),
    "dashboard": (["dashboard", "content"], ["cover", "agenda", "section", "closing"]),
    "kpi": (["dashboard", "content"], ["cover", "agenda", "section", "closing"]),
    "photo": (["photo", "picture-slots", "content"], ["cover", "closing"]),
    "image": (["photo", "picture-slots", "content"], ["cover", "closing"]),
    "screenshot": (["photo", "content"], ["cover", "closing"]),
    "team": (["photo", "content"], ["cover", "closing"]),
    "table": (["title-only", "content"], ["cover", "agenda", "section", "closing"]),
    "chart": (["chart", "title-only", "content"], ["cover", "agenda", "section", "closing"]),
    "blank": (["blank"], []),
}


def _derive_tags(name: str, phs: list[PlaceholderInfo], decor: dict[str, int], columns: int,
                 background: str) -> set:
    n = name.lower()
    tags = set()
    roles = {p.role for p in phs}
    if any(k in n for k in ("cover", "title slide")):
        tags.add("cover")
    if "agenda" in n or "agena" in n:
        tags.add("agenda")
    if any(k in n for k in ("section", "chapter", "divider")):
        tags.add("section")
    if "quote" in n:
        tags.add("quote")
    if "thank" in n:
        tags.add("closing")
    if "blank" in n and not phs:
        tags.add("blank")
    if (any(k in n for k in ("photo", "picture", "image", "screenshot", "headshot", "laptop"))
            and "no photo" not in n):
        tags.add("photo")
    if any(k in n for k in ("timeline", "progress", "stages", "arrow", "steps", "circles")):
        tags.add("process")
    if any(k in n for k in ("dashboard", "highlights", "quadrant", "infographic", "kpi",
                            "overview")):
        tags.add("dashboard")
    if "chart" in n:
        tags.add("chart")
    if "smartart" in n:
        tags.add("diagram")
    if "title only" in n or (
        "title" in roles
        and not roles & {"body", "content", "picture", "heading", "number"}
        and not tags
    ):
        tags.add("title-only")
    if columns >= 2:
        tags.add("columns")
        tags.add(f"{columns}-column")
    if "picture" in roles:
        tags.add("picture-slots")
    if roles & {"body", "content"}:
        tags.add("text")
    if background == "dark":
        tags.add("dark")
    if not tags & {"cover", "agenda", "section", "quote", "closing", "blank"}:
        tags.add("content")
    return tags


def _recommended_use(name: str, phs: list[PlaceholderInfo], tags: set, columns: int,
                     background: str, fixed: list[str]) -> str:
    bodies = len([p for p in phs if p.role in ("body", "content")])
    headings = len([p for p in phs if p.role == "heading"])
    pictures = [p for p in phs if p.role == "picture"]
    if "cover" in tags:
        return "Opening / title slide" + (" with a full-bleed photo slot" if pictures
                                          else " (no photo needed)")
    if "closing" in tags:
        return "Closing 'thank you' slide; fixed text: " + "; ".join(fixed[:3])
    if "agenda" in tags:
        return f"Agenda slide with {bodies or headings} numbered items"
    if "section" in tags or "quote" in tags:
        return ("Section divider or quote; short title (+ optional subtitle)"
                + (" on a dark background" if background == "dark" else ""))
    if "blank" in tags:
        return "Empty canvas for fully custom shapes"
    if "title-only" in tags:
        return ("Title (+ subtitle) with free canvas below for custom cards, tables, "
                "charts or images")
    parts = []
    if columns >= 2:
        parts.append(f"{columns}-column comparison / grouped points")
    if pictures:
        parts.append(f"{len(pictures)} picture slot(s)"
                     + (" that accept short text" if any(p.accepts_text for p in pictures)
                        else ""))
    if headings:
        parts.append(f"{headings} heading(s)")
    if bodies:
        parts.append(f"{bodies} text block(s)")
    if "process" in tags:
        parts.append("process / timeline steps")
    if "dashboard" in tags:
        parts.append("KPI or highlight boxes")
    return "Content slide: " + ", ".join(parts) if parts else "Content slide"


# --------------------------------------------------------------------------------- #
# loading                                                                             #
# --------------------------------------------------------------------------------- #

@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> TemplateCatalog:
    log.info("introspecting %s (cache miss)", Path(path).name)
    prs = open_presentation(path)
    catalog = TemplateCatalog.from_presentation(prs, path)
    # Interpolated into the message, not passed as `extra`: the service's formatter
    # renders the message and drops unknown record attributes, so numbers put in
    # `extra` never reach the log at all.
    log.info("template catalog loaded from %s: %d master(s), %d layout(s), "
             "%d canvas-capable, %d sample slide(s)", Path(path).name,
             len(catalog.themes), len(catalog.layouts),
             sum(1 for l in catalog.layouts if l.canvas_capable),
             catalog.sample_slide_count)
    return catalog


def _per_master(usage: dict[str, int], master_count: int) -> dict[int, int]:
    """Layout usage ("M1-L4" -> n) summed per master index."""
    return {mi: sum(n for key, n in usage.items() if key.startswith(f"M{mi}-"))
            for mi in range(master_count)}


def _reference_usage(template_path) -> dict[str, dict[str, int]]:
    """The usage counts `reference_profile` recorded beside a prepared reference deck -
    "layout_usage", "content_usage", "cover_usage", each "M1-L4" -> n; {} when there is
    no profile (the brand template)."""
    profile = Path(str(template_path)).with_name(Path(str(template_path)).stem
                                                 + ".profile.json")
    if not profile.is_file():
        return {}
    try:
        data = json.loads(profile.read_text())
        return {name: {str(key): int(n) for key, n in (data.get(name) or {}).items()}
                for name in ("layout_usage", "content_usage", "cover_usage")}
    except (ValueError, TypeError, AttributeError) as exc:
        log.warning("reference profile %s unreadable (%s); usage left at zero",
                    profile.name, exc)
        return {}


def load(path: str | None = None) -> TemplateCatalog:
    """The catalog for the one brand template, cached until the file changes.

    Args:
        path: Override, for tests and scripts; defaults to `registry.template_path()`.

    Returns:
        The shared catalog for that file.
    """
    resolved = str(path or registry.template_path())
    log.debug("catalog requested for %s", Path(resolved).name)
    return _load(resolved, Path(resolved).stat().st_mtime)


def for_classification(classification: str | None = None) -> TemplateCatalog:
    """Compatibility shim. Decision 3: classification no longer selects a template.

    Kept so `plan_tools` and `layout_tools` need no edit; the argument is ignored.
    """
    log.debug("for_classification(%r) ignores its argument; one template serves every "
              "classification", classification)
    return load()


# --------------------------------------------------------------------------------- #
# a layout's artwork, element by element                                              #
# --------------------------------------------------------------------------------- #

def layout_artwork(catalog: TemplateCatalog, key: str) -> list[dict[str, Any]]:
    """Describe, element by element, the fixed artwork one layout draws.

    The catalog keeps only where a layout's graphics are (`decor_rects`) and a few of
    their texts, unpaired. Reproducing a reference design - a Quiz or FAQ layout the
    template author drew as artwork with sample copy - needs each element's kind,
    colour, text and size together, so this re-reads that one layout. Colours are
    reported as the nearest brand token and sizes snapped to the type scale, because
    those are the only values a slide program may use.

    Args:
        catalog: The loaded catalog, for its template path and brand palette.
        key: Catalog key of the layout, e.g. "M3-L47".

    Returns:
        One dict per non-placeholder shape, in z-order, with `kind`, `rect_in` and
        whichever of `fill`, `fill_style`, `gradient_to`, `outline`, `text`,
        `text_size_pt`, `bold`, `text_color`, `align` apply. Empty when the layout or
        the file cannot be read - a reference design is guidance, never a dependency.
    """
    from app.genai.ppt import deck_builder as builder

    layout = catalog.get(key)
    if layout is None:
        log.warning("artwork requested for unknown layout %r", key)
        return []
    palette = builder.brand_palette(catalog.theme.colors)
    try:
        mtime = Path(catalog.template_path).stat().st_mtime
        shapes = _layout_artwork(catalog.template_path, mtime, layout.master_index,
                                 layout.layout_index, tuple(sorted(palette.items())))
    except Exception as exc:                                             # noqa: BLE001
        log.warning("artwork of %s could not be read from %s (%s); the reference "
                    "design will be described by its rectangles only", key,
                    Path(catalog.template_path).name, exc, exc_info=True)
        return []
    # Copies, so a caller editing an entry can never corrupt the cache.
    return [dict(shape) for shape in shapes]


@lru_cache(maxsize=16)
def _layout_artwork(path: str, mtime: float, master_index: int, layout_index: int,
                    palette_items: tuple) -> tuple:
    """`layout_artwork`'s uncached body, keyed on the file's mtime like `_load`."""
    prs = open_presentation(path)
    master = list(prs.slide_masters)[master_index]
    layout = list(master.slide_layouts)[layout_index]
    theme = _read_theme(master)
    palette = dict(palette_items)
    described = []
    for shape in layout.shapes:
        if shape.is_placeholder:
            continue
        try:
            described.append(_describe_artwork_shape(shape, theme, palette))
        except Exception as exc:                                         # noqa: BLE001
            # One unreadable element costs that element, not the reference design.
            log.debug("skipped artwork shape %r on M%d-L%d: %s",
                      getattr(shape, "name", "?"), master_index, layout_index, exc)
    log.info("read %d artwork element(s) from layout M%d-L%d %r", len(described),
             master_index, layout_index, layout.name)
    return tuple(described)


def _describe_artwork_shape(shape, theme: ThemeInfo,
                            palette: dict[str, str]) -> dict[str, Any]:
    """One artwork element as kind, rectangle, brand-token colours and text."""
    entry: dict[str, Any] = {
        "kind": _artwork_kind(shape),
        "rect_in": [_inches(shape.left), _inches(shape.top), _inches(shape.width),
                    _inches(shape.height)],
    }
    sp_pr = shape._element.find(qn("p:spPr"))
    if sp_pr is not None:
        solid = sp_pr.find(qn("a:solidFill"))
        grad = sp_pr.find(qn("a:gradFill"))
        if solid is not None and len(solid) and not _is_transparent(solid[0]):
            hex_value = _color_element_hex(solid[0], theme)
            if hex_value:
                entry["fill"] = _nearest_token(hex_value, palette)
                entry["fill_style"] = "solid"
        elif grad is not None:
            stops = [_color_element_hex(stop[0], theme)
                     for stop in grad.iter(qn("a:gs"))
                     if len(stop) and not _is_transparent(stop[0])]
            stops = [s for s in stops if s]
            if stops:
                entry["fill"] = _nearest_token(stops[0], palette)
                entry["fill_style"] = "gradient"
                entry["gradient_to"] = _nearest_token(stops[-1], palette)
        line = sp_pr.find(qn("a:ln"))
        line_fill = line.find(qn("a:solidFill")) if line is not None else None
        if (line_fill is not None and len(line_fill)
                and not _is_transparent(line_fill[0])):
            hex_value = _color_element_hex(line_fill[0], theme)
            if hex_value:
                entry["outline"] = _nearest_token(hex_value, palette)
    if shape.has_text_frame and shape.text_frame.text.strip():
        paragraphs = [p for p in shape.text_frame.paragraphs if p.text.strip()]
        entry["text"] = " / ".join(p.text.strip() for p in paragraphs)[:160]
        run = next((r for p in paragraphs for r in p.runs if r.text.strip()), None)
        if run is not None:
            if run.font.size is not None:
                entry["text_size_pt"] = _snap_to_type_scale(run.font.size.pt)
            if run.font.bold is not None:
                entry["bold"] = bool(run.font.bold)
            r_pr = run._r.find(qn("a:rPr"))
            fill = r_pr.find(qn("a:solidFill")) if r_pr is not None else None
            if fill is not None and len(fill):
                hex_value = _color_element_hex(fill[0], theme)
                if hex_value:
                    entry["text_color"] = _nearest_token(hex_value, palette)
        if paragraphs and paragraphs[0].alignment is not None:
            entry["align"] = paragraphs[0].alignment.name.lower()
        # An invisible box that only carries words is, to a slide program, a text box.
        if ("fill" not in entry and "outline" not in entry
                and entry["kind"] in ("rectangle", "shape")):
            entry["kind"] = "textbox"
    return entry


def _is_transparent(color_element) -> bool:
    """True for a colour whose alpha makes it effectively invisible (under 5%)."""
    alpha = color_element.find(qn("a:alpha"))
    return alpha is not None and int(alpha.get("val", "100000")) < 5000


def _artwork_kind(shape) -> str:
    """A DeckBuilder-friendly name for an artwork element's kind."""
    kind = shape.shape_type
    if kind == MSO_SHAPE_TYPE.PICTURE:
        return "picture"
    if kind == MSO_SHAPE_TYPE.LINE or etree.QName(shape._element).localname == "cxnSp":
        return "line"
    if kind == MSO_SHAPE_TYPE.TEXT_BOX:
        return "textbox"
    if kind == MSO_SHAPE_TYPE.GROUP:
        return "group"
    if kind == MSO_SHAPE_TYPE.AUTO_SHAPE:
        try:
            return shape.auto_shape_type.name.lower()
        except Exception:                                                # noqa: BLE001
            return "shape"
    return "shape"


def _nearest_token(hex_value: str, palette: dict[str, str]) -> str:
    """The brand token whose colour is closest to `hex_value` (RGB distance)."""
    def channels(value: str) -> tuple[int, int, int]:
        value = value.lstrip("#")
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)

    target = channels(hex_value)
    return min(palette, key=lambda token: sum(
        (a - b) ** 2 for a, b in zip(channels(palette[token]), target)))


def _snap_to_type_scale(size: float) -> int:
    """The nearest size a component may set; ties go to the smaller size."""
    return min(GeometryConstants.TYPE_SCALE, key=lambda s: (abs(s - size), s))



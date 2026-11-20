"""The deck design contract: computed once, handed to every parallel slide builder.

This is what makes a deck of independently generated slides read as one deck. Four
workers each choosing a master, a grid and a palette in isolation is precisely why the
current decks do not cohere, so those choices are made here, once, and are not the
slide builder's to revisit.

Almost all of it is deterministic from the catalog. The single judgement call - which
composition mode and which layout each slide should use - is one model call for the
whole deck, made with every slide's title and content shape in view. Its answer is
validated against the catalog before it is trusted, re-requested once if it is wrong,
and replaced by `recommend`'s top choice in `layout` mode if it is wrong twice. A model
failure degrades to that same default: it never fails the deck.
"""

import json
from dataclasses import dataclass, field
from typing import Callable, Optional

from app.config.constants import (
    GeometryConstants as GC, ModelConstants, PptCreatorConstants,
)
from app.core.models.deck_model import DeckDraft, DeckPlan
from app.genai.ppt import deck_builder as builder
from app.genai.ppt.template import catalog as catalog_mod, registry
from app.utilities import events_util as events
from app.utilities.apim_llm_utils import get_apim_llm_client
from app.utilities.logger_util import get_logger
from app.utilities.structured_output import extract_json, finish_reason, parse_json

log = get_logger(__name__)

#: Slide kinds exempt from master uniformity, matching main_v3.
_EXEMPT_KINDS = ("cover", "thanks")

#: Intents the catalog ranks for. Anything else falls back to "content".
_VISUAL_INTENTS = ("table", "chart", "photo", "process", "dashboard", "comparison",
                   "timeline")
#: Words in a slide's visual hint or LAYOUT direction that name an intent, checked in
#: this order (a "timeline of steps" is a timeline before it is a process).
_INTENT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("timeline", ("timeline", "roadmap", "milestone")),
    ("comparison", ("compar", "versus", " vs", "side by side", "side-by-side",
                    "column")),
    ("process", ("process", "step", "stage", "flow", "sequence", "journey")),
    ("dashboard", ("kpi", "metric", "dashboard")),
    ("chart", ("chart", "graph", "trend", "%")),
    ("table", ("table", "matrix")),
    ("photo", ("photo", "image", "picture", "screenshot", "keyframe")),
)

SYSTEM_PROMPT = """You are the art director for one slide deck.

You are given every slide's number, role, title, content shape and visual hint at once,
and a catalog of the brand template's layouts. Decide, for each slide, how it should be
built:

- mode "layout": fill an existing template layout's placeholders. Correct whenever the
  content maps cleanly onto a layout that already exists - three text columns, a five
  item agenda, a photo with text.
- mode "canvas": take a canvas-capable layout and compose the content region freely.
  Correct for charts, tables, cards, composed diagrams - content an existing layout
  would misrepresent.

Rules you must follow:
- Every content slide uses a layout on the preferred content master. Cover and closing
  slides may use any master.
- "canvas" is only valid on a layout the catalog marks canvas_capable.
- Reference layouts by their catalog key ("M1-L10"). Never invent one.
- Vary the layouts. A deck where every slide is the same layout reads as a template,
  not as a deck. Never give consecutive content slides the same layout unless they
  form a series (parallel steps or modules).
- When a slide carries a "layout hint" (a layout NAME the planner chose for its
  content), use that layout on the preferred content master unless it breaks a rule
  above. Its "structure" line says what shape the content wants.
- Give a layout with a picture slot ONLY to a slide that carries an image. A slide with
  0 images on a picture layout leaves an empty hole where the picture should be.

Answer with JSON only: a list of
{"slide_number": int, "mode": "layout"|"canvas", "layout_key": "M1-L10"}
"""


@dataclass(frozen=True)
class SlideAssignment:
    """How one slide is to be built."""
    slide_number: int
    mode: str
    layout_key: str
    free_region: Optional[list] = None


def frame_for_layout(classification: str, slide_width: float, slide_height: float,
                     slide_number: int, layout) -> dict:
    """The frame one slide carries: classification, logo variant, slide number.

    Module-level so a slide that is moved onto a different layout after the contract
    was built (a Quiz or FAQ slide joining its family's layout) can have its frame
    re-derived for the layout it actually lands on.

    Args:
        classification: Normalized classification, e.g. "internal".
        slide_width: Canvas width in inches.
        slide_height: Canvas height in inches.
        slide_number: Printed in the footer band.
        layout: The layout the slide sits on, for its background luminance.

    Returns:
        A `deck_builder.frame_spec` block.
    """
    luminance = getattr(layout, "background_luminance", 1.0)
    variant = builder.logo_variant_for(luminance)
    logo_file = "d2r_mark_white.png" if variant == "white" else "d2r_mark.png"
    # A reference deck already carries Rockwell's logo in its layouts
    # (`reference_rebrand`), often in the corner the frame would stamp; a second mark
    # there would sit on top of it.
    reference = registry.template_override()
    logo = None if reference is not None else registry.logo_path(logo_file)
    if reference is not None:
        log.debug("slide %d: reference design %s carries its own Rockwell logo; the "
                  "frame adds none", slide_number, reference.name)
    symbol = registry.classification_symbol(classification)
    if symbol is None:
        log.warning("slide %d: no policy symbol on disk for %r; the classification "
                    "label ships without its mark", slide_number, classification)
    if logo is None and reference is None:
        log.warning("slide %d: brand mark %s is not on disk; no logo will be "
                    "placed", slide_number, logo_file)
    log.debug("slide %d frame: %s on %s (luminance %.2f) -> %s logo",
              slide_number, classification, getattr(layout, "key", "?"), luminance,
              variant)
    return builder.frame_spec(
        slide_width, slide_height,
        label=registry.label(classification),
        symbol_path=str(symbol) if symbol else None,
        logo_path=str(logo) if logo else None,
        logo_variant=variant, slide_number=slide_number)


@dataclass
class DeckDesignContract:
    """The deck-wide decisions every slide builder inherits."""
    master_index: int
    grid: dict
    tokens: dict
    theme_font: str
    classification: str
    slide_width: float
    slide_height: float
    assignments: dict = field(default_factory=dict)

    def frame_for(self, slide_number: int, layout) -> dict:
        """The frame this slide carries: classification, logo variant, slide number.

        Args:
            slide_number: Printed in the footer band.
            layout: The layout the slide sits on, for its background luminance.

        Returns:
            A `deck_builder.frame_spec` block.
        """
        return frame_for_layout(self.classification, self.slide_width,
                                self.slide_height, slide_number, layout)

    def brief_fields(self, slide_number: int, layout) -> dict:
        """The contract fields `SlideCodeBrief` carries for this slide.

        Args:
            slide_number: The slide being briefed.
            layout: The layout it was assigned.

        Returns:
            A kwargs dict for `SlideCodeBrief`.
        """
        assignment = self.assignments[slide_number]
        log.debug("brief fields for slide %d: mode=%s layout=%s free_region=%s",
                  slide_number, assignment.mode, assignment.layout_key,
                  assignment.free_region)
        return {
            "mode": assignment.mode,
            "layout_key": assignment.layout_key,
            "grid": dict(self.grid),
            "frame": self.frame_for(slide_number, layout),
            "tokens": dict(self.tokens),
            "free_region": (list(assignment.free_region) if assignment.free_region
                            else None),
        }


def fallback_assignment(slide, catalog, master_index: int) -> SlideAssignment:
    """The safe default: `recommend`'s top choice, in `layout` mode.

    Used when the model's answer is unusable twice, and when the model is unreachable.
    A layout the template designed is always cleaner than a canvas we composed badly.

    Args:
        slide: The planned slide, for its kind and visual hint.
        catalog: The template catalog.
        master_index: The preferred content master.

    Returns:
        An assignment that is always on a real layout.
    """
    intent = _intent_for(slide)
    ranked = catalog.recommend(intent, limit=5)
    exempt = getattr(slide, "kind", "content") in _EXEMPT_KINDS
    log.debug("slide %d: falling back on intent %r, %d candidate(s), exempt=%s",
              slide.slide_number, intent, len(ranked), exempt)
    for layout, score, _reasons in ranked:
        if exempt or layout.master_index == master_index:
            log.info("slide %d assigned %s by fallback (intent %r, score %s)",
                     slide.slide_number, layout.key, intent, score)
            return SlideAssignment(slide.slide_number, "layout", layout.key, None)
    chosen = ranked[0][0] if ranked else catalog.canonical_layouts()[0]
    log.warning("slide %d: no recommendation for intent %r sits on master M%d; "
                "using %s off-master", slide.slide_number, intent, master_index,
                chosen.key)
    return SlideAssignment(slide.slide_number, "layout", chosen.key, None)


def _layout_direction(slide) -> str:
    """The LAYOUT: line of a slide's planner instructions, or ""."""
    for line in (getattr(slide, "planner_special_instructions", "") or "").splitlines():
        if line.strip().upper().startswith("LAYOUT:"):
            return line.split(":", 1)[1].strip()
    return ""


def _intent_for(slide) -> str:
    """Map a planned slide onto the catalog's intent vocabulary.

    The visual hint and the planner's LAYOUT direction are free text ("3-stage
    horizontal flow"), so their keywords are matched rather than the whole string.
    """
    kind = getattr(slide, "kind", "content")
    if kind in ("cover", "agenda", "thanks"):
        return {"thanks": "closing"}.get(kind, kind)
    visual = (getattr(slide, "visual", "") or "").strip().lower()
    if visual in _VISUAL_INTENTS:
        return visual
    text = f" {visual} {_layout_direction(slide).lower()}"
    for intent, words in _INTENT_KEYWORDS:
        if any(word in text for word in words):
            log.debug("slide %s: intent %r from its visual/layout hint",
                      getattr(slide, "slide_number", "?"), intent)
            return intent
    return "content"


def _needs_photo_slot(layout) -> bool:
    """True when a layout has a picture slot that cannot take a text label instead."""
    return any(not p.accepts_text for p in layout.by_role("picture"))


def validate_assignments(raw, plan: DeckPlan, catalog,
                         image_slides: Optional[set] = None) -> tuple[dict, list]:
    """Check the model's answer against the catalog before any of it is trusted.

    Args:
        raw: The model's list of assignment dicts.
        plan: The deck plan, for each slide's kind.
        catalog: The template catalog.
        image_slides: Numbers of the slides that carry an image. When given, a slide
            outside it may not be put on a layout whose picture slot needs an image.

    Returns:
        (accepted assignments by slide number, one problem string per rejection).
    """
    kinds = {s.slide_number: s.kind for s in plan.slides}
    accepted: dict[int, SlideAssignment] = {}
    problems: list[str] = []
    log.debug("validating %d assignment(s) against the catalog for %d planned slide(s)",
              len(raw or []), len(kinds))

    for entry in raw or []:
        if not isinstance(entry, dict):
            problems.append(f"expected an object per slide, got {entry!r}")
            continue
        try:
            number = int(entry.get("slide_number"))
        except (TypeError, ValueError):
            problems.append(f"slide_number missing or not a number in {entry!r}")
            continue
        if number not in kinds:
            problems.append(f"slide {number} is not in the deck")
            continue

        key = str(entry.get("layout_key") or "").strip()
        layout = catalog.get(key)
        if layout is None:
            problems.append(
                f"slide {number}: layout_key '{key}' is not in the catalog; use a key "
                f"exactly as the catalog lists it, e.g. 'M1-L10'")
            continue

        if getattr(layout, "high_creativity", False):
            # Only max mode moves a slide onto that master; the art director never may.
            problems.append(
                f"slide {number}: '{layout.key}' is reserved and not in the catalog; "
                f"use a key exactly as the catalog lists it")
            continue

        mode = str(entry.get("mode") or "layout").strip().lower()
        if mode not in ("layout", "canvas"):
            problems.append(f"slide {number}: mode must be 'layout' or 'canvas', "
                            f"not {mode!r}")
            continue
        if mode == "canvas" and not layout.canvas_capable:
            problems.append(
                f"slide {number}: '{layout.key}' is not canvas_capable, so it cannot be "
                f"used in canvas mode. Either use mode 'layout' on it, or pick a "
                f"canvas_capable layout")
            continue
        if (kinds[number] not in _EXEMPT_KINDS
                and layout.master_index != catalog.preferred_master):
            problems.append(
                f"slide {number}: '{layout.key}' is on master M{layout.master_index}, "
                f"but content slides must stay on the preferred content master "
                f"M{catalog.preferred_master}")
            continue

        if (image_slides is not None and number not in image_slides
                and mode == "layout" and kinds[number] not in _EXEMPT_KINDS
                and _needs_photo_slot(layout)):
            problems.append(
                f"slide {number}: '{layout.key}' has a picture slot, but slide {number} "
                f"carries no image, so the slot would be left as an empty hole. Choose "
                f"a layout without a picture slot")
            continue

        accepted[number] = SlideAssignment(
            number, mode, layout.key,
            list(layout.free_region.to_list())
            if (mode == "canvas" and layout.free_region) else None)
        log.debug("slide %d accepted: %s mode on %s", number, mode, layout.key)
    if problems:
        log.info("design contract: %d of %d assignment(s) rejected",
                 len(problems), len(raw or []))
        for problem in problems:
            log.info("  rejected: %s", problem)
    return accepted, problems


def build(plan: DeckPlan, draft: DeckDraft, *, catalog=None,
          ask: Optional[Callable[[str, str], list]] = None,
          session_id: str = "", skill_guidance="") -> DeckDesignContract:
    """Compute the whole deck's design contract, before any slide is built.

    Args:
        plan: The deck plan, for classification and each slide's role and visual hint.
        draft: The assembled slides, for each slide's actual content shape.
        catalog: Injected in tests; the template catalog otherwise.
        ask: Injected in tests; `(system, user) -> list of assignment dicts`.
        session_id: When given, one event is published if the art direction had to be
            abandoned. Nothing else here reaches the UI - the workflow already
            announces the contract it got.

    Returns:
        A contract with an assignment for every slide in the plan. Never raises: a
        model failure degrades every slide to `recommend`'s top choice.
    """
    log.info("building the design contract for %r: %d slide(s), classification %r",
             plan.title, len(plan.slides), plan.classification)
    catalog = catalog or catalog_mod.load()
    ask = ask or _ask_model
    classification = registry.normalize(plan.classification)

    tokens = builder.brand_palette(catalog.theme.colors)
    tokens["_type_scale"] = list(GC.TYPE_SCALE)
    contract = DeckDesignContract(
        master_index=catalog.preferred_master,
        grid={"columns": GC.GRID_COLUMNS, "rows": GC.GRID_ROWS,
              "gutter_in": GC.GRID_GUTTER_IN},
        tokens=tokens,
        theme_font=catalog.theme.minor_font or "Barlow",
        classification=classification,
        slide_width=catalog.slide_width,
        slide_height=catalog.slide_height)

    slides_by_number = {s.slide_number: s for s in plan.slides}
    accepted: dict[int, SlideAssignment] = {}
    image_slides = {s.slide_number for s in draft.slides
                    if any((b.image_ref or "").strip() for b in s.blocks)}
    user_message = _describe_deck(plan, draft, catalog, skill_guidance)
    degraded = ""

    for attempt in (1, 2):
        try:
            log.info("design contract: asking the art director (attempt %d/2, "
                     "%d-char prompt)", attempt, len(user_message))
            raw = ask(SYSTEM_PROMPT, user_message)
            log.debug("design contract: answer carried %d entry/entries",
                      len(raw or []))
        except Exception as exc:                             # noqa: BLE001
            # The contract is an improvement, not a dependency: a gateway failure costs
            # deck-wide coherence, never the deck.
            log.warning("design contract: model unavailable on attempt %d (%s); "
                        "falling back to recommended layouts", attempt, exc,
                        exc_info=True)
            degraded = f"the art director was unreachable ({exc})"
            break
        parsed, problems = validate_assignments(raw, plan, catalog,
                                                image_slides=image_slides)
        accepted.update(parsed)
        if not problems or len(accepted) == len(slides_by_number):
            break
        if attempt == 2:
            degraded = (f"{len(problems)} layout choice(s) were rejected twice")
        if attempt == 1:
            log.info("design contract: %d assignment(s) rejected, asking again",
                     len(problems))
            user_message = (user_message
                            + "\n\n=== YOUR PREVIOUS ANSWER WAS REJECTED ===\n"
                            + "\n".join(f"- {p}" for p in problems)
                            + "\nAnswer again, for these slides only.")

    directed = set(accepted)
    for number, slide in slides_by_number.items():
        if number not in accepted:
            accepted[number] = fallback_assignment(slide, catalog,
                                                   catalog.preferred_master)
    contract.assignments = accepted
    fell_back = sorted(set(accepted) - directed)
    canvas = sum(1 for a in accepted.values() if a.mode == "canvas")
    log.info("design contract built: master M%d, %d slide(s), %d on canvas, "
             "%d art-directed, %d fell back to a recommended layout%s",
             contract.master_index, len(accepted), canvas, len(directed),
             len(fell_back), f" (slides {fell_back})" if fell_back else "",
             extra={"master": contract.master_index, "canvas_slides": canvas,
                    "slides": len(accepted), "fell_back": len(fell_back)})
    for number, assignment in sorted(accepted.items()):
        log.debug("  slide %d -> %s on %s", number, assignment.mode,
                  assignment.layout_key)

    # The one event this module publishes. A deck that silently lost its art direction
    # still ships, and looks like a worse deck for no visible reason - so say it once,
    # and only when it actually happened.
    if degraded and session_id:
        events.publish(
            session_id, "agent_step", agent="deck",
            message=(f"Layouts were chosen automatically for {len(fell_back)} "
                     f"slide(s): {degraded}."),
            phase="design_contract", fell_back=len(fell_back))
    return contract


def _describe_deck(plan: DeckPlan, draft: DeckDraft, catalog,
                   skill_guidance="") -> str:
    """Every slide's shape plus the layout menu, as one prompt."""
    drafts = {s.slide_number: s for s in draft.slides}
    lines = [f'Deck "{plan.title}" - {len(plan.slides)} slides, '
             f'audience: {plan.audience or "unstated"}, tone: {plan.tone}',
             f"Preferred content master: M{catalog.preferred_master}", "", "SLIDES:"]
    for slide in plan.slides:
        content = drafts.get(slide.slide_number)
        blocks = len(content.blocks) if content else 0
        tables = len(content.tables) if content else 0
        images = sum(1 for b in (content.blocks if content else []) if b.image_ref)
        structure = _layout_direction(slide)
        lines.append(
            f"  {slide.slide_number}. [{slide.kind}] {slide.title!r} - "
            f"{blocks} text block(s), {tables} table(s), {images} image(s)"
            + (f", visual hint: {slide.visual}" if slide.visual else "")
            + (f", layout hint: {slide.layout_hint!r}" if slide.layout_hint else "")
            + (f", structure: {structure}" if structure else ""))
    lines += ["", "CANVAS-CAPABLE LAYOUTS (valid for mode 'canvas'):"]
    for layout in catalog.layouts:
        if layout.canvas_capable:
            lines.append(f"  {layout.key} '{layout.name}' - "
                         f"{layout.free_region.height:.2f}in of free height")
    lines += ["", "LAYOUT CATALOG:", catalog.summary_text()]
    if skill_guidance:
        lines += [
            "",
            "USER-SELECTED STYLE GUIDANCE (style only; existing hard rules take priority):",
            "Use this only to inform composition and layout. Do not change the brand "
            "palette, fonts, template, protected frame, readability, or factual content. "
            "If a request conflicts, preserve those constraints.",
            skill_guidance,
        ]
    prompt = "\n".join(lines)
    canvas_capable = sum(1 for l in catalog.layouts if l.canvas_capable)
    if not canvas_capable:
        log.warning("no layout is canvas_capable, so every 'canvas' answer will be "
                    "rejected and the whole deck will build in layout mode")
    log.debug("art-director prompt: %d chars, %d slide(s), %d canvas-capable layout(s)",
              len(prompt), len(plan.slides), canvas_capable)
    return prompt


def _ask_model(system: str, user: str) -> list:
    """One gateway call for the whole deck. Returns the parsed assignment list."""
    client = get_apim_llm_client()
    log.debug("calling %s for the deck's art direction", ModelConstants.PPT_CODER)
    response = client.foundry_chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        model=ModelConstants.PPT_CODER, max_tokens=PptCreatorConstants.MAX_TOKENS)
    text = client.reply_text(response)
    if "[" not in text:
        log.error("the art director answered with no JSON list; first 400 chars: %s",
                  text[:400])
        raise ValueError("the model did not answer with a JSON list")
    try:
        parsed = parse_json(text, opener="[")
    except json.JSONDecodeError as exc:
        log.error("the art director's JSON did not parse (%s, finish_reason=%s); "
                  "first 400 chars: %s", exc, finish_reason(response) or "-",
                  extract_json(text, opener="[")[:400])
        raise
    if not isinstance(parsed, list):
        raise ValueError("the model did not answer with a JSON list")
    log.debug("art director returned %d entry/entries", len(parsed))
    return parsed

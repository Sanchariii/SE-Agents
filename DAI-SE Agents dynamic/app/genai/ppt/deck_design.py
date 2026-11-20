"""Deck-level design memory: the ONE layout each slide family (QUIZ, FAQ) uses per deck.

The PPT Creator Agent builds slides one at a time and in parallel, but a deck's Quiz
slides must all look alike, and so must its FAQ slides. That is a deck-level decision,
so it lives here rather than in any one slide's build:

- `DeckDesignRegistry` coordinates one turn: the first slide of a family to arrive
  *leads* - it designs the family layout while the others wait - and every later slide
  of that family *follows* it, changing only the content.
- The established layouts are persisted per session (`session_store`), so a Quiz slide
  added on a later turn joins the Quiz layout the deck already has.
- SAMPLE.pptx may ship a predefined family layout (matched by name, see
  `PptCreatorConstants.FAMILY_LAYOUT_NAME_PATTERNS`). Whether the leader reuses it or
  designs its own is decided by the slide's creativity level and any custom request.

A predefined layout drawn as artwork with sample copy (no content placeholders) cannot
host real content - its sample text would show through - so it is treated as a
*reference design*: the leader reproduces it with DeckBuilder components on a clean,
canvas-capable layout from the same master.
"""

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field

from app.config.constants import CreativityLevels, PptCreatorConstants, SlideTypes
from app.core.handlers.session_store import session_store
from app.core.models.deck_model import DeckDesignContext, FamilyLayout, SlideCodeBrief
from app.genai.ppt.template import catalog as catalog_mod
from app.utilities import run_control
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

#: Two rectangles closer than this on every edge are the same artwork element.
_SAME_RECT_TOLERANCE_IN = 0.05

#: Placeholder roles that can carry slide content (anything but title and subtitle).
_CONTENT_ROLES = ("body", "content", "heading", "picture", "chart", "table",
                  "number", "caption")


# --------------------------------------------------------------------------------- #
# persistence                                                                         #
# --------------------------------------------------------------------------------- #

def load(session_id: str) -> DeckDesignContext:
    """Read the session's deck design context. Never raises.

    Args:
        session_id: Owning session.

    Returns:
        The stored context, or an empty one when nothing is stored or Redis failed (the
        turn then establishes its family layouts afresh, which costs consistency with
        earlier turns, never the deck).
    """
    try:
        stored = session_store.get_deck_design(session_id)
    except Exception as exc:                                 # noqa: BLE001
        log.warning("deck design for session %s unreadable (%s); family layouts will "
                    "be established afresh this turn", session_id, exc, exc_info=True)
        return DeckDesignContext()
    if not stored:
        log.info("no deck design stored for session %s yet", session_id)
        return DeckDesignContext()
    try:
        context = DeckDesignContext.model_validate(stored)
    except Exception as exc:                                 # noqa: BLE001
        log.warning("stored deck design for session %s is malformed (%s); ignoring "
                    "it", session_id, exc, exc_info=True)
        return DeckDesignContext()
    log.info("deck design loaded for session %s: quiz=%s, faq=%s", session_id,
             _describe(context.quiz_layout), _describe(context.faq_layout))
    return context


def save(session_id: str, context: DeckDesignContext) -> bool:
    """Persist the session's deck design context. Never raises.

    Args:
        session_id: Owning session.
        context: The context to store.

    Returns:
        True when it was stored. False keeps the layout for this turn only.
    """
    try:
        session_store.save_deck_design(session_id, context.model_dump(mode="json"))
    except Exception as exc:                                 # noqa: BLE001
        log.warning("deck design for session %s could not be stored (%s); it holds for "
                    "this turn only", session_id, exc, exc_info=True)
        return False
    log.info("deck design stored for session %s: quiz=%s, faq=%s", session_id,
             _describe(context.quiz_layout), _describe(context.faq_layout))
    return True


def _describe(entry: FamilyLayout | None) -> str:
    """One short phrase for a family entry, for log lines."""
    if entry is None:
        return "none"
    if entry.established:
        return (f"{entry.layout_key} ({entry.source or 'unknown'}, slide "
                f"{entry.established_at_slide})")
    return f"directive ({len(entry.description)} chars, layout {entry.layout_key or '-'})"


# --------------------------------------------------------------------------------- #
# the template's own family layouts                                                   #
# --------------------------------------------------------------------------------- #

def family_of(layout) -> str | None:
    """The slide family a template layout is named for, or None.

    Args:
        layout: A `LayoutInfo`.

    Returns:
        "QUIZ", "FAQ" or None.
    """
    name = (getattr(layout, "name", "") or "").lower()
    for slide_type, patterns in PptCreatorConstants.FAMILY_LAYOUT_NAME_PATTERNS.items():
        if any(pattern in name for pattern in patterns):
            return slide_type
    return None


def template_family_layout(catalog, slide_type: str):
    """The SAMPLE.pptx layout predefined for a slide family, when the template has one.

    Args:
        catalog: The template catalog.
        slide_type: "QUIZ" or "FAQ".

    Returns:
        The `LayoutInfo`, preferring the content master and then the first by index
        (so "Quiz-1" wins over "Quiz-2"), or None when the template has none.
    """
    matches = [l for l in catalog.layouts if family_of(l) == slide_type
               and not is_answered_layout(l)]
    if not matches:
        log.info("the template has no predefined %s layout", slide_type)
        return None
    matches.sort(key=lambda l: (l.master_index != catalog.preferred_master,
                                l.master_index, l.layout_index))
    log.info("the template predefines %d %s layout(s): %s; using %s", len(matches),
             slide_type, ", ".join(f"{l.key} {l.name!r}" for l in matches),
             matches[0].key)
    return matches[0]


def is_answered_layout(layout) -> bool:
    """True for a quiz layout's answer-revealed twin ("Quiz-1-Answered").

    Args:
        layout: A `LayoutInfo`.

    Returns:
        Whether the layout shows a revealed answer; such a layout is never a family's
        base, because the question slides must not reveal anything.
    """
    name = (getattr(layout, "name", "") or "").lower()
    return PptCreatorConstants.ANSWERED_LAYOUT_SUFFIX in name


def answered_reference(catalog, layout):
    """The answer-revealed twin of a template quiz layout, when the template has one.

    Args:
        catalog: The template catalog.
        layout: A quiz `LayoutInfo` such as "Quiz-1".

    Returns:
        The `LayoutInfo` named "<name>-Answered" on the same master, or None.
    """
    if layout is None:
        return None
    wanted = f"{(layout.name or '').strip().lower()}-{PptCreatorConstants.ANSWERED_LAYOUT_SUFFIX}"
    return next((l for l in catalog.layouts if l.master_index == layout.master_index
                 and (l.name or "").strip().lower() == wanted), None)


def directly_usable(layout) -> bool:
    """True when a layout can host this slide's content as it is.

    A layout with baked sample copy (`fixed_texts`) would show that copy behind the real
    content, so it never qualifies. Otherwise it must offer a content placeholder to
    fill or a free region to compose in.

    Args:
        layout: A `LayoutInfo`.

    Returns:
        True when slides can be built on it directly.
    """
    if layout.fixed_texts:
        return False
    has_content = any(p.role in _CONTENT_ROLES for p in layout.placeholders)
    return has_content or bool(layout.canvas_capable and layout.free_region)


def can_host(layout) -> bool:
    """True when a family's slides may be built on `layout` (a base or an established one).

    Looser than `directly_usable`: an ordinary layout's decorative text (agenda numbers,
    a closing line) is part of its design. Only a family *reference* - a Quiz or FAQ
    layout drawn with sample copy - is refused, because that copy would show through.

    Args:
        layout: A `LayoutInfo`, or None.

    Returns:
        True when it can carry a slide.
    """
    if layout is None:
        return False
    if family_of(layout) is not None and layout.fixed_texts:
        return False
    return bool(layout.placeholders) or bool(layout.canvas_capable and layout.free_region)


def host_layout_for(catalog, reference):
    """A clean layout to reproduce a reference design on.

    Preference: the reference's own master (same title position, same logo), then the
    content master; canvas-capable, no baked copy, not itself a family reference; the
    one whose title sits where the reference's does; then the one with the fewest
    inherited graphics and the largest free region.

    Args:
        catalog: The template catalog.
        reference: The reference design's `LayoutInfo`.

    Returns:
        The host `LayoutInfo`, or None when no layout qualifies.
    """
    host = _best_host(catalog, reference)
    if host is None:
        log.warning("no clean canvas layout to reproduce %s on; the leader keeps its "
                    "contract assignment", reference.key)
        return None
    log.info("reference %s %r will be reproduced on %s %r (master M%d, free region %s)",
             reference.key, reference.name, host.key, host.name, host.master_index,
             host.free_region.to_list())
    return host


def _best_host(catalog, reference):
    """`host_layout_for`'s choice, without its log lines."""
    ref_title = next((p for p in reference.placeholders if p.role == "title"), None)

    def title_offset(layout) -> float:
        title = next((p for p in layout.placeholders if p.role == "title"), None)
        if ref_title is None or title is None:
            return 99.0
        return (abs(title.left - ref_title.left) + abs(title.top - ref_title.top)
                + abs(title.width - ref_title.width))

    for master in (reference.master_index, catalog.preferred_master):
        candidates = [l for l in catalog.layouts
                      if l.master_index == master and l.canvas_capable
                      and l.free_region is not None and not l.fixed_texts
                      and family_of(l) is None]
        if candidates:
            candidates.sort(key=lambda l: (round(title_offset(l), 2), len(l.decor_rects),
                                           -(l.free_region.width * l.free_region.height),
                                           l.layout_index))
            return candidates[0]
    return None


def reference_design(catalog, reference, host=None) -> list[dict]:
    """The reference's artwork, minus the master furniture every layout there draws.

    Args:
        catalog: The template catalog.
        reference: The reference design's `LayoutInfo`.
        host: The layout it will be reproduced on; when None, the best host is used
            only to recognise the furniture.

    Returns:
        `catalog.layout_artwork` entries. The logo and title rule the master's layouts
        all draw are dropped, so the model is never asked to draw a second copy of
        either, nor to transplant them onto a layout that does not have them.
    """
    artwork = catalog_mod.layout_artwork(catalog, reference.key)
    furniture = host or _best_host(catalog, reference)
    if furniture is None:
        return artwork
    inherited = [r.to_list() for r in furniture.decor_rects]

    def drawn_by_host(entry: dict) -> bool:
        return any(all(abs(a - b) <= _SAME_RECT_TOLERANCE_IN
                       for a, b in zip(entry["rect_in"], rect)) for rect in inherited)

    kept = [entry for entry in artwork if not drawn_by_host(entry)]
    log.debug("reference %s: %d of %d element(s) describe the design (%d are master "
              "furniture, as drawn by %s)", reference.key, len(kept), len(artwork),
              len(artwork) - len(kept), furniture.key)
    return kept


# --------------------------------------------------------------------------------- #
# leader and follower assignments                                                     #
# --------------------------------------------------------------------------------- #

@dataclass
class FamilyAssignment:
    """How one QUIZ or FAQ slide is built this turn."""
    #: "leader" designs and registers the family layout; "follower" reuses it.
    role: str
    slide_type: str
    #: Fields to override on the slide's `SlideCodeBrief`.
    brief_updates: dict = field(default_factory=dict)
    #: The established layout (follower) or the upstream directive (leader), if any.
    family: FamilyLayout | None = None
    #: The SAMPLE.pptx reference design's `LayoutInfo`, when one is in play.
    reference: object = None
    #: Its artwork, element by element, for the prompt.
    reference_shapes: list = field(default_factory=list)
    #: "reproduce" (follow it faithfully), "inspire" (reference only) or "".
    reference_usage: str = ""
    #: What the leader will register as the family's `source`.
    source: str = "generated"


def _reframe(brief: SlideCodeBrief, layout, catalog) -> dict:
    """The frame for this slide on `layout`, when the brief carried one at all.

    A brief built without a contract assignment has no frame, and that stays true here:
    the family must not change what the frame does, only which layout it sits on.
    """
    if not brief.frame:
        return {}
    from app.genai.ppt.design_contract import frame_for_layout

    return {"frame": frame_for_layout(brief.classification, catalog.slide_width,
                                      catalog.slide_height, brief.slide_number, layout)}


def _base_updates(brief: SlideCodeBrief, layout, mode: str, catalog,
                  free_region: list | None = None) -> dict:
    """Brief overrides that move a slide onto `layout` in `mode`."""
    if mode == "canvas":
        region = free_region or (layout.free_region.to_list()
                                 if layout.free_region is not None else None)
        if region is None:
            mode = "layout"
    else:
        region = None
    updates = {"layout_key": layout.key, "mode": mode,
               "free_region": list(region) if region else None}
    updates.update(_reframe(brief, layout, catalog))
    return updates


def plan_leader(catalog, brief: SlideCodeBrief,
                directive: FamilyLayout | None) -> FamilyAssignment:
    """Decide how the slide that establishes its family's layout is built.

    The template's predefined layout is the default and creativity decides how binding
    it is: LOW always reproduces it, MED reproduces it unless a custom design was asked
    for (planner instructions or an upstream description), and HIGH treats it as
    inspiration only. An upstream directive naming a usable base layout wins outright.

    Args:
        catalog: The template catalog.
        brief: The leader's brief.
        directive: An unestablished upstream entry for this family, or None.

    Returns:
        The leader's assignment.
    """
    slide_type = brief.slide_type
    reference = template_family_layout(catalog, slide_type)
    custom = bool(brief.planner_special_instructions
                  or (directive is not None and directive.description))
    if reference is None:
        use_template = False
    elif brief.creativity_level == CreativityLevels.LOW:
        use_template = True
    elif brief.creativity_level == CreativityLevels.MED:
        use_template = not custom
    else:
        use_template = False
    assignment = FamilyAssignment(role="leader", slide_type=slide_type,
                                  family=directive)

    # 1. An upstream directive that names a real, usable layout is explicit intent.
    if directive is not None and directive.layout_key:
        base = catalog.get(directive.layout_key)
        if can_host(base):
            assignment.brief_updates = _base_updates(
                brief, base, directive.mode or "layout", catalog,
                directive.free_region)
            assignment.source = "upstream"
            log.info("slide %d leads %s on the upstream layout %s", brief.slide_number,
                     slide_type, base.key)
            return assignment
        log.warning("slide %d: upstream %s layout %r is %s; ignoring the base layout "
                    "and keeping its description", brief.slide_number, slide_type,
                    directive.layout_key,
                    "unknown" if base is None else "not usable as a base")

    # 2. The template's own family layout, used or reproduced.
    if use_template:
        assignment.source = "template"
        assignment.reference = reference
        if directly_usable(reference):
            mode = ("layout" if any(p.role in _CONTENT_ROLES
                                    for p in reference.placeholders) else "canvas")
            assignment.brief_updates = _base_updates(brief, reference, mode, catalog)
            log.info("slide %d leads %s on the template's own layout %s (%s mode)",
                     brief.slide_number, slide_type, reference.key, mode)
            return assignment
        host = host_layout_for(catalog, reference)
        if host is not None:
            assignment.brief_updates = _base_updates(brief, host, "canvas", catalog)
        assignment.reference_shapes = reference_design(catalog, reference, host)
        assignment.reference_usage = "reproduce"
        log.info("slide %d leads %s by reproducing the template's %s %r on %s "
                 "(creativity %s)", brief.slide_number, slide_type, reference.key,
                 reference.name, host.key if host else brief.layout_key or "(unassigned)",
                 brief.creativity_level)
        return assignment

    # 3. A new design; the template's layout, if any, is inspiration only.
    assignment.source = "upstream" if directive is not None else "generated"
    if reference is not None:
        assignment.reference = reference
        assignment.reference_shapes = reference_design(catalog, reference)
        assignment.reference_usage = "inspire"
    log.info("slide %d leads %s with a new design on %s (creativity %s, custom "
             "request %s, template reference %s)", brief.slide_number, slide_type,
             brief.layout_key or "(unassigned)", brief.creativity_level, custom,
             reference.key if reference is not None else "none")
    return assignment


def plan_follower(catalog, brief: SlideCodeBrief,
                  family: FamilyLayout) -> FamilyAssignment | None:
    """Move a slide onto its family's established layout.

    Args:
        catalog: The template catalog.
        brief: The follower's brief.
        family: The established family layout.

    Returns:
        The follower's assignment, or None when the established layout no longer
        resolves to anything buildable (the caller then leads instead).
    """
    layout = catalog.get(family.layout_key)
    if not can_host(layout):
        log.warning("slide %d: the established %s layout %r is %s; this slide will "
                    "re-establish the family", brief.slide_number, brief.slide_type,
                    family.layout_key,
                    "gone from the template" if layout is None else
                    "a reference design that cannot host content")
        return None
    updates = _base_updates(brief, layout, family.mode or "layout", catalog,
                            family.free_region)
    log.info("slide %d follows the %s layout established by slide %d on %s (%s mode)",
             brief.slide_number, brief.slide_type, family.established_at_slide,
             layout.key, updates["mode"])
    return FamilyAssignment(role="follower", slide_type=brief.slide_type,
                            brief_updates=updates, family=family,
                            source=family.source)


# --------------------------------------------------------------------------------- #
# upstream directives                                                                 #
# --------------------------------------------------------------------------------- #

def fingerprint(entry: FamilyLayout) -> str:
    """A stable hash of what an upstream caller asked for, to recognise it again."""
    payload = json.dumps({"layout_key": entry.layout_key, "mode": entry.mode,
                          "free_region": entry.free_region,
                          "description": entry.description,
                          "exemplar_code": entry.exemplar_code}, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def merge_upstream(stored: DeckDesignContext, upstream: DeckDesignContext | None
                   ) -> tuple[DeckDesignContext, list[str]]:
    """Apply an upstream caller's family directives to the stored context.

    A directive is applied once: the same directive arriving again (every slide of a
    turn, every later turn) is recognised by its fingerprint and changes nothing, so
    an established layout is never redesigned by repetition. A *different* directive
    is an explicit request and replaces the family's entry - the only way an
    established family is ever redesigned.

    Args:
        stored: The session's current context.
        upstream: The context an upstream agent passed in, or None.

    Returns:
        (the merged context, the slide types whose entry changed).
    """
    if upstream is None:
        return stored, []
    merged, changed = stored, []
    for slide_type in SlideTypes.FAMILIES:
        incoming = upstream.get(slide_type)
        if incoming is None:
            continue
        stamp = fingerprint(incoming)
        current = merged.get(slide_type)
        if current is not None and current.upstream_fingerprint == stamp:
            continue
        if current is not None and current.established:
            log.warning("an upstream agent replaced the %s layout established by "
                        "slide %d (%s) with a new directive; the family will be "
                        "redesigned", slide_type, current.established_at_slide,
                        current.layout_key)
        adopted = incoming.model_copy(update={
            "slide_type": slide_type, "upstream_fingerprint": stamp,
            "source": incoming.source or "upstream"})
        merged = merged.with_layout(slide_type, adopted)
        changed.append(slide_type)
        log.info("upstream %s directive adopted: %s", slide_type, _describe(adopted))
    return merged, changed


# --------------------------------------------------------------------------------- #
# coordinating one turn                                                               #
# --------------------------------------------------------------------------------- #

class DeckDesignRegistry:
    """The deck design context for one turn, shared by every parallel slide build.

    Created lazily by the agent on the first QUIZ or FAQ slide, so a deck of NORMAL
    slides never reads or writes any of this. Thread-safe.
    """

    def __init__(self, session_id: str) -> None:
        """
        Args:
            session_id: Owning session, for persistence and logs.
        """
        self.session_id = session_id
        self._state_lock = threading.Lock()
        self._loaded = False
        self._context = DeckDesignContext()
        # One lock per family: a QUIZ leader never holds up FAQ slides.
        self._family_locks = {t: threading.Lock() for t in SlideTypes.FAMILIES}

    def _ensure_loaded(self) -> None:
        """Read the stored context once per turn; the caller holds `_state_lock`."""
        if not self._loaded:
            self._context = load(self.session_id)
            self._loaded = True

    def context(self) -> DeckDesignContext:
        """The current context, loading it on first use."""
        with self._state_lock:
            self._ensure_loaded()
            return self._context

    def family(self, slide_type: str) -> FamilyLayout | None:
        """The current entry for one family, established or not."""
        return self.context().get(slide_type)

    def adopt_upstream(self, upstream: DeckDesignContext | None) -> None:
        """Merge an upstream agent's directives in, persisting when anything changed.

        Args:
            upstream: The brief's `deck_design_context`, or None.
        """
        if upstream is None:
            return
        with self._state_lock:
            self._ensure_loaded()
            merged, changed = merge_upstream(self._context, upstream)
            if not changed:
                return
            self._context = merged
            snapshot = merged
        save(self.session_id, snapshot)

    def acquire(self, slide_type: str, slide_number: int) -> FamilyLayout | None:
        """Wait until this slide may build, and say whether it leads or follows.

        Args:
            slide_type: "QUIZ" or "FAQ".
            slide_number: For the log.

        Returns:
            The established layout - the slide follows it and holds no lock - or None,
            in which case the slide LEADS: it holds the family lock and must call
            `release` (after `register`, when it succeeded).

        Raises:
            RunCancelledError / RunFencedError: When the run is stopped while waiting.
        """
        lock = self._family_locks[slide_type]
        started = time.monotonic()
        while True:
            entry = self.family(slide_type)
            if entry is not None and entry.established:
                self._log_wait(slide_type, slide_number, started)
                return entry
            if lock.acquire(timeout=PptCreatorConstants.FAMILY_LOCK_POLL_SECONDS):
                entry = self.family(slide_type)
                if entry is not None and entry.established:
                    # The leader finished while this slide was waiting for the lock.
                    lock.release()
                    self._log_wait(slide_type, slide_number, started)
                    return entry
                log.info("slide %d leads the %s family: no layout is established yet",
                         slide_number, slide_type)
                return None
            # Still waiting on the leader; a stopped run must not keep slides parked.
            run_control.checkpoint()

    @staticmethod
    def _log_wait(slide_type: str, slide_number: int, started: float) -> None:
        """Note how long a follower waited for its leader, when it waited at all."""
        waited = time.monotonic() - started
        if waited >= 0.5:
            log.info("slide %d waited %.1fs for the %s layout to be established",
                     slide_number, waited, slide_type)

    def release(self, slide_type: str) -> None:
        """Release a family lock taken by `acquire` (leaders only)."""
        self._family_locks[slide_type].release()

    def discard(self, slide_type: str, entry: FamilyLayout) -> None:
        """Forget an established layout that can no longer host slides.

        Only clears the family when `entry` is still its current layout, so a layout
        another slide has just re-established is never thrown away.

        Args:
            slide_type: "QUIZ" or "FAQ".
            entry: The layout found to be unusable.
        """
        with self._state_lock:
            self._ensure_loaded()
            if self._context.get(slide_type) != entry:
                return
            self._context = self._context.with_layout(slide_type, None)
            snapshot = self._context
        log.warning("%s layout %s (slide %d) discarded: it can no longer host slides",
                    slide_type, entry.layout_key, entry.established_at_slide)
        save(self.session_id, snapshot)

    def register(self, layout: FamilyLayout) -> None:
        """Record a newly established family layout and persist it.

        Args:
            layout: The established entry; its `slide_type` names the family.
        """
        with self._state_lock:
            self._ensure_loaded()
            self._context = self._context.with_layout(layout.slide_type, layout)
            snapshot = self._context
        log.info("%s layout established by slide %d on %s (%s, tier %d, %d-char "
                 "exemplar)", layout.slide_type, layout.established_at_slide,
                 layout.layout_key, layout.source, layout.tier,
                 len(layout.exemplar_code))
        save(self.session_id, snapshot)

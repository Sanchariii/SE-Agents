"""Tools that create, read and mutate the session's DeckPlan — the planning agent's
primary means of acting on a user's plan/edit/enrich/remove/classification instruction."""

from difflib import get_close_matches

from langchain_core.tools import tool

from app.core.handlers.session_store import session_store
from app.config.exceptions import ConflictError
from app.config.constants import (
    CreativityLevels, PlannerConstants, PptConstants, SlideTypes,
)
from app.core.models.deck_model import (
    ContentBundle, DeckDraft, DeckPlan, SlidePlan, _normalize_choice,
)
from app.genai.agents import plan_rules, planner
from app.genai.agents.brief import DeckBrief
from app.genai.ppt.template import catalog as catalog_mod, registry
from app.utilities import events_util as events
from app.utilities.logger_util import get_logger

log = get_logger(__name__)


def _layout_menu(plan_dict: dict | None) -> str:
    classification = registry.normalize((plan_dict or {}).get("classification"))
    return catalog_mod.for_classification(classification).prompt_block()


def _outline(session_id: str) -> str:
    """The stored plan's current numbering, so the agent never reuses stale numbers.

    Args:
        session_id: Session whose plan is listed.

    Returns:
        One "N. [kind] title" line per slide.
    """
    plan_dict, _ = session_store.get_plan(session_id)
    return "\n".join(f"  {s['slide_number']}. [{s.get('kind', 'content')}] {s['title']}"
                     for s in (plan_dict or {}).get("slides", []))


def remove_slides(session_id: str, target_slides: set[int]) -> dict:
    """Drop the targeted slide(s) from plan/content/draft and renumber the rest. Plain
    function (not a tool) so it can be unit-tested and reused without an LLM in the loop."""
    plan_dict, _ = session_store.get_plan(session_id)
    if not plan_dict:
        return {"removed": [], "remaining": 0, "draft_touched": False,
                "reason": "no plan exists yet"}

    plan = DeckPlan(**plan_dict)
    remaining = [s for s in plan.slides if s.slide_number not in target_slides]
    if len(remaining) == len(plan.slides):
        return {"removed": [], "remaining": len(plan.slides), "draft_touched": False,
                "reason": "no matching slide numbers"}

    renumber = {s.slide_number: i for i, s in enumerate(remaining, start=1)}
    for i, s in enumerate(remaining, start=1):
        s.slide_number = i
    plan.slides = remaining
    session_store.set_plan(session_id, plan.model_dump(),
                           status=planner.status_after_edit(session_id))

    content = session_store.get_content(session_id)
    if content:
        bundle = ContentBundle(**content)
        bundle.slides = [s for s in bundle.slides if s.slide_number in renumber]
        for s in bundle.slides:
            s.slide_number = renumber[s.slide_number]
        bundle.plan = plan.model_dump()
        session_store.save_content(session_id, bundle.model_dump(mode="json"))

    draft_touched = False
    stored_draft = session_store.get_draft(session_id)
    if stored_draft:
        draft = DeckDraft(**stored_draft)
        draft.slides = [s for s in draft.slides if s.slide_number in renumber]
        for s in draft.slides:
            s.slide_number = renumber[s.slide_number]
        session_store.save_draft(session_id, draft.model_dump(mode="json"))
        draft_touched = True

    events.publish(session_id, "agent_step", agent="planner",
                   message=f"Removed slide(s) {sorted(target_slides)}; "
                           f"{len(remaining)} slide(s) left.")
    return {"removed": sorted(target_slides), "remaining": len(remaining),
            "draft_touched": draft_touched}


def insert_slides(session_id: str, new_slides: list[SlidePlan],
                  after_slide: int | None = None) -> dict:
    """Insert slides into the plan and shift everything below them.

    Plain function (not a tool) so it can be unit-tested and reused without an LLM.
    Content and draft are renumbered alongside the plan; without that, every existing
    slide below the insertion point would silently inherit its neighbour's copy.

    Args:
        session_id: Session whose plan is being edited.
        new_slides: Slides to insert, in order.
        after_slide: Insert after this 1-based slide number; 0 inserts at the front and
            None appends to the end.

    Returns:
        added (the new slide numbers), total, and a reason when nothing was inserted
        (including when the plan would exceed the hard slide ceiling).
    """
    plan_dict, _ = session_store.get_plan(session_id)
    if not plan_dict:
        return {"added": [], "total": 0, "reason": "no plan exists yet"}
    if not new_slides:
        return {"added": [], "total": len(plan_dict.get("slides", [])),
                "reason": "no slides were drafted"}

    plan = DeckPlan(**plan_dict)
    old_numbers = {id(s): s.slide_number for s in plan.slides}

    closing_start = next(
        (index for index, slide in enumerate(plan.slides) if slide.kind in {"takeaways", "thanks"}),
        len(plan.slides), )

    at = (closing_start if after_slide is None else max(0, min(int(after_slide), closing_start)) )

    # Body slides go before the closing section; a new thank-you slide always ends the deck.
    body = [s for s in new_slides if s.kind != "thanks"]
    closers = [s for s in new_slides if s.kind == "thanks"]
    ordered = plan.slides[:at] + body + plan.slides[at:] + closers

    # Hard ceiling, checked before anything is renumbered or stored: new quiz
    # questions also bring their answer slides with them.
    refusal = plan_rules.ceiling_violation(
        plan.model_copy(update={"slides": ordered}),
        DeckBrief.load(session_id).answer_placement(), session_id)
    if refusal:
        return {"added": [], "total": len(plan.slides), "reason": refusal}

    renumber: dict[int, int] = {}
    added: list[int] = []
    for position, slide in enumerate(ordered, start=1):
        if id(slide) in old_numbers:
            renumber[old_numbers[id(slide)]] = position
        else:
            added.append(position)
        slide.slide_number = position
    plan.slides = ordered
    session_store.set_plan(session_id, plan.model_dump(),
                           status=planner.status_after_edit(session_id))

    content = session_store.get_content(session_id)
    if content:
        bundle = ContentBundle(**content)
        bundle.slides = [s for s in bundle.slides if s.slide_number in renumber]
        for s in bundle.slides:
            s.slide_number = renumber[s.slide_number]
        bundle.plan = plan.model_dump()
        session_store.save_content(session_id, bundle.model_dump(mode="json"))

    stored_draft = session_store.get_draft(session_id)
    if stored_draft:
        draft = DeckDraft(**stored_draft)
        draft.slides = [s for s in draft.slides if s.slide_number in renumber]
        for s in draft.slides:
            s.slide_number = renumber[s.slide_number]
        session_store.save_draft(session_id, draft.model_dump(mode="json"))

    events.publish(session_id, "agent_step", agent="planner",
                   message=f"Added slide(s) {added}; {len(ordered)} slide(s) total.")
    return {"added": added, "total": len(ordered)}


def move_slides(session_id: str, target_slides: set[int], after_slide: int) -> dict:
    """Move slides to a new position, keeping their content. Deterministic - no LLM.

    Content and draft are renumbered with the plan, so each moved slide keeps its own
    copy. A quiz question and its answer slide move together, whichever half is named,
    and a slide is never dropped between a question and its answer.

    Args:
        session_id: Session whose plan is being edited.
        target_slides: Current numbers of the slides to move; they keep their order.
        after_slide: Current number of the slide to place them after; 0 moves them to
            the front of the deck.

    Returns:
        moved (the slides' new numbers) and total, plus a reason when nothing moved.
    """
    plan_dict, _ = session_store.get_plan(session_id)
    if not plan_dict:
        return {"moved": [], "reason": "no plan exists yet"}
    plan = DeckPlan(**plan_dict)
    by_number = {s.slide_number: s for s in plan.slides}

    # A quiz pair is one unit: naming either half moves both.
    pair_ids = {by_number[n].quiz_id for n in target_slides
                if n in by_number and by_number[n].slide_type == SlideTypes.QUIZ
                and by_number[n].quiz_id}
    moving = [s for s in plan.slides
              if s.slide_number in target_slides or (s.quiz_id and s.quiz_id in pair_ids)]
    if not moving:
        return {"moved": [], "reason": "no matching slide numbers"}
    moving_ids = {id(s) for s in moving}
    if not 0 <= after_slide <= len(plan.slides):
        return {"moved": [], "reason": f"after_slide must be 0-{len(plan.slides)}"}
    anchor = by_number.get(after_slide)
    if anchor is not None and id(anchor) in moving_ids:
        return {"moved": [], "reason": "after_slide is one of the slides being moved"}
    # Landing after a question means landing after its answer, never between them.
    if anchor is not None and anchor.is_quiz_question:
        anchor = next((s for s in plan.slides if s.is_quiz_answer
                       and s.quiz_id == anchor.quiz_id), anchor)

    rest = [s for s in plan.slides if id(s) not in moving_ids]
    at = 0 if anchor is None else next(i for i, s in enumerate(rest) if s is anchor) + 1
    ordered = rest[:at] + moving + rest[at:]
    renumber = {s.slide_number: position for position, s in enumerate(ordered, start=1)}
    if all(old == new for old, new in renumber.items()):
        return {"moved": [], "reason": "those slides are already in that position"}

    origins = [s.slide_number for s in moving]
    for position, slide in enumerate(ordered, start=1):
        slide.slide_number = position
    plan.slides = ordered
    session_store.set_plan(session_id, plan.model_dump(),
                           status=planner.status_after_edit(session_id))
    plan_rules.renumber_stored(session_id, renumber, plan)

    moved = [s.slide_number for s in moving]
    log.info("moved slide(s) %s to %s", origins, moved, extra={"session_id": session_id})
    events.publish(session_id, "agent_step", agent="planner",
                   message=f"Moved slide(s) to position(s) {moved}.")
    return {"moved": moved, "total": len(ordered)}


def apply_slide_layout(session_id: str, target_slides: set[int], layout_name: str) -> dict:
    """Point slides at a different company-template layout. Deterministic — no LLM.

    Args:
        session_id: Session whose plan is being edited.
        target_slides: Slide numbers to change.
        layout_name: A layout NAME from the template's menu.

    Returns:
        updated (the slide numbers changed), the resolved layout name, and on failure a
        reason plus the available names.
    """
    plan_dict, _ = session_store.get_plan(session_id)
    if not plan_dict:
        return {"updated": [], "reason": "no plan exists yet"}

    classification = registry.normalize(plan_dict.get("classification"))
    catalog = catalog_mod.for_classification(classification)
    names = [l.name for l in catalog.content_layouts] or [l.name for l in catalog.layouts]

    wanted = (layout_name or "").strip()
    resolved = next((n for n in names if n.lower() == wanted.lower()), None)
    if resolved is None:
        close = get_close_matches(wanted, names, n=1, cutoff=0.6)
        resolved = close[0] if close else None
    if resolved is None:
        return {"updated": [], "reason": f"no layout matches {wanted!r}", "menu": names}

    plan = DeckPlan(**plan_dict)
    updated = []
    for slide in plan.slides:
        if slide.slide_number in target_slides and slide.layout_hint != resolved:
            slide.layout_hint = resolved
            updated.append(slide.slide_number)
    if updated:
        session_store.set_plan(session_id, plan.model_dump(),
                               status=planner.status_after_edit(session_id))
        events.publish(session_id, "agent_step", agent="planner",
                       message=f"Slide(s) {updated} moved to layout '{resolved}'.")
    return {"updated": updated, "layout": resolved}


def apply_slide_style(session_id: str, target_slides: set[int],
                      creativity_level: str = "") -> dict:
    """Set a per-slide creativity override. Deterministic - no LLM.

    "INHERIT" clears an override so the slide follows the deck's level again; "" leaves
    that level alone. A quiz answer slide follows its question, so a target naming one
    is moved onto the question (the plan rules copy the override to the answer).

    Args:
        session_id: Session whose plan is being edited.
        target_slides: Slide numbers to change.
        creativity_level: LOW | MED | HIGH | INHERIT | "".

    Returns:
        updated (the slide numbers changed), plus a reason when nothing changed.
    """
    plan_dict, _ = session_store.get_plan(session_id)
    if not plan_dict:
        return {"updated": [], "reason": "no plan exists yet"}
    plan = DeckPlan(**plan_dict)
    targets = planner.question_targets(plan, set(target_slides))

    def resolve(value: str, field: str) -> str | None:
        text = str(value or "").strip().upper()
        if not text:
            return None
        if text == "INHERIT":
            return ""
        return _normalize_choice(text, CreativityLevels.ALL, "",
                                 CreativityLevels.ALIASES, field) or None

    wanted = {"creativity_level": resolve(creativity_level, "creativity_level")}
    wanted = {k: v for k, v in wanted.items() if v is not None}
    if not wanted:
        return {"updated": [], "reason": "no valid level given (use LOW, MED, HIGH or "
                                         "INHERIT)"}
    updated = []
    for slide in plan.slides:
        if slide.slide_number in targets and any(
                getattr(slide, k) != v for k, v in wanted.items()):
            for key, value in wanted.items():
                setattr(slide, key, value)
            updated.append(slide.slide_number)
    if updated:
        session_store.set_plan(session_id, plan.model_dump(),
                               status=planner.status_after_edit(session_id))
        events.publish(session_id, "agent_step", agent="planner",
                       message=f"Slide(s) {updated} style updated.")
    return {"updated": updated} if updated else {
        "updated": [], "reason": "those slides already use that style"}


def fix_slide_consistency(session_id: str, target_slides: set[int],
                          deck_level: str) -> dict:
    """Fix slides the user called inconsistent with the rest of the deck. No LLM.

    The rule, per slide, by its resolved creativity:

    * HIGH in a HIGH deck: it sits on the High Creativity master like its neighbours,
      so it stays HIGH and is redesigned there to match them.
    * HIGH in a deck that is not HIGH: it is the odd one out, so it is set to MED.
    * MED or LOW: it is redesigned at its level to match its neighbours.

    Every target gets a new `design_revision`, so it is redrawn (not rewritten) even
    when nothing else about it changed. A quiz answer slide follows its question.

    Args:
        session_id: Session whose plan is being edited.
        target_slides: The slides the user named.
        deck_level: The deck's effective creativity level.

    Returns:
        updated (the slide numbers changed), demoted and matched (which rule applied).
    """
    plan_dict, _ = session_store.get_plan(session_id)
    if not plan_dict:
        return {"updated": [], "reason": "no plan exists yet"}
    plan = DeckPlan(**plan_dict)
    targets = planner.question_targets(plan, set(target_slides))
    demoted, matched = [], []
    for slide in plan.slides:
        # Answer slides are redrawn with their question (the plan rules copy overrides).
        if slide.slide_number not in targets and not (
                slide.is_quiz_answer and any(q.quiz_id == slide.quiz_id
                                             and q.slide_number in targets
                                             for q in plan.slides if q.is_quiz_question)):
            continue
        resolved = slide.creativity_level or deck_level
        if resolved == CreativityLevels.HIGH and deck_level != CreativityLevels.HIGH:
            slide.creativity_level = CreativityLevels.MED
            demoted.append(slide.slide_number)
        else:
            slide.match_neighbours = True
            matched.append(slide.slide_number)
        slide.design_revision += 1
    updated = sorted(demoted + matched)
    log.info("consistency fix on slides %s (deck %s): demoted to MED %s, redesigned to "
             "match neighbours %s", sorted(targets), deck_level, demoted, matched,
             extra={"session_id": session_id})
    if updated:
        session_store.set_plan(session_id, plan.model_dump(),
                               status=planner.status_after_edit(session_id))
        events.publish(session_id, "agent_step", agent="planner",
                       message=f"Slide(s) {updated} will be redesigned to match the "
                               f"rest of the deck.")
    return {"updated": updated, "demoted": demoted, "matched": matched}


def reset_deck_design(session_id: str) -> dict:
    """The whole deck looks inconsistent: clear HIGH overrides and redesign every slide.

    The deck's own level is set to MED by the caller (it lives on the brief). Per-slide
    HIGH overrides are cleared so every slide follows it; LOW and MED overrides are the
    user's own asks and stay. Every slide gets a new `design_revision`, so the whole
    deck is redrawn fresh, bypassing the cache, with its content unchanged.

    Args:
        session_id: Session whose plan is being edited.

    Returns:
        cleared (slides whose HIGH override was removed) and redesigned (all slides).
    """
    plan_dict, _ = session_store.get_plan(session_id)
    if not plan_dict:
        return {"cleared": [], "redesigned": [], "reason": "no plan exists yet"}
    plan = DeckPlan(**plan_dict)
    cleared = []
    for slide in plan.slides:
        if slide.creativity_level == CreativityLevels.HIGH:
            slide.creativity_level = ""
            cleared.append(slide.slide_number)
        slide.match_neighbours = False
        slide.design_revision += 1
    redesigned = [s.slide_number for s in plan.slides]
    session_store.set_plan(session_id, plan.model_dump(),
                           status=planner.status_after_edit(session_id))
    log.info("deck design reset: HIGH overrides cleared on %s; %d slide(s) will be "
             "redesigned", cleared, len(redesigned), extra={"session_id": session_id})
    events.publish(session_id, "agent_step", agent="planner",
                   message="The whole deck will be redesigned at Medium creativity for a "
                           "consistent look.")
    return {"cleared": cleared, "redesigned": redesigned}


def apply_family_style(session_id: str, slide_type: str, description: str) -> dict:
    """Set how every QUIZ or FAQ slide of the deck looks. Deterministic - no LLM.

    A family's slides share one layout that is designed once and then reused on every
    later turn, so a per-slide instruction cannot change it. A new description here
    becomes a new design directive: the next build discards the established layout,
    designs a fresh one for the family and keeps every slide's content.

    Args:
        session_id: Session whose plan is being edited.
        slide_type: "QUIZ" or "FAQ" (any case).
        description: The look, in a sentence or two.

    Returns:
        updated (bool), the family's slide_type and its slide numbers, plus a reason
        when nothing changed.
    """
    plan_dict, _ = session_store.get_plan(session_id)
    if not plan_dict:
        return {"updated": False, "reason": "no plan exists yet"}
    family = (slide_type or "").strip().upper()
    if family not in SlideTypes.FAMILIES:
        return {"updated": False,
                "reason": f"slide_type must be one of {', '.join(SlideTypes.FAMILIES)}"}
    text = (description or "").strip()[:PlannerConstants.MAX_FAMILY_STYLE_CHARS].rstrip()
    if not text:
        return {"updated": False, "reason": "describe how the slides should look"}

    plan = DeckPlan(**plan_dict)
    members = [s.slide_number for s in plan.slides if s.slide_type == family]
    if not members:
        return {"updated": False, "reason": f"the deck has no {family} slides"}
    if plan.family_styles.get(family) == text:
        return {"updated": False, "slide_type": family, "slides": members,
                "reason": "that look is already applied; describe a different one to "
                          "redesign it"}

    plan.family_styles = {**plan.family_styles, family: text}
    session_store.set_plan(session_id, plan.model_dump(),
                           status=planner.status_after_edit(session_id))
    log.info("%s family look set (%d chars) for slide(s) %s", family, len(text), members,
             extra={"session_id": session_id})
    events.publish(session_id, "agent_step", agent="planner",
                   message=f"{family} slide(s) {members} will be redesigned.")
    return {"updated": True, "slide_type": family, "slides": members}


# Common ways users name a supported language without using its canonical name.
_LANGUAGE_ALIASES = {"chinese": "Mandarin", "mandarin chinese": "Mandarin",
                     "simplified chinese": "Mandarin", "brazilian portuguese": "Portuguese",
                     "castilian": "Spanish", "deutsch": "German"}
_ALL_WORDS = frozenset({"all", "every", "everything", "supported"})
# Qualifiers users attach to a language name ("English only", "in French please").
_NOISE_WORDS = frozenset({"only", "just", "in", "please", "language", "languages",
                          "version", "translation", "the", "and"})


def _clean(name: str) -> str:
    """Strip qualifiers so 'English only' still matches 'English'."""
    words = name.lower().replace("-", " ").replace(",", " ").split()
    kept = [w for w in words if w not in _NOISE_WORDS]
    return " ".join(kept or words)


def normalize_languages(requested: list[str]) -> tuple[list[str], list[str]]:
    """Map free-text language names onto the supported set.

    Args:
        requested: Names as the user or model wrote them; "all" means every supported
            language.

    Returns:
        (resolved, unsupported) — resolved keeps canonical spelling with English first,
        since English is the original the others are translated from.
    """
    supported = list(PptConstants.SUPPORTED_LANGUAGES)
    by_key = {name.lower(): name for name in supported}

    resolved: list[str] = []
    unsupported: list[str] = []
    for raw in requested or []:
        name = (raw or "").strip()
        if not name:
            continue
        key = _clean(name)
        # "all"/"every" anywhere means every language; no supported name contains them,
        # so matching on whole words cannot collide with a real one.
        if _ALL_WORDS.intersection(key.split()):
            return supported, []

        hit = by_key.get(key) or by_key.get(_LANGUAGE_ALIASES.get(key, "").lower())
        if hit is None:
            close = get_close_matches(key, list(by_key), n=1, cutoff=0.75)
            hit = by_key[close[0]] if close else None
        if hit is None:
            unsupported.append(name)
        elif hit not in resolved:
            resolved.append(hit)

    resolved.sort(key=lambda name: name != PptConstants.DEFAULT_LANGUAGE)
    return resolved, unsupported


def apply_languages(session_id: str, languages: list[str]) -> dict:
    """Set which languages the deck is published in. Deterministic — no LLM.

    Only the plan changes, so no slide is rewritten: the next publish simply renders
    the existing draft once per language.

    Args:
        session_id: Session whose plan is being edited.
        languages: Requested language names, or ["all"].

    Returns:
        languages (the resolved set), changed, any unsupported names, and on failure a
        reason plus the supported names.
    """
    plan_dict, _ = session_store.get_plan(session_id)
    if not plan_dict:
        return {"languages": [], "changed": False, "reason": "no plan exists yet"}

    resolved, unsupported = normalize_languages(languages)
    if not resolved:
        return {"languages": [], "changed": False, "unsupported": unsupported,
                "reason": "no supported language was named",
                "supported": list(PptConstants.SUPPORTED_LANGUAGES)}

    if plan_dict.get("languages") == resolved:
        return {"languages": resolved, "changed": False, "unsupported": unsupported}

    plan_dict["languages"] = resolved
    session_store.set_plan(session_id, plan_dict, status=planner.status_after_edit(session_id))
    events.publish(session_id, "agent_step", agent="planner",
                   message=f"Deck will be published in: {', '.join(resolved)}.")
    return {"languages": resolved, "changed": True, "unsupported": unsupported}


def build_plan_tools(session_id: str, user_message: str = "") -> list:
    """Plan CRUD tool instances bound to one session.

    Args:
        session_id: Session whose plan the tools read and write.
        user_message: The user's raw turn. The agent passes its own paraphrase to the
            tools, so the one-shot revise/add prompts are given the user's real words
            too, to record their asks in each slide's `user_instructions`.
    """

    @tool
    def read_current_plan() -> str:
        """Read the session's current deck plan (title, audience, objective, slides with
        their key points and layout hints), if one exists. Call this first to decide
        whether the user's instruction needs a brand-new plan or a targeted edit."""
        plan_dict, status = session_store.get_plan(session_id)
        if not plan_dict:
            return "No plan exists yet for this session."
        plan = DeckPlan(**plan_dict)
        lines = [f"Plan status: {status}", f"Title: {plan.title}",
                f"Audience: {plan.audience}", f"Objective: {plan.objective}",
                f"Classification: {plan.classification}",
                f"Languages: {', '.join(plan.languages)}"]
        lines.extend(f"{family} slides look: {style}"
                     for family, style in plan.family_styles.items())
        lines.append("Slides:")
        for s in plan.slides:
            tag = ""
            if s.slide_type == "QUIZ":
                tag = " [QUIZ answer slide - mirrors its question]" if s.is_quiz_answer \
                    else " [QUIZ question]"
            elif s.slide_type == "FAQ":
                tag = " [FAQ]"
            style = (f"creativity={s.creativity_level}" if s.creativity_level else "")
            lines.append(f"  {s.slide_number}. {s.title}{tag} "
                        f"(layout_hint={s.layout_hint or 'none'}"
                        f"{', ' + style if style else ''})")
            lines.extend(f"     - {kp}" for kp in s.key_points)
            if s.user_instructions:
                lines.append(f"     user asked: {s.user_instructions}")
            if s.images_requested or s.allow_image_reuse:
                lines.append(f"     images: requested={s.images_requested}, "
                             f"reuse allowed={s.allow_image_reuse}")
            if s.planner_special_instructions:
                lines.append(f"     internal instructions: "
                             f"{planner._own_instructions(s) or '(answer placement only)'}")
        return "\n".join(lines)

    @tool
    def create_deck_plan(instruction: str) -> str:
        """Create a brand-new deck plan, or fully replan an existing one, from the user's
        instruction. Grounds the plan in the session's source documents and assigns each
        slide a layout_hint from the company template's layout menu. Use this for a new
        deck request, or when the instruction changes the deck's overall structure/scope
        rather than just a few slides. May come back asking a clarifying question instead
        of producing a plan if essential information (audience, scope, goal) is missing —
        relay that question to the user rather than guessing."""

        # Log only the size: the instruction is user-authored content and must not
        # reach pod logs / telemetry verbatim.
        log.info("tool create_deck_plan called (instruction: %d chars)", len(instruction or ""),
                 extra={"session_id": session_id})
        # The menu is what lets the LAYOUT RULE give every new slide a layout_hint.
        output = planner.build_plan(session_id, instruction,
                                    layout_menu=_layout_menu(
                                        session_store.get_plan(session_id)[0]))
        if output.action == "clarify":
            questions = "\n".join(f"- {q}" for q in output.questions)
            return f"Need more information before planning:\n{output.reply}\n{questions}".strip()
        if output.action == "plan" and output.plan:
            return (f"Plan created: '{output.plan.title}' — {len(output.plan.slides)} slide(s).\n"
                    + "\n".join(f"  {s.slide_number}. {s.title} "
                                f"(layout_hint={s.layout_hint or 'none'})"
                                for s in output.plan.slides))
        return output.reply or "No plan change was made."

    @tool
    def revise_deck_slides(instruction: str, target_slides: list[int]) -> str:
        """Apply a targeted edit/enrich instruction to specific slide numbers of the
        existing plan, leaving every other slide untouched. Grounds the revision in the
        layout menu so layout_hint stays consistent with the template. Use this for
        'update slide N', 'add more depth to slide N', or similar targeted instructions —
        never for a request that changes the deck's overall scope or slide count, never
        to move or reorder slides (use move_deck_slides), and never for how QUIZ or FAQ
        slides LOOK (use set_family_style for that)."""
        plan_dict, _ = session_store.get_plan(session_id)
        if not plan_dict:
            return "No plan exists yet — use create_deck_plan first."
        try:
            plan = planner.revise_slides(session_id, instruction, set(target_slides),
                                         layout_menu=_layout_menu(plan_dict),
                                         user_message=user_message)
        except ConflictError as exc:
            return f"Nothing revised: {exc.detail}"
        wanted = planner.question_targets(DeckPlan(**plan_dict), set(target_slides))
        touched = {s.slide_number: s for s in plan.slides if s.slide_number in wanted}
        if not touched:
            return f"No slide(s) matched target_slides={target_slides}."
        return "Revised slide(s):\n" + "\n".join(
            f"  {n}. {s.title} (layout_hint={s.layout_hint or 'none'})"
            for n, s in touched.items())

    @tool
    def remove_deck_slides(target_slides: list[int]) -> str:
        """Remove one or more slides from the plan (and the draft/content if they already
        exist) by slide_number, renumbering the rest. Use this when the user explicitly
        asks to drop or delete a slide."""
        result = remove_slides(session_id, set(target_slides))
        if not result["removed"]:
            return f"Nothing removed: {result['reason']}"
        note = " The rendered draft was updated too." if result["draft_touched"] else ""
        return (f"Removed slide(s) {result['removed']}; {result['remaining']} slide(s) left."
                f"{note}\nDeck is now numbered:\n{_outline(session_id)}")

    @tool
    def move_deck_slides(target_slides: list[int], after_slide: int) -> str:
        """Move slides to a new position in the deck, keeping their content. Use this for
        any request to move, reorder, re-sequence or rearrange slides ("put the quizzes
        in order", "move slide 9 after slide 4"). target_slides keep their relative
        order and land directly after `after_slide` (0 = the front of the deck). A quiz
        question and its answer slide always move together. Never use
        revise_deck_slides to reorder: it rewrites slides in place and cannot move them.
        Numbers change after every move, so for several moves use the renumbered outline
        each call returns."""
        result = move_slides(session_id, set(target_slides), after_slide)
        if not result["moved"]:
            return f"Nothing moved: {result['reason']}."
        return (f"Moved to position(s) {result['moved']}.\n"
                f"Deck is now numbered:\n{_outline(session_id)}")

    @tool
    def set_deck_classification(instruction: str) -> str:
        """Detect and apply an explicit classification change (public, internal,
        confidential, restricted, co-branded) named in the instruction. Deterministic —
        does nothing if no classification is explicitly named, so it's safe to call
        speculatively on any instruction."""
        changed = planner.apply_classification(session_id, instruction)
        return (f"Classification set to {changed}." if changed
                else "No explicit classification named; nothing changed.")

    @tool
    def add_deck_slides(instruction: str, after_slide: int = 0) -> str:
        """Add one or more NEW slides to the existing plan without replanning the deck.
        Use this whenever the user asks to add, insert or append a slide about something
        — never call create_deck_plan for that, because replanning discards every
        existing slide. `after_slide` is the slide number to insert after; 0 appends to
        the end of the deck. Grounds the new slide in the session's source documents."""
        plan_dict, _ = session_store.get_plan(session_id)
        if not plan_dict:
            return "No plan exists yet — use create_deck_plan first."
        plan = DeckPlan(**plan_dict)
        drafted = planner.draft_new_slides(
            session_id, instruction, layout_menu=_layout_menu(plan_dict),
            existing_titles=[s.title for s in plan.slides], user_message=user_message)
        if not drafted:
            return "Could not draft a slide for that instruction; nothing was added."

        # position = len(plan.slides) if not after_slide else after_slide
        # result = insert_slides(session_id, drafted, after_slide=position or None)
        result = insert_slides(session_id,drafted,after_slide=after_slide or None,)

        if not result["added"]:
            return f"Nothing added: {result.get('reason')}"
        # insert_slides renumbers the drafted slides in place to their final positions
        return ("Added slide(s):\n" + "\n".join(
            f"  {s.slide_number}. {s.title} (layout_hint={s.layout_hint or 'none'})"
            for s in sorted(drafted, key=lambda s: s.slide_number))
            + f"\nDeck is now numbered:\n{_outline(session_id)}")

    @tool
    def set_slide_layout(target_slides: list[int], layout_name: str) -> str:
        """Change which company-template layout specific slides use, keeping their
        content. Use this for 'put slide 5 on two columns', 'make slide 3 a timeline',
        or any request about the slide's shape rather than its words. Call
        get_available_layouts first so layout_name is a real name from the menu."""
        result = apply_slide_layout(session_id, set(target_slides), layout_name)
        if not result["updated"]:
            reason = result.get("reason", "those slides already use that layout")
            menu = result.get("menu")
            return f"No layout change: {reason}." + (
                f" Available: {', '.join(menu[:8])}" if menu else "")
        return f"Slide(s) {result['updated']} now use layout '{result['layout']}'."

    @tool
    def set_deck_languages(languages: list[str]) -> str:
        """Change which languages the deck is published in, keeping all of its content.
        Use this whenever the user asks for the deck in other languages — "also give me
        French and German", "generate it in all languages", "English only from now on".
        Pass ["all"] for every supported language. Never call create_deck_plan for a
        language change: the slides do not need rewriting, they are only re-rendered and
        translated, so replanning would throw away approved content for nothing."""
        result = apply_languages(session_id, languages)
        if not result["languages"]:
            supported = ", ".join(result.get("supported") or [])
            return (f"No language change: {result.get('reason')}."
                    + (f" Supported: {supported}." if supported else ""))

        note = ""
        if result.get("unsupported"):
            note = (f" Not supported, so ignored: {', '.join(result['unsupported'])}"
                    f" (supported: {', '.join(PptConstants.SUPPORTED_LANGUAGES)}).")
        if not result["changed"]:
            return f"Already publishing in {', '.join(result['languages'])}.{note}"
        return f"Deck will be published in {', '.join(result['languages'])}.{note}"

    @tool
    def set_slide_style(target_slides: list[int], creativity_level: str = "") -> str:
        """Change how much creative design freedom SPECIFIC slides get, keeping the rest
        of the deck at its own level. Use this for 'slide 2 can be more creative' or
        'keep slide 6 close to the template'. Levels are LOW, MED or HIGH; INHERIT puts
        a slide back on the deck's level. A request for more or less DETAIL ('make slide
        4 more detailed', 'less text on slide 6') is a content change: use
        revise_deck_slides. For a change to the WHOLE deck's creativity do not call
        this - it is handled before planning."""
        result = apply_slide_style(session_id, set(target_slides), creativity_level)
        if not result["updated"]:
            return f"No style change: {result.get('reason')}."
        return f"Slide(s) {result['updated']} style updated."

    @tool
    def set_family_style(slide_type: str, description: str) -> str:
        """Change how ALL the deck's QUIZ slides (or all its FAQ slides) LOOK, keeping
        their questions and answers. Use this for 'change the quiz layout', 'the quiz
        slides look boring', 'make the FAQ slides look like cards'. Every slide of a
        family shares one layout, so it is redesigned for the whole family at once,
        answer slides included; never use revise_deck_slides for a look-only change.
        slide_type is QUIZ or FAQ; description is the wanted look in a sentence or two,
        in the user's terms (e.g. "bold game-show style with 2x2 answer tiles")."""
        result = apply_family_style(session_id, slide_type, description)
        if not result["updated"]:
            return f"No look change: {result.get('reason')}."
        return (f"{result['slide_type']} slide(s) {result['slides']} will be redesigned "
                f"with the new look; their content is kept.")

    return [read_current_plan, create_deck_plan, revise_deck_slides,
            remove_deck_slides, move_deck_slides, set_deck_classification,
            add_deck_slides, set_slide_layout, set_slide_style, set_family_style,
            set_deck_languages]
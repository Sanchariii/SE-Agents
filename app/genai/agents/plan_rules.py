"""Deterministic plan rules applied after every planning turn.

The planner decides WHAT the deck says; these rules make the parts that must never
depend on a model hold every time:

* role slides (cover, agenda, takeaways, thanks) are NORMAL slides;
* every quiz question has a stable `quiz_id`;
* a question whose answer is revealed on a slide is followed directly by exactly one
  answer slide, and a question whose answer stays in the notes has none - orphaned or
  misplaced answer slides are dropped or moved back next to their question;
* removing a question removes its answer; removing only the answer slide is an
  explicit opt-out that sticks (the question's `reveal_answer` becomes False);
* each question's internal instructions end with where its answer goes;
* no stored plan exceeds `SlideCountConstants.MAX_PLAN_SLIDES` slides, answer slides
  included: every plan change projects its total through these rules first and is
  rejected (the previous plan kept) when it would go past the ceiling.

The rules renumber the stored content and draft alongside the plan, exactly as the
insert/remove tools do, so no slide inherits a neighbour's copy.
"""

import uuid

from app.config.constants import (
    AnswerPlacements, FaqConstants, QuizRoles, SlideCountConstants, SlideTypes,
)
from app.core.handlers.session_store import session_store
from app.core.models.deck_model import ContentBundle, DeckDraft, DeckPlan, SlidePlan
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

#: Marks the instruction line these rules own, so it is replaced rather than repeated.
PLACEMENT_MARKER = "ANSWER PLACEMENT:"
_PLACEMENT_LINES = {
    AnswerPlacements.SLIDE: (f"{PLACEMENT_MARKER} keep the correct answer off this "
                             f"slide; it goes in the speaker notes and is revealed on "
                             f"the next slide."),
    AnswerPlacements.NOTES: (f"{PLACEMENT_MARKER} keep the correct answer off this "
                             f"slide; it goes in the speaker notes only."),
}


def own_instructions(slide: SlidePlan) -> str:
    """A slide's planner instructions without the answer-placement line these rules own.

    Args:
        slide: Any plan slide.

    Returns:
        The planner's own lines only.
    """
    return "\n".join(line for line in slide.planner_special_instructions.splitlines()
                     if not line.strip().startswith(PLACEMENT_MARKER))


def _with_placement(instructions: str, placement: str) -> str:
    """Replace this module's answer-placement line in a slide's instructions.

    Args:
        instructions: The planner's text, possibly carrying an older placement line.
        placement: `AnswerPlacements.SLIDE` or `.NOTES`.

    Returns:
        The planner's own lines followed by the current placement line.
    """
    own = [line for line in instructions.splitlines()
           if line.strip() and not line.strip().startswith(PLACEMENT_MARKER)]
    return "\n".join(own + [_PLACEMENT_LINES[placement]])


def _answer_for(question: SlidePlan) -> SlidePlan:
    """The answer-reveal slide that follows a question.

    It never runs the content agent: its content is the question's own, with the
    correct choice revealed, so it carries no key points or retrieval queries.

    Args:
        question: The quiz question slide.

    Returns:
        A new answer slide (numbered later).
    """
    return SlidePlan(
        slide_number=question.slide_number, kind="content", title=question.title,
        slide_type=SlideTypes.QUIZ, quiz_role=QuizRoles.ANSWER,
        quiz_id=question.quiz_id, layout_hint=question.layout_hint,
        visual=question.visual, creativity_level=question.creativity_level,
        user_instructions=question.user_instructions)


def _reveals(question: SlidePlan, placement: str) -> bool:
    """Whether a question gets an answer slide.

    Args:
        question: The question slide.
        placement: The deck's answer placement.

    Returns:
        The question's explicit choice, else the deck's placement.
    """
    if question.reveal_answer is not None:
        return question.reveal_answer
    return placement == AnswerPlacements.SLIDE


def _opt_outs(before: DeckPlan | None, after: DeckPlan) -> set[str]:
    """Quiz ids whose answer slide was removed this turn while the question stayed.

    Args:
        before: The plan at the start of the turn, or None to skip detection (a
            style change, which is not a user removal).
        after: The plan after the planner's tools ran.

    Returns:
        The quiz ids that opted out of an answer slide.
    """
    if before is None:
        return set()
    had_answer = {s.quiz_id for s in before.slides if s.is_quiz_answer and s.quiz_id}
    has_answer = {s.quiz_id for s in after.slides if s.is_quiz_answer and s.quiz_id}
    questions = {s.quiz_id for s in after.slides if s.is_quiz_question and s.quiz_id}
    return (had_answer - has_answer) & questions


def normalize_plan(plan: DeckPlan, placement: str,
                   before: DeckPlan | None = None) -> tuple[DeckPlan, dict[int, int]]:
    """Apply every rule to a plan, without touching storage.

    Args:
        plan: The plan to normalise (not mutated).
        placement: The deck's answer placement from the brief.
        before: The plan before this turn, for opt-out detection; None skips it.

    Returns:
        The normalised plan, and old slide_number -> new slide_number for every slide
        that survived (new answer slides have no entry).
    """
    plan = plan.model_copy(deep=True)
    opted_out = _opt_outs(before, plan)

    # 1. Types and roles.
    for slide in plan.slides:
        if slide.kind != "content":
            if slide.slide_type != SlideTypes.NORMAL:
                log.info("slide %d is a %s slide; role slides are always NORMAL",
                         slide.slide_number, slide.kind)
            slide.slide_type, slide.quiz_role = SlideTypes.NORMAL, QuizRoles.NONE
            slide.reveal_answer = None
        if slide.slide_type != SlideTypes.QUIZ:
            slide.quiz_role, slide.quiz_id, slide.reveal_answer = QuizRoles.NONE, "", None
            continue
        if slide.quiz_role == QuizRoles.NONE:
            slide.quiz_role = QuizRoles.QUESTION
        if slide.quiz_role == QuizRoles.QUESTION:
            slide.quiz_id = slide.quiz_id or uuid.uuid4().hex[:8]
            if slide.quiz_id in opted_out:
                slide.reveal_answer = False
            slide.planner_special_instructions = _with_placement(
                slide.planner_special_instructions,
                AnswerPlacements.SLIDE if _reveals(slide, placement)
                else AnswerPlacements.NOTES)
        else:
            slide.reveal_answer = None

    # 2. Rebuild the order: every answer directly after its question, or nowhere.
    answers = {s.quiz_id: s for s in plan.slides if s.is_quiz_answer and s.quiz_id}
    ordered: list[SlidePlan] = []
    added = dropped = 0
    used: set[str] = set()
    for slide in plan.slides:
        if slide.is_quiz_answer:
            continue
        ordered.append(slide)
        if slide.is_quiz_question and _reveals(slide, placement):
            answer = answers.get(slide.quiz_id)
            if answer is None:
                answer = _answer_for(slide)
                added += 1
            else:
                # The answer mirrors its question's title and look.
                answer.title, answer.layout_hint = slide.title, slide.layout_hint
                answer.creativity_level = slide.creativity_level
                answer.user_instructions = slide.user_instructions
                answer.key_points, answer.content_needs = [], []
            used.add(slide.quiz_id)
            ordered.append(answer)
    dropped = sum(1 for s in plan.slides if s.is_quiz_answer and s.quiz_id not in used)

    renumber: dict[int, int] = {}
    old_numbers = {id(s): s.slide_number for s in plan.slides}
    for position, slide in enumerate(ordered, start=1):
        if id(slide) in old_numbers:
            renumber[old_numbers[id(slide)]] = position
        slide.slide_number = position
    plan.slides = ordered

    faq_slides = sum(1 for s in plan.slides if s.slide_type == SlideTypes.FAQ)
    if faq_slides > FaqConstants.MAX_SLIDES:
        log.warning("the plan has %d FAQ slides (guideline: at most %d unless the user "
                    "asked for more)", faq_slides, FaqConstants.MAX_SLIDES)
    if added or dropped or opted_out:
        log.info("plan rules: %d answer slide(s) added, %d dropped, %d opt-out(s); "
                 "placement %s", added, dropped, len(opted_out), placement)
    return plan, renumber


def projected_total(plan: DeckPlan, placement: str) -> int:
    """How many slides a plan has once these rules add or drop its answer slides.

    Args:
        plan: A candidate plan (not mutated).
        placement: The deck's answer placement from the brief.

    Returns:
        The total slide count the stored plan would have.
    """
    normalized, _ = normalize_plan(plan, placement)
    return len(normalized.slides)


def ceiling_reply(total: int) -> str:
    """Explain a rejected plan change that would exceed the hard slide ceiling.

    The planner agent relays it to the user in its own words; triage translates it.

    Args:
        total: The slide total the change would have produced.

    Returns:
        A short, polite explanation with the real numbers and what the user can do.
    """
    limit = SlideCountConstants.MAX_PLAN_SLIDES
    return (f"Sorry, I couldn't make that change: the deck would grow to {total} slides, "
            f"and in the current configuration a deck can have at most {limit} slides, "
            f"quiz answer slides included. The previous plan is unchanged; asking for "
            f"fewer slides or keeping quiz answers in the speaker notes would fit.")


def ceiling_violation(plan: DeckPlan, placement: str, session_id: str = "") -> str:
    """Check a candidate plan against the hard slide ceiling before it is stored.

    Args:
        plan: The plan a tool is about to store.
        placement: The deck's answer placement from the brief.
        session_id: For the log line.

    Returns:
        "" when the plan fits; otherwise the polite refusal to give the user.
    """
    total = projected_total(plan, placement)
    if total <= SlideCountConstants.MAX_PLAN_SLIDES:
        return ""
    log.warning("plan change rejected: %d slides (with answer slides) exceeds the "
                "ceiling of %d; the previous plan is kept", total,
                SlideCountConstants.MAX_PLAN_SLIDES, extra={"session_id": session_id})
    return ceiling_reply(total)


def renumber_stored(session_id: str, renumber: dict[int, int], plan: DeckPlan) -> None:
    """Carry a renumbering onto the stored content and draft.

    Shared by every plan change that moves slides (these rules, and the move tool), so a
    moved slide keeps its own copy instead of inheriting whatever sat at its new number.

    Args:
        session_id: The session whose content/draft follow the plan.
        renumber: old slide_number -> new slide_number for surviving slides.
        plan: The renumbered plan, recorded on the content bundle.
    """
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


def apply(session_id: str, placement: str, before: DeckPlan | None = None) -> bool:
    """Normalise the stored plan in place, renumbering content and draft with it.

    Args:
        session_id: The session whose plan is normalised.
        placement: The deck's answer placement from the brief.
        before: The plan at the start of the turn (for opt-out detection), or None.

    Returns:
        True when the stored plan changed. False when nothing changed, or when the
        normalised plan would exceed the slide ceiling (it is then not stored).
    """
    plan_dict, status = session_store.get_plan(session_id)
    if not plan_dict:
        return False
    plan = DeckPlan(**plan_dict)
    normalized, renumber = normalize_plan(plan, placement, before)
    if normalized.model_dump() == plan.model_dump():
        return False
    # Backstop: every tool checks the ceiling before storing, so this should never fire.
    if len(normalized.slides) > SlideCountConstants.MAX_PLAN_SLIDES:
        log.error("plan rules would grow the plan to %d slides (ceiling %d); the stored "
                  "plan is left as it is", len(normalized.slides),
                  SlideCountConstants.MAX_PLAN_SLIDES, extra={"session_id": session_id})
        return False
    session_store.set_plan(session_id, normalized.model_dump(), status=status)
    moved = any(old != new for old, new in renumber.items())
    if moved or len(renumber) != len(plan.slides):
        renumber_stored(session_id, renumber, normalized)
    log.info("plan normalised: %d -> %d slide(s)", len(plan.slides),
             len(normalized.slides), extra={"session_id": session_id})
    return True

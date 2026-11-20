"""Turn one planning turn into a per-slide work order.

The planning agent mutates the stored DeckPlan through its tools; this module works out
what that mutation MEANS for each slide, so the PPT agent is told exactly what to do
instead of re-deriving it from a plan it can only partly see.

Derived from a before/after plan DIFF rather than from tool arguments, because tool
arguments do not survive renumbering: `add_deck_slides` never states which numbers it
created, and `revise_deck_slides(target_slides=[5])` issued before a removal in the same
turn refers to pre-removal numbering. The diff is taken after every tool has run, so it
sees the final truth whatever order the agent chose.
"""

from app.config.constants import SlideAgentConstants as S, SlideTypes
from app.core.models.deck_model import DeckPlan, SlidePlan, SlideTask
from app.genai.agents.plan_rules import own_instructions
from app.utilities.logger_util import get_logger

log = get_logger(__name__)


def strongest_op(*ops: str) -> str:
    """The op that does the most work, for when one slide is hit by two instructions.

    Args:
        ops: Candidate ops.

    Returns:
        The earliest of ``OP_PRECEDENCE`` present, else ``OP_KEEP``.
    """
    for candidate in S.OP_PRECEDENCE:
        if candidate in ops:
            return candidate
    return S.OP_KEEP


def _fingerprint(slide: SlidePlan) -> tuple:
    """Return the content and role fields that require a slide rebuild.

    The answer-placement line of the planner's instructions is left out: whether a
    question's answer is revealed on the next slide or only in the notes does not
    change what the question slide says.
    """
    return (
        slide.kind,
        slide.title.strip(),
        tuple(slide.key_points),
        tuple(slide.content_needs),
        slide.visual.strip(),
        slide.slide_type,
        slide.quiz_role,
        slide.user_instructions,
        own_instructions(slide),
        # An ask for images changes which candidates the writer picks.
        slide.images_requested,
    )


def _design_fingerprint(slide: SlidePlan) -> tuple:
    """The fields that change only how a slide is drawn, not what it says.

    `design_revision` and `match_neighbours` are set by a consistency fix: they make a
    slide be redrawn (OP_RELAYOUT) with its content untouched.
    """
    return (slide.layout_hint or "", slide.creativity_level, slide.design_revision,
            slide.match_neighbours)


def _restyled_families(before: DeckPlan, after: DeckPlan) -> set[str]:
    """The slide families (QUIZ, FAQ) whose deck-level look changed this turn.

    Args:
        before: The plan as it was at the start of the turn.
        after: The plan as it is now.

    Returns:
        The slide types whose `family_styles` entry was set, changed or cleared.
    """
    return {family for family in SlideTypes.FAMILIES
            if before.family_styles.get(family, "") != after.family_styles.get(family, "")}


def _content_outline(plan: DeckPlan) -> list[str]:
    """The ordered content titles that the agenda and takeaways slides are written from.

    Quiz answer slides are left out: they repeat their question's title.

    Args:
        plan: A deck plan.

    Returns:
        One stripped title per content slide, in deck order.
    """
    return [s.title.strip() for s in plan.slides
            if s.kind == "content" and not s.is_quiz_answer]


def _refresh_outline_slides(before: DeckPlan, after: DeckPlan,
                            tasks: list[SlideTask]) -> list[SlideTask]:
    """Rebuild the agenda and takeaways slides when the content outline changed.

    They are written only from the content slides' titles, so adding, removing,
    retitling or moving a content slide changes them. Rewriting or redesigning a slide's
    body does not: regenerating them then would only re-roll slides the user never asked
    to touch.

    Args:
        before: The plan as it was at the start of the turn.
        after: The plan as it is now.
        tasks: One task per slide of ``after``.

    Returns:
        The tasks, with outline role slides raised to a rewrite when the outline changed.
    """
    if _content_outline(before) == _content_outline(after):
        return tasks
    refreshed, out = [], []
    for task in tasks:
        if task.kind in S.OUTLINE_ROLE_KINDS:
            op = strongest_op(task.op, S.OP_REWRITE)
            if op != task.op:
                task = task.model_copy(update={"op": op, "reuse_chunks": True})
                refreshed.append(task.slide_number)
        out.append(task)
    if refreshed:
        log.info("content outline changed; refreshing role slide(s) %s", refreshed)
    return out


def _match(before: list[SlidePlan], after: list[SlidePlan]) -> dict[int, SlidePlan | None]:
    """Map each surviving slide to the slide it used to be.

    Title first, because slide_number shifts whenever a slide is inserted or removed and
    would otherwise report every slide below the change as rewritten.

    Args:
        before: Slides as they were at the start of the turn.
        after: Slides as they are now.

    Returns:
        new slide_number -> the slide it came from, or None when it is genuinely new.
    """
    pool = list(before)
    matched: dict[int, SlidePlan | None] = {}

    for slide in after:
        hit = next((b for b in pool if b.title.strip() == slide.title.strip()), None)
        if hit is not None:
            pool.remove(hit)
            matched[slide.slide_number] = hit

    # Whatever is left was retitled: match it on the number it used to hold, so a
    # retitled slide costs a rewrite rather than a full rebuild.
    for slide in after:
        if slide.slide_number in matched:
            continue
        hit = next((b for b in pool if b.slide_number == slide.slide_number), None)
        if hit is not None:
            pool.remove(hit)
        matched[slide.slide_number] = hit
    return matched


def derive_work_order(before: DeckPlan | None, after: DeckPlan | None,
                      replanned: bool = False, enrich_slides: set[int] | None = None,
                      instruction: str = "", feedback: list[str] | None = None
                      ) -> list[SlideTask]:
    """Work out what each surviving slide needs after a planning turn.

    Args:
        before: The plan as it was before the planning agent ran, if any.
        after: The plan as it is now.
        replanned: True when ``create_deck_plan`` ran, which invalidates every slide.
        enrich_slides: Slide numbers triage tagged 'enrich' — these need NEW retrieval,
            which is the only thing distinguishing an enrich from a rewrite.
        instruction: The user's message, carried onto actionable slides.
        feedback: Prior feedback to carry onto actionable slides.

    Returns:
        One SlideTask per slide in ``after``, in slide order. Removed slides are absent:
        the remove tool already applied them to plan, content and draft.
    """
    if after is None or not after.slides:
        return []

    enrich_slides = set(enrich_slides or ())
    feedback = list(feedback or [])

    # A full replan changes the deck's structure and narrative, so no previously
    # approved copy can be assumed still correct — everything is built again.
    if replanned or before is None or not before.slides:
        return [SlideTask(slide_number=s.slide_number,kind=s.kind, op=S.OP_CREATE,
                          instruction=instruction, feedback=feedback,
                          layout_hint=s.layout_hint, reuse_chunks=False)
                for s in after.slides]

    matched = _match(before.slides, after.slides)
    # A family's look lives on the plan, not on any slide, so a change to it shows up
    # in no slide's fingerprint - yet every slide of that family must be redesigned.
    restyled = _restyled_families(before, after)
    tasks: list[SlideTask] = []

    for slide in after.slides:
        origin = matched.get(slide.slide_number)
        number = slide.slide_number

        if origin is None:
            op, reuse = S.OP_CREATE, False
        elif _fingerprint(origin) != _fingerprint(slide):
            wanted_enrich = number in enrich_slides or origin.slide_number in enrich_slides
            op = S.OP_ENRICH if wanted_enrich else S.OP_REWRITE
            reuse = True
        elif (_design_fingerprint(origin) != _design_fingerprint(slide)
              or slide.slide_type in restyled):
            op, reuse = S.OP_RELAYOUT, True
        else:
            op, reuse = S.OP_KEEP, True

        actionable = op != S.OP_KEEP
        tasks.append(SlideTask(
            slide_number=number, kind=slide.kind, op=op,
            instruction=instruction if actionable else "",
            feedback=feedback if actionable else [],
            layout_hint=slide.layout_hint, reuse_chunks=reuse))

    tasks = _answers_follow_questions(after, tasks)
    tasks = _refresh_outline_slides(before, after, tasks)
    log.info("work order derived", extra={
        "slides": len(tasks),
        "ops": {op: sum(1 for t in tasks if t.op == op) for op in S.OPS
                if any(t.op == op for t in tasks)}})
    return tasks


def _answers_follow_questions(plan: DeckPlan, tasks: list[SlideTask]) -> list[SlideTask]:
    """Give every quiz answer slide the stronger of its own op and its question's.

    An answer slide has no content of its own - it is its question with the correct
    choice revealed - so whatever rebuilds the question must rebuild the answer too.

    Args:
        plan: The plan the tasks were derived from.
        tasks: One task per slide, in plan order.

    Returns:
        The tasks, with answer-slide ops raised where their question changed.
    """
    by_number = {t.slide_number: t for t in tasks}
    question_ops = {s.quiz_id: by_number[s.slide_number].op for s in plan.slides
                    if s.is_quiz_question and s.quiz_id and s.slide_number in by_number}
    out = []
    for task in tasks:
        slide = next((s for s in plan.slides if s.slide_number == task.slide_number), None)
        if slide is not None and slide.is_quiz_answer and slide.quiz_id in question_ops:
            op = strongest_op(task.op, question_ops[slide.quiz_id])
            if op != task.op:
                task = task.model_copy(update={"op": op})
        out.append(task)
    return out

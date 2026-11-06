"""Work-order derivation: the planner must say exactly what each slide needs.

Pure functions over plan snapshots — no store, no model, no infrastructure.
"""

from app.config.constants import QuizRoles, SlideAgentConstants as S, SlideTypes
from app.core.models.deck_model import DeckPlan, SlidePlan
from app.genai.agents.work_order import derive_work_order, strongest_op


def _slide(number, title, points=("a",), needs=("q",), layout="Title + Bullets"):
    return SlidePlan(slide_number=number, title=title, key_points=list(points),
                     content_needs=list(needs), layout_hint=layout)


def _plan(*slides):
    return DeckPlan(title="Deck", audience="engineers", slides=list(slides))


def _ops(tasks):
    return {t.slide_number: t.op for t in tasks}


def test_a_first_build_creates_every_slide():
    after = _plan(_slide(1, "A"), _slide(2, "B"))
    assert _ops(derive_work_order(None, after)) == {1: S.OP_CREATE, 2: S.OP_CREATE}


def test_a_replan_rebuilds_everything_even_where_titles_match():
    """A replan changes the deck's structure, so no approved copy can be trusted."""
    before = _plan(_slide(1, "A"), _slide(2, "B"))
    tasks = derive_work_order(before, before, replanned=True)
    assert set(_ops(tasks).values()) == {S.OP_CREATE}
    assert all(t.reuse_chunks is False for t in tasks)


def test_an_untouched_turn_keeps_every_slide():
    plan = _plan(_slide(1, "A"), _slide(2, "B"))
    assert set(_ops(derive_work_order(plan, plan)).values()) == {S.OP_KEEP}


def test_changed_key_points_are_a_rewrite():
    before = _plan(_slide(1, "A"), _slide(2, "B"))
    after = _plan(_slide(1, "A"), _slide(2, "B", points=("a", "b")))
    assert _ops(derive_work_order(before, after)) == {1: S.OP_KEEP, 2: S.OP_REWRITE}


def test_the_enrich_hint_is_what_separates_enrich_from_rewrite():
    """Both leave the plan looking identical; only triage knows new retrieval is wanted."""
    before = _plan(_slide(1, "A"))
    after = _plan(_slide(1, "A", needs=("q", "deeper")))
    assert _ops(derive_work_order(before, after))[1] == S.OP_REWRITE
    assert _ops(derive_work_order(before, after, enrich_slides={1}))[1] == S.OP_ENRICH


def test_a_layout_only_change_is_a_relayout():
    before = _plan(_slide(1, "A", layout="Title + Bullets"))
    after = _plan(_slide(1, "A", layout="Three columns black heading"))
    tasks = derive_work_order(before, after)
    assert tasks[0].op == S.OP_RELAYOUT
    assert tasks[0].layout_hint == "Three columns black heading"


def test_an_inserted_slide_is_created_and_its_neighbours_are_kept():
    """The whole point of the diff: inserting must not rewrite everything below it."""
    before = _plan(_slide(1, "A"), _slide(2, "B"), _slide(3, "C"))
    after = _plan(_slide(1, "A"), _slide(2, "B"), _slide(3, "New"), _slide(4, "C"))
    assert _ops(derive_work_order(before, after)) == {
        1: S.OP_KEEP, 2: S.OP_KEEP, 3: S.OP_CREATE, 4: S.OP_KEEP}


def test_a_removal_keeps_the_survivors_despite_renumbering():
    before = _plan(_slide(1, "A"), _slide(2, "B"), _slide(3, "C"))
    after = _plan(_slide(1, "A"), _slide(2, "C"))
    assert set(_ops(derive_work_order(before, after)).values()) == {S.OP_KEEP}


def test_a_removed_slide_gets_no_task():
    before = _plan(_slide(1, "A"), _slide(2, "B"))
    after = _plan(_slide(1, "A"))
    assert [t.slide_number for t in derive_work_order(before, after)] == [1]


def test_a_retitled_slide_is_a_rewrite_not_a_rebuild():
    before = _plan(_slide(1, "Old title"))
    after = _plan(_slide(1, "New title"))
    assert _ops(derive_work_order(before, after))[1] == S.OP_REWRITE


def test_a_new_slide_starts_from_no_chunks():
    before = _plan(_slide(1, "A"))
    after = _plan(_slide(1, "A"), _slide(2, "New"))
    created = next(t for t in derive_work_order(before, after) if t.op == S.OP_CREATE)
    assert created.reuse_chunks is False


def test_the_instruction_rides_only_on_actionable_slides():
    before = _plan(_slide(1, "A"), _slide(2, "B"))
    after = _plan(_slide(1, "A"), _slide(2, "B", points=("a", "b")))
    tasks = {t.slide_number: t
             for t in derive_work_order(before, after, instruction="tighten it")}
    assert tasks[1].instruction == "" and tasks[2].instruction == "tighten it"


def test_op_precedence_picks_the_most_work():
    assert strongest_op(S.OP_KEEP, S.OP_ENRICH, S.OP_REWRITE) == S.OP_ENRICH
    assert strongest_op(S.OP_RELAYOUT, S.OP_KEEP) == S.OP_RELAYOUT
    assert strongest_op() == S.OP_KEEP


def test_an_empty_plan_yields_no_work():
    assert derive_work_order(None, None) == []
    assert derive_work_order(None, _plan()) == []


# --------------------------------------------------------------------------- #
# Agenda / takeaways and quiz-family looks                                     #
# --------------------------------------------------------------------------- #

def _quiz(number, title, role, quiz_id="q1", **fields):
    return SlidePlan(slide_number=number, title=title, slide_type=SlideTypes.QUIZ,
                     quiz_role=role, quiz_id=quiz_id, key_points=["a"], **fields)


def _quiz_deck(family_styles=None, quiz_note=""):
    """Cover, agenda, a content slide, a quiz pair and takeaways, as a training deck has."""
    return DeckPlan(title="Deck", audience="engineers", family_styles=family_styles or {},
                    slides=[SlidePlan(slide_number=1, kind="cover", title="Deck"),
                            SlidePlan(slide_number=2, kind="agenda", title="Agenda"),
                            _slide(3, "Architecture"),
                            _quiz(4, "Quiz 1", QuizRoles.QUESTION,
                                  planner_special_instructions=quiz_note),
                            _quiz(5, "Quiz 1", QuizRoles.ANSWER),
                            SlidePlan(slide_number=6, kind="takeaways",
                                      title="Key Takeaways")])


def test_editing_a_slide_body_leaves_the_agenda_and_takeaways_alone():
    """The reported bug: a quiz-only edit also rebuilt the agenda and takeaways."""
    before = _quiz_deck()
    after = _quiz_deck(quiz_note="LAYOUT: 2x2 answer tiles")
    assert _ops(derive_work_order(before, after)) == {
        1: S.OP_KEEP, 2: S.OP_KEEP, 3: S.OP_KEEP, 4: S.OP_REWRITE, 5: S.OP_REWRITE,
        6: S.OP_KEEP}


def test_a_retitled_content_slide_refreshes_the_agenda_and_takeaways():
    before = _quiz_deck()
    after = _quiz_deck()
    after.slides[2] = _slide(3, "Seven-Layer Architecture")
    ops = _ops(derive_work_order(before, after))
    assert ops[2] == ops[6] == S.OP_REWRITE
    assert ops[1] == S.OP_KEEP                      # the cover is not outline-derived


def test_an_added_content_slide_refreshes_the_agenda_and_takeaways():
    before = _quiz_deck()
    after = _quiz_deck()
    after.slides.insert(3, _slide(4, "Protocols"))
    for number, slide in enumerate(after.slides, start=1):
        slide.slide_number = number
    ops = _ops(derive_work_order(before, after))
    assert ops[2] == ops[7] == S.OP_REWRITE and ops[4] == S.OP_CREATE


def test_a_removed_content_slide_refreshes_the_agenda_and_takeaways():
    before = _quiz_deck()
    after = _quiz_deck()
    del after.slides[2]
    for number, slide in enumerate(after.slides, start=1):
        slide.slide_number = number
    ops = _ops(derive_work_order(before, after))
    assert ops[2] == ops[5] == S.OP_REWRITE
    assert ops[3] == ops[4] == S.OP_KEEP


def test_a_new_quiz_look_redesigns_the_whole_family_and_nothing_else():
    """The family's look is deck-level, so no slide's own fields changed - yet both the
    question and its answer must be redesigned, with their content kept."""
    before = _quiz_deck()
    after = _quiz_deck(family_styles={"QUIZ": "bold game-show style"})
    tasks = {t.slide_number: t for t in derive_work_order(before, after,
                                                          instruction="less boring")}
    assert {n: t.op for n, t in tasks.items()} == {
        1: S.OP_KEEP, 2: S.OP_KEEP, 3: S.OP_KEEP, 4: S.OP_RELAYOUT, 5: S.OP_RELAYOUT,
        6: S.OP_KEEP}
    assert tasks[4].instruction == "less boring" and tasks[4].reuse_chunks is True


def test_an_unchanged_quiz_look_is_not_a_redesign():
    plan = _quiz_deck(family_styles={"QUIZ": "bold game-show style"})
    assert set(_ops(derive_work_order(plan, plan)).values()) == {S.OP_KEEP}

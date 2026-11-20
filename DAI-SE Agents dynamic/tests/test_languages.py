"""Deck languages: a follow-up turn must be able to change them without a replan.

The failure this guards against: asking for "English only" first and "all languages"
later used to force `create_deck_plan`, which throws away every approved slide to change
one plan field.
"""

import pytest

from app.config.constants import PptConstants
from app.core.handlers.session_store import session_store
from app.core.models.deck_model import DeckPlan, SlidePlan
from app.genai.tools.plan_tools import apply_languages, normalize_languages
from app.genai.workflows import deck_workflow as wf

ALL = list(PptConstants.SUPPORTED_LANGUAGES)


@pytest.fixture
def session(request) -> str:
    """A session carrying an English-only plan, as a first turn would leave it."""
    sid = f"lang_{request.node.name}"
    plan = DeckPlan(title="Deck", audience="engineers", languages=["English"],
                    slides=[SlidePlan(slide_number=1, title="A"),
                            SlidePlan(slide_number=2, title="B")])
    session_store.set_plan(sid, plan.model_dump(), status="confirmed")
    return sid


# --------------------------------------------------------------------------- #
# normalize_languages                                                          #
# --------------------------------------------------------------------------- #

def test_all_means_every_supported_language():
    for word in ("all", "All languages", "every language"):
        assert normalize_languages([word]) == (ALL, [])


def test_a_whole_phrase_asking_for_all_is_understood():
    """The model is told to pass ['all'], but users phrase it as a sentence."""
    assert normalize_languages(["generate for all languages"]) == (ALL, [])
    assert normalize_languages(["every supported language"]) == (ALL, [])


def test_named_languages_keep_canonical_spelling():
    resolved, unsupported = normalize_languages(["french", "GERMAN"])
    assert resolved == ["French", "German"] and unsupported == []


def test_english_is_listed_first_because_it_is_the_original():
    resolved, _ = normalize_languages(["Japanese", "English", "French"])
    assert resolved[0] == "English"


def test_common_aliases_resolve():
    resolved, unsupported = normalize_languages(["Chinese", "Brazilian Portuguese"])
    assert resolved == ["Mandarin", "Portuguese"] and unsupported == []


def test_a_near_miss_still_resolves():
    resolved, _ = normalize_languages(["Spanish "])
    assert resolved == ["Spanish"]


def test_an_unsupported_language_is_reported_not_guessed():
    resolved, unsupported = normalize_languages(["English", "Klingon"])
    assert resolved == ["English"] and unsupported == ["Klingon"]


def test_duplicates_collapse():
    resolved, _ = normalize_languages(["French", "french", "FRENCH"])
    assert resolved == ["French"]


def test_nothing_requested_yields_nothing():
    assert normalize_languages([]) == ([], [])
    assert normalize_languages(["", "  "]) == ([], [])


# --------------------------------------------------------------------------- #
# apply_languages                                                              #
# --------------------------------------------------------------------------- #

def test_english_only_deck_can_later_go_multilingual(session):
    """The exact multi-turn case: English first, then 'generate for all languages'."""
    result = apply_languages(session, ["all"])
    assert result["changed"] is True
    assert result["languages"] == ALL
    stored, _ = session_store.get_plan(session)
    assert stored["languages"] == ALL


def test_the_slides_are_untouched_by_a_language_change(session):
    before, _ = session_store.get_plan(session)
    apply_languages(session, ["all"])
    after, _ = session_store.get_plan(session)
    assert after["slides"] == before["slides"]
    assert after["title"] == before["title"]


def test_asking_for_the_same_languages_changes_nothing(session):
    assert apply_languages(session, ["English"])["changed"] is False


def test_narrowing_back_to_english_works(session):
    apply_languages(session, ["all"])
    result = apply_languages(session, ["English only"])
    assert result["languages"] == ["English"] and result["changed"] is True


def test_an_unsupported_language_does_not_wipe_the_supported_ones(session):
    result = apply_languages(session, ["French", "Klingon"])
    assert result["languages"] == ["French"]
    assert result["unsupported"] == ["Klingon"]


def test_nothing_supported_named_leaves_the_plan_alone(session):
    result = apply_languages(session, ["Klingon"])
    assert result["changed"] is False
    assert result["supported"] == ALL
    stored, _ = session_store.get_plan(session)
    assert stored["languages"] == ["English"]


def test_no_plan_means_no_language_change():
    result = apply_languages("lang_session_without_a_plan", ["all"])
    assert result["changed"] is False and "no plan" in result["reason"]


# --------------------------------------------------------------------------- #
# Routing                                                                      #
# --------------------------------------------------------------------------- #

def test_a_language_turn_re_renders_rather_than_rebuilding():
    """It must reach `slides`, where every task is `keep`, then publish."""
    assert wf._after_planning({"actions_taken": ["language"],
                               "session_id": "s1"}) == "slides"


def test_a_language_change_produces_no_slide_work():
    """languages live on the plan, not the slides, so the diff must see no change."""
    from app.genai.agents.work_order import derive_work_order
    slides = [SlidePlan(slide_number=1, title="A"), SlidePlan(slide_number=2, title="B")]
    before = DeckPlan(title="Deck", languages=["English"], slides=slides)
    after = DeckPlan(title="Deck", languages=ALL, slides=slides)
    ops = {t.op for t in derive_work_order(before, after)}
    assert ops == {"keep"}

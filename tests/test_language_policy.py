"""New decks are English-only unless the user names other languages."""

import pytest

from app.config.constants import PptConstants
from app.core.models.deck_model import DeckPlan
from app.genai.agents.language_policy import LanguagePolicy

ALL = list(PptConstants.SUPPORTED_LANGUAGES)
policy = LanguagePolicy()


def test_plan_model_defaults_to_english():
    assert DeckPlan(title="Deck").languages == ["English"]


@pytest.mark.parametrize("text", [
    "", "Create a 5-slide training deck", "Crie um treinamento a partir deste guia",
    "build a deck about the safety guide"])
def test_no_language_named_gives_english_only(text):
    assert policy.for_new_plan(text) == ["English"]


def test_named_languages_are_used_in_supported_order():
    assert policy.for_new_plan("also in german and French") == ["French", "German"]


def test_aliases_resolve_to_supported_names():
    assert policy.for_new_plan("give me a Chinese version") == ["Mandarin"]
    assert policy.for_new_plan("Brazilian Portuguese please") == ["Portuguese"]


def test_all_languages_gives_every_supported_language():
    assert policy.for_new_plan("generate it in all languages") == ALL
    assert policy.for_new_plan("every supported language") == ALL


def test_unsupported_language_is_ignored():
    assert policy.for_new_plan("in Klingon") == ["English"]

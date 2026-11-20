"""Which languages a new deck is published in: English unless the user names others."""

import re

from app.config.constants import PptConstants

_ALIASES = {"chinese": "Mandarin", "brazilian portuguese": "Portuguese",
            "castilian": "Spanish", "deutsch": "German"}
_ALL = re.compile(r"\b(all|every)\s+(supported\s+)?languages?\b", re.IGNORECASE)


class LanguagePolicy:
    """Decides a plan's output languages in code, so the model cannot widen them."""

    def __init__(self) -> None:
        names = {n.lower(): n for n in PptConstants.SUPPORTED_LANGUAGES}
        names.update({alias.lower(): lang for alias, lang in _ALIASES.items()})
        # Longest first so "brazilian portuguese" wins over a shorter overlap.
        self._pattern = re.compile(
            r"\b(" + "|".join(sorted(map(re.escape, names), key=len, reverse=True)) + r")\b",
            re.IGNORECASE)
        self._names = names

    def for_new_plan(self, instruction: str) -> list[str]:
        """Languages explicitly asked for in `instruction`, else English only."""
        text = instruction or ""
        if _ALL.search(text):
            return list(PptConstants.SUPPORTED_LANGUAGES)
        named = {self._names[m.lower()] for m in self._pattern.findall(text)}
        # Keep supported-list order, with English first as the original.
        ordered = [l for l in PptConstants.SUPPORTED_LANGUAGES if l in named]
        return ordered or [PptConstants.DEFAULT_LANGUAGE]

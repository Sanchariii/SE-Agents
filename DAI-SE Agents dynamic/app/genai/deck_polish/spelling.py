"""US spelling: the ppt skill asks for strict US English.

One list, used by the polish rules (to fix) and the deck scorecard (to count). Only
words whose US form is certain are listed; ambiguous ones ("analyses" is also the plural
of "analysis") are left alone.
"""
from __future__ import annotations

import re

#: Stems that end in "is" in UK spelling and "iz" in US ("organis" -> "organiz").
_IZE_STEMS = ("organis", "recognis", "energis", "authoris", "minimis", "maximis",
              "prioritis", "standardis", "utilis", "summaris", "customis", "optimis",
              "visualis", "finalis", "initialis", "categoris", "emphasis", "familiaris",
              "synchronis", "neutralis", "stabilis", "normalis", "localis", "realis",
              "specialis", "memoris", "sanitis", "sterilis", "immobilis", "mobilis",
              "personalis", "digitalis", "centralis", "characteris", "apologis")
_IZE_ENDINGS = ("e", "ed", "es", "ing", "ation", "ations", "er", "ers")
_IZE = re.compile(r"\b(" + "|".join(_IZE_STEMS) + r")(" + "|".join(_IZE_ENDINGS) + r")\b",
                  re.IGNORECASE)

#: Whole words (any case) and their US form.
_WORDS = {
    "colour": "color", "colours": "colors", "coloured": "colored", "colouring": "coloring",
    "behaviour": "behavior", "behaviours": "behaviors", "behavioural": "behavioral",
    "centre": "center", "centres": "centers", "centred": "centered",
    "labour": "labor", "favour": "favor", "favourite": "favorite", "favourable": "favorable",
    "programme": "program", "programmes": "programs", "metre": "meter", "metres": "meters",
    "litre": "liter", "litres": "liters", "licence": "license", "defence": "defense",
    "catalogue": "catalog", "catalogues": "catalogs", "grey": "gray", "tyre": "tire",
    "tyres": "tires", "aluminium": "aluminum", "modelling": "modeling",
    "labelled": "labeled", "labelling": "labeling", "travelled": "traveled",
    "analyse": "analyze", "analysed": "analyzed", "analysing": "analyzing",
    "fulfil": "fulfill", "enrol": "enroll", "manoeuvre": "maneuver",
}
_WORD = re.compile(r"\b(" + "|".join(sorted(_WORDS, key=len, reverse=True)) + r")\b",
                   re.IGNORECASE)


def _keep_case(found: str, replacement: str) -> str:
    if found.isupper():
        return replacement.upper()
    if found[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def _ize(match: re.Match) -> str:
    stem, ending = match.group(1), match.group(2)
    swapped = stem[:-1] + ("Z" if stem[-1].isupper() else "z")
    return swapped + ending


def to_us(text: str) -> str:
    """The text with every listed UK spelling changed to US, case kept."""
    text = _IZE.sub(_ize, text)
    return _WORD.sub(lambda m: _keep_case(m.group(0), _WORDS[m.group(0).lower()]), text)


def uk_words(text: str) -> list[str]:
    """Every UK spelling in the text, as written."""
    return [m.group(0) for m in _IZE.finditer(text)] + [m.group(0)
                                                       for m in _WORD.finditer(text)]

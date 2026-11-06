"""Structured ("typed") clarifying questions streamed to the UI, and their answers.

A question carries a stable `id` the UI can key on - `creativity_level` and
`number_of_slides` are rendered as sliders - plus everything needed to render it
without parsing prose. Answers come back on `/invoke` as `{id: value}` and are applied
to the session brief deterministically.
"""

from pydantic import BaseModel, Field, field_validator

from app.config.constants import (
    AnswerPlacements, CreativityLevels, QuestionIds, QuestionTypes,
    SlideCountConstants,
)
from app.config.exceptions import AppError
from app.utilities.logger_util import get_logger

log = get_logger(__name__)


class QuestionOption(BaseModel):
    """One choice of a structured question."""
    #: The value sent back in `answers` (never translated).
    value: str
    #: What the UI shows (translated into the user's language).
    label: str = ""
    #: An optional one-line explanation of the choice.
    description: str = ""


class ClarifyingQuestion(BaseModel):
    """One question the assistant needs answered before it can plan."""
    #: Stable identifier, one of `QuestionIds`; the key used in `answers`.
    id: str
    #: `single_choice`, `free_text` or `scale` (an ordered slider).
    type: str = QuestionTypes.FREE_TEXT
    text: str
    options: list[QuestionOption] = Field(default_factory=list)
    #: The option value pre-selected by the UI, or "".
    default: str = ""
    required: bool = True
    #: True when the user may type an answer instead of picking an option.
    allow_free_text: bool = False

    @field_validator("type", mode="before")
    @classmethod
    def _known_type(cls, value) -> str:
        """Degrade an unknown type to free text rather than failing the turn.

        Args:
            value: The type the model or caller sent.

        Returns:
            A member of `QuestionTypes.ALL`.
        """
        text = str(value or "").strip().lower()
        if text in QuestionTypes.ALL:
            return text
        if text:
            log.warning("unknown question type %r; using free_text", value)
        return QuestionTypes.FREE_TEXT

    @field_validator("options", mode="before")
    @classmethod
    def _accept_plain_options(cls, value):
        """Accept a list of plain strings as options (value == label).

        Args:
            value: Option objects, dicts or strings.

        Returns:
            A list `QuestionOption` can validate.
        """
        if not isinstance(value, list):
            return []
        return [{"value": item, "label": item} if isinstance(item, str) else item
                for item in value]


#: Display names for answers turned into a chat message.
_ANSWER_NAMES = {QuestionIds.CREATIVITY_LEVEL: "Creativity",
                 QuestionIds.NUMBER_OF_SLIDES: "Number of slides",
                 # Retired, but still named so an answers-only turn reads sensibly.
                 "detailing_level": "Detailing",
                 QuestionIds.QUIZ_ANSWER_PLACEMENT: "Quiz answers",
                 QuestionIds.GOAL: "Goal", QuestionIds.AUDIENCE: "Audience",
                 QuestionIds.DECK_TYPE: "Deck type",
                 QuestionIds.REQUIREMENTS: "Requirements"}
_LEVEL_NAMES = {"LOW": "Low", "MED": "Medium", "HIGH": "High"}

#: The `number_of_slides` slider values, smallest first. A contract with the UI: never
#: rename. The last one is open-ended: the agent sizes the deck to the content.
SLIDE_RANGES = ("5-10", "10-15", "15-20", "20+")


def normalize_slide_range(value: str) -> str:
    """Map a deck-size value onto one of `SLIDE_RANGES`.

    Args:
        value: E.g. "10-15", "10 – 15 slides", "20+", "20 plus" or "".

    Returns:
        The matching range, or "" when the value is empty or not a known range.
    """
    key = (str(value or "").upper().replace("SLIDES", "").replace("PLUS", "+")
           .replace("\u2013", "-").replace("\u2014", "-"))
    key = "".join(key.split())
    return key if key in SLIDE_RANGES else ""


def slide_range_bounds(value: str) -> tuple[int, int] | None:
    """The planned-slide bounds of a `number_of_slides` value.

    Args:
        value: One of `SLIDE_RANGES`.

    Returns:
        (low, high); an open range ("20+") ends at the most slides the agent may choose
        on its own. None for an unknown value.
    """
    if value not in SLIDE_RANGES:
        return None
    low, _, high = value.rstrip("+").partition("-")
    return int(low), int(high) if high else SlideCountConstants.MAX_AGENT_DECIDED


def validate_answers(answers) -> dict[str, str]:
    """Check structured answers sent on `/invoke`, before any work starts.

    Args:
        answers: The raw `answers` object from the request body, or None.

    Returns:
        `{question id: value}` with values trimmed and scale values upper-cased.
        Empty values are dropped.

    Raises:
        AppError: For a non-object, an unknown id, an out-of-range scale value, an
            unknown answer placement or an over-long value.
    """
    if answers is None:
        return {}
    if not isinstance(answers, dict):
        raise AppError("answers must be an object of {question id: value}")
    clean: dict[str, str] = {}
    for key, value in answers.items():
        if key in QuestionIds.RETIRED:
            # Kept (unvalidated) so the workflow can answer "unchanged", then ignored
            # by the brief: a client still sending it must never get a 400.
            log.info("accepted the retired question id %r; it has no effect", key)
            clean[key] = str(value if value is not None else "").strip().upper()
            continue
        if key not in QuestionIds.ALL:
            raise AppError(f"unknown question id {key!r}; expected one of "
                           f"{', '.join(QuestionIds.ALL)}")
        text = str(value if value is not None else "").strip()
        if not text:
            continue
        if len(text) > QuestionIds.MAX_ANSWER_CHARS:
            raise AppError(f"answer for {key!r} exceeds "
                           f"{QuestionIds.MAX_ANSWER_CHARS} characters")
        if key == QuestionIds.NUMBER_OF_SLIDES:
            text = normalize_slide_range(text)
            if not text:
                raise AppError(f"{key} must be one of "
                               f"{', '.join(SLIDE_RANGES)}")
        elif key in QuestionIds.SCALES:
            levels = CreativityLevels
            text = levels.ALIASES.get(text.upper(), text.upper())
            if text not in levels.ALL:
                raise AppError(f"{key} must be one of {', '.join(levels.ALL)}")
        elif key == QuestionIds.QUIZ_ANSWER_PLACEMENT:
            text = text.lower()
            if text not in AnswerPlacements.ALL:
                raise AppError(f"{key} must be one of {', '.join(AnswerPlacements.ALL)}")
        clean[key] = text
    log.info("validated %d structured answer(s): %s", len(clean), sorted(clean))
    return clean


def describe_answers(answers: dict[str, str]) -> str:
    """A readable chat message for a turn that carried only structured answers.

    Args:
        answers: Validated answers.

    Returns:
        E.g. "Creativity: High. Number of slides: 10-15. Audience: new operators."
    """
    parts = []
    for key, value in answers.items():
        scaled = key in QuestionIds.STYLE + QuestionIds.RETIRED
        shown = _LEVEL_NAMES.get(value, value) if scaled else value
        parts.append(f"{_ANSWER_NAMES.get(key, key)}: {shown}.")
    return " ".join(parts)

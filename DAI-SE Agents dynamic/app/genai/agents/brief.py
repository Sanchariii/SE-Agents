"""The deck brief: what the user wants, gathered across turns, plus the plan review text.

The brief also carries the session's style preferences - the creativity level, where
quiz answers go, and any hard slide cap - and the deck size (`number_of_slides`). They
are asked for once, as structured questions, stored with the brief (Redis, snapshotted
to Postgres) and changed only when the user says so. A slide count the user states in
their own words answers the size question, so it is then never asked.
"""

from pydantic import BaseModel, field_validator

from app.config.constants import (
    AnswerPlacements, CreativityLevels, QuestionIds, QuestionTypes,
    SlideCountConstants, SlideTypes, SourceMaterialConstants,
)
from app.core.handlers.session_store import session_store
from app.core.models.deck_model import DeckPlan, _normalize_choice
from app.core.models.document_model import DocStatus
from app.core.models.question_model import (
    SLIDE_RANGES, ClarifyingQuestion, QuestionOption, normalize_slide_range,
    slide_range_bounds,
)
from app.genai.agents.reply_language import ReplyLanguage
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

DECK_TYPES = ("training", "quiz", "faq", "job_aid", "overview")
#: Questions the triage model may write per turn; the style questions come on top.
MAX_QUESTIONS = 3
APPROVE_COMMAND = "/approve-plan"

_FALLBACK_QUESTIONS = {
    "goal": "What should the audience be able to do or understand after this deck?",
    "audience": "Who is the audience (for example new operators, engineers, managers)?",
    "deck_type": "What should I create: a training deck, a quiz, an FAQ, a job aid or an overview?",
}

#: Structured fallbacks for the brief questions the model did not write.
_FALLBACK_TYPES = {
    QuestionIds.GOAL: QuestionTypes.FREE_TEXT,
    QuestionIds.AUDIENCE: QuestionTypes.FREE_TEXT,
    QuestionIds.DECK_TYPE: QuestionTypes.SINGLE_CHOICE,
}
_DECK_TYPE_LABELS = {"training": "Training deck", "quiz": "Quiz", "faq": "FAQ",
                     "job_aid": "Job aid", "overview": "Overview"}

#: The whole reply when a new deck has nothing to be grounded in: a plain chat
#: message, with no question card, since the content is shared in the chat.
SOURCE_CONTENT_REQUEST = ("I don't have any content on this topic yet. Please share the "
                          "content for the deck here in the chat (or upload a document), "
                          "and I will plan the slides from it.")

#: The style question, always built in code (never by the model) so their ids,
#: type and values are a stable contract with the UI's sliders.
_STYLE_QUESTIONS = {
    QuestionIds.CREATIVITY_LEVEL: (
        "How much creative freedom should the slide design have?",
        (CreativityLevels.LOW, "Low",
         "Stay close to Rockwell's standard layouts and templates."),
        (CreativityLevels.MED, "Medium",
         "Pick the best layout for each slide within Rockwell design patterns."),
        (CreativityLevels.HIGH, "High",
         "Bold new layouts where they communicate better, still on the Rockwell brand."),
    ),
    QuestionIds.NUMBER_OF_SLIDES: (
        "How many slides should the deck have?",
        ("5-10", "5-10 slides", "A short, focused deck."),
        ("10-15", "10-15 slides", "A standard-length deck."),
        ("15-20", "15-20 slides", "A detailed deck."),
        ("20+", "20+ slides", "In-depth: I size it to the content, up to "
                              f"{SlideCountConstants.MAX_AGENT_DECIDED} slides."),
    ),
}
#: The value each slider pre-selects.
_SCALE_DEFAULTS = {QuestionIds.CREATIVITY_LEVEL: CreativityLevels.DEFAULT,
                   QuestionIds.NUMBER_OF_SLIDES: "10-15"}
_LEVEL_LABELS = {"LOW": "Low", "MED": "Medium", "HIGH": "High"}

#: Words that move a level one step instead of naming it.
_STEP_UP = ("UP", "+1", "MORE", "INCREASE", "HIGHER")
_STEP_DOWN = ("DOWN", "-1", "LESS", "DECREASE", "LOWER")


def _level(value: str, current: str, allowed: tuple, aliases: dict, field: str) -> str:
    """Resolve a level the user stated, absolutely or as a step from the current one.

    Args:
        value: "LOW"/"MED"/"HIGH", a step word ("UP", "DOWN"), or "" for no change.
        current: The stored level ("" when never chosen).
        allowed: The ordered levels, lowest first.
        aliases: Accepted spellings mapped onto a level.
        field: The field name, for the log line.

    Returns:
        The new level, or `current` when nothing usable was stated.
    """
    key = str(value or "").strip().upper()
    if not key:
        return current
    if key in _STEP_UP + _STEP_DOWN:
        base = allowed.index(current or allowed[1])
        step = 1 if key in _STEP_UP else -1
        return allowed[max(0, min(len(allowed) - 1, base + step))]
    resolved = _normalize_choice(key, allowed, "", aliases, field)
    return resolved or current


def slide_bucket(count: int) -> str:
    """Map a slide count onto its `number_of_slides` range.

    Args:
        count: A positive number of slides the user asked for (or capped the deck at).

    Returns:
        The first closed range whose upper bound `count` does not exceed, else the
        open-ended last range ("20+").
    """
    for bucket in SLIDE_RANGES[:-1]:
        if count <= slide_range_bounds(bucket)[1]:
            return bucket
    return SLIDE_RANGES[-1]


class BriefUpdate(BaseModel):
    """What the user said on this turn; empty means not mentioned."""
    goal: str = ""
    intention: str = ""
    audience: str = ""
    deck_type: str = ""
    requirements: list[str] = []
    use_defaults: bool = False
    #: LOW | MED | HIGH, or a step ("UP"/"DOWN") from the current level.
    creativity_level: str = ""
    #: "slide" | "notes" when the user says where quiz answers go.
    quiz_answer_placement: str = ""
    #: A HARD cap ("max 8 slides", "under 10 slides"); -1 lifts an earlier cap.
    max_slides: int = 0
    #: The total number of slides the user asked for ("make it 12 slides"); 0 if none.
    slide_count: int = 0
    #: A size range the user named without a number ("more than 20" -> "20+"), or "".
    number_of_slides: str = ""
    #: True when this turn's message itself carries source material (facts, steps,
    #: pasted text), not just a topic. The message is then kept verbatim in code.
    content_provided: bool = False
    
    @field_validator("slide_count", mode="before")
    @classmethod
    def _lenient_count(cls, value) -> int:
        """Read the model's slide count leniently, so a null or "12" never fails the turn.

        Args:
            value: What the triage model wrote.

        Returns:
            The count as an int; 0 when it is missing or not a number.
        """
        try:
            return int(float(value))
        except (TypeError, ValueError):
            if value not in (None, ""):
                log.warning("ignoring an unreadable slide_count %r", value)
            return 0
    def requested_total(self) -> int:
        """The largest slide total this turn asks for, as a count or as a cap.

        Returns:
            The bigger of `slide_count` and `max_slides`; 0 when neither was stated.
        """
        return max(self.slide_count, self.max_slides, 0)

    def states_size(self) -> bool:
        """True when this turn states a deck size: a count, a cap or a named range."""
        return (self.slide_count > 0 or self.max_slides > 0
                or bool(normalize_slide_range(self.number_of_slides)))

    def over_request_limit(self) -> bool:
        """True when this turn asks for more slides than a user may request."""
        return self.requested_total() > SlideCountConstants.MAX_REQUESTED

    def without_slide_count(self) -> "BriefUpdate":
        """A copy with every deck-size fact removed, for a refused turn.

        Returns:
            The update with no slide count, range or cap, so a refused size is never
            stored while the rest of the turn (goal, audience, ...) still is.
        """
        return self.model_copy(update={"slide_count": 0, "number_of_slides": "",
                                       "max_slides": 0})
    


def has_source_documents(session_id: str) -> bool:
    """Whether the session has an uploaded document a deck can be grounded in.

    A document still uploading or extracting counts (planning waits for it); a failed
    one does not, since it will never yield any content.

    Args:
        session_id: The session to inspect.

    Returns:
        True when at least one latest document has not failed.
    """
    return any(doc.status != DocStatus.failed
               for doc in session_store.latest_docs(session_id))


class DeckBrief(BaseModel):
    goal: str = ""
    intention: str = ""
    audience: str = ""
    deck_type: str = ""
    requirements: list[str] = []
    use_defaults: bool = False
    #: "" until the user chooses (or says "you decide", which records MED).
    creativity_level: str = ""
    #: "" follows the slide cap; "slide" or "notes" is the user's explicit choice.
    quiz_answer_placement: str = ""
    #: A hard slide cap; 0 means none. A plain slide target is a requirement instead.
    max_slides: int = 0
    #: The deck size range (one of `SLIDE_RANGES`); "" until chosen or stated.
    number_of_slides: str = ""
    #: The exact slide total the user asked for, a target for the planner; 0 if none.
    slide_count: int = 0
    #: A whole-deck request (new deck or resize) waiting for its size answer; it is
    #: handed to the planner on the turn that answers, then cleared.
    pending_request: str = ""
    #: The user's chat language from the last typed turn, so a turn that carries only
    #: structured answers is still answered in it.
    chat_language: str = ""
    #: Source material the user typed or pasted into the chat, verbatim. With no
    #: uploaded document it is the only thing the deck may be grounded in.
    source_material: str = ""
    #: The uploaded PowerPoint whose DESIGN the deck copies (doc_id, version, filename,
    #: sha256, storage_path); empty when the deck uses the brand template. Its text is
    #: never content: `session_store.latest_docs` leaves it out.
    design_reference: dict = {}
    #: "doc_id:version" of an uploaded deck the user said is content, not a design, so
    #: it is not claimed automatically again; "" when none.
    design_reference_declined: str = ""

    @classmethod
    def load(cls, session_id: str) -> "DeckBrief":
        return cls(**(session_store.get_brief(session_id) or {}))

    def save(self, session_id: str) -> None:
        session_store.set_brief(session_id, self.model_dump())

    def merge(self, update: BriefUpdate) -> "DeckBrief":
        """Apply this turn's facts; a value given earlier is never blanked."""
        data = self.model_dump()
        for field in ("goal", "intention", "audience"):
            data[field] = getattr(update, field).strip() or data[field]
        if update.deck_type in DECK_TYPES:
            data["deck_type"] = update.deck_type
        data["requirements"] = list(dict.fromkeys(data["requirements"] + update.requirements))
        data["use_defaults"] = data["use_defaults"] or update.use_defaults
        data["creativity_level"] = _level(
            update.creativity_level, data["creativity_level"], CreativityLevels.ALL,
            CreativityLevels.ALIASES, "creativity_level")
        placement = update.quiz_answer_placement.strip().lower()
        if placement in AnswerPlacements.ALL:
            data["quiz_answer_placement"] = placement
        if update.max_slides > 0:
            data["max_slides"] = update.max_slides
        elif update.max_slides < 0:
            data["max_slides"] = 0
        _merge_slide_size(data, update)
        # "You decide" settles the style question too, so it is never asked again.
        if data["use_defaults"]:
            data["creativity_level"] = data["creativity_level"] or CreativityLevels.DEFAULT
        return DeckBrief(**data)

    def missing(self, include_size: bool = True) -> list[str]:
        """What the planner cannot guess. Decided in code, so asking never rests on the model.

        Args:
            include_size: Whether an unknown deck size counts. Only a session's first
                deck asks for it this way; later decks ask through `reset_size`.

        Returns:
            The missing items, in asking order; none once the user said "you decide".
        """
        if self.use_defaults:
            return []
        items = ["goal", "audience", "deck_type", QuestionIds.CREATIVITY_LEVEL]
        if include_size:
            items.append(QuestionIds.NUMBER_OF_SLIDES)
        return [f for f in items if not getattr(self, f)]

    def reset_size(self, request: str) -> "DeckBrief":
        """Forget the deck size for a new, recreated or resized deck, so it is asked again.

        Args:
            request: The user's whole-deck request, kept for the turn that answers.

        Returns:
            A copy with no size range or count and the request pending; a hard cap is
            kept, since it is a separate rule the user set.
        """
        log.info("deck size reset for a whole-deck request (was range=%s count=%s)",
                 self.number_of_slides or "-", self.slide_count or "-")
        return self.model_copy(update={"number_of_slides": "", "slide_count": 0,
                                       "pending_request": request.strip()})

    def fallback_questions(self) -> list[str]:
        return [_FALLBACK_QUESTIONS[f] for f in self.missing() if f in _FALLBACK_QUESTIONS]

    # --- source material ---------------------------------------------------- #
    def needs_source_content(self, has_documents: bool) -> bool:
        """Whether a new deck has nothing to be grounded in.

        Decided in code and never waived by "you decide": the system must not invent
        a deck's content from a bare topic.

        Args:
            has_documents: Whether the session has a usable uploaded document.

        Returns:
            True when there is no usable document and no material typed in the chat.
        """
        return not has_documents and not self.source_material.strip()

    def with_source_material(self, text: str) -> "DeckBrief":
        """Append material the user typed or pasted, kept verbatim and capped.

        Args:
            text: The user's message carrying the content.

        Returns:
            The updated brief; this instance is untouched.
        """
        text = str(text or "").strip()
        # A resent message must not be stored twice.
        if not text or text in self.source_material:
            return self
        combined = "\n\n".join(part for part in (self.source_material, text) if part)
        limit = SourceMaterialConstants.MAX_STORED_CHARS
        if len(combined) > limit:
            log.warning("source material exceeds %d chars; keeping the first %d",
                        len(combined), limit)
            combined = combined[:limit]
        log.info("stored %d chars of user-provided source material (total %d)",
                 len(text), len(combined))
        return self.model_copy(update={"source_material": combined})

    # --- style preferences -------------------------------------------------- #
    def effective_creativity(self) -> str:
        """The deck's creativity level; MED when never chosen (e.g. older sessions)."""
        return self.creativity_level or CreativityLevels.DEFAULT

    def answer_placement(self) -> str:
        """Where quiz answers go: the user's explicit choice, else notes under a hard cap.

        Returns:
            `AnswerPlacements.SLIDE` or `AnswerPlacements.NOTES`.
        """
        if self.quiz_answer_placement in AnswerPlacements.ALL:
            return self.quiz_answer_placement
        return AnswerPlacements.NOTES if self.max_slides > 0 else AnswerPlacements.SLIDE

    def style(self) -> dict:
        """The effective style preferences, as echoed to the UI to initialise sliders."""
        return {QuestionIds.CREATIVITY_LEVEL: self.effective_creativity(),
                QuestionIds.QUIZ_ANSWER_PLACEMENT: self.answer_placement(),
                "max_slides": self.max_slides or None,
                QuestionIds.NUMBER_OF_SLIDES: self.number_of_slides or None,
                "slide_count": self.slide_count or None,
                "chosen": bool(self.creativity_level)}

    def slide_target(self) -> str:
        """The deck size as the planner is told it, always ending with the request limit.

        Returns:
            The user's exact count, the chosen range, or the agent's own sizing rule,
            followed by the most planned slides a deck may have.
        """
        bounds = slide_range_bounds(self.number_of_slides)
        if self.slide_count:
            target = f"{self.slide_count} slides (the user's number; a target)"
        elif bounds and self.number_of_slides == SLIDE_RANGES[-1]:
            low, high = bounds
            target = (f"{low} to {high} slides - size it to how much the content needs, "
                      f"never more than {high}")
        elif bounds:
            low, high = bounds
            target = f"between {low} and {high} slides"
        else:
            target = ("not chosen - size it to the content, at most "
                      f"{SlideCountConstants.MAX_AGENT_DECIDED} slides")
        return (f"{target} (never more than {SlideCountConstants.MAX_REQUESTED} planned "
                "slides; answer slides the system adds are not counted)")

    def apply_answers(self, answers: dict[str, str]) -> "DeckBrief":
        """Apply structured answers from the UI. Deterministic: no model involved.

        Args:
            answers: Already validated `{question id: value}` pairs.

        Returns:
            The updated brief; this instance is untouched.
        """
        update = BriefUpdate()
        extra_requirements: list[str] = []
        for key, value in answers.items():
            if key in QuestionIds.RETIRED:
                # A retired question (detailing) from a client that has not caught up.
                log.info("ignoring the retired answer %r=%r", key, value)
            elif key in (QuestionIds.CREATIVITY_LEVEL, QuestionIds.QUIZ_ANSWER_PLACEMENT,
                         QuestionIds.NUMBER_OF_SLIDES, QuestionIds.GOAL,
                         QuestionIds.AUDIENCE):
                setattr(update, key, value)
            elif key == QuestionIds.DECK_TYPE:
                deck_type = _deck_type_of(value)
                if deck_type:
                    update.deck_type = deck_type
                else:
                    extra_requirements.append(f"deck type: {value}")
            elif key == QuestionIds.REQUIREMENTS:
                extra_requirements.append(value)
        update.requirements = extra_requirements
        merged = self.merge(update)
        log.info("applied %d structured answer(s): %s", len(answers), sorted(answers))
        return merged

    def as_prompt(self) -> str:
        def show(value: str) -> str:
            return value or "not provided - choose a sensible default and say so"
        placement = ("on a separate answer slide right after each question"
                     if self.answer_placement() == AnswerPlacements.SLIDE
                     else "in the question slide's speaker notes only (no answer slides)")
        cap = (f"at most {self.max_slides} slides in total (a hard limit)"
               if self.max_slides else "none")
        return (f"goal: {show(self.goal)}\nintention: {self.intention or 'not stated'}\n"
                f"audience: {show(self.audience)}\ndeck type: {show(self.deck_type)}\n"
                f"requirements: {'; '.join(self.requirements) or 'none'}\n"
                f"creativity level: {self.effective_creativity()}\n"
                f"quiz answers: {placement}\nslide cap: {cap}\n"
                f"slide count: {self.slide_target()}")


def _merge_slide_size(data: dict, update: BriefUpdate) -> None:
    """Fold this turn's deck-size facts into the brief data, in place.

    A stated number answers the `number_of_slides` question: an exact count is kept as
    the planner's target and mapped onto its range, a named range replaces the old one
    (dropping an exact count that no longer fits it), and a new hard cap with neither
    sets the range from the cap. The latest statement always wins.

    Args:
        data: The brief as a dict, being merged.
        update: This turn's facts.
    """
    bucket = normalize_slide_range(update.number_of_slides)
    if update.slide_count > 0:
        data["slide_count"] = update.slide_count
        data["number_of_slides"] = slide_bucket(update.slide_count)
    elif bucket:
        data["number_of_slides"] = bucket
        if data["slide_count"] and slide_bucket(data["slide_count"]) != bucket:
            # An explicit range overrides an earlier exact count outside it.
            data["slide_count"] = 0
    elif update.max_slides > 0:
        data["number_of_slides"] = slide_bucket(update.max_slides)
    else:
        return
    # The latest size wins over an older hard cap it no longer fits.
    floor = data["slide_count"] or (slide_range_bounds(data["number_of_slides"]) or (0,))[0]
    if update.max_slides <= 0 and data["max_slides"] and floor > data["max_slides"]:
        log.info("lifting the hard cap of %d slides: the new size starts at %d",
                 data["max_slides"], floor)
        data["max_slides"] = 0
    log.info("deck size: range=%s count=%s cap=%s", data["number_of_slides"],
             data["slide_count"] or "-", data["max_slides"] or "-")


def _deck_type_of(text: str) -> str:
    """Map a free-text deck type answer onto `DECK_TYPES`, or "".

    Args:
        text: The answer, e.g. "quiz", "A training deck" or "job aid".

    Returns:
        The matching deck type, or "" when none matches.
    """
    lowered = str(text or "").strip().lower().replace("-", "_").replace(" ", "_")
    for deck_type in DECK_TYPES:
        if deck_type in lowered:
            return deck_type
    return ""


class QuestionBook:
    """Builds the structured questions for a clarify turn, in the user's language."""

    def __init__(self) -> None:
        self._reply_language = ReplyLanguage()

    def build(self, brief: DeckBrief, written: list[ClarifyingQuestion],
              language: str | None = None,
              force: tuple[str, ...] = (),
              include_size: bool = True) -> list[ClarifyingQuestion]:
        """The questions to ask now: the model's brief questions, then the slider ones.

        Args:
            brief: The brief after this turn's update.
            written: Structured questions the triage model wrote.
            language: The user's chat language; ids and values are never translated.
            force: Slider ids to ask even when the brief has them (a new, resized or
                refused deck size asks `number_of_slides` again).
            include_size: Whether an unknown deck size is asked (first deck only).

        Returns:
            One question per missing brief item (the model's wording where it gave
            one), an optional requirements question, then the slider questions for
            whichever scale is still unset or forced.
        """
        missing = brief.missing(include_size)
        by_id = {q.id: q for q in written if q.id in QuestionIds.ALL}
        questions: list[ClarifyingQuestion] = []
        for item in (QuestionIds.GOAL, QuestionIds.AUDIENCE, QuestionIds.DECK_TYPE):
            if item not in missing:
                continue
            questions.append(by_id.get(item) or self._fallback(item))
        # The model's untagged questions keep their place when it wrote no tagged one.
        untagged = [q for q in written if q.id not in QuestionIds.ALL]
        questions += untagged[:max(0, MAX_QUESTIONS - len(questions))]
        optional = by_id.get(QuestionIds.REQUIREMENTS)
        if optional and len(questions) < MAX_QUESTIONS:
            questions.append(optional.model_copy(update={"required": False}))
        questions = questions[:MAX_QUESTIONS]
        style = [self._style(item) for item in QuestionIds.SCALES
                 if item in missing or item in force]
        log.info("clarify questions: %d brief, %d style (%s)", len(questions), len(style),
                 [q.id for q in questions + style])
        return self._translate(questions, language) + self._translate(style, language)

    @staticmethod
    def _fallback(item: str) -> ClarifyingQuestion:
        """The code-written question for a missing brief item."""
        options = ([QuestionOption(value=t, label=_DECK_TYPE_LABELS[t]) for t in DECK_TYPES]
                   if item == QuestionIds.DECK_TYPE else [])
        return ClarifyingQuestion(id=item, type=_FALLBACK_TYPES[item],
                                  text=_FALLBACK_QUESTIONS[item], options=options,
                                  allow_free_text=item == QuestionIds.DECK_TYPE)

    @staticmethod
    def _style(item: str) -> ClarifyingQuestion:
        """A slider question for one scale (creativity or deck size).

        Args:
            item: A `QuestionIds.SCALES` id.

        Returns:
            The `scale` question with its options and pre-selected default.
        """
        text, *options = _STYLE_QUESTIONS[item]
        return ClarifyingQuestion(
            id=item, type=QuestionTypes.SCALE, text=text,
            options=[QuestionOption(value=v, label=l, description=d) for v, l, d in options],
            default=_SCALE_DEFAULTS[item])

    def _translate(self, questions: list[ClarifyingQuestion],
                   language: str | None) -> list[ClarifyingQuestion]:
        """Translate question texts, option labels and descriptions in one call.

        Args:
            questions: Questions written in English.
            language: The target chat language.

        Returns:
            Translated copies; the originals when the language is the default or the
            translation could not be aligned.
        """
        if self._reply_language.is_default(language) or not questions:
            return questions
        texts: list[str] = []
        for q in questions:
            texts.append(q.text)
            for option in q.options:
                texts += [option.label or option.value, option.description]
        # Empty strings would be dropped by the translator and break the alignment.
        index = [i for i, t in enumerate(texts) if t.strip()]
        translated = self._reply_language.translate([texts[i] for i in index], language)
        if len(translated) != len(index):
            return questions
        for i, value in zip(index, translated):
            texts[i] = value
        cursor, out = 0, []
        for q in questions:
            text = texts[cursor]
            cursor += 1
            options = []
            for option in q.options:
                options.append(option.model_copy(update={
                    "label": texts[cursor], "description": texts[cursor + 1]}))
                cursor += 2
            out.append(q.model_copy(update={"text": text, "options": options}))
        return out


def render_questions(intro: str, questions: list[ClarifyingQuestion]) -> str:
    """The numbered chat text for structured questions, for clients without the event.

    Args:
        intro: The sentence introducing the questions.
        questions: The questions, in order.

    Returns:
        The intro followed by one numbered line per question; choice and slider
        questions list their options inline.
    """
    lines = [intro]
    for n, q in enumerate(questions, 1):
        line = f"{n}. {q.text}"
        if q.options:
            line += " (" + " / ".join(o.label or o.value for o in q.options) + ")"
        lines.append(line)
    return "\n".join(lines)


class PlanReview:
    """The proposed plan as a chat message, ending with what the user can do next."""

    def __init__(self) -> None:
        self._reply_language = ReplyLanguage()

    @staticmethod
    def _tag(slide) -> str:
        """The short type tag shown before a slide's title, or ""."""
        if slide.slide_type == SlideTypes.QUIZ:
            return "[Quiz answer] " if slide.is_quiz_answer else "[Quiz] "
        if slide.slide_type == SlideTypes.FAQ:
            return "[FAQ] "
        return ""

    def format(self, plan: DeckPlan, language: str | None = None,
               brief: DeckBrief | None = None) -> str:
        lines = [f'Proposed plan: "{plan.title}"',
                 f"Audience: {plan.audience or 'general'}"
                 f" | Goal: {plan.objective or 'not stated'}"
                 f" | Languages: {', '.join(plan.languages)}"]
        if brief is not None:
            lines.append(f"Style: creativity {_LEVEL_LABELS[brief.effective_creativity()]}")
            if any(s.slide_type == SlideTypes.QUIZ for s in plan.slides):
                lines.append("Quiz answers: "
                             + ("revealed on the slide after each question"
                                if brief.answer_placement() == AnswerPlacements.SLIDE
                                else "in the speaker notes only"))
        if plan.guardrail_warnings:
            lines.append("Flagged requests (not applied):")
            lines.extend(f"  - {warning}" for warning in plan.guardrail_warnings)
        lines.append("Slides:")
        # Planner instructions are internal and never appear here.
        for slide in plan.slides:
            lines.append(f"  {slide.slide_number}. {self._tag(slide)}{slide.title}")
            if not slide.is_quiz_answer:
                lines += [f"     - {point}" for point in slide.key_points]
        lines.append("Nothing is built yet. Tell me what to change, or reply 'approve' "
                     "and I will build the deck.")
        return self._reply_language.translate(["\n".join(lines)], language)[0]

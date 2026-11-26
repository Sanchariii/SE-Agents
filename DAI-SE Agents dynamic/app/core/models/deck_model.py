"""Schemas for planning, RAG content, deck drafts, and critic verdicts."""

from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing import Literal

from app.config.constants import (
    CreativityLevels, PlannerConstants, PptConstants,
    QuizConstants, QuizRoles, SlideTypes,
)
from app.core.models.document_model import RetrievedChunk
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

#: "section" is a divider opening a group of content slides; the planner is offered it
#: only when the design reference has section slides (see `reference_design`).
SlideKind = Literal["cover", "agenda", "section", "content", "takeaways", "thanks"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _optional_level(value, allowed: tuple, aliases: dict, field: str) -> str:
    """Normalise a per-slide level override, where "" means "inherit the deck's".

    Args:
        value: What the planner wrote: any case, None or "" for no override.
        allowed: The accepted upper-case values.
        aliases: Accepted spellings mapped onto a supported value.
        field: The field name, for the log line.

    Returns:
        One of `allowed`, or "" when there is no (usable) override.
    """
    return _normalize_choice(value, allowed, "", aliases, field)


# --- planning ---
class SlidePlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    slide_number: int
    kind: SlideKind = "content"
    title: str
    key_points: list[str] = []
    content_needs: list[str] = []      # retrieval queries for this slide
    visual: str = ""                   # diagram | keyframe | table | none
    layout_hint: str = ""              # company-template layout NAME this slide fits best

    # --- generation parameters (all optional; defaults == the original behaviour) --- #
    #: NORMAL, QUIZ or FAQ. Role slides (cover, agenda, ...) are always NORMAL.
    slide_type: str = SlideTypes.DEFAULT
    #: For QUIZ slides: "question", or "answer" for the reveal slide that follows it.
    quiz_role: str = QuizRoles.NONE
    #: Ties a quiz question to its answer slide; stable across renumbering.
    quiz_id: str = ""
    #: Question slides only: True adds an answer slide after it, False keeps the answer
    #: in the speaker notes only, None follows the deck's answer placement.
    reveal_answer: bool | None = None
    #: Per-slide override; "" inherits the deck-level choice in the brief.
    creativity_level: str = ""
    #: The user's own explicit asks for this slide, recorded near-verbatim by the
    #: planner. Outranks the planner's instructions and the design principles.
    user_instructions: str = ""
    #: Internal guidance for the content and design agents. Never shown to the user.
    planner_special_instructions: str = ""
    #: Bumped by a "redesign this" request (a consistency fix): changes the design key
    #: so the slide is redrawn without its content being rewritten. 0 = never asked.
    design_revision: int = 0
    #: The user said this slide looks inconsistent: redesign it to match its neighbours.
    match_neighbours: bool = False
    #: The user asked for images on this slide: relevant source images get priority, and
    #: a role, quiz or FAQ slide may carry them too.
    images_requested: bool = False
    #: The user asked to repeat an image already used elsewhere in the deck on this slide.
    allow_image_reuse: bool = False

    @field_validator("slide_type", mode="before")
    @classmethod
    def _plan_slide_type(cls, value) -> str:
        """Upper-case the slide type; an unknown one degrades to NORMAL."""
        return _normalize_choice(value, SlideTypes.ALL, SlideTypes.DEFAULT, {},
                                 "slide_type")

    @field_validator("quiz_role", mode="before")
    @classmethod
    def _plan_quiz_role(cls, value) -> str:
        """Lower-case the quiz role; an unknown one means no role."""
        text = str(value or "").strip().lower()
        return text if text in QuizRoles.ALL else QuizRoles.NONE

    @field_validator("creativity_level", mode="before")
    @classmethod
    def _plan_creativity(cls, value) -> str:
        """Normalise a creativity override; "" means inherit."""
        return _optional_level(value, CreativityLevels.ALL, CreativityLevels.ALIASES,
                               "creativity_level")

    @field_validator("user_instructions", mode="before")
    @classmethod
    def _plan_user_instructions(cls, value) -> str:
        """Trim and bound the user's recorded asks for this slide."""
        text = str(value or "").strip()
        limit = PlannerConstants.MAX_USER_INSTRUCTIONS_CHARS
        if len(text) > limit:
            log.info("user instructions cut from %d to %d chars", len(text), limit)
            text = text[:limit].rstrip()
        return text

    @field_validator("planner_special_instructions", mode="before")
    @classmethod
    def _plan_instructions(cls, value) -> str:
        """Trim and bound the planner's per-slide note."""
        text = str(value or "").strip()
        limit = PlannerConstants.MAX_SPECIAL_INSTRUCTIONS_CHARS
        if len(text) > limit:
            log.info("planner instructions cut from %d to %d chars", len(text), limit)
            text = text[:limit].rstrip()
        return text

    @property
    def is_quiz_question(self) -> bool:
        """True for a QUIZ slide that asks a question (not its answer slide)."""
        return self.slide_type == SlideTypes.QUIZ and self.quiz_role != QuizRoles.ANSWER

    @property
    def is_quiz_answer(self) -> bool:
        """True for the answer-reveal slide that follows a quiz question."""
        return self.slide_type == SlideTypes.QUIZ and self.quiz_role == QuizRoles.ANSWER

    @property
    def images_allowed(self) -> bool:
        """Whether source images may be offered for this slide.

        Returns:
            True when the user asked for images here, or for a NORMAL content slide.
            Cover, agenda, takeaways, thank-you, quiz and FAQ slides carry none unless asked.
        """
        if self.images_requested:
            return True
        return (self.kind == "content" and self.slide_type == SlideTypes.NORMAL
                and not self.is_quiz_answer)


class DeckPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    audience: str = ""
    objective: str = ""
    tone: str = "professional"
    # public | internal | confidential | restricted | cobranded -> which company .potx
    classification: str = "internal"
    languages: list[str] = Field(
        default_factory=lambda: [PptConstants.DEFAULT_LANGUAGE])
    slides: list[SlidePlan] = []
    #: Deck-level look the user asked for per slide family, in their own words, e.g.
    #: {"QUIZ": "game-show style"}. Empty means the template's reference design.
    family_styles: dict[str, str] = {}
    #: Rejected requests, not additions to the factual content of this plan.
    guardrail_warnings: list[str] = []

    @field_validator("family_styles", mode="before")
    @classmethod
    def _family_keys(cls, value) -> dict:
        """Keep only QUIZ/FAQ keys with non-empty text."""
        if not isinstance(value, dict):
            return {}
        out = {}
        for key, text in value.items():
            name = str(key or "").strip().upper()
            if name in SlideTypes.FAMILIES and str(text or "").strip():
                out[name] = str(text).strip()
        return out


class UnderstandingOutput(BaseModel):
    action: str                        # clarify | plan | chat
    reply: str = ""
    questions: list[str] = []
    plan: DeckPlan | None = None
    plan_ready: bool = False


# # --- triage ---
# class TriageDecision(BaseModel):
#     intent: str                        # chat | plan | enrich | edit | remove
#     reply: str = ""
#     target_slides: list[int] = []      # slides to enrich/edit/remove (empty for chat|plan)
#     reason: str = ""


class SlideRevisionOutput(BaseModel):
    """Revised versions of specific slides only — rest of the plan is untouched."""
    slides: list[SlidePlan] = []


# --- RAG content ---
class SlideContent(BaseModel):
    slide_number: int
    title: str
    chunks: list[RetrievedChunk] = []
    coverage: str = "missing"          # good | partial | missing
    note: str = ""


class ContentBundle(BaseModel):
    plan: dict
    slides: list[SlideContent] = []
    gaps: list[str] = []
    rounds_used: int = 0
    created_at: datetime = Field(default_factory=_now)


class SlideCoverage(BaseModel):
    slide_number: int
    coverage: str
    refined_queries: list[str] = []
    note: str = ""


class CoverageAssessment(BaseModel):
    slides: list[SlideCoverage] = []


# --- deck draft ---
class CardDraft(BaseModel):
    """Legacy card/step shape — only read when migrating a pre-template stored draft."""
    header: str = ""
    body: str = ""

class ContentBlock(BaseModel):
    slot: str                          # a LayoutSpec slot: subtitle | heading1 | body1 | picture1
    text: str = ""
    bullets: list[str] = []
    image_ref: str = ""                # storage path (or local path once materialized)

class TableBlock(BaseModel):
    """A structured data table positioned on the normalized slide grid."""

    left: int = Field(ge=0, le=1000)
    top: int = Field(ge=0, le=1000)
    width: int = Field(gt=0, le=1000)
    height: int = Field(gt=0, le=1000)
    headers: list[str] = Field(min_length=1)
    rows: list[list[str]] = []
    header_fill_color: str = Field(default="003E7E", pattern=r"^[0-9A-Fa-f]{6}$")
    header_text_color: str = Field(default="FFFFFF", pattern=r"^[0-9A-Fa-f]{6}$")
    body_fill_color: str = Field(default="FFFFFF", pattern=r"^[0-9A-Fa-f]{6}$")
    body_text_color: str = Field(default="000000", pattern=r"^[0-9A-Fa-f]{6}$")

class QuizContent(BaseModel):
    """One multiple-choice question, as written by the content agent.

    `reveal` is False on the question slide and True on its answer slide; it is the
    only difference between the two drafts.
    """
    model_config = ConfigDict(extra="ignore")

    question: str
    options: list[str]
    #: 0-based index of the correct option.
    correct_index: int = 0
    #: Why the correct option is correct; goes in the speaker notes.
    explanation: str = ""
    #: "Question 2 of 5", set deterministically when the deck is assembled.
    counter: str = ""
    reveal: bool = False

    @field_validator("options", mode="before")
    @classmethod
    def _clean_options(cls, value) -> list[str]:
        """Strip empty options and cap the count at the label set."""
        items = [str(v).strip() for v in (value or []) if str(v or "").strip()]
        return items[:QuizConstants.MAX_OPTIONS]

    @model_validator(mode="after")
    def _check_answer(self):
        """A question needs enough options and a correct index inside them."""
        if len(self.options) < QuizConstants.MIN_OPTIONS:
            raise ValueError(f"a quiz question needs at least "
                             f"{QuizConstants.MIN_OPTIONS} options")
        if not 0 <= self.correct_index < len(self.options):
            raise ValueError(f"correct_index {self.correct_index} is outside the "
                             f"{len(self.options)} options")
        return self

    @property
    def correct_label(self) -> str:
        """The correct option's letter, e.g. "B"."""
        return QuizConstants.OPTION_LABELS[self.correct_index]

    @property
    def correct_text(self) -> str:
        """The correct option's wording."""
        return self.options[self.correct_index]

    def answer_line(self) -> str:
        """The speaker-notes line every question carries, whatever the placement.

        Returns:
            E.g. "Answer: B - A service level upgrade. It changes the support tier."
        """
        line = f"{QuizConstants.NOTES_ANSWER_PREFIX} {self.correct_label} - {self.correct_text}."
        return f"{line} {self.explanation.strip()}" if self.explanation.strip() else line


class FaqItem(BaseModel):
    """One question and its answer on an FAQ slide."""
    question: str
    answer: str


class ImageCandidate(BaseModel):
    """A source image the writer judged relevant to a slide; the designer may use it."""
    ref: str                           # storage path of the image
    description: str = ""              # its caption, so the designer can judge it


class SlideDraft(BaseModel):
    # Drafts stored before this change carry `shapes` and `icons`, which were declared
    # but never populated by any live path (Bug 3) - geometry is decided in generated
    # code now. Ignoring unknown keys is what lets an old session reload instead of
    # erroring on fields that no longer exist.
    model_config = ConfigDict(extra="ignore")

    slide_number: int
    kind: SlideKind = "content"
    layout: str = ""
    title: str
    blocks: list[ContentBlock] = []
    tables: list[TableBlock] = []
    notes: str = ""

    # --- quiz / FAQ content (empty on every other slide) ---
    quiz: QuizContent | None = None
    faq: list[FaqItem] = []
    #: Number of this slide's first FAQ pair, so numbering continues across slides.
    faq_start: int = 1

    #: Relevant source images, most relevant first: optional assets for the designer.
    #: Never part of `design_json`; the design key carries them instead.
    image_candidates: list[ImageCandidate] = []

    def image_refs(self) -> list[str]:
        """Every image this slide may show: its candidates, then legacy block refs.

        Returns:
            Unique storage paths, candidates first, so drafts stored before candidates
            existed still render their block images.
        """
        refs = [c.ref.strip() for c in self.image_candidates if c.ref.strip()]
        refs += [(b.image_ref or "").strip() for b in self.blocks
                 if (b.image_ref or "").strip()]
        return list(dict.fromkeys(refs))

    def default_family_fields(self) -> set[str]:
        """The quiz/FAQ fields still at their defaults, to exclude from hashes/prompts.

        Returns:
            Field names safe to drop without changing what this slide means.
        """
        out = set()
        if self.quiz is None:
            out.add("quiz")
        if not self.faq:
            out.update({"faq", "faq_start"})
        return out

    def design_json(self, indent: int | None = None, include_reveal: bool = True,
                    exclude: set[str] | None = None) -> str:
        """The slide as JSON for a design prompt or a content hash.

        The legacy fields are always dropped, and so are the quiz/FAQ fields while they
        are empty - which keeps a NORMAL slide's JSON byte-identical to what it was
        before those fields existed.

        Args:
            indent: JSON indent, as for `model_dump_json`.
            include_reveal: False drops `quiz.reveal`, which the designer never needs.
            exclude: Further top-level fields to drop (e.g. "slide_number").

        Returns:
            The JSON text.
        """
        drop: dict = {name: True for name in (
            {"bullets", "cards", "steps", "stat_value", "stat_label", "image_path",
             "image_candidates"}
            | self.default_family_fields() | set(exclude or ()))}
        if self.quiz is not None and not include_reveal:
            drop["quiz"] = {"reveal"}
        return self.model_dump_json(indent=indent, exclude=drop)

    # legacy fields, kept so drafts saved before the template switch still parse
    bullets: list[str] = []
    cards: list[CardDraft] = []
    steps: list[CardDraft] = []
    stat_value: str = ""
    stat_label: str = ""
    image_path: str = ""

    @model_validator(mode="after")
    def _migrate_legacy(self):
        if self.blocks:
            return self
        blocks: list[ContentBlock] = []
        if self.stat_value:
            blocks.append(ContentBlock(
                slot="heading1", text=f"{self.stat_value} {self.stat_label}".strip()))
        for item in (self.cards or self.steps):
            blocks.append(ContentBlock(slot=f"heading{len(blocks) + 1}", text=item.header))
            blocks.append(ContentBlock(slot=f"body{len(blocks)}", text=item.body))
        if self.bullets:
            blocks.append(ContentBlock(slot="body1", bullets=self.bullets))
        if self.image_path:
            blocks.append(ContentBlock(slot="picture1", image_ref=self.image_path))
        self.blocks = blocks
        if blocks:
            log.debug("slide %d: migrated %d legacy field group(s) into %d block(s)",
                      self.slide_number,
                      sum(bool(x) for x in (self.stat_value, self.cards, self.steps,
                                            self.bullets, self.image_path)),
                      len(blocks))
        return self



class DeckDraft(BaseModel):
    template: str = "internal"         # classification -> which company .potx
    title: str
    subtitle: str = ""
    takeaways: list[str] = []
    slides: list[SlideDraft] = []

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_theme(cls, data):
        if isinstance(data, dict) and not data.get("template"):
            data = {**data, "template": "internal"}      # old drafts carried "theme" instead
        return data

# --- critic ---
class CriticVerdict(BaseModel):
    verdict: str                       # approve | revise
    feedback: list[str] = []
    slides_to_improve: list[str] = []

# --- per-slide agent ---
class SlideTask(BaseModel):
    """What the planner decided this ONE slide needs on this turn.

    `op` is what lets the agent skip work: a `rewrite` never retrieves, a `relayout`
    never calls the writer, a `keep` never runs at all.
    """
    slide_number: int
    kind: SlideKind = "content"        # cover | agenda | content | takeaways | thanks
    op: str = "create"                 # create | enrich | rewrite | relayout | keep | remove
    instruction: str = ""              # the user's own words for THIS slide
    feedback: list[str] = []           # prior critic/user feedback to address
    layout_hint: str = ""              # set when the turn changed the layout
    reuse_chunks: bool = True          # False forces fresh retrieval


class DeckContext(BaseModel):
    """Just enough deck-level context for one slide to sound like part of the deck.

    Deliberately tiny: carrying the whole deck here would rebuild the token problem
    the per-slide split exists to solve.
    """
    title: str
    audience: str = ""
    objective: str = ""
    tone: str = "professional"
    position: int = 1
    total: int = 1
    previous_title: str = ""
    next_title: str = ""


class SlideVerdict(BaseModel):
    verdict: str = "approve"           # approve | revise
    feedback: list[str] = []


class SlideResult(BaseModel):
    slide_number: int
    slide_draft: SlideDraft
    chunks: list[RetrievedChunk] = []
    coverage: str = "missing"          # good | partial | missing
    verdict: str = "approve"
    rounds_used: int = 0
    notes: str = ""


# --- code-generated slide design ---
def _normalize_choice(value, allowed: tuple, default: str, aliases: dict,
                      field: str) -> str:
    """Coerce one generation parameter onto its closed set, never failing the slide.

    The parameters are optional and may come from any upstream agent, so a bad value
    degrades to the default with a warning instead of rejecting the whole brief.

    Args:
        value: What the caller sent: any case, None or empty for "use the default".
        allowed: The accepted upper-case values.
        default: The value used when nothing usable was sent.
        aliases: Accepted spellings mapped onto a supported value (e.g. HIGHEST).
        field: The field name, for the log line.

    Returns:
        One of `allowed`.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    key = str(value).strip().upper().replace(" ", "_").replace("-", "_")
    if key in allowed:
        return key
    if key in aliases:
        log.info("%s %r is not offered; using %r", field, value, aliases[key])
        return aliases[key]
    log.warning("%s %r is not one of %s; using the default %r", field, value,
                allowed, default)
    return default


class FamilyLayout(BaseModel):
    """The one layout every slide of a family (QUIZ or FAQ) uses within a deck.

    It starts life either as an upstream directive (a description, perhaps a base
    layout) or as nothing at all, and becomes *established* once a slide of the family
    has actually been built and accepted on it. From then on every slide of that family
    in the deck is rebuilt from it with only the content changed.
    """
    model_config = ConfigDict(extra="ignore")

    slide_type: str = ""
    #: Catalog key of the layout the family is built on.
    layout_key: str = ""
    #: "layout" or "canvas", as in `SlideCodeBrief.mode`.
    mode: str = "layout"
    #: [left, top, width, height] in inches; set in canvas mode.
    free_region: list[float] | None = None
    #: Free-text description of the design, from upstream or from the agent itself.
    description: str = ""
    #: The accepted program that built the establishing slide, reused as an exemplar.
    exemplar_code: str = ""
    #: "template" (reproduces SAMPLE.pptx), "generated" (invented) or "upstream".
    source: str = ""
    #: Catalog key of the SAMPLE.pptx reference design this family reproduces, if any.
    reference_key: str = ""
    #: Rendering tier that produced the establishing slide.
    tier: int = 0
    #: Slide number of the establishing slide; 0 while it is only a directive.
    established_at_slide: int = 0
    #: Hash of the upstream directive this entry was derived from, so the same
    #: directive arriving again is recognised and does not trigger a redesign.
    upstream_fingerprint: str = ""

    @property
    def established(self) -> bool:
        """True once a slide of this family has been built and accepted on it."""
        return bool(self.layout_key) and self.established_at_slide > 0


class DeckDesignContext(BaseModel):
    """Deck-level design decisions that must hold across independently built slides.

    Passed into each PPT Creator Agent call by upstream agents (optional) and persisted
    per session by the agent, so a family layout established on one turn is reused on
    the next.
    """
    model_config = ConfigDict(extra="ignore")

    quiz_layout: FamilyLayout | None = None
    faq_layout: FamilyLayout | None = None

    @field_validator("quiz_layout", "faq_layout", mode="before")
    @classmethod
    def _accept_description(cls, value):
        """Let an upstream caller send a plain description instead of an object."""
        if isinstance(value, str):
            return FamilyLayout(description=value.strip()) if value.strip() else None
        return value

    @staticmethod
    def field_for(slide_type: str) -> str | None:
        """The attribute holding a slide type's family layout, or None for NORMAL."""
        return {SlideTypes.QUIZ: "quiz_layout",
                SlideTypes.FAQ: "faq_layout"}.get(slide_type)

    def get(self, slide_type: str) -> FamilyLayout | None:
        """The family layout recorded for a slide type, or None.

        Args:
            slide_type: One of `SlideTypes.FAMILIES`.

        Returns:
            The entry, which may still be an unestablished directive.
        """
        name = self.field_for(slide_type)
        return getattr(self, name) if name else None

    def with_layout(self, slide_type: str,
                    layout: FamilyLayout | None) -> "DeckDesignContext":
        """A copy of this context with one family's layout replaced.

        Args:
            slide_type: One of `SlideTypes.FAMILIES`.
            layout: The new entry, or None to clear it.

        Returns:
            The updated copy; this instance is left untouched.
        """
        name = self.field_for(slide_type)
        if not name:
            return self
        return self.model_copy(update={name: layout})


class SlideCodeBrief(BaseModel):
    """Everything the code-generating designer needs to build ONE slide.

    Deliberately self-contained: the designer never retrieves and never sees the rest of
    the deck, so this brief is the whole of its input. The contract fields below are
    what make a deck of independently generated slides still read as one deck - every
    parallel builder gets the same master, frame, grid and tokens, and its own assigned
    mode and layout.
    """
    slide_number: int
    kind: SlideKind = "content"
    content: SlideDraft
    context: DeckContext
    classification: str = "internal"
    # Local filesystem paths of images this slide may place, already materialized.
    image_paths: list[str] = []
    # Local path -> the image's description, so the designer can judge its relevance.
    image_notes: dict[str, str] = {}
    # The user asked for images on this slide: the offered images get priority.
    images_requested: bool = False
    # Free-text steer from the planner/user for this turn (e.g. "make it a 3-stage flow").
    design_hint: str = ""

    # --- from the deck design contract ------------------------------------- #
    #: "layout" fills a real layout's placeholders; "canvas" composes freely on a
    #: canvas_capable layout from the same master.
    mode: Literal["layout", "canvas"] = "layout"
    #: Catalog key, e.g. "M1-L10". Empty means the builder resolves one itself.
    layout_key: str = ""
    grid: dict = {}
    frame: dict = {}
    tokens: dict = {}
    #: [left, top, width, height] in inches; set in canvas mode.
    free_region: list[float] | None = None

    # --- generation parameters (all optional; defaults == the original behaviour) --- #
    #: Semantic type: NORMAL, QUIZ or FAQ. QUIZ and FAQ slides share one layout per deck.
    slide_type: str = SlideTypes.DEFAULT
    #: Visual freedom: LOW, MED or HIGH. Every level stays on the Rockwell brand.
    creativity_level: str = CreativityLevels.DEFAULT
    #: The user's own asks for this slide; they outrank the planner and the principles.
    user_instructions: str = ""
    #: Slide-specific guidance from the planner. Internal: never shown to the user.
    planner_special_instructions: str = ""
    #: Deck-level design decisions an upstream agent wants honoured (optional).
    deck_design_context: DeckDesignContext | None = None

    # --- max mode (all empty by default, so other slides are unaffected) ------- #
    #: Layouts the designer may choose between on the High Creativity master, each
    #: {"key", "kind": "title"|"blank", "free_region", "title_rect"?}.
    layout_choices: list[dict] = []
    #: Local .pptx paths of the neighbouring slides, attached as renders when the user
    #: asked for this slide to match the rest of the deck.
    neighbour_refs: list[str] = []
    #: The user said this slide looks inconsistent with its neighbours.
    match_neighbours: bool = False

    @field_validator("slide_type", mode="before")
    @classmethod
    def _normalize_slide_type(cls, value) -> str:
        """Upper-case the slide type; an unknown one degrades to NORMAL."""
        return _normalize_choice(value, SlideTypes.ALL, SlideTypes.DEFAULT, {},
                                 "slide_type")

    @field_validator("creativity_level", mode="before")
    @classmethod
    def _normalize_creativity(cls, value) -> str:
        """Upper-case the creativity level; HIGHEST maps to HIGH (not offered yet)."""
        return _normalize_choice(value, CreativityLevels.ALL, CreativityLevels.DEFAULT,
                                 CreativityLevels.ALIASES, "creativity_level")

    @field_validator("user_instructions", "planner_special_instructions", mode="before")
    @classmethod
    def _normalize_instructions(cls, value) -> str:
        """None means "no instructions"; surrounding whitespace is noise."""
        return str(value).strip() if value is not None else ""

    @property
    def is_family(self) -> bool:
        """True for QUIZ and FAQ slides, whose layout is shared across the deck."""
        return self.slide_type in SlideTypes.FAMILIES

    @property
    def uses_defaults(self) -> bool:
        """True when no generation parameter departs from the original behaviour."""
        return (self.slide_type == SlideTypes.DEFAULT
                and self.creativity_level == CreativityLevels.DEFAULT
                and not self.user_instructions
                and not self.planner_special_instructions)


class SlideBuildResult(BaseModel):
    """What came back from the rendering ladder for one slide."""
    slide_number: int
    pptx_path: str = ""                # local path of the produced single-slide .pptx
    #: Design attempts in the tier that produced the slide - a generation that came
    #: back with something to judge. 1 means "right first time", which is what the
    #: workflow's roll-up counts.
    attempts: int = 0
    #: Every gateway call this slide cost, across all tiers, including the ones that
    #: came back empty. This is the true cost; `attempts` is the design story.
    generations: int = 0
    #: Gateway calls that returned no program and no plan. High values mean an
    #: upstream problem, not a design one.
    upstream_failures: int = 0
    #: 1 = the creative designer, 2 = the layout-fitted plan, 3 = the deterministic
    #: render. `fallback_used` stays reserved for tier 3, which is what the workflow
    #: and the slide cache read.
    tier: int = 1
    layout_used: str = ""              # catalog key the slide actually landed on
    fallback_used: bool = False        # True when both model tiers were exhausted
    #: The geometry gate ran over the artifact being shipped and found nothing
    #: blocking. A slide reaching the cache with this False and `fallback_used` False
    #: escaped the ladder without ever being cleared, which is a bug, not a slide.
    gate_passed: bool = False
    #: Non-blocking notes the shipped artifact still carries (overflow warnings).
    advisory_notes: int = 0
    #: Blocking violations the shipped artifact still carries. Non-zero means it was
    #: shipped despite the gate, which only the deterministic renderer may do.
    blocking_violations: int = 0
    error: str = ""
    code_path: str = ""                # the generated program, kept for debugging
    #: The slide's semantic type, echoed back from the brief.
    slide_type: str = SlideTypes.DEFAULT
    #: "leader" when this slide established its family's layout, "follower" when it
    #: reused it, "answer" for a quiz answer slide, "" for NORMAL slides.
    family_role: str = ""
    #: The family layout the slide was built on, for QUIZ and FAQ slides.
    family_layout_key: str = ""
    #: A quiz question built at tier 1 whose program drew every option with
    #: `add_choice`, so its answer slide can be produced by re-running that program.
    reveal_ready: bool = False
    #: Built by max mode (render-and-review). `tier` stays 1: it is the same designer.
    max_mode: bool = False
    #: Visual reviews the slide cost, and the shipped candidate's score and verdict.
    visual_reviews: int = 0
    visual_score: int | None = None
    visual_passed: bool = False


class TakeawaysOutput(BaseModel):
    takeaways: list[str] = []

class DeckMetaOutput(BaseModel):
    """Deck-level fields translated separately from slide bodies."""
    title: str
    subtitle: str = ""
    takeaways: list[str] = []
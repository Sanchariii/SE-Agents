"""The max-mode visual judge: looks at one rendered slide and says whether it is good.

The geometry gate measures what a program drew; it cannot see balance, hierarchy, a
stray line or a cramped card. This module sends the slide's PNG (and, for a consistency
fix, its neighbours' PNGs) to a vision model and returns a typed verdict. Whether the
slide *passes* is decided here in code from that verdict - never by the model saying so.

It must not import `ppt_creator_agent` (which imports it).
"""

from pydantic import BaseModel, Field, field_validator

from app.config.constants import (
    AgentNames, CreativityLevels, ModelConstants, SlideReviewConstants as SRC,
)
from app.core.models.deck_model import SlideCodeBrief
from app.genai.agents.design_principles import (
    DESIGNER_PRINCIPLES, PRIORITY_RULE, REVIEW_CHECKLIST, REVIEW_CONSISTENCY_CLAUSE,
)
from app.utilities.analytics import agent_run
from app.utilities.apim_llm_utils import get_apim_llm_client, image_block
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

_SYSTEM = """You are the visual quality reviewer for Rockwell Automation slide decks used in
enterprise business meetings, corporate training and job aids. You are shown ONE slide
as it renders, and what the slide is meant to say. Judge only what you can see.

{checklist}

Context the designer worked under (use it to judge, never to redesign the brand):
{principles}

{priority}

SCORING (1-10): 10 = flawless, ready for an executive audience; 9 = polished, at most a
trivial nit; 7-8 = good but with visible flaws; 5-6 = clearly needs work; <=4 = broken.
SEVERITY per issue: "blocking" (unreadable, overlapping, clipped, broken), "major"
(clearly visible flaw an audience would notice), "minor" (polish).

Answer with JSON only:
{{"score": 1-10, "summary": "one sentence",
  "issues": [{{"severity": "blocking|major|minor", "category": "overlap|overflow|spacing|alignment|hierarchy|contrast|stray_shape|empty|image|clutter|balance|consistency|other",
              "location": "where on the slide", "problem": "what is wrong",
              "fix": "a concrete change, within the brand"}}]}}"""


class ReviewIssue(BaseModel):
    """One visible defect the judge found."""
    severity: str = "major"
    category: str = "other"
    location: str = ""
    problem: str = ""
    fix: str = ""

    @field_validator("severity", mode="before")
    @classmethod
    def _severity(cls, value) -> str:
        """Lower-case; an unknown severity counts as major, so it fails safe."""
        text = str(value or "").strip().lower()
        return text if text in SRC.SEVERITIES else "major"

    @field_validator("category", "location", "problem", "fix", mode="before")
    @classmethod
    def _bounded(cls, value) -> str:
        """Bound every free-text field; the judge's prose is fed back to the designer."""
        return str(value or "").strip()[:SRC.MAX_FIELD_CHARS]


class VisualVerdict(BaseModel):
    """The judge's answer for one render."""
    score: int = 1
    summary: str = ""
    issues: list[ReviewIssue] = Field(default_factory=list)

    @field_validator("score", mode="before")
    @classmethod
    def _score(cls, value) -> int:
        """Coerce and clamp to 1..10; an unreadable score is the worst one."""
        try:
            return max(1, min(10, int(round(float(value)))))
        except (TypeError, ValueError):
            return 1

    @field_validator("summary", mode="before")
    @classmethod
    def _summary(cls, value) -> str:
        return str(value or "").strip()[:SRC.MAX_FIELD_CHARS]

    @property
    def failing_issues(self) -> int:
        """Blocking and major issues: the ones that keep a slide from passing."""
        return sum(1 for i in self.issues if i.severity in SRC.FAILING_SEVERITIES)


def passed(verdict: VisualVerdict) -> bool:
    """Whether a slide clears the max-mode bar. Decided here, never by the model.

    Args:
        verdict: The judge's verdict.

    Returns:
        True when the score meets `PASS_SCORE` and no blocking or major issue remains.
    """
    return verdict.score >= SRC.PASS_SCORE and verdict.failing_issues == 0


def _system_prompt(with_neighbours: bool) -> str:
    """The reviewer's system prompt, with the consistency clause when neighbours exist."""
    checklist = REVIEW_CHECKLIST + ("\n\n" + REVIEW_CONSISTENCY_CLAUSE
                                    if with_neighbours else "")
    return _SYSTEM.format(checklist=checklist, principles=DESIGNER_PRINCIPLES,
                          priority=PRIORITY_RULE)


def _intent(brief: SlideCodeBrief, family_role: str, round_no: int,
            neighbours: int) -> str:
    """What the slide is meant to be, so the judge can check the content landed."""
    lines = [f"Slide {brief.slide_number} of {brief.context.total} in "
             f"\"{brief.context.title}\" (audience: {brief.context.audience or 'unstated'}).",
             f"Role: {brief.kind}; type: {brief.slide_type}; review round {round_no}."]
    if brief.creativity_level == CreativityLevels.HIGH:
        lines.append("Creativity is HIGH: bold, original composition is wanted, on brand.")
    if family_role == "leader":
        lines.append(f"This slide sets the design every later {brief.slide_type} slide "
                     f"will copy, so it must be clean and repeatable.")
    if brief.slide_type == "QUIZ":
        lines.append("Quiz question slide: options must look identical; the answer is "
                     "NOT shown here.")
    if brief.user_instructions:
        lines.append("The user's own instructions for this slide (they outrank your "
                     f"preferences): {brief.user_instructions}")
    if brief.planner_special_instructions:
        lines.append("Internal planner directions (never quote them): "
                     f"{brief.planner_special_instructions}")
    if neighbours:
        lines.append(f"The first image is the slide under review; the next {neighbours} "
                     f"image(s) are its neighbouring slides.")
    lines.append("Intended content:")
    lines.append(brief.content.design_json(indent=2, include_reveal=False))
    return "\n".join(lines)


def review(png: bytes, brief: SlideCodeBrief, *, family_role: str = "",
           round_no: int = 1, neighbours: list[bytes] | tuple = ()) -> VisualVerdict:
    """Ask the vision model to judge one rendered slide.

    Runs inside its own `slide_review` analytics span, so the judge's tokens are costed
    apart from the designer's. No work units are set: the slide is counted once, by the
    design span.

    Args:
        png: The slide render.
        brief: The slide's brief, for what it is meant to say.
        family_role: "leader" for a QUIZ/FAQ family leader, else "".
        round_no: Which review this is (1-based), for the prompt and the log.
        neighbours: Renders of the neighbouring slides, for a consistency fix.

    Returns:
        The verdict. Raises whatever the gateway raises; the caller decides.
    """
    model = ModelConstants.SLIDE_REVIEWER
    content = [image_block(png, "image/png", model)]
    content += [image_block(n, "image/png", model) for n in neighbours]
    content.append({"type": "text",
                    "text": _intent(brief, family_role, round_no, len(neighbours))})
    log.info("slide %d: visual review round %d on %s (%d neighbour image(s))",
             brief.slide_number, round_no, model, len(neighbours))
    with agent_run(AgentNames.SLIDE_REVIEW):
        verdict = get_apim_llm_client().chat_json(
            _system_prompt(bool(neighbours)), [{"role": "user", "content": content}],
            VisualVerdict, max_tokens=SRC.MAX_TOKENS, model=model,
            operation="slide_review")
    log.info("slide %d: review round %d scored %d/10 with %d issue(s) (%d failing) -> %s",
             brief.slide_number, round_no, verdict.score, len(verdict.issues),
             verdict.failing_issues, "PASS" if passed(verdict) else "redo")
    log.debug("slide %d review round %d verdict: %s", brief.slide_number, round_no,
              verdict.model_dump_json())
    return verdict

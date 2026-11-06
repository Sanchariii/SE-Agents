"""The PPT Creator Agent: turns ONE slide's content into ONE .pptx, in three tiers.

Tier 1 hands the model the company presentation guide and one slide's grounded content
and asks for a `DeckBuilder` program. Every program is checked statically, executed
locally in a throwaway directory, then reopened and measured by the geometry gate; a
static rejection, a traceback and a gate violation are all repairs sharing one budget
of `PptCreatorConstants.MAX_CODE_FIX_ATTEMPTS` generations.

Tier 2 takes over when the creative tier runs out. The model no longer writes code: it
answers with a plan that names one of a shortlist of real layouts and fills that
layout's own placeholders. The plan is measured against the layout before anything
runs - so a plan whose copy cannot fit is rejected for free, with the measurement as
the repair instruction - then compiled deterministically into a `DeckBuilder` program
and gated exactly like tier 1. It has far less freedom than tier 1 and typically needs
one pass.

Tier 3 is the deterministic render on a real template layout, with no model involved at
all, so a stubborn slide can never cost the user a slide.

This agent does no retrieval and no planning: `ppt_agent` has already decided *what* the
slide says, and this agent only decides *how it looks*. Keeping the two apart is what lets
a design failure be retried without paying for retrieval again.

How it looks is steered by optional generation parameters on the brief - `slide_type`,
`creativity_level`, `user_instructions` and `planner_special_instructions` - which add
a direction block to the prompts and add nothing when left at their defaults. The
design principles and the priority rule (hard rules > user > planner > principles) sit
in the system prompts of both model tiers (`app.genai.agents.design_principles`). QUIZ and FAQ
slides additionally share one layout per deck: the first of a family to arrive leads and
establishes it, the rest follow it (see `app.genai.ppt.deck_design`).

Executing model-written code is deliberately contained: a per-slide temp directory, an
explicit minimal environment (the service's own secrets are never inherited), no
PYTHONPATH, a hard timeout, and - when the budget runs out - a deterministic render on
a real template layout, so a stubborn program can never cost the user a slide.
"""

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.config.constants import (
    CreativityLevels, ModelConstants, PptCreatorConstants,
    QuizConstants, SlideRenderConstants, SlideReviewConstants, SlideTypes,
)
from app.config.exceptions import SlideRenderError, UpstreamError
from app.config.settings import get_settings
from app.core.models.deck_model import (
    FamilyLayout, SlideBuildResult, SlideCodeBrief, SlideDraft,
)
from app.genai.agents import slide_reviewer
from app.genai.agents.design_principles import DESIGNER_PRINCIPLES, PRIORITY_RULE
from app.genai.ppt import (
    code_guard, deck_builder, deck_design, geometry as geo, layout_plan, measure,
    sandbox, slide_render,
)
from app.genai.ppt.template import catalog as catalog_mod, registry
from app.utilities import events_util as events, run_control, storage_util as storage
from app.utilities.apim_llm_utils import get_apim_llm_client, image_block
from app.utilities.logger_util import get_logger, log_context
from app.utilities.observability import build_metrics

log = get_logger(__name__)

#: A ```-fenced block, with an optional language tag. Non-greedy so several
#: blocks in one reply are matched separately rather than as one run.
_FENCED_RE = re.compile(r"```[^\n`]*\n(?P<body>.*?)```", re.DOTALL)

SYSTEM_SUFFIX = """
=== HOW TO ANSWER ===
You are a presentation designer who answers with CODE, never with prose.

Write ONE complete Python program that builds EXACTLY ONE slide through the DeckBuilder
API and saves it. Begin it with:

    from deck_builder import DeckBuilder
    from context import TEMPLATE_PATH, CATALOG, FRAME, OUTPUT_PATH, IMAGES

Those two modules are placed beside your program. Hard rules:
- Build the slide with `DeckBuilder(TEMPLATE_PATH, CATALOG, frame=FRAME)` and save it
  with `deck.save(OUTPUT_PATH)`. Never construct `Presentation(...)` yourself.
- `deck.save(OUTPUT_PATH)` is the LAST line of the program and the ONLY way to finish
  it. DeckBuilder has no finalize(), finish(), render(), export(), write() or close();
  calling one is an AttributeError after the whole slide has been built.
- Use the layout you were assigned, by its catalog key. Never invent a layout name.
- Use only the DeckBuilder component API. Never call `.shapes.add_*` directly.
- Colours are brand token names ('red', 'navy', 'accent1'). Never a hex literal.
- Do not set fonts. Do not set a slide size. Both come from the template.
- Place only the image files in IMAGES, by their exact paths. Never invent a path,
  never use the network, never write outside the output directory.
- Put the speaker notes from the brief on the slide with `set_notes`.
- Do not draw a footer, a slide number, a logo or a classification label. All of them
  are stamped for you, and drawing them is a rule 5 violation.
- No emoji, no placeholder lorem text, no "TODO".
- The program runs unattended with no arguments and no input, and prints nothing but
  its own progress.
"""

USER_TEMPLATE = """Build slide {slide_number} of {total} for the deck "{deck_title}".

Audience: {audience} | Objective: {objective} | Tone: {tone}
Slide role: {kind}
Classification: {classification}
Design steer: {design_hint}

=== YOUR ASSIGNMENT (from the deck design contract — do not change it) ===
Composition mode: {mode}
Layout: {layout_key}
{region_line}
Grid: {grid_columns} columns x {grid_rows} rows, {grid_gutter}in gutters, over the region above

=== THE LAYOUT YOU WERE ASSIGNED ===
Its placeholders (address them by idx or slot), the graphics it already draws, the
zones you must keep clear, and the region you may compose in:
{layout_details}

Save to OUTPUT_PATH. Do not hardcode any other path.

=== SLIDE CONTENT (never invent a fact; keep the wording where it fits) ===
When the content cannot fit readably - or a "DENSITY: condense" direction says so -
tighten the on-slide wording and move the detail into the speaker notes as guidance for
the presenter on how to elaborate each point. Never drop a fact.
{content_json}

=== LOCAL IMAGE FILES YOU MAY PLACE ===
{image_block}
{direction_block}"""

FIT_SYSTEM_PROMPT = """You fit slide content into a layout the company template
already draws. You are not designing a slide; the template designed it.

Answer with ONE JSON object and nothing else - no prose, no code, no markdown fence:

{
  "layout": "<catalog key of the layout you chose, exactly as offered>",
  "title": "<the slide's title>",
  "subtitle": "<optional>",
  "placeholders": [
    {"target": "<idx or slot name from that layout>", "bullets": ["...", "..."]},
    {"target": "<idx or slot name>", "text": "..."}
  ],
  "extras": [],
  "notes": "<speaker notes>",
  "dropped": ["<any line you left off the slide, verbatim>"]
}

Hard rules:
- Choose ONE layout from the list you are given, by its catalog key. Never invent one.
- Every "target" must be a placeholder that layout actually has. They are listed for
  you with their idx, their slot name and their size in inches.
- The content must FIT. Each placeholder's capacity is given as approx_chars and
  approx_lines. You may tighten wording and spread content across the placeholders to
  make it fit, and you may choose a roomier layout. You may NOT invent a fact, and
  anything you leave off the slide you must list verbatim in "dropped" AND carry into
  "notes" as guidance on how to elaborate it.
- A placeholder fill carries text OR bullets OR an image OR a table OR a chart.
- "extras" are for content the layout has no placeholder for, and only these types:
  textbox, card, stat, icon_circle, line - positioned in inches, inside the safe
  region, never overlapping a reserved zone. Prefer an empty "extras" list.
- Colours are brand token names ('red', 'navy', 'accent1'). Never a hex value.
- Do not describe a footer, a slide number, a logo or a classification label. They are
  stamped for you.
- No emoji, no lorem text, no "TODO".
"""

FIT_USER_TEMPLATE = """Fit slide {slide_number} of {total} of the deck "{deck_title}"
into one of the layouts below.

Audience: {audience} | Objective: {objective} | Tone: {tone}
Slide role: {kind}
Design steer: {design_hint}

The free-form designer already failed on this slide {spent} time(s). Its last problem
was:
{tier1_error}

=== LAYOUTS YOU MAY CHOOSE FROM ===
The first one is the layout this slide was assigned by the deck's design contract.
Prefer it, and choose another only if this content genuinely cannot fit it.
{candidate_block}

=== SLIDE CONTENT (do not invent facts; tighten and redistribute as needed) ===
{content_json}

=== LOCAL IMAGE FILES YOU MAY PLACE ===
{image_block}
{direction_block}"""

#: Heading of the per-slide direction block appended to both model prompts. The block
#: is empty for a slide with every generation parameter at its default, so the
#: original pipeline's prompts are unchanged byte for byte.
DIRECTION_HEADER = "\n=== SLIDE DIRECTION (refines the instructions above) ===\n"

SLIDE_TYPE_GUIDANCE = {
    SlideTypes.QUIZ: """SLIDE TYPE: QUIZ - one question the audience answers.
- CONTENT.quiz holds the question, its options, the 0-based correct_index and a
  counter ("Question 2 of 5"). Use exactly that wording.
- The question is the focal point: the largest text after the title, high on the slide.
- Every answer choice is its own element of identical size and style, in content
  order, labelled with a letter badge (A, B, C, D ...).
- Never single out the correct choice visually (no different colour, tick or
  emphasis). The answer lives in the speaker notes; keep them as given.
- The counter is a small label above the question.""",
    SlideTypes.FAQ: """SLIDE TYPE: FAQ - frequently asked questions with their answers.
- CONTENT.faq holds the question/answer pairs; number them from CONTENT.faq_start so
  the numbering continues across the deck's FAQ slides.
- Each question reads as a heading and its answer as body text right beneath or beside
  it. Pair them visibly (one card, row or band per pair) so no answer can be read
  against the wrong question.
- Mark every question the same way (a number badge or "Q") and give every pair the
  same structure and size.""",
}

#: Tier 1 only: how a quiz program must draw its options so the answer slide can be
#: produced by re-running it.
QUIZ_CHOICE_GUIDANCE = """QUIZ CHOICES (mandatory): draw EVERY option with
`s.add_choice(letter, option_text, left, top, width, height, correct=<True only for
the option at CONTENT.quiz.correct_index>)`, one call per option, in order, with the
letters A, B, C ... It draws all of them identically; the answer slide re-runs this
program with the reveal switched on, which is what highlights the correct card. Do not
draw the options with any other component and do not style any of them differently."""

# There is deliberately no HIGHEST entry: a level that drops the Rockwell styling is
# possible but not offered right now (see CreativityLevels). MED is the original
# behaviour, so it adds nothing to the prompt.
CREATIVITY_GUIDANCE = {
    CreativityLevels.LOW: """CREATIVITY: LOW - stay close to the template.
- Fill the assigned layout's own placeholders first and compose only in space it leaves
  free, using the template's established patterns (columns, cards, rows).
- No decorative extras: every element you add must carry content.""",
    CreativityLevels.MED: "",
    CreativityLevels.HIGH: """CREATIVITY: HIGH - broad freedom over layout and composition.
- You may compose a new arrangement from DeckBuilder components across your region (an
  asymmetric split, a hero statement, oversized numbers, a diagram built from shapes)
  whenever it communicates this content better than the layout's default arrangement.
- Stay on brand: brand colour tokens only, the theme font and the type scale. Never
  draw the logo, footer, slide number or classification label; they are stamped for
  you.""",
}

# --- max mode: added only to max-mode prompts --------------------------------- #
MAX_MODE_GUIDANCE = """
=== MAX MODE (this slide is rendered and reviewed visually) ===
After it passes the geometry gate, your slide is rendered to an image and judged by a
visual reviewer for an enterprise audience. Aim to pass on the first look:
- no overlaps, no clipped or overflowing text, nothing jammed against an edge
- no stray, extra or decorative shapes that carry no meaning; no empty boxes
- aligned edges, even gaps and consistent margins between parallel elements
- one clear reading order: title -> message -> detail; generous whitespace
- strong contrast; images never stretched or distorted"""

HIGH_MASTER_GUIDANCE = """
=== HIGH CREATIVITY MASTER (a clean canvas; the logo and footer are already on it) ===
`LAYOUT_CHOICES` in context.py lists the layouts you may build on. Call
`deck.add_slide(<key>)` with ONE of them and compose inside THAT entry's free_region
(it replaces FREE_REGION when you pick a different entry):
{choices}
- Use the "title" layout by default (it has an accent line beside the title). Use the
  "blank" layout only when the content is one large visual (a big diagram, a hero
  image, a full-width flow) that needs the whole canvas.
- Neither layout has a title placeholder. Title the slide with `slide.set_title(...)`:
  it draws the title exactly at that entry's title_rect, in its title_size and the
  brand font, so every slide's title lines up across the deck.
- Any sample text in the template for these layouts ("Click to edit title") is only a
  placeholder: it is removed from the slide automatically. Never draw, copy or cover it.
- Colours are always brand tokens and the font is always Barlow; the template's own
  theme colours and fonts for this master are replaced for you.
- Never draw the logo, footer or page number."""

NEIGHBOUR_GUIDANCE = """CONSISTENCY (mandatory): the user said this slide looks inconsistent
with the rest of the deck. The images below are its neighbouring slides as they render.
Match their title style and position, colour use, card and spacing style and density,
while keeping this slide's own content."""

VISUAL_FEEDBACK_TEMPLATE = """The image above is how your slide renders. A visual reviewer
scored it {score}/10 (round {round}; it needs {pass_score}/10 with no blocking or major
issue). {summary}
Issues:
{issues}
Fix every blocking and major issue, and as many minor ones as you can without breaking
what already works. The hard rules still win over the reviewer: brand colour tokens and
the theme font only, the type scale, nothing in a reserved zone, the frame untouched.
Resend the whole program."""

USER_INSTRUCTIONS_GUIDANCE = """USER INSTRUCTIONS FOR THIS SLIDE (highest priority after
the hard rules - they override the planner's directions and the design principles):
{instructions}"""

PLANNER_GUIDANCE = """PLANNER DIRECTIONS FOR THIS SLIDE (internal):
Follow them unless they conflict with the hard rules, the user's instructions or an
established layout above, which take precedence; they override the general design
principles. Never put this wording on the slide or in the speaker notes.
{instructions}"""

FAMILY_LEADER_GUIDANCE = """{heading} LAYOUT FOR THIS DECK (you establish it):
This is the first {label} slide built for this deck. Every other {label} slide in the
deck will be rebuilt from your slide with only the content changed, so give it a clean,
repeatable structure: a fixed region for each role and one element pattern repeated
per item."""

FAMILY_REPRODUCE_GUIDANCE = """Reproduce the brand template's reference design "{name}" ({key})
faithfully on your assigned layout with DeckBuilder components: the same arrangement,
positions, proportions, shape kinds, colour tokens and type sizes.
- Its texts are SAMPLE copy that only show each element's role. Replace them with this
  slide's content; never copy the sample wording.
- When this content has more or fewer items than the reference, repeat or drop the
  per-item element and keep the reference's spacing rhythm.
- An element that falls outside your region or the canvas margins keeps its role and
  moves the least distance needed; a small label just under the title may go into the
  layout's subtitle placeholder.
Reference elements (inches, back to front; fill, outline and text_color are brand
tokens):
{elements}"""

FAMILY_INSPIRE_GUIDANCE = """The brand template has a reference design for {label} slides,
"{name}" ({key}). Treat it as inspiration only: you may depart from it wherever your
directions call for something different. Its texts are sample copy; never copy them.
Reference elements (inches, back to front; colours are brand tokens):
{elements}"""

FAMILY_FOLLOWER_GUIDANCE = """ESTABLISHED {heading} LAYOUT (mandatory):
This deck's {label} layout was established by slide {leader}. Rebuild that exact design
with this slide's content: the same layout ({layout_key}), the same element positions
and sizes, the same component types, colour tokens and type sizes. Change only the
words; when the number of items differs, repeat or remove the per-item element at the
same spacing. Do not redesign it."""

FAMILY_EXEMPLAR_GUIDANCE = """The program that built it follows. Adapt it: keep its
structure, replace its content with this slide's, and use this slide's own OUTPUT_PATH
and IMAGES. Never reuse its text or any file path it names.
```python
{code}
```"""

FAMILY_FIT_GUIDANCE = """ESTABLISHED {heading} LAYOUT (mandatory):
Every {label} slide in this deck uses layout {layout_key}. Choose it; do not choose
another."""


#: Follows the image list only when a slide has images. Without a user ask they are
#: optional assets, so the designer's own diagram or layout stays its first choice.
IMAGE_OPTIONAL_NOTE = """These are optional assets chosen as relevant to this slide. Use one
or two only if it makes the slide communicate better than your own diagram or layout would;
otherwise ignore them. At most 2, each at its own aspect ratio."""

IMAGE_REQUESTED_NOTE = """The user asked for images on this slide: use the most relevant one
(or two) unless none fits the content; you may combine it with your diagram or layout. At
most 2, each at its own aspect ratio."""


def image_block_text(brief: SlideCodeBrief) -> str:
    """The image section of the design prompts.

    Args:
        brief: The slide, with its local image paths and their descriptions.

    Returns:
        "(none)" when the slide has no images - exactly the text the prompts carried
        before descriptions existed - else one line per image with its description,
        then the optional or requested note.
    """
    if not brief.image_paths:
        return "(none)"
    lines = [f"- {p} - {brief.image_notes[p]}" if brief.image_notes.get(p) else f"- {p}"
             for p in brief.image_paths]
    note = IMAGE_REQUESTED_NOTE if brief.images_requested else IMAGE_OPTIONAL_NOTE
    log.info("slide %d: %d image(s) offered to the designer as %s", brief.slide_number,
             len(brief.image_paths),
             "requested" if brief.images_requested else "optional assets")
    return "\n".join(lines) + "\n" + note


def content_json(content: SlideDraft) -> str:
    """A slide's content as the design prompts show it (empty quiz/FAQ fields omitted)."""
    return content.design_json(indent=2, include_reveal=False)


def _rehome(source: str, from_dir: Path, to_dir: Path) -> str:
    """Point every absolute reference to one sandbox directory at another.

    Covers the spellings a program may carry: POSIX, native, and native with escaped
    backslashes (inside a normal string literal).

    Args:
        source: The program.
        from_dir: The sandbox it was written for.
        to_dir: The sandbox it will run in.

    Returns:
        The program with every reference moved.
    """
    native_from, native_to = str(from_dir), str(to_dir)
    for old, new in ((from_dir.as_posix(), to_dir.as_posix()),
                     (native_from.replace("\\", "\\\\"), native_to.replace("\\", "\\\\")),
                     (native_from, native_to)):
        source = source.replace(old, new)
    return source


def _render_lines(content: SlideDraft) -> list[str]:
    """The text lines the deterministic renderer puts on a slide.

    Quiz and FAQ content is structured rather than in blocks, so it is spelled out
    here: a quiz shows its counter, question and lettered options - plus the answer
    when the slide reveals it - and an FAQ shows numbered question/answer pairs.

    Args:
        content: The slide's content.

    Returns:
        The lines, in reading order.
    """
    lines: list[str] = []
    quiz = content.quiz
    if quiz is not None:
        if quiz.counter:
            lines.append(quiz.counter)
        lines.append(quiz.question)
        lines += [f"{QuizConstants.OPTION_LABELS[i]}. {option}"
                  for i, option in enumerate(quiz.options)]
        if quiz.reveal:
            lines.append(quiz.answer_line())
    for n, item in enumerate(content.faq, start=content.faq_start):
        lines += [f"{n:02d}. {item.question}", item.answer]
    lines += [item for block in content.blocks
              for item in (block.bullets or ([block.text] if block.text else []))]
    return lines


#: The size the deterministic renderer measures its body text at when spreading it.
_FALLBACK_FLOOR_PT = 12


def _spread_lines(lines: list[str], bodies: list[dict]) -> tuple[list[list[str]], list[str]]:
    """Share text lines across a layout's body slots, keeping only what fits.

    Each slot takes its even share of what is left, as long as that fits it at
    `_FALLBACK_FLOOR_PT`. Lines no slot can hold are returned as the overflow, which the
    caller moves to the speaker notes rather than letting text spill off the slide.

    Args:
        lines: The lines, in reading order.
        bodies: The body/content placeholder dicts, in reading order.

    Returns:
        (one list of lines per slot, the lines that fit nowhere).
    """
    rest = list(lines)
    groups: list[list[str]] = []
    for position, info in enumerate(bodies):
        slots_left = len(bodies) - position
        share = rest[:max(1, -(-len(rest) // slots_left))]
        while share and not measure.fits(share, info["width"], info["height"],
                                         _FALLBACK_FLOOR_PT, bullets=len(share) > 1):
            share = share[:-1]
        if not share and rest and position == 0:
            # Never leave the first slot empty: one line always lands on the slide.
            share = rest[:1]
        groups.append(share)
        rest = rest[len(share):]
    return groups, rest


def build_id_of(workdir: Path) -> str:
    """The short build id for a slide working directory.

    `tempfile.mkdtemp` names the turn's directory `ppt_creator_<random>`; the random
    part alone is the build id. Two builds of the same session get different ones,
    which is what a session id cannot distinguish, and it is also the directory a
    traceback names - so a log line and a stack trace can be tied together.

    Args:
        workdir: The turn's working directory.

    Returns:
        The id, or the directory's plain name when it carries no known prefix.
    """
    name = workdir.name
    prefix = PptCreatorConstants.WORKDIR_PREFIX
    return name[len(prefix):] if name.startswith(prefix) else name


@lru_cache
def load_brand_skill() -> str:
    """Read the company presentation guide that steers every generated slide.

    Returns:
        The markdown guide as text, or an empty string when it is missing (the slide is
        still generated, just without brand guidance).
    """
    path = (Path(__file__).resolve().parents[1] / "ppt"
            / PptCreatorConstants.SKILL_DIR / PptCreatorConstants.SKILL_MD_FILENAME)
    try:
        guide = path.read_text(encoding="utf-8")
    except OSError as exc:                                   # noqa: BLE001
        # Degraded, not fatal: better an unbranded slide than no deck at all.
        log.warning("brand skill guide unreadable at %s: %s; every slide will be "
                    "generated with no brand guidance at all", path, exc)
        return ""
    log.info("brand guide loaded from %s (%d chars)", path.name, len(guide))
    return guide

def materialize_images(slide: SlideDraft, workdir: Path,
                       session_id: str = "", notes: dict[str, str] | None = None,
                       report_missing: bool = True) -> list[str]:
    """Download the images a slide references so the generated code can place them.

    Args:
        slide: The slide whose image candidates (and legacy block `image_ref`s) carry
            storage paths.
        workdir: Where the images are written for the generated program to open.
        session_id: When given, ONE event is published if any image could not be
            fetched. A slide quietly shipping without its figure is content loss the
            user can only see by opening the deck; it is worth a line. Nothing is
            published on the happy path.
        notes: When given, filled with local path -> the image's description.
        report_missing: False logs a failed download without publishing the event, for
            images that were only optional assets.

    Returns:
        Local filesystem paths of the images that were fetched.
    """
    paths: list[str] = []
    # runs before build_slide creates this same directory, so create it here too
    workdir.mkdir(parents=True, exist_ok=True)
    wanted = slide.image_refs()
    described = {c.ref.strip(): c.description for c in slide.image_candidates}
    if wanted:
        log.info("slide %d references %d image(s); downloading them into %s",
                 slide.slide_number, len(wanted), workdir)
    for index, ref in enumerate(wanted, start=1):
        try:
            data = storage.read_file(ref)
        except Exception as exc:                             # noqa: BLE001
            # One missing image costs that image, not the slide. The program is simply
            # never told about it.
            log.warning("image %s unavailable for slide %d: %s", ref,
                        slide.slide_number, exc, exc_info=True)
            continue
        # Indexed: every document names its figures figure_001.png..., so two documents'
        # figures would otherwise overwrite each other here.
        local = workdir / f"img{index}_{Path(ref).name}"
        local.write_bytes(data)
        log.debug("materialized %s -> %s (%d bytes)", ref, local.name, len(data))
        paths.append(str(local))
        if notes is not None and described.get(ref):
            notes[str(local)] = described[ref]
    missing = len(wanted) - len(paths)
    if missing:
        log.warning("slide %d: %d of %d referenced image(s) could not be fetched and "
                    "will be missing from the slide", slide.slide_number, missing,
                    len(wanted))
        if session_id and report_missing:
            events.publish(
                session_id, "agent_step", agent="ppt-code",
                message=(f"Slide {slide.slide_number}: {missing} of {len(wanted)} "
                         f"image(s) could not be retrieved and will be missing."),
                slide_number=slide.slide_number, phase="image_missing")
    return paths

@dataclass
class _TierOutcome:
    """What one rung of the ladder achieved, and what the next rung should know.

    The agent is called from several threads and keeps no mutable state, so a tier
    reports back through a value rather than through `self`.
    """
    #: The finished slide, or None when this tier did not produce one.
    result: SlideBuildResult | None = None
    #: Design attempts this tier made - generations that came back with something to
    #: judge. Never the tier's budget: a tier that stopped early reports what it did.
    attempts: int = 0
    #: Gateway calls this tier issued, including the ones that returned nothing.
    generations: int = 0
    #: Of those, the ones that came back empty.
    upstream_failures: int = 0
    #: The last problem, already formatted for the model.
    last_error: str = ""
    #: True when every attempt failed before any output existed - a gateway outage
    #: rather than a design problem. The next model-driven tier skips itself.
    upstream_only: bool = False
    #: Why this tier did not run at all. Empty when it ran.
    skipped: str = ""


class _GenerationCounter:
    """Strictly increasing generation numbers for ONE slide, across every tier.

    Two things made the old "attempt N/3" label untrustworthy: it restarted at 1 in
    each tier, and a gateway call that returned nothing was labelled the same as a
    real design attempt. A generation number is the thing that is genuinely unique -
    one per call to the gateway, counting up until the slide is finished - so a log
    reader can see exactly how many times a slide was sent upstream.

    One counter per `build_slide` call, used from that one thread, so it needs no lock.
    """

    __slots__ = ("count",)

    def __init__(self) -> None:
        self.count = 0

    def next(self) -> int:
        """Claim the next generation number."""
        self.count += 1
        return self.count


class PptCreatorAgent:
    """Generates, runs and repairs the python-pptx program for one slide at a time.

    One instance is created per turn and is safe to call from several threads, because
    every call works inside its own slide directory and keeps no mutable state.
    """

    def __init__(self, session_id: str, workdir: Path) -> None:
        """
        Args:
            session_id: Session that receives progress events.
            workdir: Directory owned by this turn; each slide gets a subdirectory of it.
        """
        self.session_id = session_id
        self.workdir = workdir
        # The working directory's unique suffix, e.g. "ppt_creator_m41nb1mq" -> the
        # build id every line of this build carries. It is what tells two builds of
        # the SAME session apart, which a session id alone cannot do, and it is the
        # only handle that survives into historical logs and onto disk.
        self.build_id = build_id_of(workdir)
        # Which layouts fail the gate, and on which rules. Written from every worker
        # thread, so it is guarded; read once when the pass is over. This is how a
        # systemic constraint bug - one layout failing on rule 5 every single time -
        # becomes visible as a pattern instead of 40 unrelated-looking repair loops.
        self._gate_stats: Counter = Counter()
        self._gate_stats_lock = threading.Lock()
        # Deck-level family layouts (QUIZ / FAQ). Created on the first family slide, so
        # a deck of NORMAL slides never reads or writes any deck design state.
        self._design: deck_design.DeckDesignRegistry | None = None
        self._design_lock = threading.Lock()
        self.workdir.mkdir(parents=True, exist_ok=True)
        log.info("slide designer ready, working in %s",
                 workdir, extra={"session_id": session_id,
                                 "build_id": self.build_id})

    # --- prompt ------------------------------------------------------------- #
    def _system_prompt(self) -> str:
        """The brand guide plus the rules that make the generated program runnable.

        The slide size is no longer substituted in: it comes from the template and the
        program is forbidden to set it.
        """
        prompt = "\n\n".join((load_brand_skill().strip(), DESIGNER_PRINCIPLES,
                                SYSTEM_SUFFIX.strip(), PRIORITY_RULE)).strip()
        log.debug("system prompt assembled: %d chars", len(prompt))
        return prompt

    @staticmethod
    def _fit_system_prompt() -> str:
        """Tier 2's system prompt: the fitting rules, the principles and the priority."""
        return "\n\n".join((FIT_SYSTEM_PROMPT.strip(), DESIGNER_PRINCIPLES,
                             PRIORITY_RULE))

    def _layout_details_block(self, brief: SlideCodeBrief) -> str:
        """The assigned layout's contract, as JSON, for the prompt.

        Built by `design_tools.layout_details` - the same payload the
        `get_layout_details` tool returns - so the prompt and the tool can never
        describe the same layout differently.

        The design calls for the model to fetch this with a tool call. `_request_code`
        offers only the code-execution tool today, so a prompt that told it to call
        `get_layout_details` would leave it with a layout key and nothing else: no
        placeholder idx to address, no geometry, no reserved zones. Inlining is not a
        return of Bug 2 - that was `str.format` silently dropping a field the template
        never declared, and this one is declared and asserted.

        Args:
            brief: The slide's design assignment.

        Returns:
            The layout contract as indented JSON, or a short note when the layout
            cannot be resolved.
        """
        from app.genai.tools.design_tools import layout_details

        if not brief.layout_key:
            log.warning("slide %d has no assigned layout, so the prompt carries no "
                        "placeholder idx, geometry or reserved zones",
                        brief.slide_number, extra={"session_id": self.session_id})
            return "(no layout assigned; choose one that suits the content)"
        try:
            catalog = catalog_mod.load()
            layout = catalog.resolve_layout(brief.layout_key)
        except Exception as exc:                             # noqa: BLE001
            log.warning("slide %d: layout %r could not be described (%s); the model "
                        "will be working blind on this slide", brief.slide_number,
                        brief.layout_key, exc, extra={"session_id": self.session_id},
                        exc_info=True)
            return f"(layout {brief.layout_key} could not be read)"
        block = json.dumps(layout_details(catalog, layout, brief.frame or {}),
                           indent=2, default=str)
        log.debug("slide %d: inlined %s's contract into the prompt (%d chars)",
                  brief.slide_number, layout.key, len(block),
                  extra={"session_id": self.session_id})
        return block

    def _user_prompt(self, brief: SlideCodeBrief,
                     family: "deck_design.FamilyAssignment | None" = None) -> list[dict]:
        """The one slide the model is asked to draw, plus its design assignment.

        Every field named in the template is declared here, which is what Bug 2 was
        about: `template_path=` and `layout_map=` used to be passed to a template that
        declared neither, and `str.format` dropped both without a word.

        Args:
            brief: The slide's content, context and design assignment.
            family: The slide's QUIZ/FAQ family assignment, or None.

        Returns:
            The user turn as content blocks.
        """
        image_block = image_block_text(brief)
        grid = brief.grid or {}
        region = brief.free_region
        region_line = (f"Free region: left {region[0]:.2f}in, top {region[1]:.2f}in, "
                       f"{region[2]:.2f}in wide, {region[3]:.2f}in tall"
                       if region else
                       "Fill the layout's own placeholders; add components only in "
                       "space it genuinely leaves free.")
        text = USER_TEMPLATE.format(
            slide_number=brief.slide_number, total=brief.context.total,
            deck_title=brief.context.title,
            audience=brief.context.audience or "(unstated)",
            objective=brief.context.objective or "(unstated)",
            tone=brief.context.tone, kind=brief.kind,
            classification=brief.classification,
            design_hint=brief.design_hint or "(designer's choice)",
            mode=brief.mode, layout_key=brief.layout_key or "(choose one)",
            region_line=region_line,
            grid_columns=grid.get("columns", 12), grid_rows=grid.get("rows", 6),
            grid_gutter=grid.get("gutter_in", 0.15),
            layout_details=self._layout_details_block(brief),
            content_json=content_json(brief.content),
            image_block=image_block,
            direction_block=self._direction_block(brief, family, tier=1))
        log.info("slide %d assigned %s mode on layout %s; prompt is %d chars, "
                 "%d image(s), free_region=%s", brief.slide_number, brief.mode,
                 brief.layout_key or "(unassigned)", len(text),
                 len(brief.image_paths), region,
                 extra={"session_id": self.session_id})
        return [{"type": "text", "text": text}]

    # --- generation parameters ---------------------------------------------- #
    def _direction_block(self, brief: SlideCodeBrief,
                         family: "deck_design.FamilyAssignment | None",
                         tier: int) -> str:
        """The per-slide directions the generation parameters add to a prompt.

        Ordered by precedence - slide type, the user's instructions, creativity, the
        family layout, then the planner's instructions, which yield to everything above
        them and override only the general design principles.

        Args:
            brief: The slide, with its generation parameters.
            family: Its QUIZ/FAQ family assignment, or None.
            tier: 1 for the creative program, 2 for the layout-fitting plan. Tier 2 is
                the constrained fallback, so it gets no creativity freedom, no
                reference design and no exemplar program - only what still applies
                to filling a real layout.

        Returns:
            The block, or "" when nothing departs from the defaults - which is what
            keeps the original pipeline's prompts unchanged byte for byte.
        """
        sections = [SLIDE_TYPE_GUIDANCE.get(brief.slide_type, "")]
        if tier == 1 and brief.slide_type == SlideTypes.QUIZ and brief.content.quiz:
            sections.append(QUIZ_CHOICE_GUIDANCE)
        if brief.user_instructions:
            sections.append(USER_INSTRUCTIONS_GUIDANCE.format(
                instructions=brief.user_instructions))
        if tier == 1:
            sections.append(CREATIVITY_GUIDANCE.get(brief.creativity_level, ""))
        if family is not None:
            sections.append(self._family_guidance(brief, family, tier))
        if brief.planner_special_instructions:
            sections.append(PLANNER_GUIDANCE.format(
                instructions=brief.planner_special_instructions))
        sections = [s for s in sections if s]
        if not sections:
            return ""
        block = DIRECTION_HEADER + "\n\n".join(sections) + "\n"
        # The planner's wording is internal; only its size reaches the info log.
        log.info("slide %d tier %d direction: type %s, creativity %s, family %s, "
                 "%d-char user instructions, %d-char planner instructions; %d-char block",
                 brief.slide_number, tier, brief.slide_type, brief.creativity_level,
                 family.role if family else "-", len(brief.user_instructions),
                 len(brief.planner_special_instructions), len(block),
                 extra={"session_id": self.session_id})
        return block

    def _family_guidance(self, brief: SlideCodeBrief,
                         family: "deck_design.FamilyAssignment", tier: int) -> str:
        """What a QUIZ or FAQ slide is told about its family's shared layout.

        Args:
            brief: The slide, already moved onto its family's layout.
            family: Its leader or follower assignment.
            tier: 1 or 2, as in `_direction_block`.

        Returns:
            The family section of the direction block.
        """
        heading = brief.slide_type
        label = "quiz" if brief.slide_type == SlideTypes.QUIZ else brief.slide_type
        if family.role == "follower":
            established = family.family
            if tier == 2:
                return FAMILY_FIT_GUIDANCE.format(heading=heading, label=label,
                                                  layout_key=brief.layout_key)
            parts = [FAMILY_FOLLOWER_GUIDANCE.format(
                heading=heading, label=label,
                leader=established.established_at_slide,
                layout_key=brief.layout_key)]
            if established.description:
                parts.append(f"Its design brief: {established.description}")
            if established.exemplar_code:
                parts.append(FAMILY_EXEMPLAR_GUIDANCE.format(
                    code=established.exemplar_code.strip()))
            else:
                log.warning("slide %d follows the %s layout without an exemplar "
                            "program; consistency rests on the layout and its "
                            "description alone", brief.slide_number, brief.slide_type,
                            extra={"session_id": self.session_id})
            return "\n\n".join(parts)

        parts = [FAMILY_LEADER_GUIDANCE.format(heading=heading, label=label)]
        if family.family is not None and family.family.description:
            parts.append(f"Requested design for {label} slides: "
                         f"{family.family.description}")
        if tier == 1 and family.reference is not None and family.reference_usage:
            template = (FAMILY_REPRODUCE_GUIDANCE
                        if family.reference_usage == "reproduce"
                        else FAMILY_INSPIRE_GUIDANCE)
            parts.append(template.format(
                label=label, name=family.reference.name, key=family.reference.key,
                elements=self._format_reference(family.reference_shapes)))
        return "\n\n".join(parts)

    @staticmethod
    def _format_reference(shapes: list[dict]) -> str:
        """A reference design's elements, one compact JSON object per line.

        Pictures are left out: a program may only place files from IMAGES, and the
        template's own marks are inherited or stamped, never redrawn.
        """
        lines = [json.dumps(shape, separators=(",", ":"))
                 for shape in shapes if shape.get("kind") != "picture"]
        return "\n".join(lines) or "(the reference could not be read; follow its name)"

    def _apply_creativity(self, brief: SlideCodeBrief, catalog) -> SlideCodeBrief:
        """Give a HIGH-creativity slide the canvas when its layout offers one.

        `layout` mode fills placeholders; `canvas` composes freely in the layout's free
        region. HIGH creativity asks for the latter wherever the assigned layout makes
        it possible. Every other level keeps the contract's assignment.

        Args:
            brief: The slide as assigned by the contract.
            catalog: The template catalog.

        Returns:
            The brief, switched to canvas mode when that applies.
        """
        if (brief.creativity_level != CreativityLevels.HIGH or brief.mode != "layout"
                or not brief.layout_key):
            return brief
        layout = catalog.get(brief.layout_key)
        if layout is None or not layout.canvas_capable or layout.free_region is None:
            log.info("slide %d: HIGH creativity, but %s offers no free canvas; it keeps "
                     "layout mode", brief.slide_number, brief.layout_key,
                     extra={"session_id": self.session_id})
            return brief
        log.info("slide %d: HIGH creativity moves %s from layout to canvas mode",
                 brief.slide_number, layout.key, extra={"session_id": self.session_id})
        return brief.model_copy(update={"mode": "canvas",
                                        "free_region": layout.free_region.to_list()})

    # --- model ---------------------------------------------------------------#
    def _request_code(self, messages: list[dict]) -> str:
        """Ask the model for the slide program and return its source.

        Args:
            messages: The conversation so far (brief, and any prior failed program).

        Returns:
            The python source of the generated program.

        Raises:
            UpstreamError: When the gateway answered without any generated code.
        """
        log.debug("requesting a program from %s: %d message(s)",
                  ModelConstants.PPT_CODER, len(messages),
                  extra={"session_id": self.session_id})
        extra = {}
        if PptCreatorConstants.ATTACH_PPTX_SKILL:
            # The generic pptx skill brings its own python-pptx guidance, which competes
            # with the brand guide; it is attached only when switched on.
            extra["container"] = {"skills": [{"type": PptCreatorConstants.SKILL_TYPE,
                                              "skill_id": PptCreatorConstants.SKILL_ID,
                                              "version": PptCreatorConstants.SKILL_VERSION}]}
        log.debug("tier 1 request %s the pptx container skill",
                  "with" if extra else "without", extra={"session_id": self.session_id})
        model = ModelConstants.PPT_CODER
        kwargs: dict = {"max_tokens": PptCreatorConstants.MAX_TOKENS}
        if model in PptCreatorConstants.CODE_EXECUTION_UNSUPPORTED_MODELS:
            # The gateway refuses the tool (and the container skill riding on it) for
            # this model; the prompt already asks for code, so a plain reply carries it.
            log.debug("tier 1 request for %s without the code-execution tool", model,
                      extra={"session_id": self.session_id})
        else:
            kwargs["tools"] = [{"type": PptCreatorConstants.CODE_EXECUTION_TOOL_TYPE,
                                "name": PptCreatorConstants.CODE_EXECUTION_TOOL_NAME}]
            kwargs.update(extra)
        response = get_apim_llm_client().foundry_chat(messages, model=model, **kwargs)
        source = _extract_code(response)
        log.debug("received a %d-char program", len(source),
                  extra={"session_id": self.session_id})
        return source

    # --- sandbox -------------------------------------------------------------#
    @staticmethod
    def _normalize_code(source: str, slide_dir: Path) -> str:
        """Point sandbox-only paths at the slide's own directory.

        The model writes for a Linux sandbox and hardcodes paths like `/tmp/deck.pptx`,
        which are invalid on Windows and outside our control everywhere else.

        Args:
            source: The generated program.
            slide_dir: Directory the slide is allowed to write into.

        Returns:
            The program with every known sandbox prefix redirected into `slide_dir`.
        """
        # Forward slashes stay valid on Windows in Python and avoid escape headaches.
        target = f"{slide_dir.as_posix()}/"
        for prefix in PptCreatorConstants.SANDBOX_PATH_PREFIXES:
            if prefix in source:
                source = source.replace(prefix, target)
                # The program hardcoded a path it was told not to. It runs after this,
                # but it is also a sign the model ignored OUTPUT_PATH.
                log.warning("generated program hardcoded %s; redirected to %s",
                            prefix, target)
        return source

    @staticmethod
    def _sandbox_env(slide_dir: Path) -> dict:
        """Build the environment the generated program runs under.

        Deliberately minimal: the service's own secrets (APIM, Postgres, Blob) are never
        inherited by model-written code, and every temp file lands in the slide directory.

        Args:
            slide_dir: The only directory the program should write to.

        Returns:
            The environment mapping for the subprocess.
        """
        env = {"PATH": os.environ.get("PATH", ""),
               "TMP": str(slide_dir), "TEMP": str(slide_dir), "TMPDIR": str(slide_dir),
               "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
               "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
               "HOME": str(slide_dir), "USERPROFILE": str(slide_dir)}
        built = {k: v for k, v in env.items() if v}
        log.debug("sandbox env: %d variable(s) (%s), no PYTHONPATH, no inherited "
                  "secrets", len(built), ", ".join(sorted(built)))
        return built

    def _execute(self, code_path: Path, slide_dir: Path) -> tuple[bool, str]:
        """Run one generated program and report whether it succeeded.

        Args:
            code_path: The program to run.
            slide_dir: Working directory and the program's only writable location.

        Returns:
            Tuple of (success flag, error text — empty when it succeeded).
        """
        log.info("executing %s (timeout %ss)", code_path.name,
                 PptCreatorConstants.EXEC_TIMEOUT_SECONDS,
                 extra={"session_id": self.session_id})
        try:
            result = subprocess.run(
                [sys.executable, str(code_path)], cwd=str(slide_dir),
                capture_output=True, text=True, env=self._sandbox_env(slide_dir),
                timeout=PptCreatorConstants.EXEC_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            log.error("generated slide program timed out after %ss (%s)",
                      PptCreatorConstants.EXEC_TIMEOUT_SECONDS, code_path,
                      extra={"session_id": self.session_id})
            return False, (f"The program did not finish within "
                           f"{PptCreatorConstants.EXEC_TIMEOUT_SECONDS}s and was killed. "
                           f"Make it finish quickly and deterministically.")
        if result.returncode == 0:
            if result.stdout.strip():
                log.debug("program stdout: %s", result.stdout.strip()[:800],
                          extra={"session_id": self.session_id})
            log.info("%s exited cleanly", code_path.name,
                     extra={"session_id": self.session_id})
            return True, ""
        error = (result.stderr or result.stdout
                 or "exited non-zero with no output")[
            -PptCreatorConstants.MAX_STDERR_CHARS:]
        log.error("%s exited %d\n=== traceback ===\n%s", code_path.name,
                  result.returncode, error, extra={"session_id": self.session_id})
        return False, error

    @staticmethod
    def _locate_output(slide_dir: Path, expected: Path) -> Path | None:
        """Find the .pptx the program produced.

        Args:
            slide_dir: Directory the program ran in.
            expected: The path the program was told to write.

        Returns:
            The expected file, else the newest .pptx anywhere under `slide_dir`, else None
            (models occasionally rename the file despite the instruction).
        """
        if expected.exists():
            return expected
        candidates = list(slide_dir.rglob("*.pptx"))
        if not candidates:
            log.warning("no .pptx anywhere under %s; the program saved nothing",
                        slide_dir)
            return None
        found = max(candidates, key=lambda p: p.stat().st_mtime)
        # The program was told to save to OUTPUT_PATH and did not. It still counts,
        # but the gate and the stitcher both key off the expected name.
        log.warning("expected %s but the program wrote %s; using it instead",
                    expected.name, found.name)
        return found

    # --- fallback ------------------------------------------------------------#
    def _render_on_layout(self, brief: SlideCodeBrief, out_path: Path) -> Path:
        """Render this slide's content on a real template layout, no model involved.

        This is the safety net, and it is clean by construction: the template
        positioned every element, so the slide inherits the theme, the fonts and the
        master's furniture, and it passes the gate. The retired `plain_slide_pptx`
        built on a blank layout and inherited nothing at all.

        Args:
            brief: The slide that could not be generated.
            out_path: Where to write the single-slide .pptx.

        Returns:
            The path of the written .pptx.
        """
        log.info("slide %d: rendering deterministically on a template layout",
                 brief.slide_number, extra={"session_id": self.session_id})
        catalog = catalog_mod.load()
        try:
            layout = catalog.resolve_layout(brief.layout_key)
        except LookupError:
            ranked = catalog.recommend(brief.kind or "content", limit=1)
            layout = ranked[0][0] if ranked else catalog.canonical_layouts()[0]
            log.warning("slide %d: assigned layout %r is unusable; falling back to %s",
                        brief.slide_number, brief.layout_key, layout.key,
                        extra={"session_id": self.session_id})

        deck = deck_builder.DeckBuilder(registry.template_path(), catalog.to_dict(),
                                        frame=brief.frame or None)
        slide = deck.add_slide(layout.key)
        # A layout with no placeholders at all is fixed artwork - the six closing and
        # section layouts that draw their own wording, logo and socials. The brand
        # guide's instruction for those is to add nothing, and stamping a title across
        # one covers the template's own design.
        if layout.placeholders:
            slide.set_title(brief.content.title or f"Slide {brief.slide_number}")
        else:
            log.info("slide %d: %s is fixed artwork with no placeholders, so nothing "
                     "is added to it", brief.slide_number, layout.key,
                     extra={"session_id": self.session_id})

        lines = _render_lines(brief.content)
        overflow: list[str] = []
        if lines:
            bodies = [p for p in slide.layout.placeholders
                      if p.get("role") in ("body", "content")]
            if brief.slide_type != SlideTypes.NORMAL:
                # Quiz and FAQ lines read in one sequence; never split them across slots.
                bodies = bodies[:1]
            target = bodies[0]["slot"] if bodies else None
            if target:
                groups, overflow = _spread_lines(lines, bodies)
                for info, group in zip(bodies, groups):
                    if group:
                        log.debug("slide %d: %d line(s) into placeholder %r",
                                  brief.slide_number, len(group), info["slot"])
                        slide.set_bullets(info["slot"], group)
                if overflow:
                    log.info("slide %d: %d line(s) do not fit %d body slot(s) even at "
                             "%dpt; they go to the speaker notes", brief.slide_number,
                             len(overflow), len(bodies), _FALLBACK_FLOOR_PT,
                             extra={"session_id": self.session_id})
            else:
                # A title-only layout: put the copy in the free region rather than
                # dropping it. Never silently lose content.
                log.info("slide %d: %s has no body placeholder; %d line(s) go into a "
                         "text box on the grid", brief.slide_number, layout.key,
                         len(lines), extra={"session_id": self.session_id})
                slide.add_textbox(lines, *slide.grid(0, 0, cols=12, rows=5), size=15)
        notes = brief.content.notes or ""
        if overflow:
            # Condensed, never dropped: what the slide cannot hold is elaborated aloud.
            notes = (notes + "\n\nAdditional points to elaborate:\n"
                     + "\n".join(f"- {line}" for line in overflow)).strip()
        if notes:
            slide.set_notes(notes)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        written = deck.save(out_path)
        for warning in deck.warnings:
            log.warning("slide %d fallback render: %s", brief.slide_number, warning,
                        extra={"session_id": self.session_id})
        log.info("slide %d rendered on %s -> %s (%d bytes)", brief.slide_number,
                 layout.key, written.name, written.stat().st_size,
                 extra={"session_id": self.session_id})
        return written

    # --- tier 1: the creative designer ---------------------------------------#
    def _tier1_creative(self, brief: SlideCodeBrief, catalog, slide_dir: Path,
                        out_path: Path, code_path: Path,
                        counter: "_GenerationCounter",
                        family: "deck_design.FamilyAssignment | None" = None
                        ) -> _TierOutcome:
        """Let the model design the slide freely, and repair it until the gate clears.

        Unchanged from the single-tier design except for its budget and its exit: a
        spent budget is now a tier boundary rather than the end of the road.

        Args:
            brief: The slide's content, context and design assignment.
            catalog: The template catalog.
            slide_dir: This slide's staged sandbox.
            out_path: Where a program is told to save.
            code_path: Where each generated program is written.
            counter: This slide's generation counter, shared with tier 2.
            family: The slide's QUIZ/FAQ family assignment, or None.

        Returns:
            A `_TierOutcome` carrying the slide, or the reason the tier came up empty.
        """
        limit = PptCreatorConstants.MAX_CODE_FIX_ATTEMPTS
        upstream_budget = PptCreatorConstants.MAX_UPSTREAM_RETRIES
        log.info("slide %d entering tier 1 (creative, %d design attempt(s) allowed, "
                 "%d upstream retry/retries)", brief.slide_number, limit,
                 upstream_budget, extra={"session_id": self.session_id})
        messages = [{"role": "system", "content": self._system_prompt()},
                    {"role": "user", "content": self._user_prompt(brief, family)}]
        last_error, produced_anything = "", False
        # Design attempts and transport failures are counted apart on purpose: a
        # gateway that answers with nothing teaches the model nothing, so charging it
        # to the design budget spends a slide's whole allowance on an outage.
        attempt, upstream_failures, issued = 0, 0, 0
        # A slide that passed the gate but was sent back once to be polished. If the
        # polish fails, this one ships: a polish may only ever improve a slide.
        best: _TierOutcome | None = None

        while attempt < limit:
            if best is not None and attempt > best.attempts:
                log.info("slide %d: the polish attempt did not pass the gate; shipping "
                         "the slide accepted on attempt %d", brief.slide_number,
                         best.attempts, extra={"session_id": self.session_id})
                return best
            generation = counter.next()
            issued += 1
            try:
                source = self._request_code(messages)
            except Exception as exc:                          # noqa: BLE001
                upstream_failures += 1
                last_error = str(exc)
                log.error("slide %d tier 1 generation %d returned no program "
                          "(upstream failure %d/%d; design attempt %d/%d NOT "
                          "consumed): %s", brief.slide_number, generation,
                          upstream_failures, upstream_budget, attempt + 1, limit, exc,
                          extra={"session_id": self.session_id}, exc_info=True)
                if upstream_failures >= upstream_budget:
                    log.error("slide %d leaving tier 1: %d gateway call(s) in a row "
                              "produced nothing, so this is an upstream problem and "
                              "more calls will not help", brief.slide_number,
                              upstream_failures, extra={"session_id": self.session_id})
                    break
                continue

            attempt += 1
            produced_anything = True
            source = code_guard.strip_code_fences(
                self._normalize_code(source, slide_dir))
            code_path.write_text(source, encoding="utf-8")
            log.info("slide %d tier 1 attempt %d/%d (generation %d): program "
                     "generated (%d chars)", brief.slide_number, attempt, limit,
                     generation, len(source), extra={"session_id": self.session_id})

            # 1. Static check. A rejection here costs no execution at all.
            rejection = code_guard.check_message(source)
            if rejection:
                last_error = rejection
                log.warning("slide %d tier 1 attempt %d/%d rejected by the static "
                            "guard, no execution spent:\n%s", brief.slide_number,
                            attempt, limit, rejection,
                            extra={"session_id": self.session_id})
                self._step(brief, f"attempt {attempt} rejected before running",
                           phase="repair", tier=1, attempts=attempt)
                messages += [{"role": "assistant", "content": source},
                             {"role": "user", "content": rejection}]
                continue

            # 2. Execute. A choices sidecar or builder report from an earlier attempt
            # must not vouch for this one.
            (slide_dir / deck_builder.CHOICES_FILENAME).unlink(missing_ok=True)
            (slide_dir / deck_builder.REPORT_FILENAME).unlink(missing_ok=True)
            succeeded, error = self._execute(code_path, slide_dir)
            produced = self._locate_output(slide_dir, out_path) if succeeded else None
            if produced is None:
                last_error = error or "the program ran but wrote no .pptx file"
                log.warning("slide %d tier 1 attempt %d/%d: the program failed: %s",
                            brief.slide_number, attempt, limit, last_error[:500],
                            extra={"session_id": self.session_id})
                self._step(brief, f"attempt {attempt} failed; repairing the code",
                           phase="repair", tier=1, attempts=attempt)
                messages += [
                    {"role": "assistant", "content": source},
                    {"role": "user", "content":
                        "Running that program failed. Fix it so it runs unattended and "
                        "saves the slide to OUTPUT_PATH.\n\n=== ERROR ===\n"
                        + last_error}]
                continue

            # 3. Gate. Measure what landed, not what was declared.
            violations = self._gate(produced, brief, catalog)
            choice_error = (self._check_choices(brief, produced.parent)
                            if geo.is_clean(violations) else "")
            if geo.is_clean(violations) and not choice_error:
                log.info("slide %d accepted at tier 1 on attempt %d/%d (generation "
                         "%d): %s (%d advisory note(s))", brief.slide_number, attempt,
                         limit, generation, produced.name, len(violations),
                         extra={"session_id": self.session_id})
                outcome = _TierOutcome(result=SlideBuildResult(
                    slide_number=brief.slide_number, pptx_path=str(produced),
                    attempts=attempt, generations=counter.count,
                    upstream_failures=upstream_failures, tier=1,
                    layout_used=brief.layout_key, code_path=str(code_path),
                    gate_passed=True, advisory_notes=len(violations),
                    reveal_ready=self._needs_choices(brief)),
                    attempts=attempt, generations=issued,
                    upstream_failures=upstream_failures)
                polish = [v for v in violations if v.rule in (10, 15)]
                if (PptCreatorConstants.POLISH_ADVISORIES and best is None and polish
                        and attempt < limit and family is None
                        and brief.slide_type == SlideTypes.NORMAL):
                    # Keep this slide safe from the next attempt, which overwrites both
                    # the program and its output.
                    best = self._keep_accepted(outcome, produced, code_path, attempt)
                    request = (geo.format_violations(polish).replace(
                        "did not pass the geometry gate", "passed the geometry gate, "
                        "but can be cleaner") + self._builder_notes(produced.parent))
                    log.info("slide %d: accepted with %d advisory note(s) (rules %s); "
                             "asking for one polish pass", brief.slide_number,
                             len(polish), sorted({v.rule for v in polish}),
                             extra={"session_id": self.session_id})
                    self._step(brief, f"attempt {attempt} passed; polishing",
                               phase="repair", tier=1, attempts=attempt)
                    messages += [{"role": "assistant", "content": source},
                                 {"role": "user", "content": request}]
                    continue
                if best is not None:
                    log.info("slide %d: the polish on attempt %d passed the gate",
                             brief.slide_number, attempt,
                             extra={"session_id": self.session_id})
                self._step(brief, f"slide rendered on attempt {attempt}",
                           phase="done", tier=1, attempts=attempt)
                return outcome

            if choice_error:
                last_error = choice_error
                log.warning("slide %d tier 1 attempt %d/%d passed the gate but drew "
                            "its quiz choices wrongly: %s", brief.slide_number, attempt,
                            limit, choice_error, extra={"session_id": self.session_id})
                self._step(brief, f"attempt {attempt}: quiz options need fixing",
                           phase="repair", tier=1, attempts=attempt)
                messages += [{"role": "assistant", "content": source},
                             {"role": "user", "content": choice_error}]
                continue

            last_error = (geo.format_violations(violations)
                          + self._builder_notes(produced.parent))
            log.warning("slide %d tier 1 attempt %d/%d failed the gate with %d "
                        "violation(s) (%d blocking):\n%s", brief.slide_number, attempt,
                        limit, len(violations),
                        sum(1 for v in violations if v.blocking), last_error,
                        extra={"session_id": self.session_id})
            self._step(brief, f"attempt {attempt}: {len(violations)} layout issue(s); "
                              f"redesigning", phase="repair", tier=1, attempts=attempt)
            messages += [{"role": "assistant", "content": source},
                         {"role": "user", "content": last_error}]

        if best is not None:
            log.info("slide %d: tier 1 budget spent after a polish request; shipping the "
                     "slide accepted on attempt %d", brief.slide_number, best.attempts,
                     extra={"session_id": self.session_id})
            return best
        log.error("slide %d leaving tier 1 unsatisfied after %d design attempt(s) and "
                  "%d generation(s) (%d upstream failure(s)); last problem: %s",
                  brief.slide_number, attempt, issued, upstream_failures,
                  last_error[:500], extra={"session_id": self.session_id})
        return _TierOutcome(attempts=attempt, generations=issued,
                            upstream_failures=upstream_failures,
                            last_error=last_error,
                            upstream_only=not produced_anything)

    @staticmethod
    def _keep_accepted(outcome: "_TierOutcome", produced: Path, code_path: Path,
                       attempt: int, prefix: str = "accepted") -> "_TierOutcome":
        """Copy an accepted slide and its program aside, before a polish attempt.

        Args:
            outcome: The accepted attempt.
            produced: Its .pptx.
            code_path: Its program, which the next attempt overwrites.
            attempt: Its attempt number, for the file names.
            prefix: File-name prefix of the copies ("max" for max-mode candidates).

        Returns:
            The same outcome, pointing at the copies.
        """
        kept_pptx = produced.with_name(f"{prefix}_{attempt}_{produced.name}")
        kept_code = code_path.with_name(f"{prefix}_{attempt}_{code_path.name}")
        shutil.copy2(produced, kept_pptx)
        shutil.copy2(code_path, kept_code)
        outcome.result = outcome.result.model_copy(update={
            "pptx_path": str(kept_pptx), "code_path": str(kept_code)})
        return outcome

    @staticmethod
    def _builder_report(directory: Path) -> dict:
        """The builder's report sidecar from a program run, or {} when absent."""
        path = directory / deck_builder.REPORT_FILENAME
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, ValueError) as exc:                   # noqa: BLE001
            log.warning("builder report %s unreadable: %s", path, exc)
            return {}

    def _builder_notes(self, directory: Path) -> str:
        """The builder's warnings, formatted to append to a repair message."""
        warnings = self._builder_report(directory).get("warnings") or []
        if not warnings:
            return ""
        log.info("feeding %d builder warning(s) back to the model", len(warnings),
                 extra={"session_id": self.session_id})
        return ("\n\nBUILDER NOTES (DeckBuilder reported these while your program ran):\n"
                + "\n".join(f"- {w}" for w in warnings[:12]))

    # --- tier 2: fit the content into a real layout --------------------------#
    def _layout_candidates(self, brief: SlideCodeBrief, catalog,
                           family: "deck_design.FamilyAssignment | None" = None
                           ) -> list:
        """The layouts tier 2 may choose between: the assignment first, then alternates.

        Tier 1 often fails because the assigned layout cannot hold this slide's
        content, so refusing every alternative would just walk the slide to tier 3.
        Alternates are restricted to the assigned layout's own master, which is what
        keeps a deck of independently built slides reading as one deck.

        A slide following its family's established layout gets no alternates at all:
        landing it elsewhere would break the family. And the template's Quiz/FAQ
        reference layouts are never alternates, because their sample copy would show
        through whatever is fitted onto them.

        Args:
            brief: The slide's design assignment.
            catalog: The template catalog.
            family: The slide's QUIZ/FAQ family assignment, or None.

        Returns:
            `LayoutInfo` objects, the assignment first. Empty when nothing is fillable.
        """
        limit = PptCreatorConstants.LAYOUT_PLAN_CANDIDATES
        chosen: list = []
        assigned = None
        if brief.layout_key:
            try:
                assigned = catalog.resolve_layout(brief.layout_key)
            except LookupError:
                log.warning("slide %d: assigned layout %r is not in the catalog, so "
                            "tier 2 chooses freely from the preferred master",
                            brief.slide_number, brief.layout_key,
                            extra={"session_id": self.session_id})
        if assigned is not None and not assigned.placeholders:
            # Fixed artwork - the six closing and section layouts that draw their own
            # wording. There is nothing to fill, and moving the slide to some other
            # layout would overrule the deck's design rather than repair it.
            log.info("slide %d: assigned layout %s is fixed artwork with no "
                     "placeholders, so tier 2 has nothing to fit",
                     brief.slide_number, assigned.key,
                     extra={"session_id": self.session_id})
            return []
        if assigned is not None:
            chosen.append(assigned)
        if family is not None and family.role == "follower":
            log.info("slide %d: tier 2 is held to the deck's %s layout %s",
                     brief.slide_number, brief.slide_type,
                     assigned.key if assigned is not None else "(unresolved)",
                     extra={"session_id": self.session_id})
            return chosen
        master = (assigned.master_index if assigned is not None
                  else catalog.preferred_master)

        ranked = catalog.recommend(
            brief.kind or "content", text_blocks=len(brief.content.blocks) or 1,
            # Optional images must not pull the fallback toward picture layouts.
            needs_picture=brief.images_requested and bool(brief.image_paths),
            has_table=bool(brief.content.tables), limit=limit * 4)
        for layout, score, reasons in ranked:
            if len(chosen) >= limit:
                break
            if (layout.master_index != master or not layout.placeholders
                    or any(c.key == layout.key for c in chosen)
                    or deck_design.family_of(layout) is not None):
                continue
            chosen.append(layout)
            log.debug("slide %d: tier 2 candidate %s scored %.2f (%s)",
                      brief.slide_number, layout.key, score,
                      ", ".join(reasons) or "no reason given")
        log.info("slide %d: tier 2 may choose from %d layout(s) on master M%d: %s "
                 "(assigned %s)", brief.slide_number, len(chosen), master,
                 ", ".join(c.key for c in chosen) or "none",
                 assigned.key if assigned is not None else "(none)",
                 extra={"session_id": self.session_id})
        return chosen

    def _layout_plan_prompt(self, brief: SlideCodeBrief, catalog, candidates: list,
                            spent: int, tier1_error: str,
                            family: "deck_design.FamilyAssignment | None" = None
                            ) -> str:
        """The candidate layouts, this slide's content, and why tier 1 gave up.

        Each candidate is described with `design_tools.layout_details`, the same
        payload tier 1 and the `get_layout_details` tool use, so no two places can
        describe one layout differently.

        Args:
            brief: The slide's content, context and design assignment.
            catalog: The template catalog.
            candidates: Layouts from `_layout_candidates`.
            spent: Generations tier 1 spent.
            tier1_error: Tier 1's last problem, for context.
            family: The slide's QUIZ/FAQ family assignment, or None.

        Returns:
            The user turn as text.
        """
        from app.genai.tools.design_tools import layout_details

        blocks = []
        for layout in candidates:
            details = layout_details(catalog, layout, brief.frame or {})
            blocks.append(f"--- {layout.key} ({layout.name}) ---\n"
                          + json.dumps(details, indent=2, default=str))
        text = FIT_USER_TEMPLATE.format(
            slide_number=brief.slide_number, total=brief.context.total,
            deck_title=brief.context.title,
            audience=brief.context.audience or "(unstated)",
            objective=brief.context.objective or "(unstated)",
            tone=brief.context.tone, kind=brief.kind,
            design_hint=brief.design_hint or "(designer's choice)",
            spent=spent, tier1_error=(tier1_error or "(none recorded)")[:1500],
            candidate_block="\n\n".join(blocks) or "(none)",
            content_json=content_json(brief.content),
            image_block=image_block_text(brief),
            direction_block=self._direction_block(brief, family, tier=2))
        log.info("slide %d: tier 2 prompt is %d chars over %d candidate layout(s)",
                 brief.slide_number, len(text), len(candidates),
                 extra={"session_id": self.session_id})
        return text

    def _request_layout_plan(self, messages: list[dict]) -> layout_plan.LayoutPlan:
        """Ask the model for a layout plan and return it, parsed.

        `chat_json` validates against the schema and retries its own malformed-JSON
        repairs, so a plan that arrives here is structurally sound; whether it is
        *renderable* is `layout_plan.validate`'s question.

        Args:
            messages: The conversation so far (brief, and any rejected plan).

        Returns:
            The parsed plan.

        Raises:
            Exception: Whatever `chat_json` raises when the gateway or the schema
                defeats it; the tier-2 loop treats it as one spent attempt.
        """
        log.debug("requesting a layout plan from %s: %d message(s)",
                  ModelConstants.PPT_CODER, len(messages),
                  extra={"session_id": self.session_id})
        plan = get_apim_llm_client().chat_json(
            self._fit_system_prompt(), messages, layout_plan.LayoutPlan,
            max_tokens=PptCreatorConstants.LAYOUT_PLAN_MAX_TOKENS,
            model=ModelConstants.PPT_CODER)
        log.debug("received a plan on %s with %d fill(s)", plan.layout,
                  len(plan.placeholders), extra={"session_id": self.session_id})
        return plan

    def _tier2_layout_fit(self, brief: SlideCodeBrief, catalog, tier1: _TierOutcome,
                          counter: "_GenerationCounter",
                          family: "deck_design.FamilyAssignment | None" = None
                          ) -> _TierOutcome:
        """Make the content fit a real layout, from a plan that is checked before it runs.

        The model answers with a plan, not a program. An invalid plan is rejected by
        measurement - no execution spent - and the measurement is the repair
        instruction. An accepted plan is compiled deterministically and run in its own
        sandbox, then measured by the same gate as tier 1.

        Args:
            brief: The slide's content, context and design assignment.
            catalog: The template catalog.
            tier1: What tier 1 spent and how it failed, for the prompt and the skips.
            counter: This slide's generation counter, continued from tier 1.
            family: The slide's QUIZ/FAQ family assignment, or None.

        Returns:
            A `_TierOutcome` carrying the slide, or the reason this tier came up empty.
        """
        limit = PptCreatorConstants.MAX_LAYOUT_PLAN_ATTEMPTS
        upstream_budget = PptCreatorConstants.MAX_UPSTREAM_RETRIES
        if tier1.upstream_only:
            # Every tier 1 attempt died before producing anything, which is a gateway
            # problem. Three more gateway calls would fail the same way.
            reason = ("every creative attempt failed upstream, so the gateway - not "
                      "the design - is the problem")
            log.error("slide %d: skipping tier 2 (%s); going to tier 3",
                      brief.slide_number, reason,
                      extra={"session_id": self.session_id})
            return _TierOutcome(skipped=reason, last_error=tier1.last_error)

        candidates = self._layout_candidates(brief, catalog, family)
        if not candidates:
            reason = "no fillable layout to fit this slide into"
            log.warning("slide %d: skipping tier 2 (%s); going to tier 3",
                        brief.slide_number, reason,
                        extra={"session_id": self.session_id})
            return _TierOutcome(skipped=reason, last_error=tier1.last_error)

        log.info("slide %d entering tier 2 (layout-constrained, %d attempt(s) allowed) "
                 "after %d creative attempt(s)", brief.slide_number, limit,
                 tier1.attempts, extra={"session_id": self.session_id})
        self._step(brief, "fitting the content into a template layout",
                   phase="start", tier=2)

        fit_dir = (self.workdir / f"slide_{brief.slide_number}"
                   / PptCreatorConstants.LAYOUT_PLAN_DIRNAME)
        sandbox.stage(fit_dir, catalog, {
            "frame": brief.frame, "grid": brief.grid, "tokens": brief.tokens,
            "mode": "layout", "layout_key": brief.layout_key,
            "free_region": brief.free_region}, brief.image_paths)
        out_path = sandbox.output_path(fit_dir)
        code_path = fit_dir / PptCreatorConstants.LAYOUT_PLAN_CODE_FILENAME

        messages = [{"role": "user", "content": self._layout_plan_prompt(
            brief, catalog, candidates, tier1.attempts, tier1.last_error, family)}]
        last_error = tier1.last_error
        attempt, upstream_failures, issued = 0, 0, 0

        while attempt < limit:
            generation = counter.next()
            issued += 1
            try:
                plan = self._request_layout_plan(messages)
            except Exception as exc:                          # noqa: BLE001
                upstream_failures += 1
                last_error = str(exc)
                log.error("slide %d tier 2 generation %d returned no usable plan "
                          "(upstream failure %d/%d; design attempt %d/%d NOT "
                          "consumed): %s", brief.slide_number, generation,
                          upstream_failures, upstream_budget, attempt + 1, limit, exc,
                          extra={"session_id": self.session_id}, exc_info=True)
                if upstream_failures >= upstream_budget:
                    log.error("slide %d leaving tier 2: %d gateway call(s) in a row "
                              "produced no plan", brief.slide_number,
                              upstream_failures, extra={"session_id": self.session_id})
                    break
                continue

            attempt += 1
            log.info("slide %d tier 2 attempt %d/%d (generation %d): plan on %s (%s "
                     "the assigned layout), %d fill(s), %d extra(s), %d declared "
                     "omission(s)", brief.slide_number, attempt, limit, generation,
                     plan.layout,
                     "same as" if plan.layout == brief.layout_key else "NOT",
                     len(plan.placeholders), len(plan.extras), len(plan.dropped),
                     extra={"session_id": self.session_id})
            if plan.dropped:
                log.warning("slide %d tier 2: the plan leaves %d line(s) off the "
                            "slide: %s", brief.slide_number, len(plan.dropped),
                            " | ".join(str(d)[:120] for d in plan.dropped),
                            extra={"session_id": self.session_id})

            # 1. Validate the plan. A rejection here costs no execution at all.
            errors, _ = layout_plan.validate(
                plan, catalog, frame=brief.frame, allowed_images=brief.image_paths,
                slide_number=brief.slide_number)
            if errors:
                last_error = layout_plan.format_errors(errors)
                log.warning("slide %d tier 2 attempt %d/%d: the plan is not "
                            "renderable, no execution spent:\n%s", brief.slide_number,
                            attempt, limit, last_error,
                            extra={"session_id": self.session_id})
                self._step(brief, f"attempt {attempt}: the fitted plan needs "
                                  f"{len(errors)} fix(es)", phase="repair", tier=2,
                           attempts=attempt)
                messages += [{"role": "assistant", "content": plan.model_dump_json()},
                             {"role": "user", "content": last_error}]
                continue

            # A family follower may not leave its family's layout: that would quietly
            # redesign one slide of an established family.
            if family is not None and family.role == "follower":
                chosen_key = self._resolved_key(catalog, plan.layout)
                if chosen_key != brief.layout_key:
                    last_error = (f"The plan chose layout {plan.layout}, but every "
                                  f"{brief.slide_type} slide in this deck uses "
                                  f"{brief.layout_key}. Answer again on "
                                  f"{brief.layout_key}.")
                    log.warning("slide %d tier 2 attempt %d/%d left the deck's %s "
                                "layout (%s instead of %s); asking again",
                                brief.slide_number, attempt, limit, brief.slide_type,
                                chosen_key or plan.layout, brief.layout_key,
                                extra={"session_id": self.session_id})
                    messages += [{"role": "assistant",
                                  "content": plan.model_dump_json()},
                                 {"role": "user", "content": last_error}]
                    continue

            # 2. Compile and run. The program is ours, so a static rejection here is a
            #    compiler bug, not something the model can repair.
            source = layout_plan.compile_program(plan)
            rejection = code_guard.check_message(source)
            if rejection:
                last_error = rejection
                log.error("slide %d tier 2 attempt %d/%d: the COMPILED program was "
                          "rejected by the static guard. This is a bug in "
                          "layout_plan.compile_program, not in the plan:\n%s",
                          brief.slide_number, attempt, limit, rejection,
                          extra={"session_id": self.session_id})
                continue
            code_path.write_text(source, encoding="utf-8")
            succeeded, error = self._execute(code_path, fit_dir)
            produced = self._locate_output(fit_dir, out_path) if succeeded else None
            if produced is None:
                last_error = error or "the compiled program wrote no .pptx file"
                log.error("slide %d tier 2 attempt %d/%d: the compiled program "
                          "failed: %s", brief.slide_number, attempt, limit,
                          last_error[:500], extra={"session_id": self.session_id})
                self._step(brief, f"attempt {attempt}: the fitted slide would not "
                                  f"render", phase="repair", tier=2, attempts=attempt)
                messages += [
                    {"role": "assistant", "content": plan.model_dump_json()},
                    {"role": "user", "content":
                        "Rendering that plan failed. Simplify it - fewer extras, "
                        "shorter copy, a layout whose placeholders already suit this "
                        "content.\n\n=== ERROR ===\n" + last_error}]
                continue

            # 3. The same gate as tier 1, against the layout the plan actually chose.
            violations = self._gate(produced, brief, catalog, layout_key=plan.layout)
            if geo.is_clean(violations):
                log.info("slide %d accepted at tier 2 on attempt %d/%d (generation "
                         "%d) on layout %s: %s (%d advisory note(s), %d generation(s) "
                         "for this slide in total)", brief.slide_number, attempt,
                         limit, generation, plan.layout, produced.name,
                         len(violations), counter.count,
                         extra={"session_id": self.session_id})
                self._step(brief, f"slide fitted onto {plan.layout} on attempt "
                                  f"{attempt}", phase="done", tier=2, attempts=attempt)
                return _TierOutcome(result=SlideBuildResult(
                    slide_number=brief.slide_number, pptx_path=str(produced),
                    attempts=attempt, generations=counter.count,
                    upstream_failures=tier1.upstream_failures + upstream_failures,
                    tier=2, layout_used=plan.layout, code_path=str(code_path),
                    gate_passed=True, advisory_notes=len(violations)),
                    attempts=attempt, generations=issued,
                    upstream_failures=upstream_failures)

            last_error = geo.format_violations(violations)
            log.warning("slide %d tier 2 attempt %d/%d failed the gate with %d "
                        "violation(s) (%d blocking) on %s:\n%s", brief.slide_number,
                        attempt, limit, len(violations),
                        sum(1 for v in violations if v.blocking), plan.layout,
                        last_error, extra={"session_id": self.session_id})
            self._step(brief, f"attempt {attempt}: {len(violations)} layout issue(s) "
                              f"in the fitted slide", phase="repair", tier=2,
                       attempts=attempt)
            messages += [{"role": "assistant", "content": plan.model_dump_json()},
                         {"role": "user", "content": last_error}]

        log.error("slide %d leaving tier 2 unsatisfied after %d design attempt(s) and "
                  "%d generation(s) (%d upstream failure(s)); last problem: %s",
                  brief.slide_number, attempt, issued, upstream_failures,
                  last_error[:500], extra={"session_id": self.session_id})
        return _TierOutcome(attempts=attempt, generations=issued,
                            upstream_failures=upstream_failures,
                            last_error=last_error)

    # --- tier 3: the deterministic render ------------------------------------#
    def _tier3_render(self, brief: SlideCodeBrief, out_path: Path, spent: int,
                      last_error: str, upstream_failures: int = 0) -> SlideBuildResult:
        """Render the slide without a model at all. The end of the ladder.

        `_render_on_layout` is unchanged; this wraps it so the event and the result
        can say how much was spent getting here.

        Args:
            brief: The slide that defeated both model tiers.
            out_path: Where to write the single-slide .pptx.
            spent: Generations already spent across tiers 1 and 2.
            last_error: The last problem either tier reported.
            upstream_failures: How many of those generations came back empty. A high
                share means the ladder was defeated by the gateway, not by the design.

        Returns:
            A `SlideBuildResult` on a real template layout, with `fallback_used` set.
        """
        log.error("slide %d entering tier 3 after %d generation(s) across the model "
                  "tiers (%d of them upstream failures); rendering it "
                  "deterministically. Last problem: %s", brief.slide_number, spent,
                  upstream_failures, last_error[:500],
                  extra={"session_id": self.session_id})
        events.publish(
            self.session_id, "agent_step", agent="ppt-code",
            message=(f"Slide {brief.slide_number}: the designer could not produce a "
                     f"clean layout in {spent} attempt(s); the slide was rendered on a "
                     f"template layout instead."),
            slide_number=brief.slide_number, phase="fallback", tier=3, attempts=spent)
        produced = self._render_on_layout(brief, out_path)
        # The deterministic render is clean by construction - the template positioned
        # everything - but "by construction" is an assumption, and this is the one
        # artifact nobody had measured. Measuring it costs one local file read and
        # means every slide that ships has a recorded verdict.
        violations = self._gate(produced, brief, catalog_mod.load())
        blocking = sum(1 for v in violations if v.blocking)
        if blocking:
            log.error("slide %d: the deterministic render itself has %d blocking "
                      "violation(s). The template layout, the frame or the content is "
                      "at fault - this path is supposed to be clean by construction:"
                      "\n%s", brief.slide_number, blocking,
                      geo.format_violations(violations),
                      extra={"session_id": self.session_id})
        log.info("slide %d finished at tier 3: %s (%d blocking, %d advisory)",
                 brief.slide_number, produced.name, blocking,
                 len(violations) - blocking, extra={"session_id": self.session_id})
        return SlideBuildResult(slide_number=brief.slide_number,
                                pptx_path=str(produced), attempts=spent,
                                generations=spent,
                                upstream_failures=upstream_failures, tier=3,
                                layout_used=brief.layout_key, fallback_used=True,
                                gate_passed=not blocking,
                                advisory_notes=len(violations) - blocking,
                                blocking_violations=blocking,
                                error=last_error)

    # --- public API ----------------------------------------------------------#
    def build_slide(self, brief: SlideCodeBrief) -> SlideBuildResult:
        """Render one slide, dropping a tier at a time until one of them succeeds.

        Tier 1 lets the model design freely. Tier 2 makes it fit a real layout from a
        validated plan. Tier 3 renders deterministically. Each model tier owns its own
        budget, so a slide's total cost is bounded and a tier that cannot help is left
        behind rather than retried.

        Args:
            brief: The slide's content, context, design assignment and image paths.

        Returns:
            A `SlideBuildResult` always carrying a usable .pptx, with `tier` saying
            which rung produced it.
        """
        # Bound around the whole build, so every line the tiers, the sandbox, the
        # static guard and the geometry gate write underneath carries this session,
        # this build and this slide - without any of them having to know about it.
        with log_context(session_id=self.session_id, build_id=self.build_id,
                         slide=brief.slide_number),                 build_metrics.slide_build(self.session_id, brief.slide_number):
            return self._build_slide(brief)

    def _build_slide(self, brief: SlideCodeBrief) -> SlideBuildResult:
        """`build_slide`'s body, with the logging scope already bound."""
        log.info("building slide %d/%d %r: %s mode on %s, %d block(s), %d image(s); "
                 "type %s, creativity %s, %d-char user instructions, %d-char planner "
                 "instructions", brief.slide_number, brief.context.total,
                 brief.content.title, brief.mode, brief.layout_key or "(unassigned)",
                 len(brief.content.blocks), len(brief.image_paths), brief.slide_type,
                 brief.creativity_level, len(brief.user_instructions),
                 len(brief.planner_special_instructions),
                 extra={"session_id": self.session_id})
        if brief.planner_special_instructions:
            log.debug("slide %d planner instructions: %s", brief.slide_number,
                      brief.planner_special_instructions,
                      extra={"session_id": self.session_id})
        catalog = catalog_mod.load()
        if brief.is_family:
            return self._build_family_slide(brief, catalog)
        return self._run_ladder(self._apply_creativity(brief, catalog), catalog)

    def _run_ladder(self, brief: SlideCodeBrief, catalog,
                    family: "deck_design.FamilyAssignment | None" = None,
                    fallback_brief: SlideCodeBrief | None = None) -> SlideBuildResult:
        """Stage the slide's sandbox and walk the tiers until one succeeds.

        Max mode runs first for an eligible HIGH slide; everything else - and a max mode
        that produced nothing gate-clean - walks today's three tiers unchanged.

        Args:
            brief: The slide, already moved onto whatever layout it is built on.
            catalog: The template catalog.
            family: Its QUIZ/FAQ family assignment, or None for a NORMAL slide.
            fallback_brief: The slide's own contract assignment, used by tiers 2 and 3
                when `brief` sits on the High Creativity master (a follower of a family
                established there): that master has nothing to fit and nothing to
                render deterministically, so content would otherwise land in the notes.

        Returns:
            The finished slide.
        """
        slide_dir = self.workdir / f"slide_{brief.slide_number}"
        max_brief = None
        if self._max_mode_eligible(brief, family):
            max_brief = self._max_mode_brief(brief, catalog, family)
        staged = max_brief or brief
        sandbox.stage(slide_dir, catalog, self._stage_fields(staged), staged.image_paths)
        out_path = sandbox.output_path(slide_dir)
        code_path = slide_dir / PptCreatorConstants.GENERATED_CODE_FILENAME

        # One counter for the whole ladder, so "generation 5" means the fifth time this
        # slide was sent upstream - not the second attempt of the second tier.
        counter = _GenerationCounter()

        self._step(brief, self._start_message(brief, family), phase="start", tier=1,
                   **({"max_mode": True} if max_brief is not None else {}))
        maxed = _TierOutcome(skipped="not eligible")
        if max_brief is not None:
            maxed = self._tier_max(max_brief, catalog, slide_dir, out_path, code_path,
                                   counter, family)
            if maxed.result is not None:
                return maxed.result
            out_path.unlink(missing_ok=True)
            if max_brief is not brief:
                # Today's tiers run on the slide's original assignment, exactly as they
                # would have without max mode.
                sandbox.stage(slide_dir, catalog, self._stage_fields(brief),
                              brief.image_paths)
            log.warning("slide %d: max mode produced no gate-clean slide after %d "
                        "attempt(s); falling into the standard tiers on %s",
                        brief.slide_number, maxed.attempts, brief.layout_key,
                        extra={"session_id": self.session_id})

        if maxed.upstream_only:
            # Same rule as tier 1 -> tier 2: an upstream outage is not a design problem,
            # and another model tier would only call the same failing gateway.
            log.error("slide %d: max mode reached the gateway %d time(s) and got nothing; "
                      "skipping tier 1", brief.slide_number, maxed.generations,
                      extra={"session_id": self.session_id})
            tier1 = _TierOutcome(upstream_only=True, last_error=maxed.last_error,
                                 skipped="max mode was upstream-only")
        else:
            tier1 = self._tier1_creative(brief, catalog, slide_dir, out_path, code_path,
                                         counter, family)
        if tier1.result is not None:
            return tier1.result

        low_brief, low_family = brief, family
        if fallback_brief is not None and self._on_high_creativity(brief, catalog):
            log.warning("slide %d: tier 1 failed on the High Creativity layout %s; tiers "
                        "2-3 use the slide's own assignment %s instead", brief.slide_number,
                        brief.layout_key, fallback_brief.layout_key,
                        extra={"session_id": self.session_id})
            low_brief, low_family = fallback_brief, None
        tier2 = self._tier2_layout_fit(low_brief, catalog, tier1, counter, low_family)
        if tier2.result is not None:
            return tier2.result

        return self._tier3_render(
            low_brief, out_path, counter.count,
            tier2.last_error or tier1.last_error or maxed.last_error,
            upstream_failures=(maxed.upstream_failures + tier1.upstream_failures
                               + tier2.upstream_failures))

    @staticmethod
    def _stage_fields(brief: SlideCodeBrief) -> dict:
        """The contract fields a slide's sandbox is staged with."""
        return {"frame": brief.frame, "grid": brief.grid, "tokens": brief.tokens,
                "mode": brief.mode, "layout_key": brief.layout_key,
                "free_region": brief.free_region,
                "layout_choices": list(brief.layout_choices)}

    @staticmethod
    def _on_high_creativity(brief: SlideCodeBrief, catalog) -> bool:
        """Whether the brief's layout is one of the High Creativity master's."""
        layout = catalog.get(brief.layout_key) if brief.layout_key else None
        return bool(layout is not None and getattr(layout, "high_creativity", False))

    # --- max mode ------------------------------------------------------------#
    def _max_mode_eligible(self, brief: SlideCodeBrief,
                           family: "deck_design.FamilyAssignment | None") -> bool:
        """Whether this slide gets the render-and-review tier.

        Creativity is checked first, so a LOW or MED slide never reads the new
        settings, never probes the renderer and never reaches the judge.

        Args:
            brief: The slide.
            family: Its QUIZ/FAQ family assignment, or None.

        Returns:
            True for a HIGH NORMAL slide or family leader when the renderer is ready.
        """
        if brief.creativity_level != CreativityLevels.HIGH:
            return False
        if family is not None and family.role != "leader":
            log.info("slide %d: HIGH %s follower; max mode is for leaders only",
                     brief.slide_number, brief.slide_type,
                     extra={"session_id": self.session_id})
            return False
        if brief.content.quiz is not None and brief.content.quiz.reveal:
            log.info("slide %d: an answer-reveal slide is never reviewed on its own",
                     brief.slide_number, extra={"session_id": self.session_id})
            return False
        try:
            enabled = bool(get_settings().slide_max_mode_enabled)
        except Exception as exc:                              # noqa: BLE001
            log.warning("slide %d: settings unreadable (%s); max mode off",
                        brief.slide_number, exc, extra={"session_id": self.session_id})
            enabled = False
        if not enabled:
            log.info("slide %d: HIGH, but max mode is switched off; standard tiers",
                     brief.slide_number, extra={"session_id": self.session_id})
            return False
        if not slide_render.available():
            log.info("slide %d: HIGH, but no renderer is available; standard tiers",
                     brief.slide_number, extra={"session_id": self.session_id})
            return False
        return True

    def _max_mode_brief(self, brief: SlideCodeBrief, catalog,
                        family: "deck_design.FamilyAssignment | None"
                        ) -> SlideCodeBrief | None:
        """Place a max-mode slide: on the High Creativity master, or where it stands.

        Content and takeaways slides and family leaders move onto the master's title
        layout (NORMAL slides may also choose its blank canvas). Cover, agenda and
        thank-you slides, and a leader whose layout an upstream directive named, keep
        their assignment and are reviewed there. A slide on fixed artwork is skipped.

        Args:
            brief: The slide as assigned (creativity already applied).
            catalog: The template catalog.
            family: Its family assignment, or None.

        Returns:
            The brief to build in max mode, or None when max mode should not run.
        """
        leader = family is not None and family.role == "leader"
        movable = ((family is None
                    and brief.kind in PptCreatorConstants.MAX_MODE_HIGH_MASTER_KINDS)
                   or (leader and family.source != "upstream"))
        if movable:
            title = catalog.high_creativity_title()
            if title is None:
                log.warning("slide %d: the High Creativity master is unusable (%s); max "
                            "mode reviews it on %s", brief.slide_number,
                            "; ".join(catalog.high_creativity_problems()),
                            brief.layout_key or "(unassigned)",
                            extra={"session_id": self.session_id})
            else:
                choices = [{"key": title.key, "kind": "title",
                            "free_region": title.free_region.to_list()}]
                if title.title_rect is not None:
                    # No title placeholder on this master: set_title draws the title in
                    # the sample title box's place, which is stripped from the file.
                    choices[0].update({"title_rect": title.title_rect.to_list(),
                                       "title_size": title.title_font_size})
                blank = catalog.high_creativity_blank() if family is None else None
                if blank is not None:
                    choices.append({"key": blank.key, "kind": "blank",
                                    "free_region": blank.free_region.to_list(),
                                    "title_rect": blank.title_rect.to_list(),
                                    "title_size": blank.title_font_size})
                updates = {"layout_key": title.key, "mode": "canvas",
                           "free_region": title.free_region.to_list(),
                           "layout_choices": choices}
                if brief.frame:
                    from app.genai.ppt.design_contract import frame_for_layout
                    updates["frame"] = frame_for_layout(
                        brief.classification, catalog.slide_width, catalog.slide_height,
                        brief.slide_number, title)
                log.info("slide %d: max mode moves it from %s to the High Creativity "
                         "master (%s)", brief.slide_number, brief.layout_key or "-",
                         ", ".join(c["key"] for c in choices),
                         extra={"session_id": self.session_id})
                return brief.model_copy(update=updates)
        layout = catalog.get(brief.layout_key) if brief.layout_key else None
        if layout is not None and not layout.placeholders and brief.mode != "canvas":
            log.info("slide %d: %s is fixed artwork; nothing to redesign, so max mode is "
                     "skipped", brief.slide_number, layout.key,
                     extra={"session_id": self.session_id})
            return None
        log.info("slide %d: max mode reviews it on its own layout %s (%s mode)",
                 brief.slide_number, brief.layout_key or "-", brief.mode,
                 extra={"session_id": self.session_id})
        return brief

    def _max_mode_guidance(self, brief: SlideCodeBrief) -> str:
        """The max-mode sections appended to the first designer turn."""
        text = MAX_MODE_GUIDANCE
        if brief.layout_choices:
            lines = []
            for choice in brief.layout_choices:
                line = (f"- {choice['key']} ({choice['kind']}): free_region "
                        f"{choice['free_region']}")
                if choice.get("title_rect"):
                    line += (f", title_rect {choice['title_rect']}, title_size "
                             f"{choice.get('title_size') or 28}pt")
                lines.append(line)
            text += HIGH_MASTER_GUIDANCE.format(choices="\n".join(lines))
        return text

    def _render_neighbours(self, brief: SlideCodeBrief) -> list[bytes]:
        """Renders of the neighbouring slides, for a consistency fix. Best effort."""
        if not (brief.match_neighbours and brief.neighbour_refs):
            return []
        pngs = []
        # Outside the slide's sandbox, so tier 1's output search can never pick up a
        # neighbour's file.
        out_dir = self.workdir / "neighbours" / f"slide_{brief.slide_number}"
        for ref in brief.neighbour_refs:
            try:
                pngs.append(slide_render.render_png(Path(ref), out_dir).read_bytes())
            except (SlideRenderError, OSError) as exc:
                log.warning("slide %d: neighbour %s could not be rendered (%s); "
                            "continuing without it", brief.slide_number,
                            Path(ref).name, exc, extra={"session_id": self.session_id})
        log.info("slide %d: %d of %d neighbour render(s) attached for consistency",
                 brief.slide_number, len(pngs), len(brief.neighbour_refs),
                 extra={"session_id": self.session_id})
        return pngs

    @staticmethod
    def _produced_layout_key(produced: Path, catalog) -> str:
        """The catalog key of the layout the produced slide actually sits on, or ""."""
        try:
            from pptx import Presentation
            name = next(iter(Presentation(str(produced)).slides)).slide_layout.name
            return catalog.resolve_layout(name).key
        except Exception as exc:                              # noqa: BLE001
            log.warning("could not read the layout of %s (%s)", produced, exc)
            return ""

    @staticmethod
    def _strict_output(slide_dir: Path, expected: Path) -> Path | None:
        """`_locate_output`, but never a kept candidate or an older tier's file."""
        if expected.exists():
            return expected
        skip = (f"{PptCreatorConstants.MAX_MODE_KEEP_PREFIX}_", "accepted_")
        candidates = [p for p in slide_dir.glob("*.pptx") if not p.name.startswith(skip)]
        if not candidates:
            return None
        found = max(candidates, key=lambda p: p.stat().st_mtime)
        log.warning("expected %s but the program wrote %s; using it instead",
                    expected.name, found.name)
        return found

    def _check_program(self, brief: SlideCodeBrief, catalog, slide_dir: Path,
                       out_path: Path, code_path: Path, source: str
                       ) -> tuple[str, Path | None, list, str]:
        """Guard, run and gate one max-mode program, as tier 1 does.

        Args:
            brief: The max-mode brief.
            catalog: The template catalog.
            slide_dir: The slide's sandbox.
            out_path: Where the program is told to save.
            code_path: The written program.
            source: Its source.

        Returns:
            (repair message or "" when clean, produced file, violations, layout key).
        """
        rejection = code_guard.check_message(source)
        if rejection:
            return rejection, None, [], ""
        # Nothing from an earlier round may vouch for this one.
        for name in (deck_builder.CHOICES_FILENAME, deck_builder.REPORT_FILENAME):
            (slide_dir / name).unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)
        succeeded, error = self._execute(code_path, slide_dir)
        produced = self._strict_output(slide_dir, out_path) if succeeded else None
        if produced is None:
            return ("Running that program failed. Fix it so it runs unattended and saves "
                    "the slide to OUTPUT_PATH.\n\n=== ERROR ===\n"
                    + (error or "the program ran but wrote no .pptx file")), None, [], ""
        layout_key = self._produced_layout_key(produced, catalog) or brief.layout_key
        allowed = [c["key"] for c in brief.layout_choices]
        if allowed and layout_key not in allowed:
            log.warning("slide %d: max-mode program used %s, outside its choices %s",
                        brief.slide_number, layout_key, allowed,
                        extra={"session_id": self.session_id})
            return (f"The slide was built on layout {layout_key}, but it must use one of "
                    f"{', '.join(allowed)}. Call deck.add_slide with a key from "
                    f"LAYOUT_CHOICES and resend the whole program."), produced, [], \
                layout_key
        violations = self._gate(produced, brief, catalog, layout_key=layout_key)
        if not geo.is_clean(violations):
            return (geo.format_violations(violations)
                    + self._builder_notes(produced.parent)), produced, violations, layout_key
        choice_error = self._check_choices(brief, produced.parent)
        if choice_error:
            return choice_error, produced, violations, layout_key
        return "", produced, violations, layout_key

    def _visual_feedback(self, verdict: "slide_reviewer.VisualVerdict", round_no: int,
                         violations: list, directory: Path) -> str:
        """The redo request: the judge's critique plus the gate's polish notes."""
        issues = verdict.issues[:SlideReviewConstants.MAX_ISSUES_FED_BACK]
        lines = [f"{i}. [{issue.severity}] {issue.location or 'slide'}: {issue.problem}"
                 + (f" -> {issue.fix}" if issue.fix else "")
                 for i, issue in enumerate(issues, 1)]
        text = VISUAL_FEEDBACK_TEMPLATE.format(
            score=verdict.score, round=round_no,
            pass_score=SlideReviewConstants.PASS_SCORE, summary=verdict.summary,
            issues="\n".join(lines) or "(none listed; raise the overall polish)")
        polish = [v for v in violations if v.rule in (10, 15)]
        if polish:
            text += "\n\n" + geo.format_violations(polish).replace(
                "did not pass the geometry gate", "passed the geometry gate, but can be "
                "cleaner")
        return text + self._builder_notes(directory)

    def _tier_max(self, brief: SlideCodeBrief, catalog, slide_dir: Path,
                  out_path: Path, code_path: Path, counter: "_GenerationCounter",
                  family: "deck_design.FamilyAssignment | None" = None) -> _TierOutcome:
        """Max mode: design, gate, render, judge - and redo until the judge approves.

        The designer, prompt and repair messages are tier 1's. A gate-clean candidate is
        kept aside, rendered and judged; a rejected one goes back with its image and
        the critique. The best-ranked gate-clean candidate ships when the reviews, the
        generations or the clock run out, or when rendering or judging fails.

        Args:
            brief: The max-mode brief (possibly moved onto the High Creativity master).
            catalog: The template catalog.
            slide_dir: The slide's sandbox.
            out_path: Where a program is told to save.
            code_path: Where each program is written.
            counter: The slide's generation counter, shared with the later tiers.
            family: The family assignment (a leader), or None.

        Returns:
            A `_TierOutcome` with the shipped slide, or why max mode came up empty.
        """
        PCC = PptCreatorConstants
        gen_limit, review_limit = PCC.MAX_MODE_GENERATIONS, PCC.MAX_VISUAL_REVIEWS
        upstream_budget = PCC.MAX_UPSTREAM_RETRIES
        deadline = time.monotonic() + PCC.MAX_MODE_WALL_SECONDS
        review_dir = (self.workdir / SlideRenderConstants.RENDER_DIRNAME
                      / f"slide_{brief.slide_number}")
        role = family.role if family is not None else ""
        log.info("slide %d entering MAX MODE on %s (%d review(s), %d generation(s), "
                 "%ds allowed; %d layout choice(s))", brief.slide_number,
                 brief.layout_key or "-", review_limit, gen_limit,
                 PCC.MAX_MODE_WALL_SECONDS, len(brief.layout_choices),
                 extra={"session_id": self.session_id})

        neighbours = self._render_neighbours(brief)
        user = self._user_prompt(brief, family)
        user[0]["text"] += self._max_mode_guidance(brief)
        if neighbours and PCC.MAX_MODE_RENDER_TO_DESIGNER:
            user.append({"type": "text", "text": NEIGHBOUR_GUIDANCE})
            user += [image_block(png, "image/png", ModelConstants.PPT_CODER)
                     for png in neighbours]
        messages = [{"role": "system", "content": self._system_prompt()},
                    {"role": "user", "content": user}]

        best: tuple | None = None            # (rank, outcome, verdict or None)
        attempt = upstream = issued = reviews = 0
        last_error = ""
        while attempt < gen_limit and reviews < review_limit:
            if time.monotonic() > deadline:
                log.warning("slide %d: max mode ran out of time after %d attempt(s) and "
                            "%d review(s)", brief.slide_number, attempt, reviews,
                            extra={"session_id": self.session_id})
                break
            run_control.checkpoint()
            generation = counter.next()
            issued += 1
            try:
                source = self._request_code(messages)
            except Exception as exc:                          # noqa: BLE001
                upstream += 1
                last_error = str(exc)
                log.error("slide %d max mode generation %d returned no program (upstream "
                          "failure %d/%d): %s", brief.slide_number, generation, upstream,
                          upstream_budget, exc, extra={"session_id": self.session_id},
                          exc_info=True)
                if upstream >= upstream_budget:
                    break
                continue

            attempt += 1
            source = code_guard.strip_code_fences(self._normalize_code(source, slide_dir))
            code_path.write_text(source, encoding="utf-8")
            feedback, produced, violations, layout_key = self._check_program(
                brief, catalog, slide_dir, out_path, code_path, source)
            if feedback:
                last_error = feedback
                log.warning("slide %d max mode attempt %d/%d (generation %d) needs a "
                            "repair before review:\n%s", brief.slide_number, attempt,
                            gen_limit, generation, feedback[:1500],
                            extra={"session_id": self.session_id})
                self._step(brief, f"max mode attempt {attempt}: repairing the design",
                           phase="repair", tier=1, attempts=attempt, max_mode=True)
                messages = _without_images(messages) + [
                    {"role": "assistant", "content": source},
                    {"role": "user", "content": feedback}]
                continue

            candidate = self._keep_accepted(_TierOutcome(result=SlideBuildResult(
                slide_number=brief.slide_number, pptx_path=str(produced),
                attempts=attempt, generations=counter.count, upstream_failures=upstream,
                tier=1, layout_used=layout_key, code_path=str(code_path),
                gate_passed=True, advisory_notes=len(violations),
                reveal_ready=self._needs_choices(brief), max_mode=True),
                attempts=attempt, generations=issued, upstream_failures=upstream),
                produced, code_path, attempt, prefix=PCC.MAX_MODE_KEEP_PREFIX)
            try:
                run_control.checkpoint()
                png = slide_render.render_png(Path(candidate.result.pptx_path),
                                              review_dir).read_bytes()
            except (SlideRenderError, OSError) as exc:
                log.error("slide %d: rendering attempt %d failed (%s); shipping the best "
                          "gate-clean candidate", brief.slide_number, attempt, exc,
                          extra={"session_id": self.session_id})
                if best is None:
                    best = ((False, 0, -999, attempt), candidate, None)
                break
            reviews += 1
            try:
                verdict = slide_reviewer.review(png, brief, family_role=role,
                                                round_no=reviews, neighbours=neighbours)
            except Exception as exc:                          # noqa: BLE001
                log.error("slide %d: visual review %d failed (%s); shipping the best "
                          "gate-clean candidate", brief.slide_number, reviews, exc,
                          extra={"session_id": self.session_id}, exc_info=True)
                if best is None:
                    best = ((False, 0, -999, attempt), candidate, None)
                break
            approved = slide_reviewer.passed(verdict)
            rank = (approved, verdict.score, -verdict.failing_issues, attempt)
            if best is None or rank > best[0]:
                best = (rank, candidate, verdict)
            log.info("slide %d max mode attempt %d on %s: review %d/%d scored %d/10, %d "
                     "issue(s), %d failing -> %s", brief.slide_number, attempt,
                     layout_key, reviews, review_limit, verdict.score,
                     len(verdict.issues), verdict.failing_issues,
                     "approved" if approved else "redo",
                     extra={"session_id": self.session_id})
            # Score and counts only: the judge's prose can echo planner instructions.
            self._step(brief, f"visual check {reviews}/{review_limit}: "
                              + ("approved" if approved else "refining the design"),
                       phase="review", tier=1, attempts=attempt, max_mode=True,
                       score=verdict.score, issues=len(verdict.issues))
            if approved or reviews >= review_limit or attempt >= gen_limit:
                break
            content = ([image_block(png, "image/png", ModelConstants.PPT_CODER)]
                       if PCC.MAX_MODE_RENDER_TO_DESIGNER else [])
            content.append({"type": "text", "text": self._visual_feedback(
                verdict, reviews, violations, produced.parent)})
            messages = _without_images(messages) + [
                {"role": "assistant", "content": source},
                {"role": "user", "content": content}]

        if best is not None:
            _rank, outcome, verdict = best
            result = outcome.result.model_copy(update={
                "generations": counter.count, "upstream_failures": upstream,
                "visual_reviews": reviews,
                "visual_score": verdict.score if verdict is not None else None,
                "visual_passed": bool(verdict is not None
                                      and slide_reviewer.passed(verdict))})
            log.info("slide %d finished in MAX MODE: attempt %d on %s, %d review(s), "
                     "score %s, %s", brief.slide_number, outcome.attempts,
                     result.layout_used, reviews, result.visual_score,
                     "approved" if result.visual_passed else "best available",
                     extra={"session_id": self.session_id})
            self._step(brief, f"slide rendered after {reviews} visual check(s)",
                       phase="done", tier=1, attempts=outcome.attempts, max_mode=True)
            return _TierOutcome(result=result, attempts=outcome.attempts,
                                generations=issued, upstream_failures=upstream)
        log.error("slide %d leaving max mode with nothing gate-clean after %d attempt(s), "
                  "%d generation(s) (%d upstream failure(s)); last problem: %s",
                  brief.slide_number, attempt, issued, upstream, last_error[:500],
                  extra={"session_id": self.session_id})
        return _TierOutcome(attempts=attempt, generations=issued,
                            upstream_failures=upstream, last_error=last_error,
                            upstream_only=attempt == 0 and upstream > 0)

    @staticmethod
    def _start_message(brief: SlideCodeBrief,
                       family: "deck_design.FamilyAssignment | None") -> str:
        """The user-facing first step for a slide. Never carries planner wording."""
        if family is None:
            return "designing the slide"
        label = "quiz" if brief.slide_type == SlideTypes.QUIZ else brief.slide_type
        if family.role == "leader":
            return f"designing the deck's {label} layout"
        return f"designing the slide on the deck's {label} layout"

    # --- deck-level family layouts (QUIZ / FAQ) ------------------------------#
    def _design_registry(self) -> "deck_design.DeckDesignRegistry":
        """This turn's deck design registry, created on the first family slide."""
        with self._design_lock:
            if self._design is None:
                self._design = deck_design.DeckDesignRegistry(self.session_id)
            return self._design

    def _build_family_slide(self, brief: SlideCodeBrief, catalog) -> SlideBuildResult:
        """Build a QUIZ or FAQ slide on its family's one layout for this deck.

        The first slide of the family to get here leads: it designs the layout while
        the family's other slides wait, then registers it. Every other slide follows
        it with only its content changed. NORMAL slides never come through here.

        Args:
            brief: A QUIZ or FAQ slide.
            catalog: The template catalog.

        Returns:
            The finished slide, tagged with its family role.
        """
        registry = self._design_registry()
        registry.adopt_upstream(brief.deck_design_context)
        # Two passes at most: the second only happens when the established layout can
        # no longer host slides and has just been discarded.
        for _ in range(2):
            established = registry.acquire(brief.slide_type, brief.slide_number)
            if established is None:
                return self._lead_family(brief, catalog, registry)
            follower = deck_design.plan_follower(catalog, brief, established)
            if follower is not None:
                built = brief.model_copy(update=follower.brief_updates)
                return self._tag(self._run_ladder(built, catalog, follower,
                                                  fallback_brief=brief), built,
                                 "follower")
            registry.discard(brief.slide_type, established)
        log.error("slide %d: the %s family keeps resolving to a layout that cannot host "
                  "slides; building this slide on its own assignment", brief.slide_number,
                  brief.slide_type, extra={"session_id": self.session_id})
        return self._tag(self._run_ladder(self._apply_creativity(brief, catalog),
                                          catalog), brief, "")

    def _lead_family(self, brief: SlideCodeBrief, catalog,
                     registry: "deck_design.DeckDesignRegistry") -> SlideBuildResult:
        """Design the family's layout on this slide and register it. Holds the lock.

        Args:
            brief: The leading slide.
            catalog: The template catalog.
            registry: The registry whose family lock this slide holds.

        Returns:
            The finished slide. The lock is released however the build ends, so a
            failed leader hands the family to the next waiting slide.
        """
        try:
            directive = registry.family(brief.slide_type)
            assignment = deck_design.plan_leader(catalog, brief, directive)
            built = (brief.model_copy(update=assignment.brief_updates)
                     if assignment.brief_updates
                     else self._apply_creativity(brief, catalog))
            result = self._run_ladder(built, catalog, assignment)
            self._establish(registry, built, assignment, result, catalog)
            return self._tag(result, built, "leader")
        finally:
            registry.release(brief.slide_type)

    def _establish(self, registry: "deck_design.DeckDesignRegistry",
                   brief: SlideCodeBrief, assignment: "deck_design.FamilyAssignment",
                   result: SlideBuildResult, catalog) -> None:
        """Register the leader's accepted design as its family's layout.

        A leader that reached the deterministic render (tier 3) or shipped past the gate
        establishes nothing: there is no program to follow, and a fallback is not a
        design worth repeating. The next slide of the family leads instead.

        Args:
            registry: This turn's registry.
            brief: The leader's brief, as built.
            assignment: The leader's assignment.
            result: What the ladder produced.
            catalog: The template catalog.
        """
        if result.tier == 3 or not result.gate_passed:
            log.warning("slide %d ended at tier %d (gate %s), so it does not establish "
                        "the %s layout; the next %s slide will lead",
                        brief.slide_number, result.tier,
                        "passed" if result.gate_passed else "not passed",
                        brief.slide_type, brief.slide_type,
                        extra={"session_id": self.session_id})
            return
        layout_key = self._resolved_key(catalog, result.layout_used) or brief.layout_key
        used = catalog.get(layout_key)
        if (used is not None and getattr(used, "high_creativity", False)
                and used.free_region is not None):
            # A max-mode leader moved onto the High Creativity master: followers compose
            # on its canvas, never in layout mode (it has nothing to fill).
            mode, region = "canvas", used.free_region.to_list()
            log.info("slide %d establishes the %s family on the High Creativity layout "
                     "%s", brief.slide_number, brief.slide_type, layout_key,
                     extra={"session_id": self.session_id})
        elif result.tier == 1 and layout_key == brief.layout_key:
            mode, region = brief.mode, brief.free_region
        else:
            # Tier 2 fills a layout's placeholders, whichever layout its plan chose.
            mode, region = "layout", None
        directive = assignment.family
        registry.register(FamilyLayout(
            slide_type=brief.slide_type, layout_key=layout_key, mode=mode,
            free_region=region,
            description=directive.description if directive is not None else "",
            exemplar_code=self._exemplar(result.code_path, brief.slide_number),
            source=assignment.source,
            reference_key=(assignment.reference.key
                           if assignment.reference is not None else ""),
            tier=result.tier, established_at_slide=brief.slide_number,
            upstream_fingerprint=(directive.upstream_fingerprint
                                  if directive is not None else "")))

    def _exemplar(self, code_path: str, slide_number: int) -> str:
        """The leader's accepted program, made safe to hand to its followers.

        Paths into the leader's own sandbox are made relative - each follower runs in
        its own directory, and must never write into another slide's.

        Args:
            code_path: The accepted program.
            slide_number: The leader's slide number, for its sandbox path.

        Returns:
            The program, or "" when it is unreadable or too long to inline.
        """
        try:
            source = Path(code_path).read_text(encoding="utf-8")
        except (OSError, TypeError) as exc:
            log.warning("slide %d's program %r is unreadable (%s); its family will "
                        "follow the layout without an exemplar", slide_number,
                        code_path, exc, extra={"session_id": self.session_id})
            return ""
        slide_dir = self.workdir / f"slide_{slide_number}"
        native = str(slide_dir)
        for prefix, relative in ((slide_dir.as_posix() + "/", "./"),
                                 (native.replace("\\", "\\\\") + "\\\\", "./"),
                                 (native + os.sep, "./"),
                                 (slide_dir.as_posix(), "."),
                                 (native.replace("\\", "\\\\"), "."),
                                 (native, ".")):
            source = source.replace(prefix, relative)
        if len(source) > PptCreatorConstants.MAX_EXEMPLAR_CODE_CHARS:
            log.warning("slide %d's program is %d chars, over the %d-char exemplar "
                        "limit; its family will follow the layout without it",
                        slide_number, len(source),
                        PptCreatorConstants.MAX_EXEMPLAR_CODE_CHARS,
                        extra={"session_id": self.session_id})
            return ""
        return source

    @staticmethod
    def _resolved_key(catalog, ref: str) -> str:
        """The catalog key for a layout reference (a key or a name), or ""."""
        if not ref:
            return ""
        try:
            return catalog.resolve_layout(ref).key
        except LookupError:
            return ""

    @staticmethod
    def _tag(result: SlideBuildResult, brief: SlideCodeBrief,
             role: str) -> SlideBuildResult:
        """Stamp a family slide's result with its type, role and family layout."""
        return result.model_copy(update={
            "slide_type": brief.slide_type, "family_role": role,
            "family_layout_key": (result.layout_used or brief.layout_key) if role
            else ""})

    # --- quiz answer reveal --------------------------------------------------#
    @staticmethod
    def _needs_choices(brief: SlideCodeBrief) -> bool:
        """True for a quiz question slide whose options must be drawn with add_choice."""
        quiz = brief.content.quiz
        return (brief.slide_type == SlideTypes.QUIZ and quiz is not None
                and not quiz.reveal)

    def _check_choices(self, brief: SlideCodeBrief, out_dir: Path) -> str:
        """Verify a quiz program drew every option with `add_choice`, correctly.

        Args:
            brief: The slide, carrying its quiz.
            out_dir: The directory the program saved into (where the sidecar lands).

        Returns:
            "" when the choices are right (or the slide is not a quiz question),
            otherwise a repair message for the model.
        """
        if not self._needs_choices(brief):
            return ""
        quiz = brief.content.quiz
        expected = list(QuizConstants.OPTION_LABELS[:len(quiz.options)])
        usage = (f"Draw each of the {len(expected)} options with "
                 f"s.add_choice(letter, option_text, left, top, width, height, "
                 f"correct=...) using the letters {', '.join(expected)} in order, and "
                 f"pass correct=True only for option {quiz.correct_label}.")
        sidecar = out_dir / deck_builder.CHOICES_FILENAME
        try:
            rows = json.loads(sidecar.read_text(encoding="utf-8")).get("choices", [])
        except (OSError, ValueError):
            return f"This is a QUIZ slide but no option was drawn with add_choice. {usage}"
        labels = [str(row.get("label")) for row in rows]
        if labels != expected:
            return (f"The quiz options drawn with add_choice were {labels}, expected "
                    f"{expected}. {usage}")
        correct = [str(row.get("label")) for row in rows if row.get("correct")]
        if correct != [quiz.correct_label]:
            return (f"correct=True was passed for {correct or 'no option'}, but the "
                    f"correct option is {quiz.correct_label}. {usage}")
        return ""

    def build_answer_slide(self, brief: SlideCodeBrief,
                           question: SlideBuildResult) -> SlideBuildResult:
        """Build a quiz answer slide from its already-built question slide.

        The answer slide is the question slide with the correct choice highlighted.
        When the question's program drew its options with `add_choice`, that same
        program is re-run in the answer slide's own sandbox with the reveal switched
        on - no model call, and nothing but the highlight (and the page number) can
        differ. Otherwise the revealed content is rendered deterministically on the
        question's layout.

        Args:
            brief: The answer slide: its content is the question's with
                `quiz.reveal` set, and its frame carries its own page number.
            question: The question slide's build result.

        Returns:
            The answer slide, tagged `family_role="answer"`.
        """
        with log_context(session_id=self.session_id, build_id=self.build_id,
                         slide=brief.slide_number):
            catalog = catalog_mod.load()
            layout_key = (self._resolved_key(catalog, question.layout_used)
                          or brief.layout_key)
            answer = brief.model_copy(update={"layout_key": layout_key})
            used = catalog.get(layout_key) if layout_key else None
            on_high_master = bool(used is not None
                                  and getattr(used, "high_creativity", False)
                                  and used.free_region is not None)
            if on_high_master:
                # The question was built in max mode on the High Creativity master: its
                # program composes on that canvas, so the re-run must be staged there.
                from app.genai.ppt.design_contract import frame_for_layout
                updates = {"mode": "canvas", "free_region": used.free_region.to_list()}
                if brief.frame:
                    updates["frame"] = frame_for_layout(
                        brief.classification, catalog.slide_width, catalog.slide_height,
                        brief.slide_number, used)
                answer = answer.model_copy(update=updates)
                log.info("slide %d: answer re-run staged on the High Creativity layout %s",
                         brief.slide_number, layout_key,
                         extra={"session_id": self.session_id})
            self._step(answer, "revealing the answer", phase="start", tier=question.tier)
            result = None
            if question.reveal_ready and question.code_path:
                result = self._rerun_revealed(answer, question, catalog)
            if result is None:
                # The deterministic render never runs on the High Creativity master,
                # which has no body placeholders: it would push the content into notes.
                result = self._render_answer(brief if on_high_master else answer,
                                             question, catalog)
            self._step(answer, "answer slide rendered", phase="done", tier=result.tier)
            return result.model_copy(update={
                "slide_type": SlideTypes.QUIZ, "family_role": "answer",
                "family_layout_key": question.family_layout_key or layout_key})

    def _rerun_revealed(self, answer: SlideCodeBrief, question: SlideBuildResult,
                        catalog) -> SlideBuildResult | None:
        """Re-run the question's program with the reveal on, in the answer's sandbox.

        Args:
            answer: The answer slide, on the question's layout.
            question: The question's tier-1 result.
            catalog: The template catalog.

        Returns:
            The answer slide, or None when the re-run failed (the caller then falls
            back to the deterministic render).
        """
        slide_dir = self.workdir / f"slide_{answer.slide_number}"
        question_dir = Path(question.code_path).parent
        try:
            source = Path(question.code_path).read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("slide %d: the question's program is unreadable (%s)",
                        answer.slide_number, exc, extra={"session_id": self.session_id})
            return None
        sandbox.stage(slide_dir, catalog, {
            "frame": answer.frame, "grid": answer.grid, "tokens": answer.tokens,
            "mode": answer.mode, "layout_key": answer.layout_key,
            "free_region": answer.free_region}, answer.image_paths,
            reveal_answers=True)
        code_path = slide_dir / PptCreatorConstants.GENERATED_CODE_FILENAME
        # The program may name its own sandbox by absolute path; point it at this one,
        # so it can never overwrite the question's file.
        code_path.write_text(_rehome(source, question_dir, slide_dir), encoding="utf-8")
        out_path = sandbox.output_path(slide_dir)
        (slide_dir / deck_builder.CHOICES_FILENAME).unlink(missing_ok=True)
        succeeded, error = self._execute(code_path, slide_dir)
        produced = self._locate_output(slide_dir, out_path) if succeeded else None
        if produced is None:
            log.warning("slide %d: re-running the question's program failed: %s",
                        answer.slide_number, (error or "no output")[:500],
                        extra={"session_id": self.session_id})
            return None
        try:
            sidecar = json.loads((produced.parent / deck_builder.CHOICES_FILENAME)
                                 .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            sidecar = {}
        if not sidecar.get("revealed") or not any(
                row.get("correct") for row in sidecar.get("choices", [])):
            log.warning("slide %d: the re-run did not reveal a correct choice",
                        answer.slide_number, extra={"session_id": self.session_id})
            return None
        violations = self._gate(produced, answer, catalog)
        if not geo.is_clean(violations):
            log.warning("slide %d: the revealed re-run failed the gate:\n%s",
                        answer.slide_number, geo.format_violations(violations),
                        extra={"session_id": self.session_id})
            return None
        log.info("slide %d: answer revealed by re-running slide %d's program",
                 answer.slide_number, question.slide_number,
                 extra={"session_id": self.session_id})
        return SlideBuildResult(
            slide_number=answer.slide_number, pptx_path=str(produced), attempts=1,
            tier=question.tier, layout_used=answer.layout_key,
            code_path=str(code_path), gate_passed=True,
            advisory_notes=len(violations))

    def _render_answer(self, answer: SlideCodeBrief, question: SlideBuildResult,
                       catalog) -> SlideBuildResult:
        """Render the revealed content deterministically on the question's layout.

        Args:
            answer: The answer slide, its content carrying the revealed quiz (or the
                answer line, for a question that came out of the ladder without one).
            question: The question's build result, for logging.
            catalog: The template catalog.

        Returns:
            The answer slide; `fallback_used` only when even this did not clear the
            gate.
        """
        log.info("slide %d: the question (slide %d, tier %d) was not drawn with "
                 "add_choice, so the answer is rendered deterministically",
                 answer.slide_number, question.slide_number, question.tier,
                 extra={"session_id": self.session_id})
        slide_dir = self.workdir / f"slide_{answer.slide_number}"
        produced = self._render_on_layout(answer, sandbox.output_path(slide_dir))
        violations = self._gate(produced, answer, catalog)
        blocking = sum(1 for v in violations if v.blocking)
        return SlideBuildResult(
            slide_number=answer.slide_number, pptx_path=str(produced), tier=3,
            layout_used=answer.layout_key, fallback_used=bool(blocking),
            gate_passed=not blocking, advisory_notes=len(violations) - blocking,
            blocking_violations=blocking)

    def _gate(self, produced: Path, brief: SlideCodeBrief, catalog,
              layout_key: str = "") -> list:
        """Run the geometry gate over the produced file. Never raises.

        A gate that crashes must not lose the slide: an unreadable result is reported
        as one violation and repaired like any other.

        Args:
            produced: The single-slide .pptx the program wrote.
            brief: The slide's assignment, for its layout and frame.
            catalog: The template catalog.
            layout_key: Measure against this layout instead of the brief's assignment.
                Tier 2 may land the slide on a layout the contract never assigned.

        Returns:
            The violations, or one "unreadable" violation when measuring failed.
        """
        try:
            key = layout_key or brief.layout_key
            layout = catalog.resolve_layout(key) if key else None
            if layout is None:
                from pptx import Presentation
                name = next(iter(Presentation(str(produced)).slides)).slide_layout.name
                layout = catalog.resolve_layout(name)
            log.debug("slide %d: gating %s against layout %s", brief.slide_number,
                      produced.name, layout.key,
                      extra={"session_id": self.session_id})
            violations = geo.check_slide(produced, layout, frame=brief.frame,
                                         theme_colors=catalog.theme.colors,
                                         theme_font=catalog.theme.minor_font or "Barlow",
                                         slide_width=catalog.slide_width,
                                         slide_height=catalog.slide_height)
            report = self._builder_report(produced.parent)
            violations += geo.check_clutter(report.get("components") or [],
                                            int(report.get("placeholders_filled") or 0))
            self._record_gate(layout.key, brief.mode, violations)
            return violations
        except Exception as exc:                              # noqa: BLE001
            log.error("slide %d could not be measured (%s); reporting it as one "
                      "violation so the loop can still repair it",
                      brief.slide_number, exc,
                      extra={"session_id": self.session_id}, exc_info=True)
            return [geo.Violation(0, "unreadable", (
                f"The produced file could not be reopened and measured ({exc}). "
                f"Rebuild it with DeckBuilder and save it to OUTPUT_PATH."))]

    def _record_gate(self, layout_key: str, mode: str, violations: list) -> None:
        """Tally one gate result, so the pass can report which layouts keep failing.

        Args:
            layout_key: The layout measured against.
            mode: "layout" or "canvas", because the two fail for different reasons.
            violations: What the gate returned.
        """
        blocking = [v for v in violations if v.blocking]
        with self._gate_stats_lock:
            self._gate_stats[("gates", layout_key, mode)] += 1
            if blocking:
                self._gate_stats[("failed", layout_key, mode)] += 1
            for violation in blocking:
                self._gate_stats[("rule", layout_key, violation.rule)] += 1

    def gate_report(self, limit: int = 8) -> str:
        """The worst-offending layouts this pass, as one line for the log.

        Args:
            limit: How many layouts to name.

        Returns:
            A human-readable summary, or "" when no gate ever failed.
        """
        with self._gate_stats_lock:
            stats = Counter(self._gate_stats)
        gates = {(key, mode): n for (kind, key, mode), n in stats.items()
                 if kind == "gates"}
        failed = {(key, mode): n for (kind, key, mode), n in stats.items()
                  if kind == "failed"}
        if not failed:
            return ""
        rules: dict[str, Counter] = {}
        for (kind, key, rule), n in stats.items():
            if kind == "rule":
                rules.setdefault(key, Counter())[rule] += n
        worst = sorted(failed.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
        parts = []
        for (key, mode), fails in worst:
            total = gates.get((key, mode), fails)
            mix = ", ".join(f"rule {rule}x{n}" for rule, n
                            in sorted(rules.get(key, Counter()).items()))
            parts.append(f"{key}/{mode} {fails}/{total} failed ({mix})")
        return " | ".join(parts)

    def _step(self, brief: SlideCodeBrief, message: str, **data) -> None:
        """Stream one design step for this slide to the client.

        One event per attempt and one per outcome - the UI shows progress, not the
        gate's reasoning. The violations themselves go to the log, where they are
        long enough to be useful.
        """
        log.debug("event -> slide %d: %s", brief.slide_number, message,
                  extra={"session_id": self.session_id})
        events.publish(self.session_id, "agent_step", agent="ppt-code",
                       message=f"Slide {brief.slide_number}/{brief.context.total}: {message}",
                       slide_number=brief.slide_number, total=brief.context.total, **data)


def _without_images(messages: list[dict]) -> list[dict]:
    """The conversation with every earlier render replaced by a short placeholder.

    The system turn and the first user turn (the brief, plus any neighbour renders)
    are kept as they are; only renders sent on later turns are dropped, so a redo
    carries exactly one slide image - the latest - and the cost stays flat.

    Args:
        messages: The max-mode conversation so far.

    Returns:
        A new list; the input is not modified.
    """
    def is_image(block) -> bool:
        return isinstance(block, dict) and block.get("type") in ("image", "image_url")

    out = list(messages[:2])
    dropped = 0
    for message in messages[2:]:
        content = message.get("content")
        if isinstance(content, list) and any(is_image(b) for b in content):
            kept = [b for b in content if not is_image(b)]
            dropped += len(content) - len(kept)
            kept.insert(0, {"type": "text", "text": "[earlier render omitted]"})
            message = {**message, "content": kept}
        out.append(message)
    if dropped:
        log.debug("dropped %d earlier render(s) from the max-mode conversation", dropped)
    return out


def _extract_code(response: dict) -> str:
    """Pull the generated program out of a gateway response.

    The code-execution tool returns the program as a `file_text` input on a
    `server_tool_use` block; some gateway shapes wrap the same blocks under `choices`.

    Args:
        response: The raw JSON body returned by the gateway.

    Returns:
        The python source of the generated program.

    Raises:
        UpstreamError: When no code block is present.
    """
    blocks = response.get("content")
    if not isinstance(blocks, list):
        log.debug("no top-level content list; reading the choices[0] shape instead")
        message = (response.get("choices") or [{}])[0].get("message") or {}
        blocks = message.get("content") if isinstance(message.get("content"), list) else []

    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        if (block.get("type") == PptCreatorConstants.CODE_BLOCK_TYPE
                and block.get("name") in PptCreatorConstants.CODE_BLOCK_NAMES):
            source = (block.get("input") or {}).get(PptCreatorConstants.CODE_BLOCK_TEXT_KEY)
            if source:
                log.debug("program read from a %s block", block.get("name"))
                return source

    # No tool block. The reply is plain text, and the program may be fenced or bare.
    text = get_apim_llm_client().reply_text(response) or ""
    for label, candidate in _code_candidates(text):
        if _parses(candidate):
            log.info("no tool block; recovered a %d-char program from the %s of a "
                     "plain-text reply", len(candidate), label)
            return candidate

    log.error("the gateway returned no slide program. Reply was %d chars and held %d "
              "fenced block(s), none of them parseable Python. It began: %s",
              len(text), text.count("```") // 2, text[:600])
    raise UpstreamError("the model returned no slide program")


def _parses(source: str) -> bool:
    """True when `source` is Python that actually does something.

    Parsing alone is too weak a test for a bare reply: a one-word refusal like
    "sorry" is a syntactically valid module containing a single name expression, and
    accepting it would send prose to the sandbox. A program has to DO something - an
    import, an assignment, a call, or any real statement - so a module whose whole
    body is bare names and literals is prose, not code.

    Args:
        source: A candidate program.

    Returns:
        True when it is worth running through the static guard.
    """
    if not source or not source.strip():
        return False
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in tree.body:
        if not isinstance(node, ast.Expr):
            return True                    # import, assignment, def, for, ...
        if not isinstance(node.value, (ast.Name, ast.Constant, ast.Attribute,
                                       ast.Tuple)):
            return True                    # a call, a comparison, real work
    return False


def _code_candidates(text: str) -> list[tuple[str, str]]:
    """Every place a program might be hiding in a plain-text reply, best first.

    The old reader took `text.split("```")[1]` and required a fence. That lost two
    kinds of perfectly good answer, both of which cost real generations in production:
    a reply whose program is bare (no fence at all), and a reply whose FIRST fenced
    block is not the program - prose, a shell line, a numbered list - which was handed
    straight to the parser and rejected as "invalid decimal literal at line 1".

    Args:
        text: The model's plain-text reply.

    Returns:
        (description, source) pairs to try in order. The caller keeps the first that
        parses, so a wrong guess costs nothing.
    """
    candidates: list[tuple[str, str]] = []
    for index, match in enumerate(_FENCED_RE.finditer(text), start=1):
        candidates.append((f"fenced block {index}", match.group("body")))
    # A bare program is a complete answer that simply came without decoration.
    candidates.append(("whole reply", text.strip()))
    log.debug("plain-text reply offers %d code candidate(s)", len(candidates))
    return candidates


def new_workdir() -> tempfile.TemporaryDirectory:
    """Create the temp directory one turn's slide programs run inside.

    Returns:
        A TemporaryDirectory the caller must keep alive until the slides are stitched.
    """
    handle = tempfile.TemporaryDirectory(prefix=PptCreatorConstants.WORKDIR_PREFIX)
    log.debug("created slide work directory %s", handle.name)
    return handle

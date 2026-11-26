"""The PPT Content Agent: gathers and writes the CONTENT of exactly ONE slide.

One agent run == one slide. It researches its own evidence, writes the copy, and hands
it back. Nothing about the rest of the deck enters its context, which is what keeps the
token cost flat as the deck grows.

It deliberately stops at content: layout, colour and composition are decided afterwards
by `PptCreatorAgent`, which writes a python-pptx program for the slide. `SlideTask.op`
decides how much of the loop actually runs: a `keep` never invokes the model at all, a
`rewrite` skips retrieval.
"""

import json

from app.config.constants import (
    FaqConstants, ModelConstants, SlideAgentConstants, SlideTypes, SourceMaterialConstants,
)
from app.core.models.deck_model import (
    ContentBlock, DeckContext, SlideDraft, SlidePlan, SlideResult, SlideTask,
)
from pydantic import BaseModel

from app.genai.agents.design_principles import COPY_PRINCIPLES
from app.genai.agents.plan_rules import own_instructions
from app.utilities import events_util as events
from app.utilities.apim_llm_utils import get_apim_llm_client
from app.core.models.document_model import RetrievedChunk
from app.genai.tools.slide_tools import (
    SlideWorkspace, _image_candidates_of, build_slide_tools,
)
from app.genai.guardrails import (
    GuardrailCheckError, GuardrailViolation, TRUST_BOUNDARY, check_content,
)
from app.utilities.agent_utils import ToolCallingAgent
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

SYSTEM_PROMPT = """You are the PPT Content Agent. You write the CONTENT of EXACTLY ONE
slide of a training deck, by calling your tools — you never write slide JSON in your
reply, write_content does that.

Sequence:
1. research_slide (unless you are told the excerpts are already gathered) — 2-4 short,
   concrete queries from the slide's content needs and key points.
2. write_content — the title, the copy grouped into blocks, any table the sources
   support, a one-line `visual` steer for the designer, speaker notes, and any relevant
   image candidates.

You do NOT choose a layout, colours, fonts or positions: a separate designer turns your
content into the finished slide. Describe the shape the content wants in `visual`
(e.g. "4 comparison columns", "3-stage process flow", "single stat with supporting
bullets") and leave the drawing to it.

STRICT GROUNDING: write only from the excerpts you retrieved and any USER-PROVIDED SOURCE
MATERIAL in your task. Never invent a number,
threshold, product name, standard or acronym. Where the sources are thin, write less —
never pad with generic filler.

OUTPUT STYLE: never generate emoji characters in titles, text, bullets, tables, speaker
notes, or the final reply. Use plain professional text and punctuation only.

DEPTH: this is training material for professionals. Capture every distinct fact, number
and term the excerpts support for this slide - but split them: the on-slide blocks carry
only what supports the slide's one message, and every other fact goes into the speaker
notes as guidance for the presenter on how to elaborate it. On-slide text stays short.

KEEP COPY TIGHT: bullets are one idea each and parallel in form; prefer fewer, denser
points over long sentences. Never write a file path, URL or filename as visible copy.
When 'Available image assets' are listed, pass up to 3 exact image_ref values whose
description fits this slide's content as write_content's `image_candidates`, most
relevant first, or none when none fits. They are optional assets: the designer decides
whether an image, a diagram or both serves the slide best.

""" + COPY_PRINCIPLES + """

PRIORITY: the user's instructions for this slide come first, then the planner's notes,
then the copy principles.

Finish with one or two plain sentences describing the slide's content. No tool-call log."""

SYSTEM_PROMPT += "\n\n" + TRUST_BOUNDARY


class RoleSlideOutput(BaseModel):
    """Structured content returned for a planned role slide."""

    title: str = ""
    bullets: list[str] = []
    notes: str = ""
    #: Only when the user asked for images on this role slide: chosen image_refs.
    image_candidates: list[str] = []


ROLE_SLIDE_PROMPT = """Create one role slide for a professional training deck.

Use only the supplied plan and completed content-slide outline. Do not invent
technical facts, links, contact information, or product claims.

Role guidance:
- cover: concise title and short audience/objective context
- agenda: concise outline of the completed content topics
- takeaways: 2-4 learning outcomes supported by the content outline
- thanks: concise, professional closing message

Never generate emoji characters in the title, bullets, or notes. Use plain professional
text and punctuation only.

Return only JSON:
{"title": "...", "bullets": ["..."], "notes": "..."}"""

# Which tools each op is allowed. Withholding a tool is how an op skips work.
_TOOLS_BY_OP = {
    SlideAgentConstants.OP_CREATE: ("research_slide", "write_content"),
    SlideAgentConstants.OP_ENRICH: ("research_slide", "write_content"),
    SlideAgentConstants.OP_REWRITE: ("write_content",),
}


def _task_brief(task: SlideTask, plan: SlidePlan, context: DeckContext,
                has_chunks: bool, ws: SlideWorkspace | None = None, source_material: str = "") -> str:
    """The single user message that opens the agent run.

    Args:
        task: What this slide needs on this turn.
        plan: The planner's entry for this slide.
        context: Minimal deck-level context.
        has_chunks: Whether excerpts were already gathered.
        ws: The slide's workspace. When given and the op cannot call research_slide,
            its gathered excerpts and image assets are shown inline.
        source_material: Content the user typed or pasted into the chat, or "".

    Returns:
        The prompt text. Planner instructions are included but marked internal.
    """
    lines = [
        f"DECK: {context.title}",
        f"Audience: {context.audience} | Objective: {context.objective} "
        f"| Tone: {context.tone}",
        f"This is slide {context.position} of {context.total}."]
    if context.previous_title:
        lines.append(f"Previous slide: {context.previous_title}")
    if context.next_title:
        lines.append(f"Next slide: {context.next_title}")

    lines += ["",
              f"SLIDE {plan.slide_number} TO BUILD — planned title: {plan.title}",
              f"Key points: {plan.key_points}",
              f"Content needs (use these as your retrieval queries): {plan.content_needs}",
              f"Suggested visual: {plan.visual or '(none)'}",
              f"Planner's design hint: {task.layout_hint or plan.layout_hint or '(none)'}",
              "", f"TASK: {task.op}"]

    if task.op == SlideAgentConstants.OP_ENRICH:
        lines.append("This slide already exists but is too thin. Retrieve NEW material "
                     "with different queries, then rewrite it deeper.")
    elif task.op == SlideAgentConstants.OP_REWRITE:
        lines.append("Rewrite this slide from the excerpts already gathered — do not "
                     "retrieve. Keep the facts, change the expression as instructed.")
    if plan.user_instructions:
        lines.append("User's instructions for this slide (highest priority): "
                     f"{plan.user_instructions}")
    if task.instruction:
        lines.append(f"User's instruction for this slide: {task.instruction}")
    if task.feedback:
        lines.append("Address this feedback: " + " | ".join(task.feedback))
    if plan.images_requested:
        lines.append("IMAGES REQUESTED: the user asked for images on this slide; include "
                     "the best-matching image candidates unless they are clearly unrelated.")
    if has_chunks:
        lines.append(f"Excerpts already gathered for this slide are available.")
    if source_material.strip():
        # Pasted content is never indexed, so research cannot find it: hand it over.
        lines += ["", "USER-PROVIDED SOURCE MATERIAL (a source like a retrieved excerpt; "
                      "use only what belongs on this slide):",
                  source_material.strip()[:SourceMaterialConstants.MAX_SLIDE_PROMPT_CHARS]]
    if ws is not None and "research_slide" not in _TOOLS_BY_OP.get(task.op, ()):
        # Without research_slide the excerpts never reach the model any other way.
        lines += ["", "EXCERPTS GATHERED FOR THIS SLIDE:", ws.excerpt_block()]
        image_block = ws.image_asset_block()
        if image_block:
            lines += ["", image_block]

    extra = [_type_guidance(plan)]
    instructions = own_instructions(plan)
    if instructions:
        extra.append(_PLANNER_NOTES.format(instructions=instructions))
    extra = [section for section in extra if section]
    if extra:
        lines += ["", *extra]
    return "\n".join(lines)


_QUIZ_GUIDANCE = """SLIDE TYPE: QUIZ question. Write ONE multiple-choice question that tests
the key point, with 3-4 options (at most 6): exactly one correct option, stated by the
excerpts, and plausible distractors drawn from related facts in the excerpts - never
absurd ones. Pass it to write_content as `quiz`, with the reason the correct option is
correct in quiz.explanation. Keep `blocks` empty (or one short line of context), and
never reveal the answer in the title, blocks or visual."""

_FAQ_GUIDANCE = """SLIDE TYPE: FAQ. Write {low}-{high} question/answer pairs - fewer when
the content is thin - as genuine questions this audience would ask after the deck (how,
when, what if, who owns it), never trivial ones. Answer each in 1-3 sentences grounded in
the excerpts. Pass them to write_content as `faq` and keep `blocks` empty."""

_PLANNER_NOTES = """PLANNER NOTES (internal - follow them unless the user's instructions
say otherwise; they override the copy principles. Never copy this wording onto the
slide or into the speaker notes):
{instructions}"""


def _type_guidance(plan: SlidePlan) -> str:
    """The slide-type section of the task brief, or "" for a NORMAL slide."""
    if plan.is_quiz_question:
        return _QUIZ_GUIDANCE
    if plan.slide_type == SlideTypes.FAQ:
        low, high = FaqConstants.PAIRS_PER_SLIDE
        return _FAQ_GUIDANCE.format(low=low, high=high)
    return ""


def fallback_slide(plan: SlidePlan) -> SlideDraft:
    """A minimal, honest slide built straight from the plan, with no model involved.

    Used when the agent produced nothing — a slide is never dropped, because a missing
    slide silently changes the deck the user asked for.

    Args:
        plan: The planner's entry for this slide.

    Returns:
        A slide carrying the planned title and key points.
    """
    blocks = ([ContentBlock(slot="body1", bullets=list(plan.key_points[:6]))]
              if plan.key_points else [])
    return SlideDraft(slide_number=plan.slide_number, kind=plan.kind,
                      layout=plan.visual or plan.layout_hint,
                      title=plan.title, blocks=blocks)


def run_ppt_agent(session_id: str, task: SlideTask, slide_plan: SlidePlan,
                  deck_context: DeckContext,
                  previous: SlideDraft | None = None,
                  chunks: list[RetrievedChunk] | None = None,
                  source_material: str = "") -> SlideResult:
    """Write the content of one slide according to its task.

    Args:
        session_id: Session whose indexed sources this slide is grounded in.
        task: What this slide needs on this turn (op, instruction, feedback, hint).
        slide_plan: The planner's entry for this slide.
        deck_context: Minimal deck-level context (title, audience, position, neighbours).
        previous: This slide as built on an earlier turn, if any.
        chunks: Excerpts stored for this slide from an earlier turn, reused when the
            op does not retrieve.
        source_material: Content the user typed or pasted into the chat; a source
            alongside the indexed documents (the only one when nothing was uploaded).

    Returns:
        A SlideResult carrying the slide's content, its evidence, and how it went.
        Ordinary content failures fall back to a plan-only slide. Policy rejections
        and screening failures propagate so the workflow cannot publish a fallback.
    """
    ws = SlideWorkspace(session_id=session_id, task=task, plan=slide_plan,
                        context=deck_context,
                        chunks=list(chunks or []) if task.reuse_chunks else [])

    # keep / remove / relayout never reach the model: the stored copy is already correct,
    # and a relayout only changes the design, which the code designer redoes downstream.
    if task.op in (SlideAgentConstants.OP_KEEP, SlideAgentConstants.OP_REMOVE,
                   SlideAgentConstants.OP_RELAYOUT) and previous is not None:
        ws.candidate = previous.model_copy(deep=True)
        if task.op == SlideAgentConstants.OP_RELAYOUT:
            # Carry the new design steer through to the designer on the stored copy.
            ws.candidate.layout = task.layout_hint or slide_plan.layout_hint or previous.layout
        ws.coverage = "good" if ws.chunks else ws.coverage
        return SlideResult(
            slide_number=slide_plan.slide_number, slide_draft=ws.candidate,
            chunks=ws.chunks, coverage=ws.coverage, notes="; ".join(ws.notes))

    allowed = _TOOLS_BY_OP.get(task.op, _TOOLS_BY_OP[SlideAgentConstants.OP_CREATE])
    tools = [t for t in build_slide_tools(ws) if t.name in allowed]
    if ws.chunks:
        check_content(session_id, json.dumps([c.model_dump() for c in ws.chunks]), "sources")
    if "research_slide" not in allowed:
        # A rewrite cannot search, so a follow-up "add an image" needs its images fetched.
        ws.prefetch_images()

    ws.step(f"Slide {slide_plan.slide_number}/{deck_context.total}: {task.op} — "
            f"{slide_plan.title}", phase="start", op=task.op)
    try:
        # session_id is deliberately not passed: the tools publish their own precise
        # events, and the generic per-tool dump would stream whole slide payloads.
        ws.step(f" FOR Slide {slide_plan.slide_number}/{deck_context.total}: {task.op} — "
                f"{slide_plan.title} calling the agent ", phase="running", op=task.op)
        ToolCallingAgent(name="ppt", system_prompt=SYSTEM_PROMPT, tools=tools,
                         model=ModelConstants.REACT_AGENT,
                         recursion_limit=SlideAgentConstants.RECURSION_LIMIT).run(
            _task_brief(task, slide_plan, deck_context, bool(ws.chunks), ws, source_material))
    except (GuardrailViolation, GuardrailCheckError):
        raise
    except Exception as exc:                             
        log.exception("ppt agent failed on slide %d", slide_plan.slide_number,
                      extra={"session_id": session_id})
        ws.notes.append(f"agent failed: {exc}")

    if ws.candidate is None:
        log.warning("ppt agent wrote nothing for slide %d; using the plan-only fallback",
                    slide_plan.slide_number, extra={"session_id": session_id})
        ws.notes.append("fallback slide built from the plan")

    ws.step(f"Slide {slide_plan.slide_number}/{deck_context.total} content complete",
            phase="done", coverage=ws.coverage)

    return SlideResult(
            slide_number=slide_plan.slide_number,
            slide_draft=ws.candidate or fallback_slide(slide_plan),
            chunks=ws.chunks, coverage=ws.coverage, notes="; ".join(ws.notes))


def run_role_slide_agent(
    session_id: str,
    task: SlideTask,
    slide_plan: SlidePlan,
    deck_context: DeckContext,
    content_slides: list[SlideDraft],
    previous: SlideDraft | None = None,
) -> SlideResult:
    """Write the content of one planned cover, agenda, takeaway, or thank-you slide.

    Role slides are grounded in the finished content slides rather than in retrieval,
    which is why they run after the content phase and in their own function.

    Args:
        session_id: Session that receives progress events.
        task: Current work-order task.
        slide_plan: Planner definition for this physical slide.
        deck_context: Deck title, audience, objective, and total slide count.
        content_slides: Completed content drafts for agenda/takeaway grounding.
        previous: Existing draft used for a keep operation.

    Returns:
        The generated or reused role slide's content.
    """
    if task.op == SlideAgentConstants.OP_KEEP and previous is not None:
        return SlideResult(
            slide_number=slide_plan.slide_number,
            slide_draft=previous,
            coverage="good",
        )
    if slide_plan.kind == "section":
        # A section divider is its title and nothing else, so no model call is needed.
        return SlideResult(
            slide_number=slide_plan.slide_number,
            slide_draft=SlideDraft(slide_number=slide_plan.slide_number, kind="section",
                                   title=slide_plan.title, blocks=[]),
            coverage="good",
        )

    events.publish(
        session_id,
        "agent_step",
        agent="slide",
        message=(
            f"Slide {slide_plan.slide_number}/{deck_context.total}: "
            f"building {slide_plan.kind}"
        ),
        slide_number=slide_plan.slide_number,
        total=deck_context.total,
        slide_kind=slide_plan.kind,
        phase="start",
    )

    outline = "\n".join(
        f"{slide.slide_number}. {slide.title}"
        for slide in content_slides
    ) or "(No content slides available.)"

    prompt = "\n".join([
        f"Deck: {deck_context.title}",
        f"Audience: {deck_context.audience}",
        f"Objective: {deck_context.objective}",
        f"Slide role: {slide_plan.kind}",
        f"Planned title: {slide_plan.title}",
        f"Planned key points: {slide_plan.key_points}",
        "Completed content-slide outline:",
        outline,
    ])
    # Role slides carry no image unless the user asked for one on this slide.
    image_ws = None
    if slide_plan.images_requested:
        image_ws = SlideWorkspace(session_id=session_id, task=task, plan=slide_plan,
                                  context=deck_context)
        image_ws.prefetch_images()
        image_block = image_ws.image_asset_block()
        log.info("role slide %d: images requested; %d asset(s) offered",
                 slide_plan.slide_number, len(image_ws.image_assets()),
                 extra={"session_id": session_id})
        prompt += ("\n\n" + image_block + "\nAdd the chosen refs to the JSON as "
                   '"image_candidates": ["..."].')

    try:
        output = get_apim_llm_client().chat_json(
            ROLE_SLIDE_PROMPT,
            [{"role": "user", "content": prompt}],
            RoleSlideOutput,
            max_tokens=700,
            model=ModelConstants.WRITER,
        )
    except Exception as exc:  
        log.exception("role slide %d failed",slide_plan.slide_number,
            extra={"session_id": session_id,"slide_kind": slide_plan.kind,},)
        
        draft = fallback_slide(slide_plan)
        draft.kind = slide_plan.kind
        # Tell the UI that fallback generation completed.
        events.publish(session_id,"agent_step",agent="slide",
            message=(f"Slide {slide_plan.slide_number}/{deck_context.total} "
                "completed with fallback"),
            slide_number=slide_plan.slide_number,
            total=deck_context.total,
            slide_kind=slide_plan.kind,
            phase="done",
        )


        return SlideResult(
            slide_number=slide_plan.slide_number,
            slide_draft=draft,
            coverage="good",
            notes=f"role slide build failed: {exc}",
        )

    # Cover copy reads as a caption, every other role slide as bullets; the designer
    # decides how either is drawn.
    if slide_plan.kind == "cover":
        caption = output.bullets[0] if output.bullets else deck_context.audience
        blocks = [ContentBlock(slot="subtitle", text=caption)] if caption else []
    else:
        blocks = ([ContentBlock(slot="body1", bullets=output.bullets)]
                  if output.bullets else [])

    draft = SlideDraft(
        slide_number=slide_plan.slide_number,
        kind=slide_plan.kind,
        layout=slide_plan.visual or slide_plan.layout_hint,
        title=output.title.strip() or slide_plan.title,
        blocks=blocks,
        notes=output.notes.strip(),
        image_candidates=(_image_candidates_of(image_ws, output.image_candidates, [])
                          if image_ws is not None else []),
    )

    events.publish(
        session_id,
        "agent_step",
        agent="slide",
        message=f"Slide {slide_plan.slide_number}/{deck_context.total} complete",
        slide_number=slide_plan.slide_number,
        total=deck_context.total,
        slide_kind=slide_plan.kind,
        phase="done",
    )

    return SlideResult(
        slide_number=slide_plan.slide_number,
        slide_draft=draft,
        coverage="good",
    )
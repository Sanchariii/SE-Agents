"""The Planning Agent: a tool-calling agent (see app.utilities.agent_utils) that turns a
user's instruction into a layout-aware DeckPlan.

A single user turn may bundle several distinct instructions at once ("update slide 3's
numbers, remove slide 5, and mark this confidential") — this agent calls whichever plan
tools each one needs, in one run, instead of following one hardcoded path per intent.
"""

from app.config.constants import AgentConstants, PlannerConstants
from app.core.handlers.session_store import session_store
from app.core.models.deck_model import DeckPlan
from app.genai.agents import plan_rules
from app.genai.agents.brief import DeckBrief
from app.genai.agents.work_order import derive_work_order
from app.genai.agents.reply_language import ReplyLanguage
from app.genai.guardrails import TRUST_BOUNDARY
from app.genai.tools.plan_tools import build_plan_tools
from app.utilities.agent_utils import ToolCallingAgent
from app.utilities.logger_util import get_logger

log = get_logger(__name__)
_reply_language = ReplyLanguage()

SYSTEM_PROMPT = """You are the Planning Agent of a training-deck creation system. You
turn a user's instruction into a layout-aware DeckPlan by calling your tools — you never
write plan JSON yourself, the tools do that.

A single user turn may bundle SEVERAL distinct instructions at once (e.g. "update slide
3's numbers, remove slide 5, and mark this confidential"). Handle every one of them by
calling the right tool for each, in whatever order makes sense, before giving your final
answer. Don't stop after the first instruction if more remain.

Typical approach:
1. Call read_current_plan first to see what already exists.
2. Decide per instruction:
   - A brand-new deck, or a change to the deck's overall scope/structure -> create_deck_plan.
   - A targeted change to specific existing slide(s) -> revise_deck_slides with those
     slide numbers.
   - A request to drop slide(s) -> remove_deck_slides.
   - A request to move, reorder or rearrange slides (including putting quiz slides in
     order) -> move_deck_slides. Never use revise_deck_slides for this: it rewrites
     slides in place, so it cannot move one, and it refuses a revision that tries.
   - A request to add, insert or append a slide on some topic -> add_deck_slides
     (never create_deck_plan, which would discard the existing slides).
   - A request to make SPECIFIC slides more/less creative ("slide 4 can be more
     creative") -> set_slide_style. A change to the whole deck's creativity is already
     applied before you run; do nothing for it. A request for more or less detail or
     text on specific slides is a content change -> revise_deck_slides.
   - A request about how the QUIZ slides (or the FAQ slides) LOOK - their layout,
     style or design ("change the quiz layout", "the quiz slides look boring") ->
     set_family_style, once per family, with the look the user wants. These slides
     share one layout, so revise_deck_slides cannot change how they look; call it only
     if the user also asked to change what those slides say.
   - An explicit classification (public/internal/confidential/restricted/co-branded)
     mentioned anywhere in the message -> set_deck_classification.
   - A change to which LANGUAGES the deck is produced in ("also in French", "generate
     it in all languages", "English only") -> set_deck_languages. The slides are not
     rewritten for this; they are re-rendered and translated.
    - you can use tools update enricnch slides 
3. If create_deck_plan comes back asking a clarifying question, relay that question to
    the user as your final answer instead of guessing at missing information. Any topic,
    scope, or objective suggestion must be grounded in the uploaded document summaries;
    never invent or recommend unrelated subject matter.

QUIZ ANSWER SLIDES: a slide marked "QUIZ answer slide" mirrors the question before it
and is added and removed by the system. To change a quiz question or its answer, revise
the QUESTION slide. Remove an answer slide only when the user asks to drop that
answer's reveal; removing a question removes its answer too.

Tools run one at a time in the order you call them. add_deck_slides, remove_deck_slides
and move_deck_slides renumber the deck, so when one turn combines several changes, call
them in this order:
  1. revise_deck_slides / set_slide_layout (slide numbers as read)
  2. remove_deck_slides (slide numbers as read)
  3. move_deck_slides and add_deck_slides — each returns the renumbered outline; use
     the latest one for every later slide number.

Never invent target slide numbers that aren't in the current plan. End with a concise,
plain-language summary of what you did (or the clarifying question), written for the user
— not a tool-call log.

Never generate emoji characters in the plan, slide fields, clarifying questions, or final
summary. Use plain professional text and punctuation only.""" + TRUST_BOUNDARY

_TOOL_INTENT = {
    "create_deck_plan": "plan",
    "add_deck_slides": "add",
    "revise_deck_slides": "edit_or_enrich",
    "set_slide_layout": "relayout",
    "set_slide_style": "restyle",
    "set_family_style": "relayout",
    "remove_deck_slides": "remove",
    "move_deck_slides": "move",
    "set_deck_classification": "classification",
    "set_deck_languages": "language",
}


def _agent(session_id: str, user_message: str = "") -> ToolCallingAgent:
    tools = build_plan_tools(session_id, user_message=user_message)
    return ToolCallingAgent(name="planner", system_prompt=SYSTEM_PROMPT, tools=tools,
                            tool_intent=_TOOL_INTENT, session_id=session_id,
                            max_concurrency=AgentConstants.PLANNER_TOOL_CONCURRENCY,
                            redact_args=PlannerConstants.REDACTED_TOOL_ARGS)


def _snapshot(session_id: str) -> DeckPlan | None:
    """The stored plan as a value object, so a later mutation cannot alter it."""
    plan_dict, _ = session_store.get_plan(session_id)
    return DeckPlan(**plan_dict) if plan_dict else None


def run_planner(session_id: str, instruction: str, hints: str = "",
                enrich_slides: list[int] | None = None,
                user_language: str | None = None,
                baseline: dict | None = None) -> dict:
    """Run the planning agent on one user turn, which may bundle several distinct
    plan/add/edit/enrich/relayout/remove/classification requests.

    Args:
        session_id: Session being planned.
        instruction: The user's message.
        hints: Triage's pre-parsed breakdown, so the agent need not re-derive it.
        enrich_slides: Slide numbers triage tagged 'enrich' — the only signal that
            separates an enrich from a rewrite, since both look identical in the plan.
        user_language: The chat language for the reply.
        baseline: The plan as it was before triage changed it this turn (a consistency
            fix). The work order is diffed against it, so triage's changes are built.

    Returns:
        session_id, reply, actions_taken, target_slides, removed_slides, plan_status,
        and work_order: one serialised SlideTask per surviving slide.
    """
    before = DeckPlan(**baseline) if baseline else _snapshot(session_id)
    if baseline:
        log.info("planner diffs against the pre-triage plan (a consistency fix changed "
                 "it this turn)", extra={"session_id": session_id})

    content = instruction
    if hints:
        content += f"\n\n(Triage's parsed sub-instructions, for reference: {hints})"
    content += _reply_language.instruction(user_language)
    result = _agent(session_id, user_message=instruction).run(content)

    def _slides(*tool_names: str) -> list[int]:
        return sorted({n for call in result.tool_calls if call["name"] in tool_names
                      for n in (call["args"].get("target_slides") or [])})

    after = _snapshot(session_id)
    # The plan rules run after every tool, so the diff below sees the final deck,
    # including the quiz answer slides they add or drop.
    if after is not None:
        placement = DeckBrief.load(session_id).answer_placement()
        if plan_rules.apply(session_id, placement, before=before):
            after = _snapshot(session_id)
    # Only `create_deck_plan` is read from the tool calls: the diff cannot tell a
    # coincidentally-similar replan from a targeted edit, and a replan invalidates
    # every slide's approved copy.
    replanned = any(c["name"] == "create_deck_plan" for c in result.tool_calls)
    work_order = derive_work_order(before, after, replanned=replanned,
                                   enrich_slides=set(enrich_slides or ()),
                                   instruction=instruction)

    _, plan_status = session_store.get_plan(session_id)
    return {"session_id": session_id, "reply": result.reply, "actions_taken": result.actions,
            "target_slides": _slides("revise_deck_slides", "remove_deck_slides",
                                     "set_slide_layout"),
            "removed_slides": _slides("remove_deck_slides"), "plan_status": plan_status,
            "work_order": [t.model_dump() for t in work_order]}
"""The training-deck workflow: a LangGraph over triage, planning and per-slide building.

    triage ─┬─ chat ─────────────────────────────────────────────────────────► END
            ├─ (classification only) ──────────────────────────────► slides
            └─ plan ─► extraction ─► planning ─┬─ clarify ─────────────────► END
                                               ├─ (remove only) ────► collect
                                               └─ slides

    slides ─► collect ─┬─ publish ─► END
                       └─ done ────► END

The front half is agent-driven: `triage_agent.classify` splits one user turn into its
distinct instructions (a turn can bundle several — "update slide 3, remove slide 5, mark
this confidential" — a hardcoded single-intent branch can't act on more than one), and
`planner_agent.run_planner` is a tool-calling agent that decides for itself which plan
tool(s) each instruction needs. It returns a work order: one `SlideTask` per slide saying
what that slide needs this turn.

The back half builds ONE SLIDE AT A TIME in two phases. `slides` runs the content agent
per slide (`ppt_agent`), which is what keeps the token cost flat as the deck grows and
scopes a retry to the slide that failed rather than the whole deck. `collect` then
renumbers the deck, has `PptCreatorAgent` design each changed slide as its own .pptx —
reusing the cached file of every slide whose content is unchanged — and stitches the lot
into one deck. Publishing (minting a new numbered .pptx version, plus its translations)
happens exactly once per user turn, in ``_publish``.
"""

import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import TypedDict


from langgraph.graph import END, StateGraph

from app.config.constants import (
    AgentNames, CreativityLevels, EventTypes, PlanStatus,
    PptCreatorConstants, QuestionIds, QuizConstants, ReferenceDesignConstants, RunStages,
    SlideAgentConstants, SlideCountConstants, SlideTypes, WorkUnitType,
)
from app.core.handlers.session_store import session_store
from app.core.models.deck_model import (
    ContentBlock, ContentBundle, DeckContext, DeckDesignContext, DeckDraft, DeckPlan,
    SlideCodeBrief, SlideContent, SlideResult, SlideTask,
)
from app.genai.agents import plan_rules
from app.genai.agents.plan_rules import own_instructions
from app.genai.agents.brief import (
    SOURCE_CONTENT_REQUEST, DeckBrief, PlanReview, QuestionBook, has_source_documents,
    render_questions,
)
from app.genai.agents.planner import apply_classification
from app.genai.agents.work_order import derive_work_order
from app.genai.agents.planner_agent import run_planner
from app.genai.agents.reply_language import ReplyLanguage
from app.genai.agents.ppt_agent import fallback_slide, run_ppt_agent, run_role_slide_agent
from app.genai.agents.ppt_creator_agent import (
    PptCreatorAgent, build_id_of, materialize_images,
)
from app.genai.agents.triage_agent import (
    SIZE_INTENTS, TriageDecision, classify as triage_classify,
)
from app.genai.tools import plan_tools
from app.genai.guardrails import (
    GuardrailCheckError, GuardrailViolation, check_content, report_violation,
)
from app.genai.ppt import design_contract, reference_design, slide_cache
from app.genai.skills.designer_enrichment import (
    add_skill_guidance, prepare_active_skill, skill_cache_suffix,
)
from app.genai.ppt.stitcher import DeckStitcher
from app.genai.ppt.template import catalog as catalog_mod, registry
from app.genai.ingestion.pipeline import ensure_session_indexed
from app.genai.ppt.builder import persist_deck
from app.genai.deck_polish import engine as deck_polish
from app.genai.skill_deck import deck_engine as skill_deck
from app.genai.workflows.recovery import stage_node
from app.utilities import events_util as events
from app.utilities import run_control
from app.utilities.analytics import agent_run, workflow_run
from app.utilities.observability import build_metrics
from app.utilities.logger_util import (ContextThreadPoolExecutor, get_logger,
                                       log_context, run_id_ctx)

log = get_logger(__name__)
_reply_language = ReplyLanguage()
_plan_review = PlanReview()
_QUESTION_BOOK = QuestionBook()


class WorkflowState(TypedDict, total=False):
    session_id: str
    message: str
    refresh_content: bool
    action: str
    reply: str
    deck_version: int
    pending_publish: bool
    needs_planner: bool
    route: str
    user_language: str
    hints: str
    actions_taken: list
    target_slides: list
    removed_slides: list
    classification_changed: str
    work_order: list
    slide_results: list
    enrich_slides: list
    deck_gaps: list
    deck_path: str
    deck_workdir: str
    #: Structured answers sent with the turn (`{question id: value}`), already validated.
    answers: dict
    #: True when the turn carried only structured answers and no typed message.
    answers_only: bool
    #: The structured questions this turn asks (ClarifyingQuestion dicts).
    questions: list
    #: Which deck-wide style preferences changed this turn.
    style_change: dict
    #: The stored plan before this turn's answer-placement normalisation, if any.
    plan_before: dict
    guardrail_flags: list[dict]


def _step(session_id: str, agent: str, message: str) -> None:
    events.publish(session_id, "agent_step", agent=agent, message=message)


# --------------------------------------------------------------------------- #
# Nodes                                                                        #
# --------------------------------------------------------------------------- #

def _triage(state: WorkflowState) -> dict:
    sid, message = state["session_id"], state["message"]
    answers = state.get("answers") or {}
    answers_only = bool(state.get("answers_only"))
    check_content(sid, json.dumps({"message": message, "answers": answers}), "request")
    if not answers_only:
        # First, before anything reads the session's documents: a deck the user asks
        # to copy the design of is design only, so it is out of the content from here.
        note = reference_design.claim_from_message(sid, message)
        if note:
            _step(sid, "triage", note)
    with agent_run(AgentNames.TRIAGE):
        before = DeckBrief.load(sid)
        # Structured answers need no model to understand them, so an answers-only turn
        # skips the triage call entirely.
        decision = (_answers_decision(before) if answers_only
                    else triage_classify(sid, message))
        if not decision.on_topic:
            raise GuardrailViolation("unsupported_topic", "request")
        # No plan or brief mutation is permitted until both gates accept the turn.
        changed = None if answers_only else apply_classification(sid, message)
        if changed:
            _step(sid, "triage", f"Classification set to {changed} — deck will be rebuilt "
                                 f"on the {changed} template.")
        # Deck-size limit: the oversized number is never stored, the rest of the turn is.
        refused_slides = 0
        if decision.brief_update.over_request_limit():
            refused_slides = decision.brief_update.requested_total()
            log.warning("triage: %d slides requested; the limit is %d, so the turn is "
                        "refused", refused_slides, SlideCountConstants.MAX_REQUESTED,
                        extra={"session_id": sid})
            decision.brief_update = decision.brief_update.without_slide_count()
        brief = before.merge(decision.brief_update)
        # Answers are applied last, so nothing the model read into the text overrides
        # what the user picked explicitly.
        if answers:
            brief = brief.apply_answers(answers)
        # Material typed into the chat is kept verbatim in code, never re-written by the
        # model, so the planner and the slide writers ground on the user's own words.
        if decision.brief_update.content_provided and not answers_only:
            brief = brief.with_source_material(message)
        if not answers_only:
            brief.chat_language = decision.user_language
        # Deck size: asked again only for a new, recreated or resized deck that names
        # no size. Slide edits, adds and removals never reopen it.
        ask_size = False
        if _whole_deck_unsized(decision, answers):
            delegated = decision.brief_update.use_defaults
            # "You decide" in the same breath: the agent sizes it, nothing is asked.
            brief = brief.reset_size("" if delegated else message)
            ask_size = not delegated
        elif (brief.pending_request and decision.turn_type == "request"
              and not decision.brief_update.states_size()):
            log.info("triage: a new request replaces the unanswered whole-deck request",
                     extra={"session_id": sid})
            brief.pending_request = ""
        brief.save(sid)
        if refused_slides:
            return _refuse_slide_request(sid, decision, brief, refused_slides, changed,
                                         ask_size=ask_size)

        style_change = _style_change(before, brief)
        plan_dict, plan_status = session_store.get_plan(sid)
        built = bool(session_store.list_decks(sid))
        plan_before = None
        if style_change["placement"] and plan_dict:
            refusal = plan_rules.ceiling_violation(
                DeckPlan(**plan_dict), brief.answer_placement(), sid)
            if refusal:
                # Answer slides would push the deck past the ceiling: keep the old
                # placement (pinned, since a lifted cap can flip it too) and say why.
                brief.quiz_answer_placement = before.answer_placement()
                brief.save(sid)
                style_change["placement"] = False
                decision.reply = _reply_language.translate(
                    [refusal], decision.user_language)[0]
        if style_change["placement"] and plan_dict:
            # Answer slides are added or dropped now, before anything plans or builds.
            plan_before = plan_dict
            plan_rules.apply(sid, brief.answer_placement())
            plan_dict, plan_status = session_store.get_plan(sid)

        # "It looks inconsistent" is fixed in code, like a style change: no planner.
        consistency = [i for i in decision.instructions if i.intent == "consistency"]
        style_change["design"] = False
        if consistency and plan_dict:
            plan_before = plan_before or plan_dict
            if any(not i.target_slides for i in consistency):
                # The whole deck: back to MED, HIGH overrides cleared, all redrawn.
                brief.creativity_level = CreativityLevels.MED
                brief.save(sid)
                outcome = plan_tools.reset_deck_design(sid)
                log.info("triage: whole-deck consistency fix: %s", outcome,
                         extra={"session_id": sid})
            else:
                targets = {n for i in consistency for n in i.target_slides}
                outcome = plan_tools.fix_slide_consistency(
                    sid, targets, brief.effective_creativity())
                log.info("triage: consistency fix for slides %s: %s", sorted(targets),
                         outcome, extra={"session_id": sid})
            style_change["design"] = True
            style_change["creativity"] = _style_change(before, brief)["creativity"]
            plan_dict, plan_status = session_store.get_plan(sid)
        elif consistency:
            log.info("triage: a consistency complaint with no plan yet; nothing to fix",
                     extra={"session_id": sid})

        work = [i for i in decision.instructions if i.intent not in ("style", "consistency")]
        # Only slider/placement answers are a style turn; brief answers (goal,
        # audience, number of slides, ...) carry on to planning like a typed answer would.
        # Retired ids (detailing) still count, so an old slider answers "unchanged".
        style_answer_ids = (set(QuestionIds.STYLE) | set(QuestionIds.RETIRED)
                            | {QuestionIds.QUIZ_ANSWER_PLACEMENT})
        style_asked = ((answers_only and set(answers) <= style_answer_ids)
                       or len(work) < len(decision.instructions))
        if style_asked and not work and plan_dict and not any(style_change.values()):
            # Nothing to plan and nothing to restyle: say so rather than replanning.
            decision.needs_planner = False
        # A brand-new deck needs something to be grounded in; without an upload or
        # pasted content the planner would only invent it, so the turn stops here.
        needs_content = (not plan_dict and not built
                         and brief.needs_source_content(has_source_documents(sid)))
        route = _route(decision, brief, plan_status, built,
                       style_only=style_asked and not work and any(style_change.values()),
                       has_plan=bool(plan_dict), ask_size=ask_size,
                       needs_content=needs_content)
        if route == "clarify" and needs_content:
            log.info("no source documents or pasted content for a new deck; asking the "
                     "user for content before planning", extra={"session_id": sid})
            _step(sid, "triage", "No source material yet; asking for content before "
                                 "planning.")
        if route == "chat" and style_asked and not work and not any(style_change.values()):
            decision.reply = decision.reply or _reply_language.translate(
                ["Your style preferences are unchanged."], decision.user_language)[0]
        hints = "; ".join(
            f"[{i.intent}{f' slides={i.target_slides}' if i.target_slides else ''}] {i.text}"
            for i in work)
        if route == "extraction" and brief.pending_request:
            # The size was just answered: plan the whole-deck request it was asked for.
            lead = ("[plan] The user's whole-deck request, now that the deck size is "
                    f"chosen: {brief.pending_request}")
            hints = f"{lead}; {hints}" if hints else lead
            log.info("triage: planning the pending whole-deck request",
                     extra={"session_id": sid})
            brief.pending_request = ""
            brief.save(sid)
        hints = hints or "[plan] Create the deck plan from the agreed brief."

        _step(sid, "triage", f"turn={decision.turn_type} route={route} "
                             f"instructions={[i.intent for i in decision.instructions]}")

        # Only triage knows an 'enrich' was asked for: an enrich and a rewrite leave the
        # plan looking identical, and the difference is whether to retrieve again.
        enrich_slides = sorted({n for i in decision.instructions
                                if i.intent == "enrich" for n in i.target_slides})

        # Source material is shared in chat, not through a structured question card.
        questions = (_clarify_questions(decision, brief, ask_size,
                                        first_deck=not plan_dict and not built)
                     if route == "clarify" and not needs_content else [])
        reply = _triage_reply(decision, brief, route, questions, needs_content)
        if questions:
            # The typed questions go out as their own event, so the UI can render
            # sliders and choices instead of parsing the numbered reply.
            events.publish(sid, EventTypes.QUESTIONS, agent="triage",
                           message=reply.splitlines()[0] if reply else "",
                           questions=[q.model_dump() for q in questions])

        return {"needs_planner": route == "extraction", "hints": hints, "route": route,
                "action": "clarify" if route == "clarify" else "chat",
                "reply": reply,
                "classification_changed": changed,
                "user_language": decision.user_language,
                "enrich_slides": enrich_slides,
                "questions": [q.model_dump() for q in questions],
                "style_change": style_change,
                "plan_before": plan_before}


def _refuse_slide_request(sid: str, decision: TriageDecision, brief: DeckBrief,
                          requested: int, classification_changed: str | None,
                          ask_size: bool = False) -> dict:
    """End the turn politely when it asks for more slides than a user may request.

    For a first, new, recreated or resized deck the reply re-asks the deck size as a
    `number_of_slides` slider, so the user can pick a valid size or let the agent
    decide with "20+". An edit to an existing deck (e.g. adding slides) leaves the
    deck unchanged and asks nothing.

    Args:
        sid: The session.
        decision: Triage's decision for the turn (for its language and questions).
        brief: The saved brief, without the refused size.
        requested: The slide total the user asked for.
        classification_changed: A classification applied this turn, if any.
        ask_size: True for a whole-deck request (new deck or resize).

    Returns:
        The triage node's state update: no planning, a reply, and any questions.
    """
    plan_dict, _ = session_store.get_plan(sid)
    first_deck = not plan_dict and not session_store.list_decks(sid)
    show_slider = first_deck or ask_size
    limit = SlideCountConstants.MAX_REQUESTED
    # The triage model writes the refusal in context and in the user's language; the
    # fallback only covers a turn where it wrote none.
    intro = decision.reply.strip() or _reply_language.translate(
        [f"Sorry, I can't create a deck with {requested} slides: in the current "
         f"configuration a deck can have at most {limit} slides."
         + (" Please choose a deck size below." if show_slider else
            " The deck is unchanged.")],
        decision.user_language)[0]
    questions = (_clarify_questions(decision, brief, True, first_deck)
                 if show_slider else [])
    reply = render_questions(intro, questions) if questions else intro
    if questions:
        events.publish(sid, EventTypes.QUESTIONS, agent="triage", message=intro,
                       questions=[q.model_dump() for q in questions])
    route = "clarify" if questions else "chat"
    _step(sid, "triage", f"Refused a {requested}-slide request (limit {limit}); "
                         f"route={route}")
    return {"needs_planner": False, "hints": "", "route": route,
            "action": "clarify" if questions else "chat", "reply": reply,
            "classification_changed": classification_changed,
            "user_language": decision.user_language, "enrich_slides": [],
            "questions": [q.model_dump() for q in questions],
            "style_change": {"creativity": False, "placement": False, "design": False},
            "plan_before": None}


def _whole_deck_unsized(decision: TriageDecision, answers: dict) -> bool:
    """Whether this turn asks for a new, recreated or resized deck without a size.

    Args:
        decision: Triage's decision for the turn.
        answers: Structured answers sent with the turn.

    Returns:
        True for a "new_deck"/"resize" request that states no count, cap or range and
        picks no size answer; such a turn asks the deck size again.
    """
    if (decision.size_intent not in SIZE_INTENTS or decision.turn_type != "request"
            or not decision.needs_planner):
        return False
    stated = (decision.brief_update.states_size()
              or QuestionIds.NUMBER_OF_SLIDES in answers)
    log.info("triage: whole-deck request (%s), size %s", decision.size_intent,
             "stated" if stated else "not stated")
    return not stated


def _clarify_questions(decision: TriageDecision, brief: DeckBrief, ask_size: bool,
                       first_deck: bool) -> list:
    """The structured questions for a clarify turn.

    Args:
        decision: Triage's decision (its written questions and the chat language).
        brief: The brief after this turn.
        ask_size: True when the deck size must be asked (whole-deck request or refusal).
        first_deck: True when the session has no plan or deck yet.

    Returns:
        The questions: a resize of an existing deck asks only the size; otherwise the
        missing brief items plus the size where it is due.
    """
    force = (QuestionIds.NUMBER_OF_SLIDES,) if ask_size else ()
    if ask_size and not first_deck and decision.size_intent == "resize":
        # The rest of the brief stands for a resize, so nothing else is asked.
        return _QUESTION_BOOK.build(brief.model_copy(update={"use_defaults": True}), [],
                                    decision.user_language, force=force)
    return _QUESTION_BOOK.build(brief, decision.questions, decision.user_language,
                                force=force, include_size=first_deck)


def _answers_decision(brief: DeckBrief) -> TriageDecision:
    """The triage decision for a turn that carried only structured answers.

    Args:
        brief: The brief before the answers are applied (for the chat language).

    Returns:
        An "answer" turn in the user's last known chat language.
    """
    return TriageDecision(turn_type="answer", needs_planner=True,
                          user_language=brief.chat_language or "English")


def _style_change(before: DeckBrief, after: DeckBrief) -> dict:
    """Which deck-wide style preferences this turn changed.

    Effective values are compared, so a session that never chose a level (it was built
    at MED) changes only when the new choice differs from MED.

    Args:
        before: The brief at the start of the turn.
        after: The brief after this turn's update and answers.

    Returns:
        {"creativity": bool, "placement": bool}
    """
    return {
        "creativity": before.effective_creativity() != after.effective_creativity(),
        "placement": before.answer_placement() != after.answer_placement(),
    }


def _route(decision, brief: DeckBrief, plan_status: str, built: bool,
           style_only: bool = False, has_plan: bool = False,
           ask_size: bool = False, needs_content: bool = False) -> str:
    """Whether to ask, build, plan, restyle or just answer. Missing information is
    decided here, in code, so asking never depends on the model remembering to.

    Args:
        decision: The triage decision for this turn.
        brief: The brief after this turn's update.
        plan_status: The stored plan's status.
        built: Whether a deck has been built in this session.
        style_only: True when the turn only changes deck-wide style.
        has_plan: Whether a plan is stored.
        ask_size: True when a new or resized deck needs a fresh size choice.
        needs_content: True when a new deck has no source material to ground it.

    Returns:
        "approve", "restyle", "chat", "clarify" or "extraction".
    """
    if decision.turn_type == "approve":
        return "approve"
    # A deck-wide style change needs no planner: the stored plan is restyled in code.
    if style_only and has_plan:
        return "restyle"
    if not decision.needs_planner:
        return "chat"
    # "You decide" can waive brief questions, but never the need for source material.
    if needs_content or ask_size:
        return "clarify"
    replan = any(i.intent == "plan" for i in decision.instructions)
    # A proposed plan being revised, or a small edit to a built deck, needs no questions.
    if plan_status == PlanStatus.DRAFT or (built and not replan):
        return "extraction"
    first_deck = not has_plan and not built
    return "clarify" if brief.missing(include_size=first_deck) else "extraction"


def _triage_reply(decision, brief: DeckBrief, route: str,
                  questions: list | None = None, needs_content: bool = False) -> str:
    """The message for turns that end at triage: questions, a polite decline, or chat.

    Args:
        decision: The triage decision for this turn.
        brief: The brief after this turn's update.
        route: The route chosen for this turn.
        questions: The structured questions, when already built.
        needs_content: True when the turn is blocked on source material.

    Returns:
        The chat reply text.
    """
    if route == "clarify" and needs_content:
        # Code-written, so the request for content is always plain and the same.
        return _reply_language.translate([SOURCE_CONTENT_REQUEST],
                                         decision.user_language)[0]
    if route == "clarify":
        if questions is None:
            questions = _QUESTION_BOOK.build(brief, decision.questions,
                                             decision.user_language)
        intro = decision.reply or _reply_language.translate(
            ["I need a little more information before planning."],
            decision.user_language)[0]
        return render_questions(intro, questions)
    if not decision.on_topic and decision.suggestions:
        return "\n".join([decision.reply, *[f"- {s}" for s in decision.suggestions]])
    return decision.reply


def _restyle(state: WorkflowState) -> dict:
    """Apply a deck-wide style change to the stored plan without the planner.

    Creativity changes only how slides are drawn: the design cache key carries it, so
    every slide is redesigned and none is rewritten. An answer-placement change adds or drops answer slides (already applied to the plan
    by triage). A proposed plan that is not built yet is simply shown again.
    """
    sid = state["session_id"]
    brief = DeckBrief.load(sid)
    plan_dict, plan_status = session_store.get_plan(sid)
    plan = DeckPlan(**plan_dict)
    if plan_status == PlanStatus.DRAFT or not session_store.list_decks(sid):
        _step(sid, "planning", "Style preferences updated; showing the plan again.")
        return {"action": "await_approval", "actions_taken": ["restyle"],
                "work_order": [],
                "reply": _plan_review.format(plan, state.get("user_language"), brief)}

    before = DeckPlan(**(state.get("plan_before") or plan_dict))
    order = derive_work_order(before, plan)
    changed = [k for k, v in (state.get("style_change") or {}).items() if v]
    _step(sid, "planning", f"Restyling the deck ({', '.join(changed)}).")
    log.info("restyle: %s changed; %d slide task(s)", changed, len(order),
             extra={"session_id": sid})
    return {"action": "plan", "actions_taken": ["restyle"], "reply": "",
            "work_order": [t.model_dump() for t in order]}


def _extraction(state: WorkflowState) -> dict:
    # Gate: planning must see fully-indexed docs, so wait out in-flight ingestion first.
    ensure_session_indexed(state["session_id"])
    check_content(state["session_id"], json.dumps({
        "message": state["message"], "answers": state.get("answers") or {},
    }), "request")
    return {}


def _planning(state: WorkflowState) -> dict:
    """Hand the turn to the planning agent — a tool-calling agent that itself decides
    which plan tool(s) the (possibly several) triaged instructions need, including
    creating/revising/removing slides and setting classification, in one run."""

    sid = state["session_id"]
    _step(sid, "planning", "Interpreting the request against the source documents…")

    with agent_run(AgentNames.PLANNER):
        # A consistency fix already changed the plan in triage; diff from before it.
        baseline = (state.get("plan_before")
                    if (state.get("style_change") or {}).get("design") else None)
        result = run_planner(sid, state["message"], hints=state.get("hints", ""),
                             enrich_slides=state.get("enrich_slides") or [],
                             user_language=state.get("user_language"),
                             baseline=baseline)
    actions = result.get("actions_taken") or []
    _step(sid, "planning", f"actions={actions or '(clarifying question)'}")

    # A pure removal trims the stored draft in place and skips `write` entirely (see
    # `_after_planning`), so nothing else would ever flag that the trimmed draft still
    # needs a fresh .pptx build. Without this, the removal is applied to session state
    # but never republished — the deck a user downloads still has the "removed" slides.
    pending_publish = bool(actions and set(actions) <= {"remove"}
                           and session_store.get_draft(sid))

    # A proposed plan is shown for review and nothing is built until the user approves it.
    plan_dict, plan_status = session_store.get_plan(sid)
    awaiting = bool(actions) and plan_status == PlanStatus.DRAFT and bool(plan_dict)
    reply = (_plan_review.format(DeckPlan(**plan_dict), state.get("user_language"),
                                 DeckBrief.load(sid))
             if awaiting else result.get("reply", ""))

    work_order = result.get("work_order", [])

    return {"action": ("await_approval" if awaiting else "plan" if actions else "clarify"),
            "reply": reply,
            "actions_taken": actions, "target_slides": result.get("target_slides", []),
            "removed_slides": result.get("removed_slides", []),
            "work_order": work_order,
            "pending_publish": pending_publish}


def _approve(state: WorkflowState) -> dict:
    """The user accepted the proposed plan: confirm it and build every slide of it."""
    sid = state["session_id"]
    plan_dict, _ = session_store.get_plan(sid)
    check_content(sid, DeckPlan(**plan_dict).model_dump_json(
        exclude={"guardrail_warnings"}), "plan")
    session_store.set_plan(sid, plan_dict, status=PlanStatus.CONFIRMED)
    order = derive_work_order(None, DeckPlan(**plan_dict), replanned=True)
    _step(sid, "planning", "Plan approved; building the deck.")
    return {"action": "plan", "actions_taken": ["approve"],
            "work_order": [t.model_dump() for t in order]}


def _deck_context(plan: DeckPlan, position: int) -> DeckContext:
    """Deck-level context for the slide at 1-based `position` — neighbours only, never
    the whole deck, or the per-slide token saving is given straight back."""
    titles = [s.title for s in plan.slides]
    return DeckContext(
        title=plan.title, audience=plan.audience, objective=plan.objective,
        tone=plan.tone, position=position, total=len(plan.slides),
        previous_title=titles[position - 2] if position > 1 else "",
        next_title=titles[position] if position < len(titles) else "")


def _slides(state: WorkflowState) -> dict:
    """Run the PPT agent once per actionable slide, per the planner's work order."""
    sid = state["session_id"]
    plan_dict, _ = session_store.get_plan(sid)
    if not plan_dict:
        log.warning("slides skipped: no plan", extra={"session_id": sid})
        _step(sid, "slide", "No plan found — nothing to build this turn.")
        return {"slide_results": []}

    plan = DeckPlan(**plan_dict)
    check_content(sid, plan.model_dump_json(exclude={"guardrail_warnings"}), "plan")
    plan_by_number = {s.slide_number: s for s in plan.slides}

    stored_draft = session_store.get_draft(sid)
    previous = ({s.slide_number: s for s in DeckDraft(**stored_draft).slides}
                if stored_draft else {})
    stored_content = session_store.get_content(sid)
    chunks_by_number = ({s.slide_number: s.chunks
                         for s in ContentBundle(**stored_content).slides}
                        if stored_content else {})

    tasks = [SlideTask(**t) for t in (state.get("work_order") or [])]
    # Loaded once for the deck: chat-pasted content is a source every slide may draw on.
    source_material = DeckBrief.load(sid).source_material

    if not tasks:
        # No work order: a classification-only turn re-renders what exists, a first
        # build has nothing stored and must create everything.
        default_op = (SlideAgentConstants.OP_KEEP if previous
                      else SlideAgentConstants.OP_CREATE)
        tasks = [SlideTask(slide_number=s.slide_number, kind=s.kind, op=default_op,
                           layout_hint=s.layout_hint,
                           reuse_chunks=default_op != SlideAgentConstants.OP_CREATE)
                 for s in plan.slides]

    feedback = state.get("feedback") or []

    # Agenda and takeaways are refreshed by the work order only when the content outline
    # changed (see work_order._refresh_outline_slides), never on every content edit.
    actionable = [t for t in tasks if t.op not in (SlideAgentConstants.OP_KEEP,
                                                   SlideAgentConstants.OP_REMOVE)]
    
    # Answer slides are their question with the correct choice revealed: they are
    # derived from the question's draft in `collect`, never written by the agent.
    answer_numbers = {s.slide_number for s in plan.slides if s.is_quiz_answer}
    content_tasks = [task for task in actionable if task.kind == "content"
                     and task.slide_number not in answer_numbers]
    role_tasks = [task for task in actionable if task.kind != "content"]

    _step(
        sid,
        "slide",
        f"Processing {len(actionable)} changed slide(s) out of "
        f"{len(tasks)} planned slide(s): {len(content_tasks)} content, "
        f"{len(role_tasks)} role.",
    )

    def build_content (task: SlideTask) -> SlideResult | None:
        # A stopped run must not start another slide. The stop exceptions derive from
        # BaseException, so the per-slide fallback below can never swallow them.
        run_control.checkpoint()
        planned = plan_by_number.get(task.slide_number)
        if planned is None:
            return None

        if feedback and task.op != SlideAgentConstants.OP_KEEP:
            task = task.model_copy(update={"feedback": feedback})
        # One span per slide: each worker thread inherits a copied context, so parallel
        # slides accumulate their own tokens/cost without racing.
        with agent_run(AgentNames.SLIDE_CONTENT) as span:
            span.set_work_units(1, WorkUnitType.SLIDE)
            try:
                return run_ppt_agent(
                    sid, task, planned,
                    _deck_context(plan, task.slide_number),
                    previous=previous.get(task.slide_number),
                    chunks=list(chunks_by_number.get(task.slide_number) or []),
                    source_material=source_material)
            except (GuardrailViolation, GuardrailCheckError):
                raise
            except Exception as exc:  # noqa: BLE001
                # One slide must never take the deck down with it, least of all when the
                # slides are running concurrently.
                span.mark_fallback()
                log.exception("slide %d failed to build", task.slide_number,
                              extra={"session_id": sid})
                _step(sid, "slide", f"Slide {task.slide_number} failed ({exc}); "
                                    f"falling back to its planned outline.")
                stored = previous.get(task.slide_number)
                return SlideResult(
                    slide_number=task.slide_number,
                    slide_draft=stored or fallback_slide(planned),
                    chunks=list(chunks_by_number.get(task.slide_number) or []),
                    notes=f"slide build failed: {exc}")

    workers = min(SlideAgentConstants.MAX_WORKERS, len(content_tasks) or 1)
    if workers > 1:
        with ContextThreadPoolExecutor(max_workers=workers) as pool:
            content_results = list(pool.map(build_content, content_tasks))
    else:
        content_results = [build_content(task) for task in content_tasks]

    fresh_content = {result.slide_number: result.slide_draft for result in content_results
                      if result is not None}
    
    completed_content = [fresh_content.get(slide.slide_number) or previous.get(slide.slide_number)
                         for slide in plan.slides
                         if slide.kind == "content" and not slide.is_quiz_answer]
    completed_content = [ slide for slide in completed_content if slide is not None]
    
    role_results = []
    for task in role_tasks:
        run_control.checkpoint()
        planned = plan_by_number.get(task.slide_number)
        if planned is None:
            continue

        with agent_run(AgentNames.SLIDE_CONTENT) as span:
            span.set_work_units(1, WorkUnitType.SLIDE)
            result = run_role_slide_agent(
                session_id=sid,
                task=task,
                slide_plan=planned,
                deck_context=_deck_context(plan, task.slide_number),
                content_slides=completed_content,
                previous=previous.get(task.slide_number),
            )
        role_results.append(result)

    results = content_results + role_results

    # Ordered by slide number, never by completion order.
    ordered = sorted((r for r in results if r is not None),
                     key=lambda r: r.slide_number)
    # Writers pick images in parallel and blind to each other; one figure per deck.
    kept = {number: slide for number, slide in previous.items()
            if number in plan_by_number and number not in {r.slide_number for r in ordered}}
    _dedupe_images(sid, ordered, kept)
    _assign_images(sid, ordered, kept, plan_by_number)
    return {"slide_results": [r.model_dump(mode="json") for r in ordered]}


def _dedupe_images(session_id: str, written: list[SlideResult],
                   kept: dict[int, object]) -> int:
    """Let each image appear on one slide of the deck only.

    Only the slides written this turn are changed, so a slide that is kept keeps its
    image and its cached design. An image a kept slide already shows is taken; among
    this turn's slides the earliest keeps it. A block left with neither text nor an
    image is dropped.

    Args:
        session_id: Owning session, for logs.
        written: This turn's slide results, in slide order. Modified in place.
        kept: Slide number -> draft of every slide that is not rewritten this turn.

    Returns:
        How many duplicate image references were removed.
    """
    taken = {(b.image_ref or "").strip() for slide in kept.values()
             for b in slide.blocks if (b.image_ref or "").strip()}
    removed = 0
    for result in written:
        blocks = []
        for block in result.slide_draft.blocks:
            ref = (block.image_ref or "").strip()
            if ref and ref in taken:
                removed += 1
                log.info("slide %d: dropped image %s, already used elsewhere in the deck",
                         result.slide_number, ref, extra={"session_id": session_id})
                block = block.model_copy(update={"image_ref": ""})
                if not block.text.strip() and not block.bullets:
                    continue
            elif ref:
                taken.add(ref)
            blocks.append(block)
        result.slide_draft.blocks = blocks
    if removed:
        log.info("image dedup: removed %d duplicate image reference(s) across %d slide(s) "
                 "written this turn", removed, len(written), extra={"session_id": session_id})
    return removed


def _assign_images(session_id: str, written: list[SlideResult],
                   kept: dict[int, object], plan_by_number: dict) -> int:
    """Give each image candidate to one slide of the deck.

    Designers run in parallel and blind to each other, so an image offered to two
    slides could land on both. Each ref goes to one slide: an image a kept slide
    already carries is taken; otherwise a slide whose user asked for images wins, then
    the slide that ranked the image higher, then the earliest. A slide the user allowed
    to reuse images keeps its candidates. A slide that may carry no image (cover,
    agenda, takeaways, thanks, quiz, FAQ - unless asked) keeps none.

    Args:
        session_id: Owning session, for logs and the user-facing event.
        written: This turn's slide results, in slide order. Modified in place.
        kept: Slide number -> draft of every slide that is not rewritten this turn.
        plan_by_number: Slide number -> its plan entry.

    Returns:
        How many candidates were removed.
    """
    taken = {ref for slide in kept.values() for ref in slide.image_refs()}
    # Block images (the older way of attaching one) stay where they are.
    taken |= {(b.image_ref or "").strip() for r in written for b in r.slide_draft.blocks
              if (b.image_ref or "").strip()}
    winner: dict[str, tuple] = {}
    for result in written:
        planned = plan_by_number.get(result.slide_number)
        requested = bool(planned is not None and planned.images_requested)
        for rank, candidate in enumerate(result.slide_draft.image_candidates):
            claim = (0 if requested else 1, rank, result.slide_number)
            if candidate.ref not in winner or claim < winner[candidate.ref]:
                winner[candidate.ref] = claim

    removed = 0
    for result in written:
        planned = plan_by_number.get(result.slide_number)
        draft = result.slide_draft
        if not draft.image_candidates:
            if planned is not None and planned.images_requested:
                _no_image_found(session_id, result.slide_number)
            continue
        if planned is not None and not planned.images_allowed:
            log.info("slide %d: %d image candidate(s) removed; images are not used on "
                     "this slide unless the user asks", result.slide_number,
                     len(draft.image_candidates), extra={"session_id": session_id})
            removed += len(draft.image_candidates)
            draft.image_candidates = []
            continue
        reuse = bool(planned is not None and planned.allow_image_reuse)
        keep = [c for c in draft.image_candidates
                if reuse or (c.ref not in taken
                             and winner.get(c.ref, (0, 0, result.slide_number))[2]
                             == result.slide_number)]
        dropped = len(draft.image_candidates) - len(keep)
        if dropped:
            removed += dropped
            log.info("slide %d: %d image candidate(s) given to another slide; %d kept",
                     result.slide_number, dropped, len(keep),
                     extra={"session_id": session_id})
        draft.image_candidates = keep
        if not keep and planned is not None and planned.images_requested:
            _no_image_found(session_id, result.slide_number)
    log.info("image assignment: %d candidate(s) across %d slide(s) written this turn, "
             "%d removed", sum(len(r.slide_draft.image_candidates) for r in written),
             len(written), removed, extra={"session_id": session_id})
    return removed


def _no_image_found(session_id: str, slide_number: int) -> None:
    """Tell the user why a slide they asked images for has none."""
    log.info("slide %d: images requested but no relevant source image is available",
             slide_number, extra={"session_id": session_id})
    events.publish(session_id, "agent_step", agent="slide",
                   message=(f"Slide {slide_number}: no relevant source image was found "
                            f"for this slide."),
                   slide_number=slide_number, phase="image_missing")


def _renumber_quiz_title(title: str, number: int) -> str:
    """Give a numbered quiz title its question's position in the deck.

    Args:
        title: The question slide's title, e.g. "Quiz 3: Open Agent Protocols".
        number: The question's 1-based position among the deck's quiz questions.

    Returns:
        The title with its leading quiz number replaced; unchanged when it has none.
    """
    return re.sub(QuizConstants.TITLE_NUMBER_PATTERN, rf"\g<1>{number}", title or "",
                  count=1, flags=re.IGNORECASE)


def _answer_draft(question, position: int):
    """The draft of a quiz answer slide: its question with the answer revealed.

    Args:
        question: The question slide's assembled draft.
        position: The answer slide's 1-based position in the deck.

    Returns:
        A copy of the question draft. With structured quiz data the copy only flips
        `quiz.reveal`; without it (a fallback question), the notes' answer line is
        added as a visible block so the answer slide still reveals something.
    """
    slide = question.model_copy(deep=True)
    slide.slide_number = position
    if slide.quiz is not None:
        slide.quiz.reveal = True
        return slide
    line = next((ln for ln in (slide.notes or "").splitlines()
                 if ln.strip().startswith(QuizConstants.NOTES_ANSWER_PREFIX)), "")
    if line:
        slide.blocks = list(slide.blocks) + [ContentBlock(slot="answer", text=line.strip())]
    return slide


def _deck_level_checks(slides: list, answer_slides: frozenset = frozenset()) -> list[str]:
    """Cross-slide faults a per-slide agent cannot see. Deterministic — no LLM.

    Args:
        slides: The assembled slide drafts.
        answer_slides: Numbers of quiz answer slides, which repeat their question's
            title by design.
    """
    if not slides:
        return []
    notes = []
    seen: dict[str, int] = {}
    for slide in slides:
        if slide.slide_number in answer_slides:
            continue
        # Normalized so near-duplicates ("A & B" vs "A and B") are caught too: a real
        # run shipped the same closing slide twice, spelled two ways.
        key = _title_key(slide.title)
        if key and key in seen:
            notes.append(f"Slides {seen[key]} and {slide.slide_number} duplicate the "
                         f"same topic ('{slide.title}').")
        seen[key] = slide.slide_number
    return notes


def _title_key(title: str) -> str:
    """Reduce a slide title to a comparable key.

    Args:
        title: The slide's title as written.

    Returns:
        Lowercase alphanumeric words joined by single spaces, with '&' read as 'and'.
    """
    text = (title or "").lower().replace("&", " and ")
    return " ".join("".join(c if c.isalnum() else " " for c in text).split())


def _generation_params(planned, prefs: DeckBrief, plan: DeckPlan) -> dict:
    """The PPT Creator Agent's generation parameters for one slide.

    Args:
        planned: The slide's plan entry, or None for a slide the plan does not know.
        prefs: The session brief, carrying the deck-level style levels.
        plan: The deck plan, carrying any user-described quiz/FAQ look.

    Returns:
        Keyword arguments for `SlideCodeBrief`; empty when the plan does not know the
        slide (the brief's defaults then apply).
    """
    if planned is None:
        return {}
    params = {
        "slide_type": planned.slide_type,
        "creativity_level": planned.creativity_level or prefs.effective_creativity(),
        "user_instructions": planned.user_instructions,
        "planner_special_instructions": planned.planner_special_instructions,
    }
    style = plan.family_styles.get(planned.slide_type)
    field = DeckDesignContext.field_for(planned.slide_type)
    if style and field:
        # The user's own description of the family's look; the designer applies it
        # once and then reuses the layout it established for every slide of the family.
        params["deck_design_context"] = DeckDesignContext(**{field: style})
    if planned.match_neighbours:
        # Only present when set, so every other slide's parameters are unchanged.
        params["match_neighbours"] = True
    return params


#: Where the retired detailing level sat in `_design_key`; its old default value.
_RETIRED_DETAILING_SLOT = "MED"


def _image_key(slide, planned) -> str:
    """The cache-key suffix for a slide's image candidates.

    Candidates are kept out of `design_json`, so a slide's content hash never sees
    them. This suffix makes a changed candidate set (or a new ask for images) re-design
    the slide.

    Args:
        slide: The slide's draft.
        planned: Its plan entry, or None.

    Returns:
        "" for a slide without candidates - so its cache key is exactly what it was
        before candidates existed - else a short stable suffix.
    """
    refs = [c.ref for c in slide.image_candidates]
    if not refs:
        return ""
    requested = bool(planned is not None and planned.images_requested)
    digest = hashlib.sha1("|".join(refs).encode("utf-8")).hexdigest()[:10]
    return f"|img:{digest}:{'req' if requested else 'opt'}"


def _design_key(params: dict, planned, enrichment=None) -> str:
    """The non-default generation parameters, as part of the slide-cache key.

    Args:
        params: From `_generation_params`.
        planned: The slide's plan entry, or None.

    Returns:
        "" when every parameter is at its default - so the slide's cache hash is
        exactly what it was before the parameters existed - else a stable string
        that changes whenever how the slide is designed changes.
    """
    if not params or planned is None:
        return skill_cache_suffix(enrichment)
    instructions = own_instructions(planned)
    context = params.get("deck_design_context")
    # The retired detailing level keeps its slot as a constant, so every key - and so
    # every cached slide - is exactly what it was before detailing was removed.
    parts = (params["slide_type"], params["creativity_level"], _RETIRED_DETAILING_SLOT,
             planned.quiz_role,
             hashlib.sha1(instructions.encode("utf-8")).hexdigest()[:10]
             if instructions else "",
             context.model_dump_json() if context is not None else "")
    defaults = (SlideTypes.DEFAULT, CreativityLevels.DEFAULT, _RETIRED_DETAILING_SLOT,
                "", "", "")
    user = params.get("user_instructions") or ""
    if user:
        # Appended only when present, so a slide without user asks keeps its old key.
        parts += ("user:" + hashlib.sha1(user.encode("utf-8")).hexdigest()[:10],)
    revision = getattr(planned, "design_revision", 0) or 0
    if revision > 0:
        # A consistency fix asked for a fresh design: a new key, so the cache misses
        # once. Appended only when set, so every other key is exactly what it was.
        parts += (f"rev:{revision}",)
    key = "" if parts == defaults else "|".join(parts)
    return key + skill_cache_suffix(enrichment)


def _quiz_pairs(plan: DeckPlan, slide_count: int) -> dict[int, int]:
    """Quiz question position -> answer slide position, for the slides being designed.

    Args:
        plan: The deck plan (numbered like the draft).
        slide_count: How many slides the draft has.

    Returns:
        One entry per question that is directly followed by its answer slide.
    """
    pairs = {}
    slides = plan.slides
    for current, following in zip(slides, slides[1:]):
        if (current.is_quiz_question and following.is_quiz_answer
                and current.quiz_id and current.quiz_id == following.quiz_id
                and following.slide_number <= slide_count):
            pairs[current.slide_number] = following.slide_number
    return pairs


def _design_slides(session_id: str, draft: DeckDraft, plan: DeckPlan, workdir: Path,
                   contract, enrichment=None, reference=None
                   ) -> tuple[list[tuple[int, Path]], dict[int, dict], list[str]]:
    """Get one .pptx per slide: generated where the content changed, cached where it did not.

    Args:
        session_id: Owning session, for events, caching and logs.
        draft: The assembled deck, already renumbered.
        plan: The deck plan, for classification and the per-slide design hint.
        workdir: Directory every slide file is written into.
        contract: The deck design contract, so every parallel worker builds to the
            same master, frame, grid and tokens.
        reference: The prepared reference design (`reference_design.Prepared`) when the
            deck copies an uploaded deck's design; the caller has already switched the
            template to it. Every slide gets the copy-the-design direction and a cache
            key of its own.

    Returns:
        Tuple of (slide files as (number, path) pairs, the new cache map, gap notes).
    """
    # Bound for the whole design pass, so staging, image download, stitching and the
    # roll-up all carry the build too - not just the lines inside `build_slide`.
    with log_context(build_id=build_id_of(workdir)):
        return _design_slides_in_scope(session_id, draft, plan, workdir, contract,
                                       enrichment, reference)


def _design_slides_in_scope(session_id: str, draft: DeckDraft, plan: DeckPlan,
                            workdir: Path, contract, enrichment=None, reference=None):
    """`_design_slides`' body, with the build's logging scope already bound."""
    classification = registry.normalize(plan.classification)
    cached = slide_cache.load(session_id)
    agent = PptCreatorAgent(session_id, workdir)
    plan_by_number = {s.slide_number: s for s in plan.slides}
    catalog = catalog_mod.load()
    log.info("designing %d slide(s) in %s (classification %s, %d cached entries)",
             len(draft.slides), workdir, classification, len(cached),
             extra={"session_id": session_id})

    reused, to_build = {}, []
    prefs = DeckBrief.load(session_id)
    design_keys: dict[int, str] = {}
    params_by_position: dict[int, dict] = {}
    for position, slide in enumerate(draft.slides, start=1):
        params = _generation_params(plan_by_number.get(position), prefs, plan)
        params = add_skill_guidance(params, enrichment)
        if reference is not None:
            params = reference_design.with_guidance(params)
        params_by_position[position] = params
        design_keys[position] = (_design_key(
            params, plan_by_number.get(position), enrichment)
            + _image_key(slide, plan_by_number.get(position))
            + (reference.cache_key if reference is not None else ""))
        digest = slide_cache.content_hash(slide, design_keys[position])
        entry = cached.get(position)
        # Reuse only on an exact content match: any copy change must be redrawn.
        if entry and entry.get("hash") == digest:
            local = workdir / f"cached_slide_{position}.pptx"
            if slide_cache.fetch(session_id, entry, local) is not None:
                reused[position] = (local, entry)
                continue
        to_build.append((position, slide))

    # A quiz question and its answer slide are one unit: the answer is produced from
    # the question's program, so when either half must be built, both are.
    pairs = _quiz_pairs(plan, len(draft.slides))
    building = {position for position, _ in to_build}
    for question_pos, answer_pos in pairs.items():
        if (question_pos in building) != (answer_pos in building):
            for position in (question_pos, answer_pos):
                if position in reused:
                    reused.pop(position)
                    to_build.append((position, draft.slides[position - 1]))
    to_build.sort(key=lambda item: item[0])
    answers_to_build = [item for item in to_build if item[0] in pairs.values()]
    to_build = [item for item in to_build if item[0] not in pairs.values()]

    _step(session_id, "deck", f"Designing {len(to_build) + len(answers_to_build)} "
                              f"slide(s); reusing {len(reused)} unchanged slide(s).")

    def build(item: tuple[int, object]):
        position, slide = item
        run_control.checkpoint()            # a stopped run designs no further slides
        # Bound here rather than only inside `build_slide`, so image materialization
        # and the contract lookup - both of which log, and both of which run before
        # the agent is called - are attributable to this slide too.
        with log_context(slide=position), agent_run(AgentNames.SLIDE_DESIGN) as span:
            span.set_work_units(1, WorkUnitType.SLIDE)
            return _build_one(position, slide)

    def _brief(position: int, slide) -> SlideCodeBrief:
        planned = plan_by_number.get(position)
        assignment = contract.assignments.get(position)
        layout = catalog.get(assignment.layout_key) if assignment else None
        # A slide the contract never reached still builds; it just builds without the
        # deck-wide steer, which is a coherence loss rather than a failure.
        if layout is None:
            log.warning("slide %d has no usable contract assignment (%s); building it "
                        "without the deck-wide master, frame, grid and tokens",
                        position,
                        f"layout {assignment.layout_key!r} not in the catalog"
                        if assignment else "no assignment",
                        extra={"session_id": session_id})
        contract_fields = (contract.brief_fields(position, layout)
                           if layout is not None else {})
        extra = {}
        if planned is not None and planned.match_neighbours:
            # The neighbours reused this pass are already on disk; ones being rebuilt
            # are not ready yet and are left out.
            extra["neighbour_refs"] = [str(reused[p][0]) for p in (position - 1,
                                                                   position + 1)
                                       if p in reused]
            log.info("slide %d matches its neighbours: %d reference slide(s)", position,
                     len(extra["neighbour_refs"]), extra={"session_id": session_id})
        requested = bool(planned is not None and planned.images_requested)
        image_notes: dict[str, str] = {}
        # Images live in blob; the generated program can only open local files. A missing
        # optional candidate is only logged; a requested or legacy image is reported.
        image_paths = materialize_images(
            slide, workdir / f"slide_{position}", session_id, notes=image_notes,
            report_missing=requested or not slide.image_candidates)
        return SlideCodeBrief(
            slide_number=position, kind=slide.kind, content=slide,
            context=_deck_context(plan, position) if planned else DeckContext(
                title=draft.title, position=position, total=len(draft.slides)),
            classification=classification,
            image_paths=image_paths, image_notes=image_notes,
            images_requested=requested and bool(image_paths),
            design_hint=slide.layout or (planned.visual if planned else ""),
            **contract_fields, **params_by_position.get(position, {}), **extra)

    def _build_one(position: int, slide):
        return position, slide, agent.build_slide(_brief(position, slide))

    workers = min(PptCreatorConstants.MAX_WORKERS, len(to_build) or 1)
    # Deck builds from different sessions share this pool, so a build's wall-clock
    # time only means something next to how many others were running. Counted here
    # rather than per slide, because the pool is what they contend for.
    with build_metrics.deck_build(session_id, len(to_build) + len(answers_to_build)):
        if workers > 1:
            # Slides share no state, so their code generation and execution run
            # concurrently.
            with ContextThreadPoolExecutor(max_workers=workers) as pool:
                built = list(pool.map(build, to_build))
        else:
            built = [build(item) for item in to_build]

        # Pass 2: answer slides, from their freshly built questions. No model call, so
        # nothing in pass 1 ever waits on them.
        questions = {position: result for position, _, result in built}
        answer_of = {answer: question for question, answer in pairs.items()}

        def build_answer(item: tuple[int, object]):
            position, slide = item
            run_control.checkpoint()
            with log_context(slide=position), agent_run(AgentNames.SLIDE_DESIGN) as span:
                span.set_work_units(1, WorkUnitType.SLIDE)
                question = questions.get(answer_of[position])
                if question is None:
                    log.warning("answer slide %d has no question built this turn; "
                                "building it as a regular slide", position,
                                extra={"session_id": session_id})
                    return _build_one(position, slide)
                return position, slide, agent.build_answer_slide(
                    _brief(position, slide), question)

        if answers_to_build:
            answer_workers = min(PptCreatorConstants.MAX_WORKERS, len(answers_to_build))
            with ContextThreadPoolExecutor(max_workers=answer_workers) as pool:
                built += list(pool.map(build_answer, answers_to_build))

    artifacts: dict[int, dict] = {}
    files: list[tuple[int, Path]] = []
    gaps: list[str] = []

    for position, (local, entry) in reused.items():
        files.append((position, local))
        artifacts[position] = entry

    for position, slide, result in built:
        path = Path(result.pptx_path)
        files.append((position, path))
        # One line per slide, immediately before it is persisted, saying exactly what
        # is shipping. Reading a log and still not knowing what went into the deck is
        # how a blank slide reached a customer unnoticed.
        log.info("slide %d FINAL: tier %d on %s, %d design attempt(s), %d "
                 "generation(s), gate %s (%d blocking, %d advisory), fallback=%s -> %s",
                 position, result.tier, result.layout_used or "(unassigned)",
                 result.attempts, result.generations,
                 "passed" if result.gate_passed else "NOT PASSED",
                 result.blocking_violations, result.advisory_notes,
                 result.fallback_used, path.name,
                 extra={"session_id": session_id})

        # The fail-safe. Every path out of the ladder either clears the gate or is the
        # deterministic render; anything else escaped without ever being cleared, which
        # is a bug in the ladder rather than a slide. Such an artifact still ships -
        # dropping it would lose the content - but it is never cached, so the next turn
        # rebuilds it instead of reusing something nothing ever approved.
        if not (result.gate_passed or result.fallback_used):
            log.error("slide %d reached the cache without passing the gate and "
                      "without being a deterministic render (tier %d, %d blocking "
                      "violation(s)). Shipping it this turn but NOT caching it, so "
                      "the next turn rebuilds it. This is a ladder bug.",
                      position, result.tier, result.blocking_violations,
                      extra={"session_id": session_id})
            gaps.append(f"Slide {position} ('{slide.title}'): shipped with "
                        f"{result.blocking_violations} unresolved layout issue(s).")
            continue

        artifacts[position] = slide_cache.put(session_id, position, slide, path,
                                              fallback=result.fallback_used,
                                              design_key=design_keys.get(position, ""))
        if result.fallback_used:
            gaps.append(f"Slide {position} ('{slide.title}'): the designer could not "
                        f"produce working code; a plain slide was used.")

    # Slides removed this turn leave their blobs behind; drop them with the same map.
    slide_cache.evict(session_id, cached, keep=set(artifacts))
    log.info("slide design finished: %d file(s) - %d built, %d reused, %d fell back "
             "to a template render", len(files), len(built), len(reused), len(gaps),
             extra={"session_id": session_id})

    # One roll-up for the whole design pass. The per-slide events say what is
    # happening; this says how it went, which is the thing a user actually reads.
    if built:
        first_try = sum(1 for _, _, r in built if r.tier == 1 and r.attempts == 1)
        repaired = sum(1 for _, _, r in built if r.tier == 1 and r.attempts > 1)
        fitted = sum(1 for _, _, r in built if r.tier == 2)
        fell_back = sum(1 for _, _, r in built if r.fallback_used)
        parts = [f"{first_try} first try"]
        if repaired:
            parts.append(f"{repaired} after redesign")
        if fitted:
            parts.append(f"{fitted} fitted onto a template layout")
        if fell_back:
            parts.append(f"{fell_back} rendered from a template layout")
        reviewed = [r for _, _, r in built if r.max_mode]
        if reviewed:
            approved = sum(1 for r in reviewed if r.visual_passed)
            parts.append(f"{len(reviewed)} reviewed visually ({approved} passed)")
            for _, _, r in built:
                if r.max_mode:
                    log.info("slide %d MAX MODE: %d review(s), score %s, %s, on %s",
                             r.slide_number, r.visual_reviews, r.visual_score,
                             "approved" if r.visual_passed else "best available",
                             r.layout_used, extra={"session_id": session_id})
        generations = sum(r.generations for _, _, r in built)
        upstream = sum(r.upstream_failures for _, _, r in built)
        log.info("design tiers: %d at tier 1 first try, %d at tier 1 after repair, "
                 "%d at tier 2, %d at tier 3 | %d gateway generation(s) for %d "
                 "slide(s) (%.1f per slide), %d of them returning nothing",
                 first_try, repaired, fitted, fell_back, generations, len(built),
                 generations / len(built), upstream,
                 extra={"session_id": session_id})
        if upstream:
            log.warning("%d of %d generation(s) this pass came back empty; that is an "
                        "upstream problem, not a design one", upstream, generations,
                        extra={"session_id": session_id})
        # Which layouts keep failing, and on which rules. A layout failing nearly
        # every gate is a constraint bug in that layout's definition or its prompt,
        # not forty independent slides having bad luck - and that distinction is
        # invisible without this line.
        report = agent.gate_report()
        if report:
            log.info("gate failures by layout: %s", report,
                     extra={"session_id": session_id})
        _step(session_id, "deck",
              f"Designed {len(built)} slide(s): {', '.join(parts)}.")
    return files, artifacts, gaps


def _collect(state: WorkflowState) -> dict:
    """Assemble the finished slides, design each one, and stitch them into a deck."""
    sid = state["session_id"]
    plan_dict, _ = session_store.get_plan(sid)
    if not plan_dict:
        return {"pending_publish": False}
    plan = DeckPlan(**plan_dict)

    results = {r["slide_number"]: SlideResult(**r)
               for r in (state.get("slide_results") or [])}
    stored_draft = session_store.get_draft(sid)
    old_draft = DeckDraft(**stored_draft) if stored_draft else None
    old_slides = {s.slide_number: s for s in (old_draft.slides if old_draft else [])}
    stored_content = session_store.get_content(sid)
    old_chunks = {s.slide_number: s.chunks for s in
                  (ContentBundle(**stored_content).slides if stored_content else [])}

    slides, contents, gaps = [], [], []
    # Quiz counters and FAQ numbering are deck-level facts, so they are set here, in
    # code, after every slide's content is known.
    questions_total = sum(1 for s in plan.slides if s.is_quiz_question)
    question_no, faq_next = 0, 1
    question_drafts: dict[str, object] = {}
    answer_numbers: set[int] = set()
    for position, planned in enumerate(plan.slides, start=1):
        result = results.get(planned.slide_number)
        if planned.is_quiz_answer:
            # An answer slide is its question's draft with the correct choice revealed.
            question = question_drafts.get(planned.quiz_id)
            if question is None:
                log.warning("collect: answer slide %d has no built question (quiz %s); "
                            "skipping it", planned.slide_number, planned.quiz_id,
                            extra={"session_id": sid})
                continue
            slide = _answer_draft(question, position)
            slides.append(slide)
            answer_numbers.add(position)
            contents.append(SlideContent(slide_number=position, title=planned.title,
                                         coverage="good"))
            continue
        source = result.slide_draft if result else old_slides.get(planned.slide_number)
        if source is None:
            log.warning("collect: slide %d has neither a fresh nor a stored draft",
                        planned.slide_number, extra={"session_id": sid})
            continue

        # Renumber contiguously so an insertion or removal leaves no gap.
        slide = source.model_copy(deep=True)
        slide.slide_number = position
        if planned.is_quiz_question:
            question_no += 1
            # Titles are written per slide and keep whatever number they were planned
            # with; renumber them by position so the deck always reads Quiz 1, 2, 3...
            slide.title = _renumber_quiz_title(slide.title, question_no)
            if slide.quiz is not None:
                slide.quiz.counter = f"Question {question_no} of {questions_total}"
                slide.quiz.reveal = False
            question_drafts[planned.quiz_id] = slide
        if slide.faq:
            slide.faq_start = faq_next
            faq_next += len(slide.faq)
        slides.append(slide)

        contents.append(SlideContent(
            slide_number=position, title=planned.title,
            chunks=list(result.chunks if result
                        else old_chunks.get(planned.slide_number, [])),
            coverage=result.coverage if result else "good",
            note=result.notes if result else ""))
        if result and result.notes:
            gaps.append(f"Slide {position} ('{planned.title}'): {result.notes}")

    if not slides:
        log.warning("collect produced no slides", extra={"session_id": sid})
        return {"pending_publish": False}

    gaps += _deck_level_checks(slides, frozenset(answer_numbers))

    classification = registry.normalize(plan.classification)
    draft = DeckDraft(template=classification, title=plan.title,
                      subtitle=plan.objective,
                      slides=slides)

    session_store.save_draft(sid, draft.model_dump(mode="json"))

    # Design and stitch. The work directory holds the generated programs and slide files
    # and must outlive this node, so `_publish` (or the failure path below) removes it.
    workdir = Path(tempfile.mkdtemp(prefix=PptCreatorConstants.WORKDIR_PREFIX))
    try:
        enrichment = prepare_active_skill(session_store.get_active_skill(sid))
        if enrichment:
            gaps.append("The selected style skill is guidance only; current brand and "
                        "readability rules remain fixed, and visual fidelity to the "
                        "reference has not been evaluated.")
        # Reference design mode: the deck copies the design of a PowerPoint the user
        # uploaded. Inside `using_template` the contract, every slide program, the
        # sandbox and the stitcher read that deck instead of the brand template.
        reference, reference_note = reference_design.prepare(
            sid, getattr(DeckBrief.load(sid), "design_reference", None) or {})
        if reference_note:
            gaps.append(reference_note)
            _step(sid, "deck", reference_note)
        if reference is not None:
            _step(sid, "deck", f"Copying the design of {reference.filename}.")
            gaps.append(f"Design copied from {reference.filename} (layouts, colours and "
                        "fonts only); the Rockwell classification label and mark stay "
                        "on every slide.")
        with registry.using_template(reference.path if reference else None):
            # One contract for the whole deck, before any slide is built: this is what
            # makes four parallel workers produce slides that read as one deck rather
            # than four, and it is what the skill deck agent builds to as well. Inside the
            # try because it opens the template, and a template that cannot be read
            # should take the same "content saved, nothing published" path as any other
            # assembly failure.
            contract = design_contract.build(
                plan, draft, session_id=sid,
                skill_guidance="\n\n".join(part for part in (
                    ReferenceDesignConstants.GUIDANCE if reference else "",
                    enrichment["guidance"] if enrichment else "") if part))
            canvas = sum(1 for a in contract.assignments.values() if a.mode == "canvas")
            log.info("deck design contract ready: master M%d, %d slide(s), %d on canvas",
                     contract.master_index, len(contract.assignments), canvas,
                     extra={"session_id": sid})
            _step(sid, "deck", f"Design contract: master M{contract.master_index}, "
                               f"{canvas} slide(s) composed on canvas.")
            if skill_deck.enabled():
                # DECK_ENGINE=skill_deck: ONE generated program designs the whole deck on
                # the official template, to the same contract. The session's style skill,
                # when one is active, goes to the model as one more skill and its pinned
                # reference profiles become the style target the agent checks against.
                references = skill_deck.active_reference_profiles(
                    session_store.get_active_skill(sid),
                    session_store.get_skill_references(sid)) if enrichment else []
                _step(sid, "deck", "Building the deck with the skill deck agent.")
                with agent_run(AgentNames.SKILL_DECK):
                    deck_path, engine_notes = skill_deck.build_deck(
                        sid, draft, plan, workdir, classification, contract,
                        creativity=DeckBrief.load(sid).effective_creativity(),
                        style_guidance=enrichment["guidance"] if enrichment else "",
                        references=references,
                        image_paths=_deck_images(sid, draft, plan, workdir),
                        on_step=lambda message: _step(sid, "deck", message))
                gaps += engine_notes
            else:
                slide_files, artifacts, design_gaps = _design_slides(
                    sid, draft, plan, workdir, contract, enrichment, reference)
                gaps += design_gaps
                stitcher = DeckStitcher(sid)
                deck_path = stitcher.stitch(slide_files, workdir / "deck.pptx")
                gaps += stitcher.notes
                slide_cache.save(sid, artifacts)
        # Polish recolours and restyles towards the Rockwell brand, which would undo a
        # copied design, so it never runs on one.
        if deck_polish.enabled() and reference is None:
            deck_path, polish_notes = _polish(sid, Path(deck_path), workdir, enrichment)
            gaps += polish_notes
    except Exception as exc:                                 # noqa: BLE001
        # Nothing to publish, but the content of this turn is already saved, so the next
        # turn can retry the design without re-running retrieval.
        log.exception("deck design/stitching failed", extra={"session_id": sid})
        shutil.rmtree(workdir, ignore_errors=True)
        _step(sid, "deck", f"The deck could not be assembled ({exc}).")
        session_store.save_content(sid, ContentBundle(
            plan=plan.model_dump(), slides=contents,
            gaps=gaps + [f"deck assembly failed: {exc}"]).model_dump(mode="json"))
        return {"pending_publish": False, "deck_gaps": gaps}

    session_store.save_content(sid, ContentBundle(
        plan=plan.model_dump(), slides=contents, gaps=gaps,
        rounds_used=sum(r.rounds_used for r in results.values())).model_dump(mode="json"))

    _step(sid, "deck", f"Assembled {len(slides)} slide(s)"
          + (f"; {len(gaps)} note(s)." if gaps else "."))
    return {"pending_publish": True, "deck_gaps": gaps,
            "deck_path": str(deck_path), "deck_workdir": str(workdir)}


def _deck_images(sid: str, draft: DeckDraft, plan: DeckPlan, workdir: Path) -> dict:
    """Local copies of every slide's images, for the skill deck agent's program."""
    planned = {s.slide_number: s for s in plan.slides}
    images = {}
    for position, slide in enumerate(draft.slides, start=1):
        requested = bool(getattr(planned.get(position), "images_requested", False))
        paths = materialize_images(slide, workdir / f"slide_{position}", sid,
                                   report_missing=requested or not slide.image_candidates)
        if paths:
            images[position] = paths
    return images


def _polish(sid: str, deck_path: Path, workdir: Path, enrichment) -> tuple[Path, list[str]]:
    """DECK_POLISH: review the finished deck and polish it at the deck's creativity
    level. A failure never costs the deck: it is published as built."""
    level = deck_polish.level_for(DeckBrief.load(sid).effective_creativity())
    try:
        with agent_run(AgentNames.DECK_POLISH):
            return deck_polish.polish_deck(
                deck_path, workdir, level,
                style_guidance=enrichment["guidance"] if enrichment else "",
                on_step=lambda message: _step(sid, "deck", message))
    except Exception:                                        # noqa: BLE001
        log.exception("deck polish failed; publishing the deck as built",
                      extra={"session_id": sid})
        _step(sid, "deck", "Deck polish failed; the deck is published as built.")
        return deck_path, []


def _publish(state: WorkflowState) -> dict:
    """Persist the stitched deck as a new numbered version exactly once per user turn."""
    sid = state["session_id"]
    draft = session_store.get_draft(sid)
    content = session_store.get_content(sid)
    deck_path = state.get("deck_path")
    workdir = state.get("deck_workdir")
    if not (draft and content and deck_path):
        # Reachable only if pending_publish was set but the draft/content/file vanished
        # between collect and here (e.g. a concurrent turn) — should be rare, so log it.
        log.warning("publish skipped: missing draft/content/deck file despite pending_publish",
                    extra={"session_id": sid, "has_draft": bool(draft),
                           "has_content": bool(content), "has_deck": bool(deck_path)})
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)
        return {}
    try:
        plan_dict, _ = session_store.get_plan(sid)
        bundle = ContentBundle(**{**content, "plan": plan_dict or content.get("plan")})
        artifact = persist_deck(sid, DeckDraft(**draft), bundle, Path(deck_path))
    finally:
        # The generated programs and slide files exist only for this turn.
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)
    return {"deck_version": artifact.deck_version}


# --------------------------------------------------------------------------- #
# Routing                                                                      #
# --------------------------------------------------------------------------- #

def _after_triage(state: WorkflowState) -> str:
    route = state.get("route", "chat")
    if (route == "chat" and state.get("classification_changed")
            and session_store.list_decks(state["session_id"])):
        # Re-render the existing deck on the new template; no content changes.
        return "slides"
    return route


def _after_restyle(state: WorkflowState) -> str:
    """A restyled proposal ends the turn; a restyled built deck is rebuilt."""
    return "done" if state.get("action") == "await_approval" else "slides"


def _after_planning(state: WorkflowState) -> str:
    """Route on what the planning agent actually did this turn, not a pre-guessed intent:
    nothing mutated -> it was a clarifying question; a proposed plan waits for approval;
    a pure removal already trimmed the
    stored draft in place, so it only needs republishing; anything else needs slides
    built."""
    if state.get("action") == "await_approval":
        return "await_approval"
    actions = set(state.get("actions_taken") or [])
    sid = state["session_id"]

    _step(sid, "planning", f"actions={', '.join(actions)}")
    if not actions:
        return "clarify"
    if actions <= {"remove"}:
        # The removal tool already updates plan/draft; the work order refreshes the
        # agenda and takeaways when a content slide was removed, so the deck is rebuilt.
        return "slides" if session_store.get_draft(sid) else "done"
    return "slides"


def _after_collect(state: WorkflowState) -> str:
    return "publish" if state.get("pending_publish") else "done"


def build_graph():
    g = StateGraph(WorkflowState)
    # Every node runs through `stage_node`: stage-scoped logs, run-state bookkeeping, a
    # stop/lease check before work starts, and bounded recovery (see recovery.py).
    g.add_node("triage", stage_node(RunStages.TRIAGE, _triage))
    g.add_node("extraction", stage_node(RunStages.EXTRACTION, _extraction))
    g.add_node("planning", stage_node(RunStages.PLANNING, _planning))
    g.add_node("approve", stage_node(RunStages.PLANNING, _approve))
    g.add_node("restyle", stage_node(RunStages.PLANNING, _restyle))
    g.add_node("slides", stage_node(RunStages.SLIDES, _slides))
    g.add_node("collect", stage_node(RunStages.COLLECT, _collect))
    g.add_node("publish", stage_node(RunStages.PUBLISH, _publish))

    g.set_entry_point("triage")
    g.add_conditional_edges("triage", _after_triage,
                            {"chat": END, "clarify": END, "extraction": "extraction",
                             "approve": "approve", "restyle": "restyle",
                             "slides": "slides"})
    g.add_edge("approve", "slides")
    g.add_conditional_edges("restyle", _after_restyle, {"done": END, "slides": "slides"})
    g.add_edge("extraction", "planning")

    g.add_conditional_edges("planning", _after_planning,
                            {"clarify": END, "await_approval": END, "slides": "slides",
                             "collect": "collect", "done": END})
    g.add_edge("slides", "collect")
    g.add_conditional_edges("collect", _after_collect,
                            {"publish": "publish", "done": END})
    g.add_edge("publish", END)
    return g.compile()


_graph = None


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def _compose_reply(session_id: str, final: dict, decks: list) -> str:
    """Rebuild the chat reply from the end state. Using triage/planning's guess meant the
    user saw a reply written before the slides had even been built."""

    if (final.get("action") == "clarify" or not final.get("actions_taken")
            or not final.get("deck_version") or not decks):
        return final.get("reply", "")

    draft = session_store.get_draft(session_id) or {}
    slides = draft.get("slides", [])
    title = draft.get("title") or "the deck"
    theme = draft.get("theme", "")

    outline = []
    for slide in slides:
        lines = [f"Slide {slide.get('slide_number')}: {slide.get('title', '')}"]
        lines += [f"  - {point}" for point in slide.get("bullets", [])]
        if slide.get("image_path"):
            lines.append("  (includes a source keyframe)")
        outline.append("\n".join(lines))

    removed = final.get("removed_slides")
    segments = [
        f'Your training deck "{title}" is ready — v{decks[-1].deck_version}'
        f'{f", {theme} theme" if theme else ""}, {len(slides)} slide(s).',
        "Slide text:\n" + "\n\n".join(outline) if outline else "",
        f"Removed slide(s) {', '.join(map(str, removed))}; the rest were renumbered."
        if removed else ""]
    # The reply is chat, so all of it follows the user's language; the deck file stays English.
    present = [s for s in segments if s]
    translated = iter(_reply_language.translate(present, final.get("user_language")))
    return "\n\n".join(next(translated) for s in segments if s)


def _workflow_run_meta(session_id: str) -> tuple[int | None, int | None]:
    """Best-effort owner and 1-based turn index for a workflow-run row.

    Reads the owning user and counts prior analytics workflow runs for the session.
    Any failure yields ``(None, None)`` — telemetry metadata must never block a turn.

    Args:
        session_id: The session/project the turn belongs to.

    Returns:
        A ``(user_id, turn_index)`` pair; either element may be ``None``.
    """
    from app.config.constants import AnalyticsStatus, PgTables
    from app.utilities.pgsql_util import PgsqlUtil
    try:
        pg = PgsqlUtil()
        owner = pg.query(
            f"SELECT user_id FROM {PgTables.USER_SESSIONS} WHERE session_id = :s LIMIT 1",
            {"s": session_id})
        user_id = owner[0]["user_id"] if owner else None
        # Stopped runs are usage only, not turns, so they never consume a turn number.
        prior = pg.query(
            f"""SELECT COUNT(*) AS n FROM {PgTables.ANALYTICS_WORKFLOW_RUNS}
                WHERE session_id = :s AND status <> :cancelled""",
            {"s": session_id, "cancelled": AnalyticsStatus.CANCELLED})
        turn_index = (prior[0]["n"] or 0) + 1 if prior else None
        return user_id, turn_index
    except Exception as exc:                          # noqa: BLE001
        log.warning("could not resolve workflow run meta for %s: %s", session_id, exc)
        return None, None


def _record_workflow_outputs(span, final: dict) -> None:
    """Copy delivered slide counts, deck version and intent from the end state onto span.

    Args:
        span: The active :class:`WorkflowRunHandle`.
        final: The graph's final state.

    Returns:
        None.
    """
    span.deck_version_id = final.get("deck_version")
    actions = final.get("actions_taken") or []
    span.intent = ",".join(sorted(set(actions))) if actions else "chat"
    # Attribute each work-order slide to created vs updated by its operation.
    for task in (final.get("work_order") or []):
        op = task.get("op") if isinstance(task, dict) else None
        if op == SlideAgentConstants.OP_CREATE:
            span.slides_created += 1
        elif op in (SlideAgentConstants.OP_REWRITE, SlideAgentConstants.OP_RELAYOUT,
                    SlideAgentConstants.OP_ENRICH):
            span.slides_updated += 1
    span.slides_translated = len(final.get("translated_slides") or [])


def run_workflow(session_id: str, message: str, refresh_content: bool = False,
                 run_id: str | None = None, answers: dict | None = None,
                 answers_only: bool = False) -> dict:
    """Synchronous: the graph is CPU/IO blocking, so callers run it in a worker thread.

    Args:
        session_id: Session the turn runs for.
        message: The user's request.
        refresh_content: Whether retrieval content must be refreshed.
        run_id: The operational run id, reused as the analytics workflow-run id so
            the two tables join. Defaults to the run bound on the log context.
        answers: Validated structured answers (`{question id: value}`) sent with the
            turn, applied to the brief before anything else.
        answers_only: True when the turn carried answers and no typed message, so
            triage needs no model call.
    """
    run_id = run_id or run_id_ctx.get() or None
    # Bound once, here, at the outermost edge of the turn: every node, every worker
    # thread the nodes fan out to, and every library call underneath them inherits it.
    # Without this, concurrent decks write interleaved log lines nothing can separate.
    user_id, turn_index = _workflow_run_meta(session_id)
    with log_context(session_id=session_id, run_id=run_id), \
            workflow_run(session_id, user_id=user_id, turn_index=turn_index,
                         run_id=run_id) as span:
        log.info("workflow starting for %r (refresh_content=%s, %d answer(s))",
                 message[:120], refresh_content, len(answers or {}))
        before = session_store.run_snapshot(session_id)
        try:
            final = get_graph().invoke(
                {"session_id": session_id, "message": message,
                 "refresh_content": refresh_content, "answers": dict(answers or {}),
                 "answers_only": answers_only},
                config={"recursion_limit": 50})
        except (GuardrailViolation, GuardrailCheckError) as exc:
            # A later tool/excerpt rejection must undo earlier edits in the same turn.
            session_store.restore_snapshot(session_id, before)
            if isinstance(exc, GuardrailCheckError):
                raise
            warning = report_violation(session_id, exc)
            reply = _reply_language.translate(
                [exc.detail], DeckBrief.load(session_id).chat_language)[0]
            final = {"session_id": session_id, "message": message, "action": "blocked",
                     "reply": reply, "guardrail_flags": [warning],
                     "actions_taken": [], "work_order": [], "slide_results": []}
        # Record what the turn delivered before the span closes and enqueues its row.
        _record_workflow_outputs(span, final)
        return _summarize_turn(session_id, final)


def _summarize_turn(session_id: str, final: dict) -> dict:
    """Shape the graph's end state into the turn's API response.

    Args:
        session_id: The session the turn ran for.
        final: The graph's final state.

    Returns:
        The response body `run_workflow` hands back to its caller.
    """

    plan, status = session_store.get_plan(session_id)
    decks = session_store.list_decks(session_id)
    content = session_store.get_content(session_id)
    draft = session_store.get_draft(session_id) or {}
    actions = final.get("actions_taken") or []
    results = final.get("slide_results") or []
    work_order = [SlideTask(**t) for t in (final.get("work_order") or [])]
    questions = final.get("questions") or []
    return {
        "session_id": session_id,
        "action": final.get("action", "plan"),
        "intent": ",".join(sorted(set(actions))) if actions else "chat",
        "target_slides": final.get("target_slides", []),
        "removed_slides": final.get("removed_slides", []),
        "reply": _compose_reply(session_id, final, decks),
        "plan_status": status,
        "awaiting_approval": status == PlanStatus.DRAFT,
        # Structured questions this turn asked; the UI renders them (a slider for the
        # creativity_level id) and answers through /invoke.
        "questions": questions,
        "awaiting_answers": bool(questions),
        "guardrail_flags": final.get("guardrail_flags") or [],
        # The session's effective style, so the UI can initialise its sliders.
        "style": DeckBrief.load(session_id).style(),
        "deck_version": decks[-1].deck_version if decks else None,
        # True only when THIS turn built/updated the deck — deck_version above is the
        # latest regardless of turn, so callers must not use it to infer "just built".
        "deck_updated": bool(final.get("deck_version")),
        # Every slide settled before collect ran, so a published deck is always approved.
        "verdict": "approve" if results else None,
        "iterations_used": sum(r.get("rounds_used", 0) for r in results),
        "slides_built": len(results),
        "gaps": (content or {}).get("gaps", []),
        "user_message": final.get("message", ""),
        "actions_taken": actions,
        "classification": (plan or {}).get("classification"),
        "classification_changed": final.get("classification_changed") or None,
        "enrich_slides": final.get("enrich_slides") or [],
        "deck_title": draft.get("title") or (plan or {}).get("title", ""),
        "slide_count": len(draft.get("slides", [])),
        "slides_kept": sum(1 for t in work_order if t.op == SlideAgentConstants.OP_KEEP),
        "operations": {str(t.slide_number): t.op for t in work_order},
        "outline": [{"slide_number": s.get("slide_number"), "kind": s.get("kind"),
                     "title": s.get("title", "")} for s in draft.get("slides", [])],
        "coverage": {str(s.get("slide_number")): s.get("coverage")
                     for s in (content or {}).get("slides", [])},
    }

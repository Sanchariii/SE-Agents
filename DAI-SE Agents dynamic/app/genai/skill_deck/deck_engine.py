"""Skill deck agent inside the app: turn the deck the workflow already wrote into a PPT.

The deck workflow keeps doing triage, extraction, planning, approval, slide writing and
the deck design contract. When ``DECK_ENGINE=skill_deck``, its collect stage calls
:func:`build_deck` instead of the per-slide designer and stitcher: ONE program designs
every slide on the official template with DeckBuilder, guided by the app's ppt skill,
our whole-deck skill and the session's style skill (if any). The result is published as
a normal deck version, so preview, download, versions and feedback work unchanged.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.config.constants import DeckEngines, SkillDeckConstants as C
from app.config.settings import get_settings
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

#: SlideDraft fields the program does not need: the layout comes from the design
#: contract and images are passed as local files (``image_paths``).
_DROP_FIELDS = {"layout", "image_candidates"}


def enabled() -> bool:
    """True when this deployment builds decks with the skill deck agent.

    Returns:
        Whether the DECK_ENGINE setting selects the skills engine.
    """
    engine = (get_settings().deck_engine or DeckEngines.CLASSIC).strip().lower()
    if engine not in DeckEngines.ALL:
        log.warning("unknown DECK_ENGINE %r; using %r", engine, DeckEngines.CLASSIC)
        return False
    return engine == DeckEngines.SKILL_DECK


def _prune(value):
    """Drop empty values so the spec sent to the model stays small."""
    if isinstance(value, dict):
        out = {k: _prune(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v not in ("", [], {}, None)}
    if isinstance(value, list):
        return [p for p in (_prune(v) for v in value) if p not in ("", [], {}, None)]
    return value


def deck_spec(draft, plan) -> dict:
    """The written deck as compact JSON for the model.

    Args:
        draft: The assembled DeckDraft (slides already renumbered).
        plan: The DeckPlan, for audience, objective and tone.

    Returns:
        A dict with deck-level fields and one entry per slide.
    """
    slides = []
    for slide in draft.slides:
        data = slide.model_dump(mode="json", exclude=_DROP_FIELDS)
        for block in data.get("blocks") or []:
            block.pop("image_ref", None)
        slides.append(_prune(data))
    return _prune({"title": draft.title, "subtitle": draft.subtitle,
                   "audience": getattr(plan, "audience", ""),
                   "objective": getattr(plan, "objective", ""),
                   "tone": getattr(plan, "tone", ""),
                   "takeaways": draft.takeaways, "slides": slides})


def active_reference_profiles(skill: dict | None, stored: dict) -> list[dict]:
    """The inspected profiles of the references pinned to the session's active skill.

    Args:
        skill: ``session_store.get_active_skill(...)`` (None when no skill is active).
        stored: ``session_store.get_skill_references(...)``.

    Returns:
        Profiles whose digest still matches the pin; a changed or missing reference is
        skipped rather than trusted.
    """
    profiles = []
    for reference in (skill or {}).get("references") or []:
        profile = (stored.get(reference.get("reference_id")) or {}).get("profile")
        if profile and profile.get("sha256") == reference.get("sha256"):
            profiles.append(profile)
    return profiles


def build_deck(session_id: str, draft, plan, workdir: Path, classification: str,
               contract, catalog=None, creativity: str = "MED", style_guidance: str = "",
               references: list[dict] | None = None, image_paths: dict | None = None,
               writer=None, on_step=None) -> tuple[Path, list[str]]:
    """Build the whole deck with the skill deck agent.

    Args:
        session_id: Owning session, for logs.
        draft: The assembled DeckDraft.
        plan: The DeckPlan.
        workdir: The turn's work directory; the agent runs in a sub-folder of it.
        classification: Normalised classification (each slide's frame carries it).
        contract: The deck design contract (``design_contract.build``) - the same
            master, layouts, frame, grid and tokens the classic engine would use.
        catalog: The template catalog; loaded when not given.
        creativity: The deck's creativity level (LOW, MED or HIGH).
        style_guidance: The session's active style skill ("" when none is active).
        references: Inspected profiles of that skill's references
            (:func:`active_reference_profiles`); the deck is checked against them.
        image_paths: Local image files per slide number.
        writer: Program writer; defaults to the gateway model (tests pass a fake).
        on_step: Callback for progress lines shown in the chat.

    Returns:
        Tuple of (deck path, notes: remaining problems and style differences).

    Raises:
        RuntimeError: When no deck could be built; the workflow reports it and keeps
            the turn's content for a retry.
    """
    from app.genai.skill_deck import prompts, staging, style_match
    from app.genai.skill_deck.graph import build_graph
    from app.genai.skill_deck.llm import GatewayProgramWriter

    if catalog is None:
        from app.genai.ppt.template import catalog as catalog_mod
        catalog = catalog_mod.load()
    spec = deck_spec(draft, plan)
    slides = staging.slide_specs(draft, contract, catalog, image_paths)
    run_dir = Path(workdir) / C.RUN_DIRNAME
    staging.stage(run_dir, catalog, slides, contract)
    target = style_match.style_target(references or [])
    if references and not target:
        log.info("style references carry no slide geometry; style check skipped")
    system, names = prompts.system_prompt(creativity, style_guidance,
                                          style_match.describe(target))
    message = prompts.user_message(spec, slides, catalog, classification, creativity)
    log.info("skill deck agent: %d slide(s) for session %s, skills %s, creativity %s",
             len(slides), session_id, names, creativity, extra={"session_id": session_id})
    if on_step:
        on_step(f"Skill deck agent: passing skills {', '.join(names)}.")
    state = build_graph(writer or GatewayProgramWriter(), on_step).invoke({
        "system": system, "messages": [{"role": "user", "content": message}],
        "run_dir": str(run_dir), "output_path": str(Path(workdir) / "deck.pptx"),
        "slide_count": len(slides), "style_target": target, "spec": spec,
        "attempts": 0, "layout_rounds": 0, "warnings": [], "good_deck": ""})
    if state.get("status") != "done":
        raise RuntimeError(f"skill deck agent gave up after {state.get('attempts')} "
                           f"attempt(s): {state.get('error')}")
    return Path(state["final_deck"]), list(state.get("warnings") or [])

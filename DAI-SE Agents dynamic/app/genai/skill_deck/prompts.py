"""What the model receives.

System prompt, in this order (later never overrides earlier):
    1. the app's ppt skill (app/genai/ppt/skills/rockwell-skill.md) - the same guide
       the classic per-slide designer gets;
    2. the shared design principles and priority rule (app/genai/agents);
    3. our whole-deck skill (skills/whole-deck/SKILL.md);
    4. the deck's creativity level, with the classic designer's own wording;
    5. the session's style skill from the dynamic skills feature, if one is active.

User message: the deck, the layouts its slides were assigned (described exactly as the
classic designer's prompt describes them), and every slide's assignment and content.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.config.constants import (CreativityLevels, PptCreatorConstants,
                                  SkillDeckConstants as C)
from app.genai.skill_deck import skills, staging
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

PPT_SKILL = (Path(__file__).resolve().parents[1] / "ppt" / PptCreatorConstants.SKILL_DIR
             / PptCreatorConstants.SKILL_MD_FILENAME)
_LEVEL_NAMES = {"LOW": "Low", "MED": "Medium", "HIGH": "High"}


def ppt_skill() -> str:
    """The app's ppt skill, as the classic designer reads it."""
    try:
        return PPT_SKILL.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"the app's ppt skill is missing at {PPT_SKILL}") from exc


def creativity_guidance(level: str) -> str:
    """The classic designer's own words for this creativity level ("" for MED)."""
    from app.genai.agents.ppt_creator_agent import CREATIVITY_GUIDANCE
    return CREATIVITY_GUIDANCE.get(level, "")


def system_prompt(creativity: str, style_guidance: str = "", style_target: str = "",
                  skill_names=C.DEFAULT_SKILLS) -> tuple[str, list[str]]:
    """The system prompt and the names of the skills in it, in order."""
    from app.genai.agents.design_principles import DESIGNER_PRINCIPLES, PRIORITY_RULE
    chosen = skills.select(list(skill_names))
    style = skills.style_skill("\n\n".join(p for p in (style_guidance, style_target) if p))
    if style:
        chosen.append(style)
    parts = [ppt_skill(), DESIGNER_PRINCIPLES.strip()]
    parts += [skills.render(skill) for skill in chosen if skill.name != C.STYLE_SKILL_NAME]
    guidance = creativity_guidance(creativity)
    if guidance:
        parts.append(guidance.strip())
    if style:
        parts.append(skills.render(style))
    parts.append(PRIORITY_RULE.strip())
    names = [C.PPT_SKILL_NAME] + [s.name for s in chosen]
    return "\n\n".join(parts), names


def user_message(spec: dict, slides: list[dict], catalog, classification: str,
                 creativity: str) -> str:
    """The brief: deck facts, the assigned layouts, then every slide."""
    by_number = {s.get("slide_number", i): s for i, s in enumerate(spec.get("slides", []), 1)}
    layouts = {}
    for slide in slides:
        if slide["layout_key"] not in layouts:
            layouts[slide["layout_key"]] = staging.layout_info(catalog, slide["layout_key"],
                                                               slide["frame"])
    head = (f"Build the deck \"{spec.get('title', '')}\" - {len(slides)} slide(s).\n"
            f"Audience: {spec.get('audience', '')} | Objective: "
            f"{spec.get('objective', '')} | Tone: {spec.get('tone', '')}\n"
            f"Classification: {classification} | Creativity: "
            f"{_LEVEL_NAMES.get(creativity, creativity)}")
    layout_block = "\n\n".join(f"{key}:\n{json.dumps(info, ensure_ascii=False)}"
                               for key, info in layouts.items())
    lines = []
    for slide in slides:
        number = slide["number"]
        where = f"layout {slide['layout_key']} ({slide['mode']} mode)"
        if slide.get("free_region"):
            where += f", free region {slide['free_region']}"
        extra = []
        if slide.get("reveal"):
            extra.append("ANSWER slide: build it with the same helper and content as its "
                         "question; deck.save highlights the correct option")
        if slide.get("images"):
            extra.append(f"images (deck.images({number})): {slide['images']}")
        content = by_number.get(number, {})
        lines.append(f"--- Slide {number} ({slide['kind']}) - {where}\n"
                     + "".join(f"{e}\n" for e in extra)
                     + json.dumps(content, ensure_ascii=False))
    return (head + "\n\n=== THE LAYOUTS YOUR SLIDES WERE ASSIGNED (from the deck design "
            "contract; do not change them) ===\n" + layout_block
            + "\n\n=== THE SLIDES (the content is final) ===\n" + "\n\n".join(lines)
            + f"\n\nSave with deck.save(OUTPUT_PATH).")

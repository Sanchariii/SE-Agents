"""What the model receives: the level's skill, the brand rules from the app's ppt skill,
the user's style skill, then the deck and what is still wrong with it."""
from __future__ import annotations

import json
import re
from pathlib import Path

from app.config.constants import (DeckPolishConstants as C, PptCreatorConstants,
                                  SkillDeckConstants as SD)
from app.genai.deck_polish import reader
from app.genai.skill_deck import skills as skill_files

SKILLS_DIR = Path(__file__).resolve().parent / C.SKILLS_DIRNAME
PPT_SKILL = (Path(__file__).resolve().parents[1] / "ppt" / PptCreatorConstants.SKILL_DIR
             / PptCreatorConstants.SKILL_MD_FILENAME)

#: One line per operation, shown only for the operations allowed at the level.
OP_DOCS = {
    "move": '{"op": "move", "slide": 3, "shape": 7, "left": 1.0, "top": 2.2}',
    "resize": '{"op": "resize", "slide": 3, "shape": 7, "width": 5.0, "height": 2.4}',
    "font_size": '{"op": "font_size", "slide": 3, "shape": 7, "pt": 16}',
    "shorten": ('{"op": "shorten", "slide": 4, "shape": 14, "text": ["short phrase", '
                '"short phrase"], "notes": "every fact taken off the slide"}'),
    "to_cards": ('{"op": "to_cards", "slide": 2, "shape": 5}  - each paragraph becomes a '
                 'card; "Lead: rest" becomes a card title and body. Optional "box".'),
    "to_steps": ('{"op": "to_steps", "slide": 2, "shape": 5}  - numbered steps in a row. '
                 'Optional "box".'),
    "to_timeline": ('{"op": "to_timeline", "slide": 2, "shape": 5}  - steps joined by a '
                    'line. Optional "box".'),
    "stat": ('{"op": "stat", "slide": 2, "value": "6", "label": "steps, in order", '
             '"box": [9.0, 4.6, 3.6, 1.6]}  - the value must already be on the slide.'),
    "divider": '{"op": "divider", "before": 5, "title": "Restarting safely"}',
}

HOW_TO_ANSWER = """=== HOW TO ANSWER ===
Answer with ONE JSON array of operations and nothing else (an empty array [] when the
deck needs nothing). Shapes are named by their "id" on the slide. A box is
[left, top, width, height] in inches; keep {margin} in from the edges and the bottom
{footer} in free (the stamped frame lives there). Operations run in order; a list that
was turned into cards no longer exists for a later operation.

Operations you may use at this level:
{ops}

Every operation is checked by code before it is applied; one that breaks a rule is
skipped, and the deck is scored before and after, so a round that makes it worse is
thrown away. At most {max_ops} operations."""


def ppt_skill_sections(names=C.PPT_SKILL_SECTIONS, path: Path = PPT_SKILL) -> str:
    """The named "## " sections of the app's ppt skill, verbatim."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    found = []
    for name in names:
        match = re.search(rf"^## {re.escape(name)}\s*$(.*?)(?=^## |\Z)", text,
                          re.MULTILINE | re.DOTALL)
        if match:
            body = match.group(1).strip().rstrip("-").strip()
            found.append(f"## {name}\n{body}")
    return "\n\n".join(found)


def level_skill(level: str):
    return skill_files.load_skill(SKILLS_DIR / C.LEVEL_SKILLS[level])


def system_prompt(level: str, style_guidance: str = "") -> str:
    parts = [level_skill(level).body]
    brand = ppt_skill_sections()
    if brand:
        parts.append("=== FROM THE ROCKWELL PPT SKILL (rockwell-skill.md) ===\n"
                     "The deck was built with these rules. Your changes keep them; the "
                     "colours and fonts of what you add are set by code.\n\n" + brand)
    if style_guidance.strip():
        parts.append(f"=== {SD.STYLE_SKILL_NAME.upper()} (user-selected; style only) ===\n"
                     f"{style_guidance.strip()}\nThe Rockwell rules win on any conflict.")
    ops = "\n".join(f"- {OP_DOCS[name]}" for name in C.OPS_BY_LEVEL[level])
    parts.append(HOW_TO_ANSWER.format(margin=C.EDGE_MARGIN_IN, footer=C.FOOTER_BAND_IN,
                                      ops=ops, max_ops=C.MAX_OPS))
    return "\n\n".join(parts)


def user_prompt(prs, issues, level: str) -> str:
    deck = reader.describe_deck(prs)
    open_issues = [i.line() for i in issues if i.fix != "rule"][:C.MAX_ISSUES_SHOWN]
    return (f"Polish this deck at the {C.LEVEL_LABELS[level]} level.\n\n"
            "=== WHAT IS STILL WRONG (after the rule fixes) ===\n"
            + ("\n".join(f"- {line}" for line in open_issues) or "- nothing measured")
            + "\n\n=== THE DECK (positions in inches; \"fixed\": placed by the template) ===\n"
            + json.dumps(deck, ensure_ascii=False, separators=(",", ":")))

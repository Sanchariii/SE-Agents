# Skill deck agent (v3)

A skill-based agent that builds and designs the WHOLE deck in one go, inside the main
deck workflow. Switch it on with `DECK_ENGINE=skill_deck` in `app/config/dev.env`
(`classic`, the per-slide designer, stays the default).

## What it reuses from the app

Everything that makes a classic deck official is reused, not copied:

| From the app | Used for |
|---|---|
| `app/genai/ppt/skills/rockwell-skill.md` | the ppt skill - first in the system prompt |
| `design_principles.py` | the same design principles and priority rule |
| `design_contract.build` | the same master, layout per slide, frame, grid and tokens |
| `deck_builder.py` + `SAMPLE.pptx` | the same API on the official template |
| `code_guard.py` | the same check before a program may run |
| `ppt_creator_agent.CREATIVITY_GUIDANCE` | the same words for Low / Medium / High |
| the dynamic skills (`app/genai/skills`) | the user's style skill and its references |

## What is new

* ONE program designs every slide, so the deck can keep one visual language
  (`skills/whole-deck/SKILL.md` tells the model how).
* `deck_kit.WholeDeck` - DeckBuilder for a whole deck: each slide on its assigned layout,
  with its own frame, and the quiz answer highlighted on answer slides only.
* A check-and-fix round on the whole deck (DeckBuilder's warnings, the layout check, the
  slide count, the style match) and keep-best: a fix round that makes it worse is
  thrown away.
* The deck scorecard on every deck.

## Files

| File | What it does |
|---|---|
| `deck_engine.py` | `build_deck` - stage, prompt, run the graph |
| `staging.py` | the run folder: `deck_builder.py`, `deck_kit.py`, `catalog.json`, `context.py` |
| `deck_kit.py` | `WholeDeck` (runs inside the sandbox; no `app.*` imports) |
| `prompts.py` | system prompt (skills in order) and the brief |
| `skills.py`, `skills/whole-deck/SKILL.md` | our skill, and how skills are read |
| `graph.py` | write -> run -> check -> fix once -> finish -> score |
| `sandbox.py` | the code guard, then the program runs with no secrets |
| `llm.py` | the gateway call |
| `layout_check.py`, `style_match.py`, `scorecard.py` | the checks and the scorecard |

## Score any deck

    python -m scripts.developer_tools.deck_scorecard classic.pptx skill.pptx

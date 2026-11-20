# Reference PPT Design Implementation Plan

> **Status:** draft for review (team plan review, week of 23 Nov 2026). No code is written from this plan until it is approved. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a user picks a `.pptx` with `@` (or a selected skill carries one), every slide is built on that deck's design - its layouts, backgrounds, colours and fonts - with the content from the user's sources and Rockwell's logo and classification label on every slide. Without a reference, nothing changes: decks are built on `SAMPLE.pptx` exactly as today.

**Architecture:** No new pipeline. Reference design mode already exists on branch `reference-design-rockwell-logo`: `reference_design.prepare()` downloads the deck, strips its slides, swaps any other company's logo for Rockwell's (`reference_rebrand`), and the design step runs inside `registry.using_template(path)`, so the art director and the PPT creator search and build on the reference's catalog instead of `SAMPLE.pptx`. The planner, researcher and writers are not touched: a reference is style, never content. This plan adds the explicit `@` entry point, the skill-carried reference, and three quality fixes, and brings the branch to the team's code rules.

**Tech stack:** Python 3.11+, `python-pptx`, Pillow, pydantic v2. **No new dependencies.**

---

## Global Constraints

- **Minimal change, all functionality intact.** Touch only the files listed under [File Structure](#file-structure). A deck built without a reference must be byte-for-byte unchanged in behaviour.
- **Team code rules (20 Nov sync):** Python classes for new work, no type annotations in new code, every new or rewritten file under ~200 lines, no helper functions beyond what a task needs.
- **`app/genai/ppt/deck_builder.py` imports nothing from `app`** (copied into the sandbox; `tests/test_import_hygiene.py`). This plan does not modify it.
- **Rockwell furniture is never optional:** the Rockwell logo and classification label are on every slide, whatever the reference or a skill says.
- **The uploaded file is never modified.** All work happens on a stripped copy in the temp cache.
- **LOW, MED and HIGH creativity all keep working** on a reference: the three-tier ladder and the geometry gate for LOW/MED, max mode (render + vision review) for HIGH.
- **Logging:** `log = get_logger(__name__)`, lazy `%s`, `extra={"session_id": ...}` at every decision (claimed, dropped, prepared, rebranded, fallback).

## Decisions (from the 20 Nov sync)

1. **`@` is for references only.** A `.pptx` picked with `@` is always the design reference, never content. There is no "design or content?" prompt.
2. **References and sources are uploaded in different places.** References live in the new references/skills area (Amal's tables); sources stay in the existing upload.
3. **The `@` choice is resolved in code, not by the triage model.** The UI sends doc ids; triage looks them up. No tokens, no misses.
4. **A skill may carry a reference `.pptx`.** A `.pptx` picked with `@` overrides the skill's own.
5. **Priority:** Rockwell brand rules (logo, classification, readability) > the user's instructions > the reference's design (layouts, look) > the skill's design guidance > default principles.
6. **Users cannot `@` their own earlier output** - the session already has the latest deck.

## Open Questions (for the review meeting)

1. Should the deck-wide polish pass stay off for reference decks (`deck_workflow.py:1496`), or run with reference-aware guidance?
2. Once the references UI is live, should the interim rules (PPT + document uploaded together -> design; "copy the design of..." wording) be switched off, or kept for users who skip `@`?
3. Amal: final names for the references table columns and the `/invoke` field (`references: [doc_id]` proposed).

---

## File Structure

### New files

| File | Responsibility |
|---|---|
| `app/genai/ppt/rebrand/marks.py` | `MarkLibrary`: Rockwell logo fingerprints, classification-symbol fingerprints, logo files. Split out of `reference_rebrand.py`. |
| `app/genai/ppt/rebrand/finder.py` | `LogoFinder`: logo candidates on masters and layouts (groups included) and the Rockwell / other-company verdict. |
| `app/genai/ppt/rebrand/placer.py` | `MarkPlacer`: swap another company's logo in place; add a mark to a layout with none. |
| `app/genai/ppt/rebrand/__init__.py` | `Rebrander.run(prs, ...)` and `RebrandReport` - the public entry point, same behaviour as today's `reference_rebrand.rebrand`. |

`reference_rebrand.py` (608 lines) is deleted once the four files above pass its tests unchanged.

### Modified files

| File | Change |
|---|---|
| `app/genai/ppt/reference_design.py` | `claim_from_selection(session_id, doc_ids)` for `@`; skill-carried reference; one switch for the interim rules; layout usage counts kept before stripping. |
| `app/genai/workflows/deck_workflow.py` | `_triage`: call `claim_from_selection` before `claim_from_message` when the turn carries references (one call site). |
| `app/core/services/deck_service.py` | `/invoke`: read `references`, pass it to the run (with Amal; field name per Open Question 3). |
| `app/genai/agents/slide_reviewer.py` | In reference mode, judge against the reference's design; only logo, classification and readability are fixed. |
| `app/genai/agents/ppt_creator_agent.py` | Log once, not per slide, that a reference has no High Creativity master (max mode reviews on the reference layout). |
| `app/config/constants.py` | `ReferenceDesignConstants`: `INTERIM_RULES_ENABLED`, reviewer wording. |
| `app/core/models/deck_model.py` | Session response carries the active design reference (filename, how it was chosen). |

---

## Tasks

### Task 1 - Bring the branch to the team's code rules
- [ ] Split `reference_rebrand.py` into `rebrand/marks.py`, `finder.py`, `placer.py`, `__init__.py` as classes; no type annotations; each file < 200 lines.
- [ ] `reference_design.py` imports `Rebrander` instead of `reference_rebrand`; no behaviour change.
- [ ] `tests/test_reference_rebrand.py` passes unchanged except for the import line.

### Task 2 - `@` reference -> design reference
- [ ] `claim_from_selection(session_id, doc_ids)`: the first `.pptx`/`.potx` id is looked up (references table; session uploads until it exists), recorded as `brief.design_reference`, and wins over every interim rule. A non-reference or unknown id is rejected with a user-facing note, never silently used.
- [ ] `_triage` calls it when the turn carries `references`; `claim_from_message` still runs otherwise.
- [ ] `/invoke` accepts `references: [doc_id]` (validated list of strings, max 10) and passes it to the run.
- [ ] Tests: `@` pptx claimed; `@` overrides an automatic claim; unknown id rejected; no references -> today's behaviour.

### Task 3 - Skill-carried reference
- [ ] When the active skill lists a `.pptx` reference and the turn has no `@` pptx, claim the skill's reference the same way.
- [ ] Tests: skill reference claimed; `@` pptx overrides it; removing the skill drops it.

### Task 4 - One switch for the interim rules
- [ ] `ReferenceDesignConstants.INTERIM_RULES_ENABLED = True` gates the automatic (deck + document) and wording rules in `claim_from_message`.
- [ ] Test: with the switch off, only `@` and skill references claim a design.

### Task 5 - Follow the layouts the reference actually uses
- [ ] Before stripping, count how many of the reference's slides use each layout (counts only, no text), store them beside the prepared copy, and feed them into the catalog's `usage_count` so `search`/`recommend` favour those layouts.
- [ ] Test: the most-used reference layout ranks first for a matching query; no reference wording reaches the prepared file.

### Task 6 - HIGH creativity on a reference
- [ ] `slide_reviewer`: in reference mode, the system prompt judges against the reference's look (keep its colours and style); only logo, classification and readability are fixed.
- [ ] Log the missing High Creativity master once per deck, not per slide.
- [ ] Test: reviewer prompt carries the reference wording in reference mode and is unchanged otherwise.

### Task 7 - Show the active design reference
- [ ] Session response includes `design_reference: {filename, source: "@"|"skill"|"auto"|"asked"}` or `null`.
- [ ] Test: present after a claim, `null` after a drop.

### Task 8 - Verify on real decks (office laptop, VPN)
- [ ] Happy path from the PR rules with `D2R ILT - Account Planning.pptx` and `D2R JOB AID TEMPLATE 1.pptx`, at LOW, MED and HIGH.
- [ ] Every slide passes the 15-rule geometry gate; side-by-side of reference vs generated recorded for Nishant.
- [ ] A deck with no reference compared against `main` - output unchanged.

---

## Verification

- `pytest tests/test_reference_rebrand.py tests/test_reference_design_mode.py tests/test_design_contract.py tests/test_import_hygiene.py` green after every task.
- Full `pytest`: no failures beyond the 51 already failing on `main` (recorded 20 Nov).
- Task 8 before the PR; PR raised in Azure DevOps after Nishant's demo.

## Dependencies

| Needed from | What | Blocks |
|---|---|---|
| Amal | References table (doc_id, file type, sha256, Blob path) and skill -> reference link | Tasks 2 (final lookup) and 3 |
| Amal / UI | `@` picker sending `references: [doc_id]` | End-to-end test of Task 2 |
| Atharv | Router output format, if skills later carry font/colour settings | Out of scope here |

Tasks 1, 4, 5 and 6 depend on no one and go first.

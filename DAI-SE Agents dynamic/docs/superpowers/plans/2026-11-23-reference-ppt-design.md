# Reference PPT Implementation Plan

> **Status:** draft for review, week of 23 Nov 2026. No code is written from this plan until it is approved. Steps use checkbox (`- [ ]`) syntax.
>
> **Owner:** reference PPTs (Dynamic User Skills workstream). **Builds on:** *Dynamic User Skills for Training-Material Generation* (5 Nov, "the skills plan") and the 20 Nov team sync.

**Goal:** When a user gives a reference `.pptx` - attached to a skill, or picked with `@` in chat - the generated deck follows that reference, the content comes only from the user's source documents, and every slide stays Rockwell-compliant (logo, classification, readability). Without a reference, every deck is built exactly as today.

**Approach:** No new graph node and no new pipeline. A reference is turned once, at upload, into a **reference profile** (structure and layout patterns, never text). The workflow reads the profile at the planning and design stages through the same `skill_context` block the skills plan already adds. What the profile is allowed to change depends on Decision 0.

---

## Decision 0 (needed before Phase 2): what does a reference control?

| | **Option A - Skeleton on the Rockwell template** (recommended for v1) | **Option B - Reference look, Rockwell furniture** |
|---|---|---|
| What is copied | Slide sequence and roles, layout patterns (columns, cards, image position, steps), density | Everything in A **plus** the reference's own layouts, backgrounds, colours and fonts |
| Template | `SAMPLE.pptx` (RB-TEMPLATE-01 holds) | The reference deck, slides stripped |
| Rockwell logo / classification | From the template, as today | Forced on every layout (`reference_rebrand`, already built) |
| Fits the skills plan | Yes - "visual properties are ignored", base rules RB-TEMPLATE/COLOR/FONT unchanged | No - needs RB-TEMPLATE/COLOR/FONT exceptions for references |
| Effort | Medium: new profile + layout mapping | Small: built on branch `reference-design-rockwell-logo`; needs the fixes in Phase 3B |

**Recommendation:** ship **A** in v1, as the skills plan and the brand rules require. Keep **B** in the code behind a switch that is **off** (`REFERENCE_FULL_DESIGN_ENABLED = False`), so it can be turned on later for approved internal templates (e.g. the D2R decks) without new work. Only Phase 3A *or* 3B is built, depending on this decision.

---

## Global constraints

- **Minimal change, everything else intact.** No new graph nodes, no change to `planner_agent.SYSTEM_PROMPT`, `code_guard`, the geometry gate or `plan_rules`. With no reference, prompts and slide-cache keys are byte-for-byte unchanged.
- **Team code rules (20 Nov):** Python classes for new work, no type annotations in new code, each new or rewritten file under ~200 lines.
- **A reference is never a factual source** (RB-GROUNDING). Its text never reaches retrieval, the planner, the writers or the output. The profile stores counts and pattern labels only.
- **The uploaded file is never modified.** All processing happens on a copy.
- **Rockwell base rules win** (skills plan, Appendix B). Order: base rules > the user's words > skill text > reference-derived directives > planner > principles. A reference habit never beats what the user or the skill wrote.
- **All creativity levels keep working:** the three-tier ladder and geometry gate (LOW/MED) and max mode (HIGH).
- **`deck_builder.py` imports nothing from `app`** (`tests/test_import_hygiene.py`); this plan does not modify it.

## Decisions already made (20 Nov sync and skills plan)

1. `@` in chat lists references only; a `.pptx` picked with `@` is always a reference, never a source.
2. References are uploaded in the references/skills area, separate from source documents.
3. A skill may carry reference files (`skill_files`, role `exemplar`). An `@` reference overrides the skill's own.
4. The selection reaches the backend as ids on `/invoke`, resolved in code - not by the triage model.
5. A user cannot `@` their own earlier output.
6. Skills themselves are `.md` only.

## Open questions

0. Decision 0 above.
1. Should `@` references be part of v1, or does v1 use only references attached to a skill (as in the skills plan)?
2. `/invoke` field name for `@` references - proposed `reference_ids: [file_id]`, next to `skill_ids`.
3. Option B only: should the deck-wide polish pass stay off for reference decks (`deck_workflow.py:1496`)?

---

## Interfaces with other workstreams

| With | I need | I provide |
|---|---|---|
| Amal (tables, upload, UI) | `skill_files` rows (`file_id`, `sha256`, `storage_path`, `ingest_status`, `profile` jsonb) per the skills plan P2; `reference_ids` on `/invoke` | `ReferenceProfiler.profile(file)` to fill `skill_files.profile` at upload; the active reference for the UI chip |
| Atharv (`skill_context`, router/rewriter) | A place for the reference block in the planning and design stages | Reference profile as `reference_profile` directives (strength at most `should`) |
| Nishant | Decision 0, open questions 1-3 | Demo and side-by-sides before the PR |

---

## Phase 1 - Foundation (needed by A and B; no dependencies, start now)

### Task 1.1 - Bring the existing branch to the team's code rules
- [ ] Split `app/genai/ppt/reference_rebrand.py` (608 lines) into small classes under `app/genai/ppt/rebrand/` (`MarkLibrary`, `LogoFinder`, `MarkPlacer`, `Rebrander`), each < 200 lines, no type annotations, same behaviour.
- [ ] `tests/test_reference_rebrand.py` passes with only its import line changed.

### Task 1.2 - Reference profiler (`app/genai/ppt/reference_profile.py`, new, < 200 lines)
`ReferenceProfiler.profile(path)` reads the reference once and returns a JSON profile - **no text from the deck**:
- [ ] per slide: **role** (cover, agenda, section, content, quiz, closing - from layout name and placeholders), **pattern** (columns, cards, image left/right, table, chart, numbered steps, quote), **density** (bullet count, character bucket);
- [ ] deck level: slide count, section cadence (e.g. "quiz every 4 content slides"), layout usage counts;
- [ ] uses the existing `template/catalog.py` readers; no LLM in v1 (an optional vision description is a later upgrade).
- [ ] Tests on `PPTs/D2R ILT - Account Planning.pptx` and `PPTs/D2R JOB AID TEMPLATE 1.pptx`, plus a canary test: a unique phrase on a reference slide never appears in the profile.

### Task 1.3 - Resolve the active reference
`ReferenceResolver.for_turn(session_id, reference_ids, active_skill)` in `reference_design.py`:
- [ ] an `@` id wins; else the pinned skill version's `exemplar` `.pptx`; else none.
- [ ] Unknown, foreign or non-`.pptx` id -> 404 / clear message, never silently used (same access rules as skills: non-owner gets 404).
- [ ] Called once at the start of the run in code; the profile is pinned with the turn like the skill pin.
- [ ] The interim message-wording rules on the branch go behind `INTERIM_RULES_ENABLED` (off once the UI ships).

## Phase 2 - Reference reaches planning (A and B)

### Task 2.1 - Planning block
- [ ] `skill_context` (Atharv's module) gets a `reference` section rendered from the profile: slide sequence by role, section cadence, target density. Labelled "structure only - not content".
- [ ] Planner follows the sequence and roles; the user's words and the skill text outrank it.
- [ ] `DeckPlan.skill_notes` (skills plan M2) lists what was applied from the reference, e.g. "Applied: agenda after cover, quiz every 4 slides (from reference)".
- [ ] Test: with no reference the planner prompt is unchanged (snapshot).

## Phase 3A - Option A: Rockwell layouts matching the reference

### Task 3A.1 - Map reference patterns to Rockwell layouts (`design_contract`)
- [ ] For each planned slide, the reference pattern becomes a **layout preference** (the skills plan's `layout_preference` -> intent bias in `design_contract.build`), e.g. "3 columns" -> Rockwell three-column layout, "image right" -> image-right layout.
- [ ] Only catalog layouts can be chosen (RB-LAYOUT-CATALOG); an unmatched pattern falls back to `recommend()` as today.

### Task 3A.2 - Pattern description to the designer
- [ ] The slide's reference pattern is added to the designer's design block (`SlideCodeBrief` skill design notes, skills plan M3). `code_guard` keeps colours, fonts and logo on brand.
- [ ] The reference's design hash joins the design key so a changed reference never reuses stale slides.

## Phase 3B - Option B: build on the reference's design (only if Decision 0 = B)

- [ ] `REFERENCE_FULL_DESIGN_ENABLED` switch; when on, the stripped and rebranded reference replaces `SAMPLE.pptx` for the deck (`registry.using_template`, already built).
- [ ] Layout usage counts from Task 1.2 feed `catalog.usage_count`, so search favours the layouts the reference uses (reference layout names such as `SSCPT0015` defeat name matching).
- [ ] Max mode: the vision reviewer judges against the reference's look; only logo, classification and readability are fixed. Log the missing High Creativity master once per deck.
- [ ] Base-rule registry: RB-TEMPLATE/COLOR/FONT entries note the approved-reference exception.

## Phase 4 - Visibility and verification

### Task 4.1 - Show the active reference
- [ ] Session data and the plan review show "Reference: <filename> (from @ / from skill <name>)"; `null` when none.

### Task 4.2 - Verify (office laptop, VPN)
- [ ] PR happy path with both D2R decks at LOW, MED and HIGH, plus one deck with no reference compared against `main`.
- [ ] Side-by-side of reference vs generated deck recorded for Nishant.

---

## Acceptance criteria

- [ ] Slide-role order of the generated deck matches the reference's **≥ 90%** (when the user and skill set no conflicting structure).
- [ ] Every slide passes the 15-rule geometry gate; **0** base-rule violations (Option A: no non-template fonts or colours; both: logo and classification on every slide).
- [ ] **0** canary leaks: no reference-only text in the plan, slides or notes.
- [ ] With no reference: plan, prompts and design keys identical to today (golden comparison on 5 standard requests).
- [ ] Full `pytest`: no failures beyond the 51 already failing on `main` (recorded 20 Nov).

## Out of scope for v1

Vision-model description of reference slides; more than one reference per deck; referencing the user's own earlier output; `.docx` output (separate workstream).

## Order of work

1. Week of 23 Nov: Phase 1 (no dependencies) while Decision 0 and the open questions are settled.
2. Week of 30 Nov: Phase 2 with Atharv's `skill_context`; Phase 3A or 3B.
3. Then Phase 4, demo to Nishant, PR in Azure DevOps.

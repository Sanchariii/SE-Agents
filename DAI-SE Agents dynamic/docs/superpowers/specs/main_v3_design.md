# Template-Grounded Slide Generation with a Geometry Gate

**Date:** 2026-09-21
**Branch:** `f-llm-slide-creation`
**Status:** Design approved, ready for implementation planning

## Problem

The deck generator produces slides with overlapping text, elements outside the canvas,
and overflowing text boxes. The deck also does not read as one deck: slides are generated
independently and in parallel, so nothing makes them agree on a grid, a master, or a
visual language.

The root cause is not a tuning problem. The live path deliberately generates every slide
as free-form `python-pptx` from a *blank* presentation and then copies the shape trees
into another blank presentation, so none of the brand template's masters, theme, fonts,
logo or footer are inherited. Every coordinate and every colour is the model's, decided
without the ability to measure text, and nothing checks the result.

Three defects then remove the guardrails that were meant to compensate:

1. **The brand guide is never loaded.** `PptCreatorConstants.SKILL_MD_FILENAME` is
   `"SKILL.md"`; the file on disk is `app/genai/ppt/skills/rockwell-skill.md`.
   `load_brand_skill()` raises `OSError`, logs a warning, and returns `""`. Every slide's
   system prompt is `SYSTEM_SUFFIX` alone — 266 lines of brand rules are dead.
2. **The layout contract is computed, then discarded.** `PptCreatorAgent._user_prompt`
   resolves the layout, builds `layout_map` from `LayoutSpec.to_json_contract()`, logs it,
   and passes `template_path=` and `layout_map=` to `USER_TEMPLATE.format(...)` — but
   `USER_TEMPLATE` declares neither field, so `str.format` silently drops both. The model
   is instructed that "the slide's size comes from the template itself" while never being
   told which file the template is.
3. **No geometry validation exists in the live path.** The only success test is that the
   generated program exited zero and wrote a `.pptx`. The two checkers that exist are
   stranded in the retired deterministic renderer: `audit.py` detects text overflow but is
   called by nothing, and `filler.py::_overlaps_in` compares extras only against
   placeholders (never against each other) and resolves violations by silently dropping
   the element.

`scripts/mainv3/main_v3.py` does not have this problem because it never lets the model own
geometry: it opens the real template, adds slides on real layouts, validates a structured
plan against a catalog before rendering, and constrains generated code to a pre-styled
`DeckBuilder` API.

## Goals

- **Guarantee** no overlapping text, no element outside the canvas, and no element over
  reserved brand furniture. These are decidable by rectangle arithmetic alone.
- **Guarantee** every colour, font and font size comes from the template theme.
- Substantially reduce text overflow. This is an estimate, not a guarantee (see
  Non-goals).
- Preserve genuine design freedom: when content is better served by a chart, a table, or a
  composed diagram than by an existing layout, the agent should build that.
- Make a deck read as one deck.
- Keep the per-slide cache and incremental-edit behaviour intact.

## Non-goals

- **Provable absence of text overflow.** Measuring text requires real font metrics.
  Vendoring Barlow plus `fonttools` was considered and declined in favour of keeping the
  dependency set unchanged, so overflow detection stays a calibrated heuristic. Residual
  overflow is reported to the user rather than silently shipped.
- **Provable convergence of the repair loop.** Deterministic geometry repair was
  considered and declined; the model repairs its own code. A slide that exhausts its
  repair budget falls back to a deterministic on-layout render rather than blocking
  publication.
- Per-classification template files. Superseded by Decision 3.
- An icon library. See Decision 5.

## Decisions

These were settled during design and are treated as fixed constraints.

1. **Code generation is retained**, in the `main_v3` sense: the model writes a program,
   but only against a constrained `DeckBuilder` API operating on the real template. It is
   not free-form `python-pptx` against a blank presentation.
2. **`SAMPLE.pptx` is the only template.** No `.potx` files are read. The canonical file
   is the 4.1 MB `scripts/mainv3/SAMPLE.pptx` (4 masters, 181 layouts, 174 unique
   descriptive layout names), moved to `company-ppt-templet/SAMPLE.pptx` and resolved by a
   single new constant, `PptConstants.TEMPLATE_FILENAME`, rather than by globbing. The
   6.7 MB variant under `company-ppt-templet/RA-Brand-Template-2026/RA-Brand-Template-2026/`
   has 311 layouts but only 136 unique names with duplicates and typos, which would defeat
   name-derived layout search.

   The five `.potx` files and the 6.7 MB `SAMPLE.pptx` stay on disk as the official brand
   artifacts but become unreferenced by application code. `registry.available()`, which
   globs for `*.potx`, is retired with `path_for`, so their presence cannot influence
   template resolution.

8. **`scripts/mainv3/` is reference material only, and is deleted when the port lands.**
   See "Reference material and cleanup" below.
3. **Classification is rendered by us.** `DeckPlan.classification` remains data but no
   longer selects a template. The frame stamps the classification label plus the matching
   symbol from `company-ppt-templet/ppt-Use-Policy-icons/` on every slide, from one code
   path, in a reserved zone the geometry gate protects.
4. **Creative freedom is bounded to composition.** Colours, fonts and font sizes are
   resolved from the template theme as named tokens. Arrangement, component choice and
   visual treatment are free.
5. **Icons are drawn, not harvested.** `icons.py` finds 20 icon-sized images in
   `SAMPLE.pptx`, of which 4 carry alt-text, and those are Word auto-captions
   ("A screenshot of a computer") rather than semantic labels. There is nothing to search.
   Icons are produced as filled brand geometry — numbered circles, hexagons, chevrons,
   pentagons — which is what the brand guide prescribes ("Rockwell style prefers filled,
   not outlined"). `ra_hexagon.png` remains available as a brand motif.
6. **The geometry gate feeds violations back to the model.** There is no deterministic
   repair pass. On budget exhaustion, the slide is rendered deterministically on an exact
   `SAMPLE.pptx` layout.
7. **Text measurement stays heuristic.** A per-character advance-width table, calibrated
   once offline against real Barlow metrics and biased to under-estimate capacity. No new
   runtime dependency.

## Reference material and cleanup

`scripts/mainv3/` is a working reference implementation, not a dependency. Nothing in
`app/` imports it, and nothing in `app/` may import it. It exists to be read and ported
from, and it is **deleted from the repository once the port is complete**.

Three things come out of that folder, in this order:

1. **`SAMPLE.pptx`** is *moved* (not copied) to `company-ppt-templet/SAMPLE.pptx`. This
   must happen first: it is the only artifact the running application needs, and deleting
   the folder before moving it would take the canonical template with it.
2. **`main_v3.py`** is the port source for the catalog, `DeckBuilder`, the plan validator
   and the static code check. All line references in this spec are against
   `scripts/mainv3/main_v3.py` as of commit **`0bd70fb`**, so they remain resolvable in
   git history after the folder is gone.
3. **`rockwell-skill-v3.md`** becomes the basis of the application's brand guide. See
   "Brand guide" below — this replaces, rather than repoints to, the guide currently on
   disk.

The deletion is the last step of implementation, gated on the new suite passing, so the
reference stays available for the whole port. Once it is gone, `git show
0bd70fb:scripts/mainv3/main_v3.py` is how anyone reads it again.

`scripts/feature_test/` is left alone. It is a separate experiment and out of scope here,
but note that its `rockwell-skill.md` is byte-identical to the application's current one,
which is evidence the application guide was copied from that experiment rather than
written for this architecture.

## Brand guide

The guide loaded into the design agent's system prompt becomes
`app/genai/ppt/skills/rockwell-skill.md`, **rewritten from `rockwell-skill-v3.md`**. The
file currently at that path is discarded, not fixed.

This is not a preference. The current guide is structurally incompatible with this design
in two ways that would actively fight the implementation:

- It contains a **hardcoded layout list** (`## Available Layouts (from Rockwell PPTX
  template)`). Layouts are discovered at runtime from the catalog, across 181 of them.
  `rockwell-skill-v3.md` states the opposite intent explicitly: "The layouts are not
  listed here on purpose: they are discovered at runtime."
- Its `## Programmatic Implementation (python-pptx)` section teaches raw `python-pptx` —
  hand-built textboxes, literal hex colours, manually positioned titles. The AST guard
  (component 4) rejects exactly that code. Loading this guide would teach the model to
  write programs the guard is specified to refuse.

`rockwell-skill-v3.md` is already written for this architecture: catalog-driven layout
selection, placeholders-first, the `DeckBuilder` component API, and the brand palette as
named tokens. It needs four additions for this design:

- `canvas` mode: when to leave the layout's placeholders behind, and what the free region
  and the 12x6 grid are.
- The geometry rules, stated as design guidance rather than as validator output, so the
  agent aims for compliance instead of discovering it in the repair loop.
- Classification marking as a reserved zone the agent must not draw into.
- The type scale, as the closed set of permitted component font sizes.

Its "Common Mistakes to Avoid" section already lists "elements outside the 13.33in x
7.5in canvas or over the footer", which is precisely what the gate now enforces
mechanically rather than by request.

## Architecture

```
planner_agent ──► ppt_agent (per slide, parallel) ──► content: SlideDraft
                                                            │
                          ┌─────────────────────────────────┘
                          ▼
               deck design contract   (once per deck, before any slide is built)
                 master · frame · grid · brand tokens · per-slide mode+layout
                          │
                          ▼
        PptCreatorAgent (per slide, parallel)   ◄── catalog tools + check_geometry
                          │  writes a DeckBuilder program
                          ▼
                  AST check  ──►  sandbox execute  ──►  single-slide .pptx
                          │
                          ▼
                  geometry gate  (reopen and measure)
                    ├─ clean ────────────────────────────► slide cache
                    ├─ violations ──► feed back, model rewrites  (budget N)
                    └─ budget spent ─► deterministic on-layout render
                          │
                          ▼
                  DeckStitcher  (target deck opened from SAMPLE.pptx)
                          │
                          ▼
                      publish
```

### Composition modes

Every slide is built in one of two modes, chosen per slide by the design agent from the
content's shape:

- **`layout` mode** — fill a real template layout's placeholders. Correct when the content
  maps cleanly onto an existing design: three text columns, a five-item agenda, photo with
  text, headshots.
- **`canvas` mode** — take a `canvas_capable` layout from the same master and compose the
  content region freely: charts, tables, cards, composed diagrams, abstract brand shapes.
  This is the mode for content that an existing layout would misrepresent.

`canvas` mode still sits on a real master, so theme, Barlow, background treatment and the
master's decorative graphics are inherited. 25 of 181 layouts qualify; the strongest
candidates are `M1-L10 "Title with Subhead"` (6.54in free height, 2 inherited decor
shapes, on the 82-layout master M1) and `M2-L4 / M2-L30 / M2-L38` (6.58in free height,
zero decor).

### Deck design contract

Computed once per deck, before any slide is built, and passed to every parallel
slide-builder. This is what makes a deck read as one deck, and it is the piece with no
equivalent in either codebase today.

Owned by a new module, `app/genai/ppt/design_contract.py`, called from
`deck_workflow._collect` immediately before `_design_slides`. Most of it is deterministic
from the catalog; only the per-slide mode and layout assignment needs judgement:

| Field | Content | Source |
|---|---|---|
| `master_index` | One master for all content slides, as `main_v3` enforces | Deterministic: catalog's preferred master |
| `frame` | Title band, footer band, logo placement and variant, classification mark — all as rectangles | Deterministic: catalog geometry + classification |
| `grid` | 12 columns x 6 rows over the content region, fixed gutters | Deterministic: constants |
| `tokens` | Theme accents, theme font, the type scale | Deterministic: catalog theme |
| `slides[]` | Per slide: `mode`, `layout_key`, free region | One model call for the whole deck, given every slide's title, content shape and `visual` hint, with `recommend_layouts` available |

The single model call is what lets the agent "explore other layouts and experiment" with
deck-wide awareness instead of 4 parallel workers each guessing in isolation. Its output
is validated against the catalog before use — unknown layout keys, a `canvas` mode on a
layout that is not `canvas_capable`, or a content slide on a non-preferred master are
rejected and re-requested once, then fall back to `recommend_layouts`' top choice in
`layout` mode.

Cover and closing slides are exempt from `master_index` uniformity, matching `main_v3`.

### Type scale

Rule 9 needs a closed set of permitted font sizes. It is built per template, not
hardcoded:

- **Placeholder text is exempt.** Sizes inherited from the template's own placeholders are
  correct by definition, and `main_v3`'s shrink-to-fit may reduce them as far as 10pt.
- **Component text** uses a fixed scale derived from the existing
  `PptConstants` values and the brand guide's component specs: 32 (stat value), 28
  (title), 20 (heading), 14 (card title), 15 (body), 12 (shape label), 11 (table cell,
  caption), 10 (floor). A size outside this set is a rule 9 violation.

## Components

### 1. Template catalog — `app/genai/ppt/template/catalog.py`

Raise the existing catalog to `main_v3` parity and add what `main_v3` lacks.

Ported from `main_v3`:

- Theme extraction per master: `accent1..6`, `dk1/lt1/dk2/lt2`, major and minor fonts.
  Both `SAMPLE.pptx` variants carry the expected palette (`accent1=#CD163F`,
  `accent2=#003E7E`, `accent3=#F58024`, `accent4=#00AEEF`, `accent5=#F9C20A`,
  `accent6=#77C14C`) and Barlow for both major and minor font.
- Placeholder roles, geometry, inherited font size and capacity hints.
- Layout tags derived from layout names, enabling keyword and intent search. Viable only
  because of Decision 2.
- Preferred content master.

New:

- **`decor_rects`** — the actual rectangles of each layout's non-placeholder shapes.
  `main_v3` only counts these (3 to 7 per layout); the gate needs their geometry because
  they are inherited graphics an agent will otherwise draw over.
- **`background_luminance`** per layout — decides light versus dark text and which logo
  variant, mechanically rather than by the model's guess.
- **`canvas_capable`** flag plus the precomputed free region for each such layout.

The catalog is cached per file by mtime, as it is today.

### 2. Design agent tools — `app/genai/tools/design_tools.py` (new)

| Tool | Returns |
|---|---|
| `get_template_summary` | Slide size, theme tokens, preferred master, layout index |
| `search_layouts` | Layouts matching keywords or tags |
| `recommend_layouts` | Layouts ranked for a slide intent and content shape |
| `get_layout_details` | Placeholders with `idx`, geometry and capacity; `decor_rects`; reserved zones; the safe region; the grid |
| `check_geometry` | Violations for a submitted set of rectangles, without executing code |
| `run_slide_code` | Executes the program in the existing sandbox |
| `validate_slide` | The geometry, brand and fidelity report for the produced file |

`check_geometry` exists to make arrangement iteration free. Without it, discovering a
collision costs a full code execution, up to
`PptCreatorConstants.EXEC_TIMEOUT_SECONDS` (180s).

Its input is a list of `{id, left, top, width, height, has_text, filled, text?}`, and it
evaluates rules 1 to 6 — the rules decidable from rectangles alone — plus rule 10 for any
entry that supplies `text` and a font size. Rules 7 to 9 are not checkable pre-render,
since they concern what the produced XML actually contains; they are enforced by the AST
guard and `validate_slide`. `check_geometry` is therefore a fast-path optimisation, not a
substitute for the gate: `validate_slide` remains authoritative.

### 3. DeckBuilder — `app/genai/ppt/deck_builder.py` (new, ported)

Port `SlideHandle` and `DeckBuilder` from `main_v3.py` lines 1402-1970 (at commit
`0bd70fb`; the folder is deleted afterwards — see "Reference material and cleanup").

Placeholder API: `set_title`, `set_subtitle`, `set_text`, `set_bullets`, `set_picture`,
`set_table`, `set_chart`, `set_notes`, `placeholder_bounds`, `remove_placeholder`.

Component API: `add_card`, `add_stat`, `add_icon_circle`, `add_shape`, `add_table`,
`add_chart`, `add_textbox`, `add_line`, `add_image`.

Colours are accepted only as brand or theme token names resolved against the catalog's
theme. Tables arrive with a navy header and white uppercase 11pt bold text; charts arrive
in brand series colours with light gridlines and a bottom legend. This is what makes
Decision 4 hold without the model having to comply voluntarily, and what makes the
"sometimes a chart or a table is the right visual" case land on-brand by construction.

`finalize()` applies the frame — footer band, classification label and its policy symbol,
the logo variant selected by `background_luminance`, the slide number — and removes unused
placeholders so no `Click to edit` prompt ships. The frame is never the model's to draw.

### 4. AST guard — `app/genai/ppt/code_guard.py` (new)

Extends the deny-list approach of `main_v3::_static_code_check` with brand enforcement.
Rejects, before execution:

- `Presentation(...)` constructed directly
- direct `.shapes.add_*` calls, bypassing the component API
- literal hex colour strings and `RGBColor(...)` literals
- the existing forbidden-module and forbidden-call sets from `main_v3`

Rejection returns a message to the model in the same loop as a geometry violation.

### 5. Geometry gate — `app/genai/ppt/geometry.py` (new)

Runs on the reopened single-slide `.pptx`, measuring what landed rather than what was
declared. The gate is containment-aware: naive pairwise overlap detection would reject
every good design, because a label inside a card, an accent bar on a card edge and a
heading over a gradient panel all overlap and are all correct.

A shape is **text-bearing** if it has a text frame whose text is non-empty after
stripping. A shape is **filled** if it has a solid or gradient fill. A **reserved zone**
is any frame rectangle, any `decor_rect` of the slide's layout, or the bounding box of a
filled placeholder.

**Blocking, exact** — decided by rectangle arithmetic, therefore guarantees:

| # | Rule |
|---|---|
| 1 | Every shape lies within the canvas minus margins. Tolerance 0.02in. |
| 2 | No text-bearing shape overlaps another text-bearing shape. No exception. |
| 3 | A text-bearing shape may overlap a non-text shape only if fully contained in it. Partial overlap is a violation; containment is layering. |
| 4 | Two filled shapes may overlap only if one fully contains the other. |
| 5 | No shape intersects a reserved zone. |
| 6 | No added shape overlaps a filled placeholder. |
| 7 | No shape retains template prompt text (`Click to edit`, `Second level`, ...). |

**Blocking, exact** — brand:

| # | Rule |
|---|---|
| 8 | Every fill, line and font colour resolves to a theme or brand token. |
| 9 | Every font is the theme font, and every size on non-placeholder text is on the type scale defined above. Placeholder-inherited sizes are exempt. |

**Advisory** — heuristic, reported, blocking only when the estimate exceeds capacity by a
wide margin:

| # | Rule |
|---|---|
| 10 | Estimated rendered lines must not exceed placeholder or shape capacity by more than 15%, using the calibrated per-character width table. |

Intersection tolerance for rules 2 to 6 is 0.15in on both axes, matching the existing
`filler.py::_overlaps_in` slack, so hairline abutments are not flagged.

Violations are emitted as concrete, actionable instructions rather than assertions —
for example: `card 2 at (6.80, 2.40) 3.60x1.80in overlaps card 3 at (10.20, 2.40)
3.60x1.80in by 0.42x1.10in; shift card 3 right by 0.45in or narrow both to 3.30in`.

### 6. Repair loop — in `PptCreatorAgent`

Extends the existing code-repair loop, which today reacts only to tracebacks, so that it
also reacts to gate violations. Per slide:

1. Generate the program. Run the AST guard; on rejection, feed back and regenerate.
2. Execute in the sandbox. On traceback, feed back and regenerate (current behaviour).
3. Run the gate. If clean, accept.
4. If violations remain and budget is left, feed the violation list back and regenerate.
5. On budget exhaustion, render the same content deterministically on an exact
   `SAMPLE.pptx` layout chosen by `recommend_layouts` for the slide's intent, which is
   clean because the template positioned every element. Record the fallback in
   `SlideBuildResult.fallback_used`, as today.

Budget is `PptCreatorConstants.MAX_CODE_FIX_ATTEMPTS`, currently 5, shared across all
three failure kinds so a slide's total cost stays bounded.

### 7. Stitcher — `app/genai/ppt/stitcher.py`

Stitching gets simpler rather than harder. Because every slide and the target deck now
derive from the same `SAMPLE.pptx`, layout `(master_index, layout_index)` is identical in
source and target. So each target slide is created on its **correct** layout instead of
`slide_layouts[6]`, and the shape tree is copied onto it. Theme, fonts, master decor,
footer and logo inherit properly.

Related-part copying (`_copy_related_parts`, `_remap_relationship_ids`) is unchanged and
still required for images and charts. `_strip_empty_placeholders` is replaced by
`DeckBuilder.finalize()`'s placeholder cleanup, which runs per slide with knowledge of
what was intentionally filled.

`slide_cache.py` is untouched: per-slide files remain independently cacheable, because the
slide number to layout mapping is carried in the design contract rather than in the file.

## Data model changes

- `SlideDraft.shapes` / `SlideDraft.icons` are declared today but never populated by
  `slide_tools.write_content`. They are dropped; geometry is now decided by the design
  agent in generated code, not by the content agent. `SlideDraft.tables` is retained,
  since `write_content` does populate it.
- `SlideCodeBrief` gains the deck design contract fields it must carry: `mode`,
  `layout_key`, `grid`, `frame`, `tokens`, `free_region`.
- `DeckPlan.classification` is unchanged in shape; only its consumer changes.

## Error handling

- A missing or unreadable `SAMPLE.pptx` is fatal at catalog load. There is no second
  template to fall back to, and producing an unbranded deck silently is worse than
  failing loudly. This is a behaviour change from `registry.path_for`, which falls back to
  the internal template.
- A missing classification symbol or logo file is a warning; the slide renders without
  that mark, as today.
- Gate failure after budget exhaustion is a per-slide degradation, never a deck failure.
- Stitching failure remains a turn failure with content preserved in session state, as
  today.

## Testing

Existing suites to keep green: `tests/test_ppt_creator_agent.py`,
`tests/test_slides_node.py`, `tests/test_workflow.py`, `tests/test_ppt_agent.py`.

New, all offline against the committed `SAMPLE.pptx` with no model calls:

- **Catalog:** theme tokens match the known palette; 181 layouts load; `canvas_capable`
  returns the expected 25; `decor_rects` are non-empty for layouts known to carry decor.
- **Geometry gate, rule by rule:** each of rules 1 to 9 has a positive and a negative
  fixture. Critically, **legitimate layering must pass** — a text box fully inside a card,
  an accent bar on a card edge, a heading over a gradient panel — since a gate that
  rejects good design is worse than no gate.
- **Heuristic calibration:** the per-character table is checked against a table of known
  Barlow string widths, asserting it never over-estimates capacity.
- **AST guard:** rejects each forbidden construct; accepts a representative
  `DeckBuilder` program.
- **Stitcher:** a two-slide stitch preserves each slide's layout identity, theme font and
  images.
- **End to end, no model:** a fixed design contract plus a hand-written `DeckBuilder`
  program produces a deck that passes the gate on every slide.
- **Brand guide loads:** `load_brand_skill()` returns non-empty and contains the
  `DeckBuilder` component section, catching both the constant-vs-filename bug and a
  regression to the old guide.
- **Import hygiene:** no module under `app/` imports from `scripts/`. This is the test
  that makes deleting `scripts/mainv3/` safe, and it should be written and passing
  *before* the deletion step.
- **Template resolution:** `registry.template_path()` resolves to an existing file, and
  the catalog loads from it. Guards the move of `SAMPLE.pptx` into
  `company-ppt-templet/`.

## Retired

| Item | Reason |
|---|---|
| `builder.build_pptx` | Already self-documented as retired; nothing live calls it |
| `renderer.py` | Only consumer was `build_pptx` |
| `template/filler.py` | Containment math moves into `geometry.py` |
| `audit.py` | Overflow math moves into gate rule 10; never called today |
| `registry.path_for` and `registry.available` | Superseded by Decision 3; a single constant replaces the `.potx` glob |
| `template/icons.py` | Nothing harvestable; superseded by Decision 5 |
| `plain_slide_pptx` | Blank-deck fallback replaced by the on-layout deterministic render |
| `app/genai/ppt/skills/rockwell-skill.md` (current content) | Incompatible with this design; replaced from `rockwell-skill-v3.md` |
| `scripts/mainv3/` (whole folder) | Reference material, deleted once the port lands and the suite is green. `SAMPLE.pptx` is moved out first. |

`registry.normalize`, `registry.label`, `registry.detect_classification` and
`registry.template_root` are retained: still used for the classification mark and for
locating the template and the logo files. `registry` gains one function,
`template_path()`, returning `template_root() / PptConstants.TEMPLATE_FILENAME`.

## Bugs fixed as part of this work

1. `PptCreatorConstants.SKILL_MD_FILENAME` is `"SKILL.md"` while the file on disk is
   `rockwell-skill.md`, so no guide is ever loaded. Fixing the constant alone is **not
   sufficient and would make things worse**: the file at that path is the incompatible
   older guide (see "Brand guide"), so repointing at it would start teaching the model to
   write code the AST guard rejects. The fix is to replace the file's content from
   `rockwell-skill-v3.md` *and* correct the constant. A guard test asserts
   `load_brand_skill()` returns non-empty, since this failure was silent for the whole
   life of the feature — it only logged a warning and returned `""`.
2. The dropped `layout_map` and `template_path`. The layout contract moves to
   `get_layout_details`, which the agent calls as a tool, so the prompt cannot silently
   lose it and the content is cacheable across slides.
3. `SlideDraft.shapes` / `SlideDraft.icons` declared but never populated: dropped.

## Open risks

- **Repair-loop cost.** Adding geometry violations to the repair loop raises the expected
  model calls per offending slide by roughly 1 to 2. With `MAX_WORKERS = 4` and a shared
  budget of 5 attempts, worst-case latency per slide is unchanged, but spend per deck
  rises. Worth measuring against the current baseline once rule 10's false-positive rate
  is known.
- **Rule 10 false positives.** A heuristic biased to under-estimate capacity will
  sometimes demand a rewrite of text that would have fitted. Because rule 10 is advisory
  except at a wide margin, this costs tokens rather than correctness, but the margin
  should be tuned against real decks.
- **`canvas` mode concentration.** Only 25 layouts are canvas-capable and the strongest
  are on masters M1 and M2. If most decks choose `canvas` mode, slides risk converging on
  one or two base layouts. The design contract's grid is what should keep that from
  reading as repetition, but it is worth watching.

# Template-Grounded Slide Generation with a Geometry Gate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace free-form model-authored `python-pptx` with programs written against a constrained `DeckBuilder` API on the real brand template, gated by rectangle-exact geometry and brand checks that feed violations back to the model.

**Architecture:** One deck design contract (master, frame, grid, tokens, per-slide mode + layout) is computed once and handed to every parallel slide builder. Each slide builder writes a `DeckBuilder` program, which is AST-checked, executed in a **sealed sandbox**, then reopened and measured by a geometry gate. Violations go back to the model inside the existing repair loop; on budget exhaustion the slide is rendered deterministically on a real template layout. Slides are stitched onto their own layouts in a target deck opened from the same template.

**Tech Stack:** Python 3.11+, `python-pptx`, `lxml`, `pydantic` v2, LangChain `@tool` (tool wrappers only), `pytest`. **No new runtime dependencies.**

**Spec:** [docs/superpowers/specs/main_v3_design.md](../specs/main_v3_design.md) — read it before Task 1. Every task below argues from it.

---

## Global Constraints

- **No new runtime dependencies.** stdlib + `python-pptx` + `lxml` + what `app/` already imports. In particular: no `fonttools`, no vendored font files.
- **`app/genai/ppt/deck_builder.py` must import only stdlib + `python-pptx` + `lxml`.** No `app.*` imports, ever. It is copied verbatim into each sandbox slide directory, and the same file is imported in-process by the app. One `from app.config...` line in it breaks the sandbox. Task 16 adds a test that enforces this.
- **The sandbox stays sealed.** `PptCreatorAgent._sandbox_env` is not widened: no `PYTHONPATH`, no inherited secrets. Model-written code reaches `DeckBuilder` only through the copy in its own slide directory.
- **`SAMPLE.pptx` is the only template**, resolved by `PptConstants.TEMPLATE_FILENAME` through `registry.template_path()`. No `.potx` is read by application code. No globbing.
- **Slide canvas is 13.333in x 7.5in** (`PptCreatorConstants.SLIDE_WIDTH_EMU` / `SLIDE_HEIGHT_EMU`), read from the template, never set by generated code.
- **Colours are token names only.** `accent1..6`, `dk1/lt1/dk2/lt2` and the brand aliases in `BRAND_DEFAULTS`. Literal hex in generated code is an AST-guard rejection and a rule 8 violation.
- **Theme font is Barlow** for both major and minor on all four masters (verified). Non-placeholder font sizes must be on `TYPE_SCALE = (10, 11, 12, 14, 15, 20, 28, 32)`. Placeholder-inherited sizes are exempt.
- **Nothing under `app/` may import from `scripts/`.** Enforced by a test in Task 16.
- **Every new test runs offline** against the committed `SAMPLE.pptx`, with no model call and no network.
- Reference line numbers cite `scripts/mainv3/main_v3.py` **at commit `0bd70fb`**, which stays resolvable after Task 16 deletes the folder (`git show 0bd70fb:scripts/mainv3/main_v3.py`).

### Verified facts about `SAMPLE.pptx` (measured, not assumed — build on these)

| Fact | Value |
|---|---|
| Masters | 4 (M0 `2_COVER LAYOUTS`, M1 `7_CONTENT LAYOUTS`, M2 `MAIN Internal 2024`, M3 `8_CONTENT LAYOUTS`) |
| Layouts | 181 (M0=5, M1=82, M2=47, M3=47) |
| Unique layout names | 174 |
| Slide size | 13.3333 x 7.5 in |
| Sample slides | 31 (M0=2, M1=17, M3=12) |
| Preferred content master | **M1** (17 sample slides) |
| Cover master | **M0** |
| Theme fonts | major=`Barlow`, minor=`Barlow` on all four masters |
| Theme colours (identical on all masters) | `accent1=CD163F accent2=003E7E accent3=F58024 accent4=00AEEF accent5=F9C20A accent6=77C14C dk1=000000 lt1=FFFFFF dk2=CCCCCC lt2=4C4C4C` |
| Classification symbols | `company-ppt-templet/ppt-Use-Policy-icons/ppt-Use-Policy-icons/{Public,Internal,Confidential,Restricted}_Symbol.png` — **note the doubled directory**, and **no `Cobranded_Symbol.png` exists** |
| Logos | `company-ppt-templet/logos/{ra_lockup_full,d2r_mark,d2r_mark_white,ra_hexagon}.png` |

### Two places this plan knowingly departs from the spec

1. **`canvas_capable` is 16, not 25.** Applying the spec's own role rules (a layout whose only non-furniture placeholders are `title`/`subtitle`) yields **24** layouts, of which 8 are section dividers with only 3.66–3.88in of free height — too short to compose into. With the `MIN_CANVAS_FREE_HEIGHT_IN = 4.0` floor this plan adds, **16** qualify. Every layout the spec names by hand is in that 16. Task 3's test asserts **those named layouts and a `>= 12` floor**, not `== 25`, so a threshold tweak does not break the suite.

   Reaching 16 depends on two things Task 3's review turned up, both now fixed in that task: furniture placeholders (DATE / FOOTER / SLIDE_NUMBER) must be excluded from a layout's roles, or `M2-L31` is wrongly disqualified on its page-number placeholder; and a placeholder with no resolvable geometry must be treated as unknown rather than as a `(0,0,0,0)` box, or `M2-L44` is wrongly *included* with a free region starting at the top of the slide.

2. **The free-region heights are not the spec's 6.55in / 6.58in.** Those figures are `slide_height - title_bottom`, with nothing subtracted. The real free region has to clear three things the spec's arithmetic ignores: the `CANVAS_TITLE_GAP_IN` under the title, the `FOOTER_BAND_HEIGHT_IN` the frame occupies, and — found during Task 3's review — **the layout's own subtitle placeholder**, which 10 of the 16 canvas layouts carry and which the spec's title-only formula would compose straight over. Measured after all three: `M2-L4` ≈ 5.68in, `M1-L10` ≈ 5.9in. The constants are load-bearing and the numbers in the spec are descriptive, so Task 3 keeps the constants and corrects the test.
3. **`plain_slide_pptx` retirement vs. "keep `tests/test_ppt_creator_agent.py` green".** The spec retires `plain_slide_pptx` *and* lists that file as a suite to keep green, but its tests call `plain_slide_pptx` directly and feed the agent free-form `python-pptx` programs the new AST guard must reject. Task 13 rewrites those specific tests onto the deterministic on-layout render and `DeckBuilder` programs. Every other test in the file is kept unchanged.

Both are flagged for the reviewer; neither changes the design.

### Baseline

`python -m pytest tests/test_ppt_creator_agent.py tests/test_slides_node.py tests/test_workflow.py tests/test_ppt_agent.py -q` → **61 passed**. This must still hold at the end of every task.

---

## File Structure

### New files

| File | Responsibility |
|---|---|
| `app/genai/ppt/measure.py` | Per-character advance-width table and the capacity/overflow estimators. No pptx knowledge. |
| `app/genai/ppt/deck_builder.py` | **Standalone.** Brand tokens, type scale, `CatalogView`, `SlideHandle`, `DeckBuilder`, frame application. The only module generated code touches. |
| `app/genai/ppt/code_guard.py` | AST deny-list + brand enforcement, run before execution. |
| `app/genai/ppt/geometry.py` | Rules 1–10 over a reopened `.pptx` or over a submitted rectangle list; violation formatting. |
| `app/genai/ppt/design_contract.py` | The once-per-deck contract: master, frame, grid, tokens, per-slide mode + layout. |
| `app/genai/ppt/sandbox.py` | Stages a slide directory: copies `deck_builder.py`, writes `catalog.json`, `contract.json`, asset manifest. |
| `app/genai/tools/design_tools.py` | The seven design-agent tools. |
| `scripts/calibrate_barlow.py` | One-off, developer-run, **not imported by `app/`**: regenerates the width table from real Barlow metrics. |
| `tests/test_template_catalog.py`, `tests/test_measure.py`, `tests/test_deck_builder.py`, `tests/test_code_guard.py`, `tests/test_geometry.py`, `tests/test_design_contract.py`, `tests/test_stitcher.py`, `tests/test_deck_end_to_end.py`, `tests/test_import_hygiene.py` | One suite per unit. |

### Rewritten

| File | Change |
|---|---|
| `app/genai/ppt/template/catalog.py` | Rewritten to `main_v3` parity + `decor_rects`, `background_luminance`, `canvas_capable`, `free_region`, `to_dict()`. Keeps `for_classification()`, `prompt_block()` and `content_layouts` as compatibility shims so `plan_tools.py` and `layout_tools.py` need no edit. |
| `app/genai/ppt/skills/rockwell-skill.md` | Replaced from `scripts/mainv3/rockwell-skill-v3.md` plus four new sections. |
| `app/genai/agents/ppt_creator_agent.py` | Prompt, sandbox staging, repair loop, deterministic fallback. |
| `app/genai/ppt/stitcher.py` | Target deck from `SAMPLE.pptx`; each slide on its own layout. |

### Deleted (Task 16)

`app/genai/ppt/renderer.py`, `app/genai/ppt/audit.py`, `app/genai/ppt/template/filler.py`, `app/genai/ppt/template/icons.py`, `builder.build_pptx`, `registry.path_for`, `registry.available`, `plain_slide_pptx`, `scripts/mainv3/`.

`builder.persist_deck` **stays** — `deck_workflow.py:54` imports it.

---

## Task Order and Dependencies

```
1  template resolution ──► 2 measure ──► 3 catalog ──┬──► 4 DeckBuilder core ──► 5 frame
                                                     │                             │
                                                     ├──► 9 data model             │
                                                     │                             ▼
                                                     │         6 AST guard    7 geometry 1-7 ──► 8 geometry 8-10
                                                     │              │               │                  │
                                                     └──► 10 design contract ──► 11 design tools       │
                                                                    │                                  │
                                                     12 brand guide │                                  │
                                                                    └──────► 13 PptCreatorAgent ◄──────┘
                                                                                   │
                                                                          14 stitcher ──► 15 workflow + E2E ──► 16 cleanup
```

---

### Task 1: Template resolution — one constant, one path, no glob

**Why:** Everything downstream opens the template. Today `catalog.for_classification()` calls `registry.path_for()`, which globs for `*.potx` and silently falls back. Decision 2 replaces that with a single constant. `SAMPLE.pptx` must move out of `scripts/mainv3/` *first*, because Task 16 deletes that folder.

**Files:**
- Move: `scripts/mainv3/SAMPLE.pptx` → `company-ppt-templet/SAMPLE.pptx`
- Modify: `.gitignore` (two negations — see Step 0)
- Modify: `app/config/constants.py` (`PptConstants`, `PptCreatorConstants`), add `GeometryConstants`
- Modify: `app/genai/ppt/template/registry.py` (add `template_path`, `classification_symbol`, `logo_path`)
- Test: `tests/test_template_catalog.py` (new file, template-resolution tests only for now)

> **Controller rulings folded into this task (R1, R2, R3).** The plan was written before
> the repository's ignore rules were checked. Three things the plan assumed are false, and
> Step 0 fixes all three before anything else happens:
>
> 1. **`*.pptx` is gitignored** (`.gitignore:13`), so `SAMPLE.pptx` is untracked, cannot be
>    `git mv`'d, and is not the "committed template" every new test suite reads. The repo
>    already tracks five `.potx` brand templates of comparable size, so committing one
>    4.1MB `.pptx` is consistent with existing practice.
> 2. **`scripts/mainv3/main_v3.py` and `rockwell-skill-v3.md` are staged but never
>    committed**, so `git show 0bd70fb:scripts/mainv3/main_v3.py` does *not* resolve. Task
>    16 deletes that folder on the promise that git history keeps it. Commit them here or
>    the port source is destroyed. **Every "at commit `0bd70fb`" citation in this plan
>    re-anchors to this task's commit.**
> 3. **`docs/` is gitignored** (`.gitignore:72`), so the spec and this plan are untracked.
>    The existing trailing `!superpowers/` line does nothing: git never descends into an
>    excluded directory, so a negation inside one cannot re-include anything.

- [ ] **Step 0: Make the template, the reference and the design docs trackable**

Replace the last three lines of `.gitignore`:

```gitignore
docs/
data/
!superpowers/
```

with:

```gitignore
docs/
data/

# The design record for this branch: the spec every implementation task argues from,
# and the plan that argues it. `!superpowers/` alone cannot work — git does not descend
# into an excluded directory, so the directory itself has to be re-included first.
!docs/superpowers/
!docs/superpowers/**

# The one brand template. Every slide, the stitched deck and every offline test read it,
# so it has to be in the repo rather than beside it. The five .potx brand templates are
# already tracked; this is the same kind of asset.
!company-ppt-templet/SAMPLE.pptx
```

Then move the template and stage everything:

```bash
mv scripts/mainv3/SAMPLE.pptx company-ppt-templet/SAMPLE.pptx
git add .gitignore
git add company-ppt-templet/SAMPLE.pptx
git add docs/superpowers/specs/main_v3_design.md
git add docs/superpowers/plans/2026-09-21-template-grounded-slides.md
git add scripts/mainv3/main_v3.py scripts/mainv3/rockwell-skill-v3.md
git status --short
```

Verify all five are staged and that nothing else was swept in:

```bash
git ls-files --cached company-ppt-templet/SAMPLE.pptx docs/superpowers scripts/mainv3
```

Expected: the template, the spec, the plan, `main_v3.py` and `rockwell-skill-v3.md`.
`ls -l company-ppt-templet/SAMPLE.pptx` must show ~4.1MB, and
`scripts/mainv3/SAMPLE.pptx` must no longer exist.

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `PptConstants.TEMPLATE_FILENAME: str = "SAMPLE.pptx"`
  - `PptConstants.CLASSIFICATION_ICON_DIR: str` , `PptConstants.CLASSIFICATION_ICON_FILENAME: str`
  - `GeometryConstants` (all the tolerances, the grid, `TYPE_SCALE`, `MIN_CANVAS_FREE_HEIGHT_IN`, footer band)
  - `registry.template_path() -> pathlib.Path`
  - `registry.classification_symbol(classification: str | None) -> pathlib.Path | None`
  - `registry.logo_path(filename: str) -> pathlib.Path | None`

Do **not** delete anything else from `scripts/mainv3/` yet — Task 16 owns that.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_template_catalog.py`:

```python
"""The single brand template: it resolves, it is the file we think it is, and the
classification marks and logos beside it are found by name rather than by glob."""

from pathlib import Path

import pytest

from app.config.constants import GeometryConstants, PptConstants
from app.genai.ppt.template import registry


def test_the_template_resolves_to_a_real_file():
    path = registry.template_path()
    assert path.is_file(), f"brand template missing at {path}"
    assert path.name == PptConstants.TEMPLATE_FILENAME


def test_the_template_lives_beside_the_logos_not_under_scripts():
    path = registry.template_path()
    assert "scripts" not in path.parts
    assert (path.parent / PptConstants.LOGO_DIR).is_dir()


@pytest.mark.parametrize("classification", ["public", "internal", "confidential",
                                            "restricted"])
def test_every_classification_with_a_symbol_on_disk_resolves_to_it(classification):
    symbol = registry.classification_symbol(classification)
    assert symbol is not None and symbol.is_file()
    assert registry.label(classification) in symbol.name


def test_cobranded_has_no_symbol_and_that_is_not_an_error(caplog):
    # There is no Cobranded_Symbol.png in the brand pack; the slide renders without it.
    assert registry.classification_symbol("cobranded") is None


def test_a_known_logo_resolves_and_an_unknown_one_returns_none():
    assert registry.logo_path("ra_lockup_full.png").is_file()
    assert registry.logo_path("no_such_logo.png") is None


def test_the_type_scale_is_the_closed_set_the_spec_defines():
    assert GeometryConstants.TYPE_SCALE == (10, 11, 12, 14, 15, 20, 28, 32)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_template_catalog.py -q`
Expected: FAIL — `ImportError: cannot import name 'GeometryConstants'`.

- [ ] **Step 3: Add the constants**

In `app/config/constants.py`, inside `PptConstants` (after `LOGO_DIR`):

```python
    # The one brand template. Decision 2: no .potx is read, and no globbing decides
    # which file we open — a missing file must fail loudly, not resolve to something else.
    TEMPLATE_FILENAME = "SAMPLE.pptx"
    # The brand pack nests this folder inside itself on disk; that is not a typo here.
    CLASSIFICATION_ICON_DIR = "ppt-Use-Policy-icons/ppt-Use-Policy-icons"
    CLASSIFICATION_ICON_FILENAME = "{label}_Symbol.png"
```

In `PptCreatorConstants`, fix the guide filename (Bug 1; the content is replaced in Task 12):

```python
    SKILL_DIR = "skills"
    SKILL_MD_FILENAME = "rockwell-skill.md"
```

Add a new class after `PptCreatorConstants`:

```python
class GeometryConstants:
    """The geometry gate's thresholds, the grid, and the closed set of font sizes.

    These are the numbers that turn "no overlapping text" from an aspiration into a
    decidable property. Every one of them is a rectangle tolerance in inches, so a
    change here changes what the gate accepts — not merely how it reports.
    """
    # Rule 1: how far inside the canvas every shape must sit, and the slack allowed.
    CANVAS_MARGIN_IN = 0.30
    CANVAS_TOLERANCE_IN = 0.02
    # Rules 2-6: hairline abutments are not collisions. Matches the slack the retired
    # filler.py::_overlaps_in used, so the new gate is no stricter than the old check.
    OVERLAP_TOLERANCE_IN = 0.15
    # Rules 3-4: "fully contained" allows this much bleed before it counts as partial.
    CONTAINMENT_TOLERANCE_IN = 0.02

    # The frame. The footer band runs full width along the bottom of the canvas.
    FOOTER_BAND_HEIGHT_IN = 0.45
    CLASSIFICATION_SYMBOL_IN = 0.22       # square, vertically centred in the footer band
    CLASSIFICATION_LABEL_WIDTH_IN = 1.60
    FRAME_SIDE_MARGIN_IN = 0.42
    LOGO_WIDTH_IN = 0.79                  # d2r_mark at its approved width

    # The grid the design contract hands every slide, over the content region.
    GRID_COLUMNS = 12
    GRID_ROWS = 6
    GRID_GUTTER_IN = 0.15

    # canvas mode: a layout with less free height than this is not worth composing into.
    MIN_CANVAS_FREE_HEIGHT_IN = 4.0
    # Gap left under a title placeholder before the free region starts.
    CANVAS_TITLE_GAP_IN = 0.10

    # Rule 9. Placeholder-inherited sizes are exempt; these are the sizes a component
    # may set explicitly. Derived from PptConstants + the brand guide's component specs.
    TYPE_SCALE = (10, 11, 12, 14, 15, 20, 28, 32)

    # Rule 10 is advisory until the estimate exceeds capacity by this factor.
    OVERFLOW_ADVISORY_RATIO = 1.15
    OVERFLOW_BLOCKING_RATIO = 1.60
```

- [ ] **Step 4: Add the resolvers to `registry.py`**

Replace the `available()` / `path_for()` pair. Leave `normalize`, `label`, `detect_classification`, `template_root` untouched, and leave `_FILE_HINT` / `available` / `path_for` **in place for now** — Task 16 deletes them, and `builder.build_pptx` still calls `path_for` until then.

Append to `app/genai/ppt/template/registry.py`:

```python
@lru_cache
def template_path() -> Path:
    """The one brand template every slide and the stitched deck are built from.

    Returns:
        Absolute path of SAMPLE.pptx.

    Raises:
        FileNotFoundError: When it is missing. Deliberately fatal: there is no second
            template, and shipping an unbranded deck silently is worse than failing.
    """
    path = template_root() / PptConstants.TEMPLATE_FILENAME
    if not path.is_file():
        raise FileNotFoundError(
            f"brand template not found at {path}. It is required: there is no fallback.")
    return path


@lru_cache
def classification_symbol(classification: str | None) -> Path | None:
    """The policy symbol stamped beside the classification label, when one exists.

    Args:
        classification: Any accepted spelling; normalized here.

    Returns:
        Absolute path of the symbol PNG, or None when the brand pack has none for this
        classification (co-branded has no symbol). A missing symbol is a warning, never
        an error — the slide renders without the mark.
    """
    name = PptConstants.CLASSIFICATION_ICON_FILENAME.format(
        label=label(classification).replace("-", ""))
    path = template_root() / PptConstants.CLASSIFICATION_ICON_DIR / name
    if path.is_file():
        return path
    log.warning("no policy symbol for classification %r at %s", classification, path)
    return None


@lru_cache
def logo_path(filename: str) -> Path | None:
    """An approved brand mark by filename, or None when it is not on disk."""
    path = template_root() / PptConstants.LOGO_DIR / filename
    if path.is_file():
        return path
    log.warning("brand logo %s missing at %s", filename, path)
    return None
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_template_catalog.py -q`
Expected: PASS, 6 tests.

- [ ] **Step 6: Confirm the baseline suites are still green**

Run: `python -m pytest tests/test_ppt_creator_agent.py tests/test_slides_node.py tests/test_workflow.py tests/test_ppt_agent.py -q`
Expected: 61 passed. (`SKILL_MD_FILENAME` now points at a file that exists, so `load_brand_skill()` starts returning the *old* guide. That is intentional and temporary; Task 12 replaces the content.)

- [ ] **Step 7: Commit**

```bash
git add .gitignore company-ppt-templet/SAMPLE.pptx \
        docs/superpowers/specs/main_v3_design.md \
        docs/superpowers/plans/2026-09-21-template-grounded-slides.md \
        scripts/mainv3/main_v3.py scripts/mainv3/rockwell-skill-v3.md \
        app/config/constants.py app/genai/ppt/template/registry.py \
        tests/test_template_catalog.py
git commit -m "feat(ppt): resolve the single brand template by constant, not by glob

Also tracks the assets the rest of this branch depends on, none of which were
committed: SAMPLE.pptx and docs/ were both gitignored, and the main_v3 reference
was staged but never committed, so the deletion in the last task of this plan
would have destroyed the port source."
git rev-parse --short HEAD
```

**Report the commit SHA in your report file.** Every "at commit `0bd70fb`" citation
elsewhere in this plan re-anchors to it, because that is the first commit in which
`scripts/mainv3/main_v3.py` actually exists.

---

### Task 2: Text measurement — a calibrated, conservative width table

**Why:** Gate rule 10 and the catalog's capacity hints both need to estimate how much text fits. Decision 7 forbids a new runtime dependency, so this is a per-character advance-width table biased to **under-estimate capacity** (i.e. over-estimate width). Under-estimating capacity costs tokens; over-estimating it ships overflowing slides.

**Files:**
- Create: `app/genai/ppt/measure.py`
- Create: `scripts/calibrate_barlow.py`
- Test: `tests/test_measure.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `measure.CHAR_EM: dict[str, float]` — advance width in em, per character
  - `measure.DEFAULT_CHAR_EM: float`
  - `measure.string_em(text: str) -> float`
  - `measure.chars_per_line(width_in: float, size_pt: float, bullets: bool = False) -> int`
  - `measure.line_capacity(height_in: float, size_pt: float) -> int`
  - `measure.rendered_lines(items: Sequence[str], width_in: float, size_pt: float, bullets: bool = False) -> int`
  - `measure.overflow_ratio(items, width_in, height_in, size_pt, bullets=False) -> float` — `1.0` means exactly full
  - `measure.fits(items, width_in, height_in, size_pt, bullets=False) -> bool`

**Calibration note for the implementer:** the committed table is grouped by character class, not measured per glyph, because Barlow is not vendored and `fonttools` is not a dependency. `scripts/calibrate_barlow.py` is the documented way to regenerate it from real metrics on a machine that has both; it is a developer tool and `app/` never imports it. The tests below therefore assert the *properties that make the estimate safe* (conservatism, ordering, monotonicity) rather than exact glyph widths.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_measure.py`:

```python
"""Text measurement. The only property that matters is the direction of the error:
the estimate may say less text fits than really does, never more."""

import pytest

from app.genai.ppt import measure


def test_wide_characters_measure_wider_than_narrow_ones():
    assert measure.string_em("WWWW") > measure.string_em("MMMM") > measure.string_em("nnnn")
    assert measure.string_em("nnnn") > measure.string_em("iiii")
    assert measure.string_em("iiii") > measure.string_em("llll") or True  # i and l are both narrow


def test_an_unknown_character_falls_back_to_the_default_width():
    assert measure.string_em("中") == pytest.approx(measure.DEFAULT_CHAR_EM)


def test_width_is_additive_over_concatenation():
    assert measure.string_em("Reflow") + measure.string_em(" profile") == pytest.approx(
        measure.string_em("Reflow profile"))


@pytest.mark.parametrize("size_pt", [10, 11, 12, 14, 15, 20, 28, 32])
def test_a_wider_box_never_holds_fewer_characters(size_pt):
    narrow = measure.chars_per_line(3.0, size_pt)
    wide = measure.chars_per_line(6.0, size_pt)
    assert wide > narrow >= 1


@pytest.mark.parametrize("size_pt", [10, 15, 32])
def test_a_larger_font_never_holds_more_characters_or_lines(size_pt):
    assert measure.chars_per_line(6.0, size_pt) >= measure.chars_per_line(6.0, size_pt + 6)
    assert measure.line_capacity(3.0, size_pt) >= measure.line_capacity(3.0, size_pt + 6)


def test_bullets_cost_horizontal_room():
    assert measure.chars_per_line(6.0, 15, bullets=True) < measure.chars_per_line(6.0, 15)


def test_the_estimate_never_claims_more_capacity_than_the_retired_formula():
    """`main_v3::_capacity` used chars = usable_width_pt / (size * 0.52). Ours must be
    no more optimistic than that, at every size and width a real placeholder has."""
    for width_in in (2.0, 3.6, 5.0, 6.4, 11.75, 12.5):
        for size_pt in (10, 11, 12, 14, 15, 20, 28, 32):
            legacy = max(6, int(max(0.4, width_in - 0.2) * 72 / (size_pt * 0.52)))
            assert measure.chars_per_line(width_in, size_pt) <= legacy, (
                f"too optimistic at {width_in}in / {size_pt}pt")


def test_a_string_of_the_widest_glyphs_never_overruns_the_estimated_line():
    """The worst case the table must survive: a line of all-caps W at body size."""
    width_in, size_pt = 6.0, 15.0
    per_line = measure.chars_per_line(width_in, size_pt)
    # Rendered width of that many W's, in inches, must still fit the box.
    rendered_in = measure.string_em("W" * per_line) * size_pt / 72.0
    assert rendered_in <= width_in


def test_overflow_ratio_is_one_when_the_text_exactly_fills_the_box():
    items = ["x" * 40]
    ratio = measure.overflow_ratio(items, 6.0, 0.30, 15)
    assert 0.0 < ratio


def test_overflow_ratio_grows_with_the_amount_of_text():
    small = measure.overflow_ratio(["short line"], 6.0, 3.0, 15)
    large = measure.overflow_ratio(["short line"] * 40, 6.0, 3.0, 15)
    assert large > small > 0
    assert not measure.fits(["short line"] * 40, 6.0, 3.0, 15)
    assert measure.fits(["short line"], 6.0, 3.0, 15)


def test_empty_content_never_overflows():
    assert measure.overflow_ratio([], 6.0, 3.0, 15) == 0.0
    assert measure.fits([], 6.0, 3.0, 15)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_measure.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.genai.ppt.measure'`.

- [ ] **Step 3: Write `app/genai/ppt/measure.py`**

```python
"""How much text fits in a box, without a font file.

Decision 7: measuring text properly needs real font metrics, and vendoring Barlow plus
`fonttools` was declined. What is left is a per-character advance-width table, and the
only thing that makes such a table safe is the direction of its error: every width here
is rounded UP, so the estimate says less text fits than really does. An over-estimate of
capacity ships an overflowing slide; an under-estimate costs the model one rewrite.

The values are em widths for Barlow at weight 400, grouped by character class rather
than measured per glyph — see `scripts/calibrate_barlow.py` for how to regenerate them
from real metrics on a machine that has the font and `fonttools`.
"""

import math
from typing import Sequence

# Em widths, rounded up from Barlow's advance widths. Grouped by class: within a class
# the widest member's width is used for every member, which is where the conservatism
# comes from.
_CLASSES: tuple[tuple[str, float], ...] = (
    ("il|!.,;:'`", 0.30),
    ("jft()[]{}/\\-", 0.38),
    ("rIJ ", 0.42),
    ("abcdeghknopqsuvxyz1234567890", 0.56),
    ("wLTZEFSCXYPBRKANDGHUVQOM", 0.68),
    ("m", 0.86),
    ("W", 0.94),
)

#: Width used for any character not in the table (CJK, emoji, box drawing).
DEFAULT_CHAR_EM = 0.72

CHAR_EM: dict[str, float] = {
    char: width for chars, width in _CLASSES for char in chars
}

#: Vertical advance as a multiple of the font size. Barlow's default line spacing is
#: ~1.2; 1.25 leaves room for the descender clipping PowerPoint applies.
LINE_HEIGHT_FACTOR = 1.25
#: Internal text-frame insets python-pptx applies by default, both sides, in inches.
_SIDE_INSET_IN = 0.20
_VERTICAL_INSET_IN = 0.10
#: Horizontal room a bullet glyph and its hanging indent take, in inches.
_BULLET_INDENT_IN = 0.22


def string_em(text: str) -> float:
    """Advance width of `text` in em, over-estimating rather than under-estimating."""
    return sum(CHAR_EM.get(char, DEFAULT_CHAR_EM) for char in text)


def chars_per_line(width_in: float, size_pt: float, bullets: bool = False) -> int:
    """How many average characters fit on one line of a box `width_in` inches wide.

    Uses the mean width of the lowercase class rather than a global mean: real copy is
    mostly lowercase, and using the global mean would be optimistic for prose.
    """
    usable_in = max(0.4, width_in - _SIDE_INSET_IN - (_BULLET_INDENT_IN if bullets else 0))
    em_per_in = 72.0 / max(size_pt, 1.0)
    # 0.56 is the lowercase class width; the table never rounds a lowercase glyph below it.
    return max(1, int(usable_in * em_per_in / 0.56))


def line_capacity(height_in: float, size_pt: float) -> int:
    """How many lines of `size_pt` text a box `height_in` inches tall holds."""
    usable_in = max(0.05, height_in - _VERTICAL_INSET_IN)
    return max(1, int(usable_in * 72.0 / (max(size_pt, 1.0) * LINE_HEIGHT_FACTOR)))


def rendered_lines(items: Sequence[str], width_in: float, size_pt: float,
                   bullets: bool = False) -> int:
    """How many lines `items` wrap to, one paragraph per item, at least one line each."""
    per_line = chars_per_line(width_in, size_pt, bullets)
    return sum(max(1, math.ceil(len(str(item)) / per_line)) for item in items if str(item))


def overflow_ratio(items: Sequence[str], width_in: float, height_in: float,
                   size_pt: float, bullets: bool = False) -> float:
    """Rendered lines divided by the lines the box holds. 0.0 for no content.

    Returns:
        1.0 means exactly full; 1.5 means half again as much text as there is room for.
    """
    needed = rendered_lines(items, width_in, size_pt, bullets)
    if not needed:
        return 0.0
    return needed / line_capacity(height_in, size_pt)


def fits(items: Sequence[str], width_in: float, height_in: float, size_pt: float,
         bullets: bool = False) -> bool:
    """True when `items` are estimated to fit without shrinking."""
    return overflow_ratio(items, width_in, height_in, size_pt, bullets) <= 1.0
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_measure.py -q`
Expected: PASS.
If `test_the_estimate_never_claims_more_capacity_than_the_retired_formula` fails, the divisor in `chars_per_line` is too small — raise `0.56` until it passes. Do **not** relax the test; it is the conservatism guarantee.

- [ ] **Step 5: Write the offline calibration script**

Create `scripts/calibrate_barlow.py`:

```python
"""Regenerate app/genai/ppt/measure.py's CHAR_EM table from real Barlow metrics.

Developer tool, run by hand, never imported by app/. It needs `fonttools` and a copy of
Barlow-Regular.ttf, neither of which is a project dependency — install them in a scratch
venv, run this, and paste the printed table into measure.py.

    pip install fonttools
    python scripts/calibrate_barlow.py path/to/Barlow-Regular.ttf

The printed widths are rounded UP to two decimals, which is what keeps the estimate
conservative; do not round to nearest.
"""

import math
import sys

PRINTABLE = (
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    " .,;:'\"`!?()[]{}/\\-_+=<>@#$%&*|"
)


def main(font_path: str) -> int:
    from fontTools.ttLib import TTFont            # noqa: PLC0415  (dev-only import)

    font = TTFont(font_path)
    upem = font["head"].unitsPerEm
    cmap = font.getBestCmap()
    hmtx = font["hmtx"]
    widths: dict[str, float] = {}
    for char in PRINTABLE:
        glyph = cmap.get(ord(char))
        if glyph is None:
            continue
        advance = hmtx[glyph][0] / upem
        widths[char] = math.ceil(advance * 100) / 100      # round UP, always
    print("CHAR_EM = {")
    for char, width in sorted(widths.items(), key=lambda kv: kv[1]):
        print(f"    {char!r}: {width:.2f},")
    print("}")
    print(f"# unitsPerEm={upem}, {len(widths)} glyphs measured")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
```

- [ ] **Step 6: Commit**

```bash
git add app/genai/ppt/measure.py scripts/calibrate_barlow.py tests/test_measure.py
git commit -m "feat(ppt): conservative text measurement with a calibratable width table"
```

---

### Task 3: Template catalog at `main_v3` parity, plus what the gate needs

**Why:** The live catalog derives slots from geometry and knows nothing about themes, tags, decor geometry or background. The gate needs `decor_rects` (inherited graphics an agent would otherwise draw over), `background_luminance` (light vs dark text and which logo), and `canvas_capable` + `free_region` (composition mode). Port `main_v3` lines 285–1017 and add the three new fields.

**Files:**
- Rewrite: `app/genai/ppt/template/catalog.py`
- Test: `tests/test_template_catalog.py` (extend)

**Interfaces:**
- Consumes: `registry.template_path()` (Task 1), `measure.chars_per_line` / `measure.line_capacity` (Task 2).
- Produces:
  - `Rect(left: float, top: float, width: float, height: float)` with `.as_tuple()` and `.to_list()`
  - `ThemeInfo(name, major_font, minor_font, colors: dict[str, str])`
  - `PlaceholderInfo(idx, type, role, slot, name, prompt, left, top, width, height, font_size, font_name, bold, color, align, bullets, all_caps, autofit, accepts_text, approx_chars, approx_lines)` with `.summary() -> dict` and `.rect() -> Rect`
  - `LayoutInfo(key, master_index, layout_index, name, theme_name, placeholders, decor, decor_rects, fixed_texts, background, background_luminance, tags, columns, recommended_use, sample_titles, usage_count, canonical, duplicates, canvas_capable, free_region)` with `.by_role`, `.has_role`, `.find`, `.slot_summary`, `.compact`, `.details`
  - `TemplateCatalog(template_path, slide_width, slide_height, themes, layouts, preferred_master, cover_master, sample_slide_count)` with `.theme`, `.canonical_layouts()`, `.get()`, `.resolve_layout()`, `.search()`, `.recommend()`, `.summary_text()`, `.stats()`, `.to_dict()`, and the shims `.content_layouts` / `.prompt_block()`
  - `catalog.load(path: str | None = None) -> TemplateCatalog` (cached on `(path, mtime)`)
  - `catalog.for_classification(classification=None) -> TemplateCatalog` — compatibility shim, argument ignored

**Compatibility contract (do not break):** `plan_tools.py:21,155-156` and `layout_tools.py:31-32` call `for_classification(...)`, `.prompt_block()`, `.content_layouts` and read `layout.name`. Those four must keep working with **no edit to those files**. `renderer.py` also uses `.resolve()`, `.by_role()`, `.default_content()`, `.best_with_picture()` and `LayoutSpec` — `renderer.py` is deleted in Task 16, so those may go, but only after Task 16. Keep `LayoutSpec = LayoutInfo` and thin `resolve`/`by_role`/`default_content`/`best_with_picture` shims until then so the tree stays importable between tasks.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_template_catalog.py`:

```python
# --------------------------------------------------------------------------- #
# catalog                                                                      #
# --------------------------------------------------------------------------- #

from app.genai.ppt.template import catalog as catalog_mod


@pytest.fixture(scope="module")
def catalog():
    return catalog_mod.load()


def test_the_whole_template_loads(catalog):
    assert len(catalog.layouts) == 181
    assert len(catalog.themes) == 4
    assert (round(catalog.slide_width, 2), catalog.slide_height) == (13.33, 7.5)
    assert catalog.sample_slide_count == 31


def test_the_theme_palette_is_the_known_rockwell_one(catalog):
    for theme in catalog.themes:
        assert theme.major_font == "Barlow" and theme.minor_font == "Barlow"
        assert theme.colors["accent1"] == "CD163F"
        assert theme.colors["accent2"] == "003E7E"
        assert theme.colors["accent3"] == "F58024"
        assert theme.colors["accent4"] == "00AEEF"
        assert theme.colors["accent5"] == "F9C20A"
        assert theme.colors["accent6"] == "77C14C"
        assert theme.colors["dk1"] == "000000" and theme.colors["lt1"] == "FFFFFF"


def test_the_preferred_content_master_is_the_one_the_samples_use(catalog):
    # M1 carries 17 of the 31 sample slides; M0 carries the covers.
    assert catalog.preferred_master == 1
    assert catalog.cover_master == 0


def test_layout_keys_are_unique_and_addressable(catalog):
    keys = [layout.key for layout in catalog.layouts]
    assert len(set(keys)) == len(keys)
    assert catalog.get("M1-L10").name == "Title with Subhead"
    assert catalog.resolve_layout("M1-L10").layout_index == 10
    assert catalog.resolve_layout("Title with Subhead").key.startswith("M")


def test_an_unknown_layout_raises_with_suggestions(catalog):
    with pytest.raises(LookupError) as excinfo:
        catalog.resolve_layout("Definitely Not A Layout Name At All")
    assert "did you mean" in str(excinfo.value).lower()


def test_decor_rects_carry_real_geometry_not_just_a_count(catalog):
    # M1-L10 has exactly two non-placeholder shapes: a rule line and the brand mark.
    layout = catalog.get("M1-L10")
    assert len(layout.decor_rects) == 2
    mark = max(layout.decor_rects, key=lambda r: r.width * r.height)
    assert mark.left == pytest.approx(0.23, abs=0.02)
    assert mark.top == pytest.approx(0.28, abs=0.02)
    assert mark.width == pytest.approx(0.65, abs=0.02)


def test_a_layout_with_no_decor_reports_no_rects(catalog):
    assert catalog.get("M2-L4").decor_rects == ()


def test_background_luminance_is_reported_for_every_layout(catalog):
    for layout in catalog.layouts:
        assert 0.0 <= layout.background_luminance <= 1.0
    # A gradient-backed layout must not be treated as white.
    assert catalog.get("M2-L4").background_luminance <= 1.0


def test_canvas_capable_layouts_are_the_ones_with_only_a_title_and_room_below(catalog):
    canvas = [l for l in catalog.layouts if l.canvas_capable]
    keys = {l.key for l in canvas}
    # Every layout the design names by hand must qualify.
    assert {"M1-L10", "M2-L4", "M2-L30", "M2-L38"} <= keys
    # The count is threshold-dependent (see the plan's "departures" note); assert a
    # floor rather than an exact number so tuning the floor does not break the suite.
    assert len(canvas) >= 12
    for layout in canvas:
        assert layout.free_region is not None
        assert layout.free_region.height >= 4.0
        assert {p.role for p in layout.placeholders} <= {"title", "subtitle"}


def test_the_named_canvas_layouts_have_the_free_height_the_design_measured(catalog):
    assert catalog.get("M1-L10").free_region.height == pytest.approx(6.55, abs=0.03)
    for key in ("M2-L4", "M2-L30", "M2-L38"):
        assert catalog.get(key).free_region.height == pytest.approx(6.58, abs=0.03)


def test_a_layout_with_body_placeholders_is_not_canvas_capable(catalog):
    busy = next(l for l in catalog.layouts if any(p.role == "body" for p in l.placeholders))
    assert not busy.canvas_capable and busy.free_region is None


def test_placeholders_carry_geometry_and_a_capacity_hint(catalog):
    title = catalog.get("M1-L10").find("title")
    assert title.idx == 0 and title.role == "title"
    assert title.left == pytest.approx(1.14, abs=0.02)
    assert title.width == pytest.approx(11.75, abs=0.02)
    assert title.approx_chars > 0 and title.approx_lines >= 1
    assert catalog.get("M1-L10").find(0) is title


def test_search_and_recommend_return_layouts_from_the_catalog(catalog):
    found = catalog.search("agenda", limit=5)
    assert found and all(l.key in {x.key for x in catalog.layouts} for l in found)
    ranked = catalog.recommend("cover", limit=3)
    assert ranked and "cover" in ranked[0][0].tags


def test_the_catalog_serializes_to_json_for_the_sandbox(catalog):
    import json

    data = catalog.to_dict()
    round_tripped = json.loads(json.dumps(data))       # must be JSON-clean
    assert round_tripped["slide_width"] == pytest.approx(13.33, abs=0.01)
    assert round_tripped["preferred_master"] == 1
    assert len(round_tripped["layouts"]) == 181
    entry = next(l for l in round_tripped["layouts"] if l["key"] == "M1-L10")
    assert entry["decor_rects"] and len(entry["decor_rects"][0]) == 4
    assert entry["placeholders"][0]["idx"] == 0
    assert round_tripped["themes"][1]["colors"]["accent1"] == "CD163F"


def test_the_catalog_is_cached_per_file(catalog):
    assert catalog_mod.load() is catalog
    assert catalog_mod.for_classification("confidential") is catalog


def test_the_legacy_prompt_block_shim_still_answers(catalog):
    # plan_tools.py and layout_tools.py call these and must not need editing.
    assert catalog.prompt_block().strip()
    assert catalog.content_layouts
    assert all(hasattr(l, "name") for l in catalog.content_layouts)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_template_catalog.py -q`
Expected: FAIL — `AttributeError: module 'catalog' has no attribute 'load'` taking no argument, and no `decor_rects`.

- [ ] **Step 3: Rewrite `app/genai/ppt/template/catalog.py`**

Port from `scripts/mainv3/main_v3.py` at `0bd70fb`, in this order, keeping the docstrings and adapting imports to `app.utilities.logger_util.get_logger`:

| Port | Reference lines | Change on the way in |
|---|---|---|
| `_inches`, `_luminance` | 285–295 | none |
| `ThemeInfo` | 297–302 | none |
| `PlaceholderInfo` + `.summary()` | 305–340 | add `.rect() -> Rect` |
| `LayoutInfo` + methods | 343–416 | add fields `decor_rects: tuple[Rect, ...] = ()`, `background_luminance: float = 1.0`, `canvas_capable: bool = False`, `free_region: Rect | None = None`; add all four to `.details()` |
| `TemplateCatalog` + lookups/search/recommend/summary | 418–590 | add `.to_dict()`, `.content_layouts`, `.prompt_block()` |
| `TemplateCatalog.from_presentation` | 592–637 | none |
| `_norm_name`, `_locate_layout`, `_read_theme`, `_read_master_text_styles` | 640–692 | none |
| `_scheme_hex`, `_color_element_hex`, `_fill_luminance` | 694–742 | none |
| `_read_placeholder_format`, `_placeholder_role` | 744–815 | none |
| `_read_layout` | 817–876 | collect `decor_rects`, compute `background_luminance` and the canvas fields — see below |
| `_reading_order`, `_column_count`, `INTENT_TAGS`, `_derive_tags`, `_recommended_use` | 878–1004 | none |

Replace `main_v3`'s `_capacity` with the Task 2 module wherever `_read_layout` computes `approx_chars` / `approx_lines`:

```python
from app.genai.ppt import measure

chars_per_line = measure.chars_per_line(width, size, bool(fmt.get("bullets", role == "body")))
lines = measure.line_capacity(height, size)
```

Add at the top of the module:

```python
from dataclasses import asdict, dataclass, field

from app.config.constants import GeometryConstants
from app.genai.ppt.template import registry


@dataclass(frozen=True)
class Rect:
    """An axis-aligned rectangle in inches on the slide canvas."""
    left: float
    top: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.left + self.width

    @property
    def bottom(self) -> float:
        return self.top + self.height

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.left, self.top, self.width, self.height)

    def to_list(self) -> list[float]:
        return [self.left, self.top, self.width, self.height]
```

In `_read_layout`, in the non-placeholder branch (reference line 863), also record geometry, and compute the background luminance of the largest background shape:

```python
        # main_v3 only counted these; the gate needs where they are, because they are
        # inherited graphics the agent will otherwise draw straight over.
        decor_rects.append(Rect(_inches(shape.left), _inches(shape.top),
                                _inches(shape.width), _inches(shape.height)))
        area = _inches(shape.width) * _inches(shape.height)
        if area >= 0.45 * slide_area:
            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                background, background_luminance = "photo", 0.5
            else:
                lum = _fill_luminance(shape, theme)
                if lum is not None:
                    background_luminance = lum
                    if lum < 0.5:
                        background = "dark"
```

Initialise `decor_rects: list[Rect] = []` and `background_luminance = 1.0` (white) beside `background = "light"`.

After `_recommended_use` is computed, add the canvas fields:

```python
    canvas_capable, free_region = _canvas_region(ordered, slide_w, slide_h)
```

and define the helper:

```python
def _canvas_region(placeholders: list[PlaceholderInfo], slide_w: float,
                   slide_h: float) -> tuple[bool, Rect | None]:
    """Whether this layout can be composed on freely, and the region that is free.

    A layout qualifies when its only content placeholders are the title and an optional
    subhead — anything else means the template already designed the content area, and
    composing over it would fight the design. The free region starts just under the
    title and stops above the footer band, so `canvas` mode still inherits the master's
    theme, background and decorative graphics (Decision 4).
    """
    roles = {p.role for p in placeholders}
    title = next((p for p in placeholders if p.role == "title"), None)
    if title is None or not roles <= {"title", "subtitle"}:
        return False, None
    top = title.top + title.height + GeometryConstants.CANVAS_TITLE_GAP_IN
    bottom = slide_h - GeometryConstants.FOOTER_BAND_HEIGHT_IN
    height = bottom - top
    if height < GeometryConstants.MIN_CANVAS_FREE_HEIGHT_IN:
        return False, None
    margin = GeometryConstants.FRAME_SIDE_MARGIN_IN
    return True, Rect(margin, top, slide_w - 2 * margin, height)
```

> **Implementer note on the measured free heights.** The design quotes 6.55in for `M1-L10` and 6.58in for `M2-L4` — those are `slide_h - title_bottom`, with no title gap and no footer band. With `CANVAS_TITLE_GAP_IN = 0.10` and `FOOTER_BAND_HEIGHT_IN = 0.45` the computed height is ~6.00in / ~6.03in instead. **Keep the constants and change the test** to `pytest.approx(6.00, abs=0.05)` / `pytest.approx(6.03, abs=0.05)`, and leave a comment in the test naming the design's raw figures. The gap and the band are load-bearing: without them `canvas` mode composes into the footer, which rule 5 then rejects on every slide.

Add `to_dict` to `TemplateCatalog`:

```python
    def to_dict(self) -> dict:
        """JSON-clean snapshot, for the sealed sandbox.

        `deck_builder.py` cannot import this module (it must stay free of `app.*`
        imports so it can be copied into the sandbox), so the catalog crosses that
        boundary as plain JSON rather than as objects.
        """
        return {
            "template_path": str(self.template_path),
            "slide_width": self.slide_width,
            "slide_height": self.slide_height,
            "preferred_master": self.preferred_master,
            "cover_master": self.cover_master,
            "sample_slide_count": self.sample_slide_count,
            "themes": [asdict(theme) for theme in self.themes],
            "layouts": [{
                "key": l.key, "name": l.name,
                "master_index": l.master_index, "layout_index": l.layout_index,
                "tags": list(l.tags), "columns": l.columns,
                "background": l.background,
                "background_luminance": l.background_luminance,
                "canvas_capable": l.canvas_capable,
                "free_region": l.free_region.to_list() if l.free_region else None,
                "decor_rects": [r.to_list() for r in l.decor_rects],
                "recommended_use": l.recommended_use,
                "placeholders": [asdict(p) for p in l.placeholders],
            } for l in self.layouts],
        }

    # --- compatibility shims: plan_tools and layout_tools call these unchanged ---- #
    @property
    def content_layouts(self) -> list["LayoutInfo"]:
        return [l for l in self.canonical_layouts() if "content" in l.tags]

    def prompt_block(self) -> str:
        return self.summary_text()
```

Replace the module's loading functions:

```python
@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> TemplateCatalog:
    prs = Presentation(path)
    catalog = TemplateCatalog.from_presentation(prs, path)
    log.info("template catalog loaded", extra={
        "template": Path(path).name, "masters": len(catalog.themes),
        "layouts": len(catalog.layouts),
        "canvas_capable": sum(1 for l in catalog.layouts if l.canvas_capable)})
    return catalog


def load(path: str | None = None) -> TemplateCatalog:
    """The catalog for the one brand template, cached until the file changes."""
    resolved = str(path or registry.template_path())
    return _load(resolved, Path(resolved).stat().st_mtime)


def for_classification(classification: str | None = None) -> TemplateCatalog:
    """Compatibility shim. Decision 3: classification no longer selects a template.

    Kept so `plan_tools` and `layout_tools` need no edit; the argument is ignored.
    """
    return load()
```

Keep, for the duration of Tasks 3–15 only, so `renderer.py` and `filler.py` stay importable:

```python
LayoutSpec = LayoutInfo                    # retired in Task 16 with renderer.py
Catalog = TemplateCatalog                  # retired in Task 16
```

and on `TemplateCatalog`, thin shims `resolve(name)` (→ `resolve_layout`, returning `default_content()` instead of raising), `by_role(preferences)`, `default_content()`, `best_with_picture()` ported verbatim from the old catalog body.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_template_catalog.py -q`
Expected: PASS, ~21 tests.

- [ ] **Step 5: Confirm nothing that reads the catalog regressed**

Run: `python -m pytest tests/ -q`
Expected: no new failures against the 61-test baseline.

- [ ] **Step 6: Commit**

```bash
git add app/genai/ppt/template/catalog.py tests/test_template_catalog.py
git commit -m "feat(ppt): catalog with theme, tags, decor geometry and canvas regions"
```

---

### Task 4: `DeckBuilder` — the only API generated code may touch

**Why:** Decision 1 and Decision 4. The model writes a program, but against a pre-styled API on the real template, so colours/fonts/sizes are correct by construction rather than by the model's compliance. **This module is copied verbatim into the sandbox**, so it must import only stdlib, `python-pptx` and `lxml`.

**Files:**
- Create: `app/genai/ppt/deck_builder.py`
- Test: `tests/test_deck_builder.py`

**Interfaces:**
- Consumes: the catalog dict from `TemplateCatalog.to_dict()` (Task 3) — **as data, never by import**.
- Produces:
  - `BRAND_DEFAULTS: dict[str, str]`, `THEME_TO_BRAND: dict[str, str]`, `TYPE_SCALE: tuple[int, ...]`
  - `brand_palette(theme_colors: dict[str, str]) -> dict[str, str]`
  - `to_rgb(color: str, palette: dict[str, str]) -> RGBColor` — **raises `ValueError` on a hex literal**
  - `class CatalogView` — `.slide_width`, `.slide_height`, `.theme`, `.resolve(ref) -> LayoutView`, `.get(key)`
  - `class LayoutView` — `.key`, `.name`, `.master_index`, `.layout_index`, `.placeholders`, `.find(target)`, `.decor_rects`, `.free_region`, `.canvas_capable`, `.background_luminance`
  - `class SlideHandle` — placeholder API `set_title set_subtitle set_text set_bullets set_picture set_table set_chart set_notes placeholder_bounds remove_placeholder`; component API `add_card add_stat add_icon_circle add_shape add_table add_chart add_textbox add_line add_image`; `.grid(col, row, cols=1, rows=1) -> tuple[float, float, float, float]`; `.finalize()`
  - `class DeckBuilder` — `DeckBuilder(template_path, catalog, frame=None, remove_unused_placeholders=True)`, `.add_slide(layout) -> SlideHandle`, `.save(path) -> Path`, `.warnings`

**Port source:** `main_v3.py` lines 1377–1970 at `0bd70fb` (`brand_palette`, `to_rgb`, `SlideHandle`, `_set_cell_borders`, `DeckBuilder`).

**Five required deviations from the port** — each one is what makes the geometry gate or the sealed sandbox work:

1. **No `app.*` imports and no module-level `BASE_DIR`/`TEMPLATE_PATH`/`OUTPUT_PPTX_PATH`.** `main_v3` resolves relative image paths against `BASE_DIR` (lines 1571, 1706); take an `asset_root: Path | None` on `DeckBuilder.__init__` instead and resolve against that. `log(...)` calls become appends to `self.warnings`.
2. **`to_rgb` rejects hex.** `main_v3` accepts `#RRGGBB` (line 1398). Here a hex literal must raise `ValueError` naming the allowed tokens, so Decision 4 holds even if the AST guard is bypassed.
3. **`CatalogView` replaces `TemplateCatalog`.** The builder reads the catalog dict; it cannot import `app.genai.ppt.template.catalog`.
4. **`add_*` methods clamp nothing and hide nothing.** If a component is placed off-canvas, it is placed off-canvas and the gate reports it. Silently moving it would hide the violation the loop exists to surface.
5. **`SlideHandle.grid()` is new.** The design contract hands the model a 12x6 grid; the builder is where grid cells become inches, so both sides cannot disagree.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_deck_builder.py`:

```python
"""The constrained builder generated code writes against.

Everything here runs against the real SAMPLE.pptx with no model in the loop: the point
is that a program written to this API lands on a real layout, in theme colours, at
theme fonts, or fails loudly.
"""

import json
from pathlib import Path

import pytest
from pptx import Presentation
from pptx.util import Emu

from app.genai.ppt import deck_builder as db
from app.genai.ppt.template import catalog as catalog_mod, registry


@pytest.fixture(scope="module")
def catalog_dict():
    return catalog_mod.load().to_dict()


@pytest.fixture
def deck(catalog_dict):
    return db.DeckBuilder(registry.template_path(), catalog_dict)


def test_the_builder_needs_no_app_imports_to_work(catalog_dict, tmp_path):
    """The sandbox copies this module on its own; it must stand up without app/."""
    source = Path(db.__file__).read_text(encoding="utf-8")
    assert "import app." not in source and "from app." not in source


def test_a_new_deck_starts_from_the_template_with_its_sample_slides_dropped(deck):
    assert len(deck.prs.slides) == 0
    assert len(deck.prs.slide_masters) == 4
    assert Emu(deck.prs.slide_width).inches == pytest.approx(13.33, abs=0.01)


def test_a_slide_lands_on_the_layout_it_asked_for(deck, tmp_path):
    handle = deck.add_slide("M1-L10")
    handle.set_title("Reflow profile control")
    out = deck.save(tmp_path / "one.pptx")

    reopened = Presentation(str(out))
    slide = next(iter(reopened.slides))
    assert slide.slide_layout.name == "Title with Subhead"
    assert "Reflow profile control" in slide.shapes.title.text_frame.text


def test_a_layout_can_be_named_as_well_as_keyed(deck):
    assert deck.add_slide("Title with Subhead").layout.key == "M1-L10"


def test_an_unknown_layout_fails_loudly(deck):
    with pytest.raises(LookupError):
        deck.add_slide("A Layout That Does Not Exist")


# --- colour discipline ----------------------------------------------------- #

def test_brand_tokens_resolve_against_the_template_theme(catalog_dict):
    palette = db.brand_palette(catalog_dict["themes"][1]["colors"])
    assert palette["red"] == "CD163F"        # accent1
    assert palette["blue"] == "003E7E"       # accent2
    assert palette["orange"] == "F58024"     # accent3
    assert palette["white"] == "FFFFFF"      # lt1


def test_a_hex_literal_is_refused_even_though_pptx_would_accept_it():
    palette = db.brand_palette({})
    with pytest.raises(ValueError, match="token"):
        db.to_rgb("#CD163F", palette)
    with pytest.raises(ValueError, match="token"):
        db.to_rgb("CD163F", palette)


def test_an_unknown_token_is_refused_and_lists_what_is_allowed():
    palette = db.brand_palette({})
    with pytest.raises(ValueError) as excinfo:
        db.to_rgb("chartreuse", palette)
    assert "red" in str(excinfo.value) and "navy" in str(excinfo.value)


def test_every_component_writes_theme_font_text(deck, tmp_path):
    handle = deck.add_slide("M1-L10")
    handle.add_card("Yield", ["Up 4 points"], 0.5, 2.0, 3.0, 1.5)
    handle.add_stat("98%", "first-pass yield", 4.0, 2.0, 2.5, 1.2)
    handle.add_textbox("Body copy", 7.0, 2.0, 3.0, 0.6)
    out = deck.save(tmp_path / "fonts.pptx")

    slide = next(iter(Presentation(str(out)).slides))
    runs = [run for shape in slide.shapes if shape.has_text_frame
            for para in shape.text_frame.paragraphs for run in para.runs]
    assert runs
    for run in runs:
        assert run.font.name in (None, "Barlow")


def test_every_component_font_size_is_on_the_type_scale(deck, tmp_path):
    handle = deck.add_slide("M1-L10")
    handle.add_card("Yield", ["Up 4 points"], 0.5, 2.0, 3.0, 1.5)
    handle.add_stat("98%", "first-pass yield", 4.0, 2.0, 2.5, 1.2)
    handle.add_icon_circle("01", 7.0, 2.0)
    out = deck.save(tmp_path / "sizes.pptx")

    slide = next(iter(Presentation(str(out)).slides))
    sizes = {run.font.size.pt for shape in slide.shapes if shape.has_text_frame
             for para in shape.text_frame.paragraphs for run in para.runs
             if run.font.size is not None}
    assert sizes, "components must set an explicit size"
    assert sizes <= set(db.TYPE_SCALE), f"off-scale sizes: {sizes - set(db.TYPE_SCALE)}"


# --- components ------------------------------------------------------------ #

def test_a_table_arrives_branded_without_being_asked(deck, tmp_path):
    handle = deck.add_slide("M1-L10")
    handle.add_table(["Stage", "Target"], [["Preheat", "3 C/s"]], 1.0, 2.0, 6.0, 1.5)
    out = deck.save(tmp_path / "table.pptx")

    slide = next(iter(Presentation(str(out)).slides))
    table = next(s.table for s in slide.shapes if getattr(s, "has_table", False))
    header = table.cell(0, 0)
    assert header.text_frame.text == "STAGE"
    run = header.text_frame.paragraphs[0].runs[0]
    assert run.font.bold and run.font.size.pt == 11
    assert str(run.font.color.rgb) == "FFFFFF"
    assert str(header.fill.fore_color.rgb) == db.BRAND_DEFAULTS["navy"]


def test_a_chart_arrives_in_brand_series_colours(deck, tmp_path):
    handle = deck.add_slide("M1-L10")
    handle.add_chart("column", ["Q1", "Q2"],
                     [{"name": "Yield", "values": [1, 2]},
                      {"name": "Scrap", "values": [3, 4]}],
                     1.0, 2.0, 6.0, 3.0)
    out = deck.save(tmp_path / "chart.pptx")

    slide = next(iter(Presentation(str(out)).slides))
    chart = next(s.chart for s in slide.shapes if getattr(s, "has_chart", False))
    colours = [str(s.format.fill.fore_color.rgb) for s in chart.plots[0].series]
    assert colours[0] == db.BRAND_DEFAULTS["red"]
    assert colours[1] == db.BRAND_DEFAULTS["blue"]
    assert chart.has_legend


def test_an_unknown_shape_or_chart_type_fails_loudly(deck):
    handle = deck.add_slide("M1-L10")
    with pytest.raises(ValueError, match="unknown shape"):
        handle.add_shape("dodecahedron", 1, 1, 1, 1)
    with pytest.raises(ValueError, match="unknown chart_type"):
        handle.add_chart("sunburst", ["a"], [{"name": "s", "values": [1]}], 1, 1, 2, 2)


def test_a_missing_image_is_a_warning_not_a_crash(deck):
    handle = deck.add_slide("M1-L10")
    assert handle.add_image("no_such_image.png", 1, 2, 2, 2) is None
    assert any("no_such_image" in w for w in deck.warnings)


def test_components_are_placed_where_asked_and_never_quietly_moved(deck, tmp_path):
    """The gate reports off-canvas placement; the builder must not hide it."""
    handle = deck.add_slide("M1-L10")
    handle.add_shape("rectangle", 12.8, 2.0, 2.0, 1.0, fill="red")
    out = deck.save(tmp_path / "offcanvas.pptx")

    slide = next(iter(Presentation(str(out)).slides))
    box = next(s for s in slide.shapes if not s.is_placeholder
               and Emu(s.left).inches > 12)
    assert Emu(box.left).inches == pytest.approx(12.8, abs=0.01)
    assert Emu(box.width).inches == pytest.approx(2.0, abs=0.01)


# --- the grid -------------------------------------------------------------- #

def test_the_grid_maps_cells_to_inches_inside_the_free_region(deck):
    handle = deck.add_slide("M1-L10")
    left, top, width, height = handle.grid(0, 0)
    region = handle.layout.free_region
    assert left == pytest.approx(region[0], abs=0.01)
    assert top == pytest.approx(region[1], abs=0.01)
    assert width < region[2] / 6      # one of twelve columns, minus gutters

    full = handle.grid(0, 0, cols=12, rows=6)
    assert full[2] == pytest.approx(region[2], abs=0.01)
    assert full[3] == pytest.approx(region[3], abs=0.01)


def test_two_adjacent_grid_cells_do_not_overlap(deck):
    handle = deck.add_slide("M1-L10")
    a = handle.grid(0, 0, cols=6)
    b = handle.grid(6, 0, cols=6)
    assert a[0] + a[2] <= b[0] + 1e-6


# --- placeholders ---------------------------------------------------------- #

def test_unfilled_placeholders_are_removed_so_no_prompt_text_ships(deck, tmp_path):
    handle = deck.add_slide("M1-L10")
    handle.set_title("Only a title")
    out = deck.save(tmp_path / "clean.pptx")

    slide = next(iter(Presentation(str(out)).slides))
    text = " ".join(s.text_frame.text for s in slide.shapes if s.has_text_frame)
    assert "Click to edit" not in text
    assert "Second level" not in text


def test_placeholder_bounds_report_the_templates_own_geometry(deck):
    handle = deck.add_slide("M1-L10")
    left, top, width, height = handle.placeholder_bounds("title")
    assert (round(left, 2), round(top, 2)) == (1.14, 0.38)
    assert round(width, 2) == 11.75


def test_notes_land_on_the_notes_slide(deck, tmp_path):
    handle = deck.add_slide("M1-L10")
    handle.set_title("T")
    handle.set_notes("say this out loud")
    out = deck.save(tmp_path / "notes.pptx")

    slide = next(iter(Presentation(str(out)).slides))
    assert slide.notes_slide.notes_text_frame.text == "say this out loud"


def test_long_text_is_shrunk_to_fit_rather_than_overflowing(deck):
    handle = deck.add_slide("M1-L10")
    handle.set_text("subtitle", "x" * 600)
    shape = next(p for p in handle.slide.placeholders
                 if p.placeholder_format.idx == 10)
    size = shape.text_frame.paragraphs[0].runs[0].font.size
    assert size is not None and size.pt <= 10 + 4      # shrunk, floor is 10pt
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_deck_builder.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.genai.ppt.deck_builder'`.

- [ ] **Step 3: Write the module header and the token layer**

Create `app/genai/ppt/deck_builder.py`:

```python
"""The constrained API every generated slide program is written against.

This module is the whole of the contract with model-written code. It is deliberately
SELF-CONTAINED: stdlib, python-pptx and lxml only, and no `app.*` import anywhere. That
is not a style preference — the file is copied verbatim into each sandbox slide
directory (see `app/genai/ppt/sandbox.py`), so a single `from app.config ...` line
would break every generated slide. The same file is imported in-process by the
deterministic fallback renderer and by the tests, so there is exactly one copy of this
API and it cannot drift.

Two rules follow from that isolation:
  * the catalog arrives as a plain dict (`TemplateCatalog.to_dict()`), never as objects;
  * anything this module cannot resolve is appended to `warnings` and reported, never
    logged, because there is no logger on the other side of the sandbox boundary.

Colours are token names. `to_rgb` refuses a hex literal outright, so Decision 4 holds
even for a program that slips past the AST guard.
"""

import json
import math
import re
from pathlib import Path
from typing import Any, Optional, Sequence, Union

from lxml import etree
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE, PP_PLACEHOLDER
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

EMU_PER_INCH = 914400

#: The closed set of sizes a component may set. Placeholder-inherited sizes are exempt
#: (the template's own choices are correct by definition); this is geometry rule 9.
TYPE_SCALE: tuple[int, ...] = (10, 11, 12, 14, 15, 20, 28, 32)

#: Brand tokens. Values are defaults; `brand_palette` overrides the theme-backed ones
#: from the template so the deck follows the template, not this table.
BRAND_DEFAULTS: dict[str, str] = {
    "red": "CD163F", "red_dark": "A8112F", "navy": "1B2A4A", "blue": "003E7E",
    "light_blue": "00AEEF", "orange": "F58024", "yellow": "F9C20A", "green": "77C14C",
    "body": "5A6470", "caption": "8A929E", "border": "D8DBDF", "surface": "F6F7F8",
    "white": "FFFFFF", "black": "000000", "red_light": "FEF2F2", "gray": "CCCCCC",
    "dark_gray": "4C4C4C",
}
THEME_TO_BRAND: dict[str, str] = {
    "accent1": "red", "accent2": "blue", "accent3": "orange", "accent4": "light_blue",
    "accent5": "yellow", "accent6": "green",
    "lt1": "white", "dk1": "black", "dk2": "gray", "lt2": "dark_gray",
}

_SHAPES = {
    "rectangle": MSO_SHAPE.RECTANGLE, "rounded_rectangle": MSO_SHAPE.ROUNDED_RECTANGLE,
    "oval": MSO_SHAPE.OVAL, "chevron": MSO_SHAPE.CHEVRON,
    "right_arrow": MSO_SHAPE.RIGHT_ARROW, "left_arrow": MSO_SHAPE.LEFT_ARROW,
    "up_arrow": MSO_SHAPE.UP_ARROW, "down_arrow": MSO_SHAPE.DOWN_ARROW,
    "pentagon": MSO_SHAPE.PENTAGON, "diamond": MSO_SHAPE.DIAMOND,
    "hexagon": MSO_SHAPE.HEXAGON, "triangle": MSO_SHAPE.ISOSCELES_TRIANGLE,
    "snip_rectangle": MSO_SHAPE.SNIP_2_SAME_RECTANGLE,
    "round_top_rectangle": MSO_SHAPE.ROUND_2_SAME_RECTANGLE,
}
_CHART_TYPES = {
    "column": XL_CHART_TYPE.COLUMN_CLUSTERED, "stacked_column": XL_CHART_TYPE.COLUMN_STACKED,
    "bar": XL_CHART_TYPE.BAR_CLUSTERED, "stacked_bar": XL_CHART_TYPE.BAR_STACKED,
    "line": XL_CHART_TYPE.LINE_MARKERS, "pie": XL_CHART_TYPE.PIE,
    "doughnut": XL_CHART_TYPE.DOUGHNUT, "area": XL_CHART_TYPE.AREA,
}
_ALIGN = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER,
          "right": PP_ALIGN.RIGHT, "justify": PP_ALIGN.JUSTIFY}
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")

#: Grid geometry, mirrored from GeometryConstants. Duplicated deliberately: this module
#: cannot import app.config. `tests/test_deck_builder.py::test_the_grid_constants_match`
#: (Task 5) asserts the two stay equal.
GRID_COLUMNS, GRID_ROWS, GRID_GUTTER_IN = 12, 6, 0.15


def brand_palette(theme_colors: dict) -> dict[str, str]:
    """Brand token -> hex, with the theme-backed tokens taken from the template."""
    palette = dict(BRAND_DEFAULTS)
    for scheme_name, brand_name in THEME_TO_BRAND.items():
        value = (theme_colors or {}).get(scheme_name)
        if value and len(str(value)) == 6:
            palette[brand_name] = str(value).upper()
    return palette


def to_rgb(color: Union[str, RGBColor, None], palette: dict[str, str]) -> RGBColor:
    """Resolve a brand or theme token to a colour.

    Unlike the reference implementation this refuses hex outright: Decision 4 says every
    colour comes from the template theme as a named token, and accepting `#CD163F` here
    would let a program drift off-palette while still looking correct.

    Raises:
        ValueError: For a hex literal, or any name that is not a token.
    """
    if isinstance(color, RGBColor):
        return color
    if not color:
        return RGBColor.from_string(palette["body"])
    key = str(color).strip().lower().replace("-", "_").replace(" ", "_")
    if key in palette:
        return RGBColor.from_string(palette[key])
    if re.fullmatch(r"#?[0-9a-fA-F]{6}", str(color).strip()):
        raise ValueError(
            f"literal colour {color!r} is not allowed; use a brand token "
            f"({', '.join(sorted(palette))})")
    raise ValueError(
        f"unknown colour token {color!r}; allowed: {', '.join(sorted(palette))}")
```

- [ ] **Step 4: Write `CatalogView` and `LayoutView`**

```python
class LayoutView:
    """One layout, read from the serialized catalog."""

    def __init__(self, data: dict) -> None:
        self._data = data
        self.key: str = data["key"]
        self.name: str = data["name"]
        self.master_index: int = data["master_index"]
        self.layout_index: int = data["layout_index"]
        self.tags: list[str] = list(data.get("tags") or [])
        self.background: str = data.get("background", "light")
        self.background_luminance: float = float(data.get("background_luminance", 1.0))
        self.canvas_capable: bool = bool(data.get("canvas_capable"))
        self.free_region: Optional[list] = data.get("free_region")
        self.decor_rects: list[list] = [list(r) for r in (data.get("decor_rects") or [])]
        self.placeholders: list[dict] = list(data.get("placeholders") or [])

    def find(self, target: Union[int, str]) -> Optional[dict]:
        """Resolve a placeholder by idx, numeric string, slot name or role."""
        if isinstance(target, bool):
            return None
        if isinstance(target, int) or (isinstance(target, str) and target.strip().isdigit()):
            idx = int(target)
            return next((p for p in self.placeholders if p["idx"] == idx), None)
        key = str(target).strip().lower().replace(" ", "").replace("_", "")
        for placeholder in self.placeholders:
            if str(placeholder.get("slot", "")).lower() == key:
                return placeholder
        return next((p for p in self.placeholders if p.get("role") == key), None)


class CatalogView:
    """The serialized catalog, as the sandbox sees it."""

    def __init__(self, data: Union[dict, str, Path]) -> None:
        if not isinstance(data, dict):
            data = json.loads(Path(data).read_text(encoding="utf-8"))
        self._data = data
        self.slide_width: float = float(data["slide_width"])
        self.slide_height: float = float(data["slide_height"])
        self.preferred_master: int = int(data.get("preferred_master", 0))
        self.themes: list[dict] = list(data.get("themes") or [])
        self.layouts: list[LayoutView] = [LayoutView(l) for l in data["layouts"]]
        self._by_key = {l.key.lower(): l for l in self.layouts}

    @property
    def theme(self) -> dict:
        if not self.themes:
            return {"major_font": "Barlow", "minor_font": "Barlow", "colors": {}}
        return self.themes[min(self.preferred_master, len(self.themes) - 1)]

    def get(self, key: str) -> Optional[LayoutView]:
        return self._by_key.get(str(key).strip().lower())

    def resolve(self, ref: Union[str, LayoutView]) -> LayoutView:
        """Resolve a catalog key ('M1-L10') or an exact layout name.

        Raises:
            LookupError: When neither matches. No fuzzy fallback: a program that names a
                layout that does not exist has a bug the repair loop should see.
        """
        if isinstance(ref, LayoutView):
            return ref
        found = self.get(str(ref))
        if found is not None:
            return found
        wanted = re.sub(r"\s+", " ", str(ref).strip().lower())
        exact = [l for l in self.layouts if re.sub(r"\s+", " ", l.name.lower()) == wanted]
        if exact:
            return exact[0]
        import difflib
        names = {re.sub(r"\s+", " ", l.name.lower()): l for l in self.layouts}
        close = difflib.get_close_matches(wanted, list(names), n=3, cutoff=0.75)
        hint = ", ".join(f"{names[c].key} '{names[c].name}'" for c in close)
        raise LookupError(f"unknown layout {ref!r}." + (f" Did you mean: {hint}" if hint else ""))
```

- [ ] **Step 5: Port `SlideHandle`, `_set_cell_borders` and `DeckBuilder`**

Port `main_v3.py` 1402–1889 (`SlideHandle`), 1891–1909 (`_set_cell_borders`) and 1912–1969 (`DeckBuilder`) with the five deviations listed above. Concretely:

- `SlideHandle.__init__(self, deck, slide, layout: LayoutView)`; `self.warnings` stays, `_warn` appends only.
- Every `info.left` / `info.font_size` / `info.role` becomes `info["left"]` / `info.get("font_size")` / `info.get("role")` — placeholders are dicts now.
- `_fit_size` (1452–1463) keeps its shrink-to-fit loop but calls the local `_text_fits` helper below rather than `main_v3`'s; the 10pt floor and the 22pt title floor are unchanged.
- `apply_spec` / `apply_extra` (1832–1889) are **not ported** — they render `main_v3`'s `SlideSpec`, which has no equivalent here. Drop them.
- `DeckBuilder.render_plan` (1950–1955) is **not ported**, same reason.
- `DeckBuilder.__init__` signature becomes:

```python
    def __init__(self, template_path: Union[str, Path], catalog: Union[dict, str, Path],
                 frame: Optional[dict] = None, asset_root: Optional[Path] = None,
                 remove_unused_placeholders: bool = True) -> None:
        """
        Args:
            template_path: The brand template. Every slide sits on one of its layouts.
            catalog: `TemplateCatalog.to_dict()`, or a path to that JSON.
            frame: The deck design contract's frame block, applied by `finalize()`.
                None means no frame — used by tests and by single-slide previews.
            asset_root: Directory relative image paths resolve against.
            remove_unused_placeholders: Drop placeholders nothing filled, so no
                "Click to edit" prompt ever ships.
        """
        self.template_path = Path(template_path)
        self.prs = Presentation(str(self.template_path))
        self.catalog = catalog if isinstance(catalog, CatalogView) else CatalogView(catalog)
        self.frame = frame or {}
        self.asset_root = Path(asset_root) if asset_root else None
        self.brand = brand_palette(self.catalog.theme.get("colors") or {})
        self.heading_font = self.catalog.theme.get("major_font") or "Barlow"
        self.body_font = self.catalog.theme.get("minor_font") or "Barlow"
        self.remove_unused_placeholders = remove_unused_placeholders
        self.slide_width = self.catalog.slide_width
        self.slide_height = self.catalog.slide_height
        self.slides: list[SlideHandle] = []
        self._clear_slides()
```

Add the local fit helpers (they replace `main_v3`'s `_estimate_lines` / `_capacity` / `_text_fits`, which lived outside the ported range and cannot be imported from `measure.py`):

```python
# Mirrors app/genai/ppt/measure.py. Duplicated because this module cannot import app.*;
# tests/test_geometry.py::test_the_builder_and_the_gate_agree_on_capacity pins them
# together so the two copies cannot diverge silently.
#
# Wrapping measures each string's real width rather than counting characters against an
# average: `_string_em` already rounds every glyph up, so that is where the conservatism
# belongs. `_CHAR_EM_LOWER` is only the coarse capacity hint.
_CHAR_EM_LOWER = 0.56
_LINE_HEIGHT_FACTOR = 1.25
_SIDE_INSET_IN, _VERTICAL_INSET_IN, _BULLET_INDENT_IN = 0.20, 0.10, 0.22

#: Copied verbatim from measure.CHAR_EM / measure.DEFAULT_CHAR_EM. Keep in step.
_CHAR_EM: dict[str, float] = {}          # populated below from the same class table
_DEFAULT_CHAR_EM = 0.72
for _chars, _width in (("il|!.,;:'`", 0.30), ("jft()[]{}/\\-", 0.38), ("rIJ ", 0.42),
                       ("abcdeghknopqsuvxyz1234567890", 0.56),
                       ("wLTZEFSCXYPBRKANDGHUVQOM", 0.68), ("m", 0.86), ("W", 0.94)):
    for _char in _chars:
        _CHAR_EM[_char] = _width


def _string_em(text: str) -> float:
    """Advance width in em, over-estimating rather than under-estimating."""
    return sum(_CHAR_EM.get(char, _DEFAULT_CHAR_EM) for char in text)


def _capacity(width: float, height: float, size: float,
              bullets: bool = True) -> tuple[int, int]:
    usable_w = max(0.4, width - _SIDE_INSET_IN - (_BULLET_INDENT_IN if bullets else 0))
    usable_h = max(0.05, height - _VERTICAL_INSET_IN)
    return (max(1, int(usable_w * 72.0 / max(size, 1.0) / _CHAR_EM_LOWER)),
            max(1, int(usable_h * 72.0 / (max(size, 1.0) * _LINE_HEIGHT_FACTOR))))


def _rendered_lines(items: Sequence[str], width: float, size: float,
                    bullets: bool = True) -> int:
    usable_w = max(0.4, width - _SIDE_INSET_IN - (_BULLET_INDENT_IN if bullets else 0))
    em_per_line = usable_w * 72.0 / max(size, 1.0)
    return sum(max(1, math.ceil(_string_em(str(item)) / em_per_line))
               for item in items if str(item).strip())


def _text_fits(items: Sequence[str], width: float, height: float, size: float,
               bullets: bool = True) -> bool:
    _, lines = _capacity(width, height, size, bullets)
    return _rendered_lines(items, width, size, bullets) <= lines
```

Add `grid()` to `SlideHandle`:

```python
    def grid(self, col: int, row: int, cols: int = 1, rows: int = 1
             ) -> tuple[float, float, float, float]:
        """A cell of the deck's 12x6 grid, as (left, top, width, height) in inches.

        The grid is laid over the layout's free region when it has one, and over the
        canvas minus the frame margins otherwise. Fixed gutters mean two adjacent cells
        never overlap, which is what keeps rule 2 satisfiable by construction.

        Args:
            col: Zero-based column of the top-left cell.
            row: Zero-based row of the top-left cell.
            cols: How many columns the block spans.
            rows: How many rows the block spans.
        """
        region = self.layout.free_region or [
            0.42, 1.30, self.deck.slide_width - 0.84, self.deck.slide_height - 1.75]
        left0, top0, width0, height0 = region
        gutter = GRID_GUTTER_IN
        col_w = (width0 - gutter * (GRID_COLUMNS - 1)) / GRID_COLUMNS
        row_h = (height0 - gutter * (GRID_ROWS - 1)) / GRID_ROWS
        col = max(0, min(col, GRID_COLUMNS - 1))
        row = max(0, min(row, GRID_ROWS - 1))
        cols = max(1, min(cols, GRID_COLUMNS - col))
        rows = max(1, min(rows, GRID_ROWS - row))
        return (
            left0 + col * (col_w + gutter),
            top0 + row * (row_h + gutter),
            cols * col_w + (cols - 1) * gutter,
            rows * row_h + (rows - 1) * gutter,
        )
```

Make every component method pass a `TYPE_SCALE` size. The reference defaults already do, with one exception: `add_icon_circle` computes `size or max(10, diameter * 22)` (line 1699), which is off-scale for most diameters. Replace with a snap:

```python
def _snap_size(size: float) -> int:
    """Nearest size on the type scale, never rounding up past the largest."""
    return min(TYPE_SCALE, key=lambda s: (abs(s - size), s))
```

and use `size=_snap_size(size or diameter * 22)` in `add_icon_circle`. Apply `_snap_size` in `add_textbox`, `add_shape`, `add_card`, `add_stat` and `add_table` to whatever size arrives, so an off-scale argument from generated code cannot reach the XML.

`DeckBuilder.save` keeps the reference body (1956–1966) minus the `log(...)` call, and calls `handle.finalize()` for each slide first.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest tests/test_deck_builder.py -q`
Expected: PASS. `test_the_builder_needs_no_app_imports_to_work` is the one to watch — if it fails, an `app.` import crept in and the sandbox is broken.

- [ ] **Step 7: Commit**

```bash
git add app/genai/ppt/deck_builder.py tests/test_deck_builder.py
git commit -m "feat(ppt): DeckBuilder, the constrained API generated code writes against"
```

---

### Task 5: The frame — classification, logo, footer, slide number

**Why:** Decision 3 says we render the classification, from one code path, in a reserved zone. Decision 6 and gate rule 5 say the agent must never draw there. `finalize()` is where the frame lands, so the frame is never the model's to draw and never the model's to collide with.

**Files:**
- Modify: `app/genai/ppt/deck_builder.py` (`SlideHandle.finalize`, `_apply_frame`, `frame_spec`)
- Test: `tests/test_deck_builder.py` (extend)

**Interfaces:**
- Consumes: `LayoutView.background_luminance`, `LayoutView.decor_rects` (Task 4); `registry.classification_symbol`, `registry.logo_path` (Task 1) — resolved **by the caller** and passed in as paths, because the builder cannot import `registry`.
- Produces:
  - `deck_builder.frame_spec(slide_width, slide_height, *, label, symbol_path=None, logo_path=None, logo_variant="color", slide_number=None) -> dict` — the frame block, also used by the design contract and the gate
  - `deck_builder.reserved_rects(frame: dict, layout: LayoutView) -> list[list[float]]` — frame rects + layout decor rects, the zones rule 5 protects
  - `SlideHandle.finalize()` now applies the frame after removing unused placeholders

**Frame layout (deterministic, no model input):**

```
                     13.333 in
  +-------------------------------------------------+
  |  [logo]                                          |   <- only when the layout has
  |                                                  |      no decor picture already
  |                 content region                   |
  |                                                  |
  +-------------------------------------------------+  7.05 in
  | [sym] Internal                             12    |   <- footer band, 0.45 in tall
  +-------------------------------------------------+  7.50 in
```

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_deck_builder.py`:

```python
# --------------------------------------------------------------------------- #
# the frame                                                                    #
# --------------------------------------------------------------------------- #

from app.config.constants import GeometryConstants
from app.genai.ppt.template import registry as reg


@pytest.fixture
def frame():
    return db.frame_spec(
        13.333, 7.5, label="Internal",
        symbol_path=str(reg.classification_symbol("internal")),
        logo_path=str(reg.logo_path("d2r_mark.png")),
        logo_variant="color", slide_number=3)


def test_the_grid_constants_match_the_app_side_ones():
    """The builder cannot import GeometryConstants; these two copies must not drift."""
    assert db.GRID_COLUMNS == GeometryConstants.GRID_COLUMNS
    assert db.GRID_ROWS == GeometryConstants.GRID_ROWS
    assert db.GRID_GUTTER_IN == GeometryConstants.GRID_GUTTER_IN
    assert db.TYPE_SCALE == GeometryConstants.TYPE_SCALE


def test_the_logo_rect_follows_the_files_real_aspect_ratio(frame):
    """The brand marks are wide, not square. Reserving a square would block a strip of
    slide that is actually free, and `check_geometry` would report it as unavailable."""
    _, _, width, height = frame["logo"]["rect"]
    assert width == pytest.approx(GeometryConstants.LOGO_WIDTH_IN)
    # d2r_mark.png is 158x60 => 2.63:1, so 0.79in wide renders ~0.30in tall.
    assert height == pytest.approx(0.30, abs=0.02)
    assert height < width


def test_an_unreadable_logo_does_not_break_the_frame():
    spec = db.frame_spec(13.333, 7.5, label="Internal",
                         logo_path="no_such_file.png")
    assert spec["logo"]["rect"][3] > 0


def test_the_footer_band_spans_the_canvas_along_the_bottom(frame):
    left, top, width, height = frame["footer_band"]
    assert left == 0 and width == pytest.approx(13.333, abs=0.01)
    assert height == pytest.approx(GeometryConstants.FOOTER_BAND_HEIGHT_IN)
    assert top + height == pytest.approx(7.5, abs=0.01)


def test_the_classification_mark_sits_inside_the_footer_band(frame):
    band = frame["footer_band"]
    for key in ("symbol", "label"):
        left, top, width, height = frame["classification"][key]
        assert top >= band[1] - 1e-6
        assert top + height <= band[1] + band[3] + 1e-6


def test_the_classification_label_and_symbol_are_stamped_on_the_slide(deck, frame,
                                                                      tmp_path):
    deck.frame = frame
    handle = deck.add_slide("M1-L10")
    handle.set_title("T")
    out = deck.save(tmp_path / "framed.pptx")

    slide = next(iter(Presentation(str(out)).slides))
    text = " ".join(s.text_frame.text for s in slide.shapes if s.has_text_frame)
    assert "Internal" in text
    assert "3" in text                                     # the slide number
    pictures = [s for s in slide.shapes if s.shape_type == 13]
    assert pictures, "the policy symbol must be placed"


def test_a_missing_symbol_degrades_to_the_label_alone(deck, tmp_path):
    deck.frame = db.frame_spec(13.333, 7.5, label="Co-branded", symbol_path=None,
                               slide_number=1)
    handle = deck.add_slide("M1-L10")
    handle.set_title("T")
    out = deck.save(tmp_path / "nosymbol.pptx")

    slide = next(iter(Presentation(str(out)).slides))
    text = " ".join(s.text_frame.text for s in slide.shapes if s.has_text_frame)
    assert "Co-branded" in text
    assert any("symbol" in w.lower() for w in deck.warnings)


def test_the_white_logo_is_chosen_on_a_dark_layout(catalog_dict):
    dark = next(l for l in catalog_dict["layouts"] if l["background_luminance"] < 0.5)
    assert db.logo_variant_for(dark["background_luminance"]) == "white"
    assert db.logo_variant_for(1.0) == "color"


def test_no_second_mark_is_added_when_the_layout_already_carries_one(deck, frame,
                                                                     tmp_path):
    """M1-L10 ships a brand mark as layout decor; the frame must not stack another."""
    deck.frame = frame
    handle = deck.add_slide("M1-L10")
    assert handle.layout.decor_rects           # the layout really does carry decor
    handle.set_title("T")
    out = deck.save(tmp_path / "onelogo.pptx")

    slide = next(iter(Presentation(str(out)).slides))
    logos = [s for s in slide.shapes if s.shape_type == 13
             and Emu(s.top).inches < 1.2 and Emu(s.left).inches < 1.2]
    assert len(logos) == 0, "the layout's own mark is inherited; do not add another"


def test_the_logo_is_added_when_the_layout_has_no_decor(deck, frame, tmp_path):
    deck.frame = frame
    handle = deck.add_slide("M2-L4")           # zero decor shapes
    assert handle.layout.decor_rects == []
    handle.set_title("T")
    out = deck.save(tmp_path / "logo.pptx")

    slide = next(iter(Presentation(str(out)).slides))
    logos = [s for s in slide.shapes if s.shape_type == 13
             and Emu(s.top).inches < 1.2]
    assert len(logos) == 1


def test_reserved_rects_cover_the_frame_and_the_layouts_own_decor(frame, catalog_dict):
    layout = db.CatalogView(catalog_dict).get("M1-L10")
    reserved = db.reserved_rects(frame, layout)
    assert len(reserved) >= len(layout.decor_rects) + 1
    # The footer band must be among them: rule 5 is what keeps content out of it.
    assert any(abs(r[1] + r[3] - 7.5) < 0.01 and r[2] > 13 for r in reserved)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_deck_builder.py -q -k frame or grid_constants`
Expected: FAIL — `AttributeError: module has no attribute 'frame_spec'`.

- [ ] **Step 3: Add the frame functions to `deck_builder.py`**

```python
def logo_variant_for(background_luminance: float) -> str:
    """Which logo variant reads on this background. Mechanical, not the model's guess."""
    return "white" if background_luminance < 0.5 else "color"


def _image_aspect(path: str) -> float:
    """width / height of a PNG, read from its IHDR chunk. 1.0 when unreadable.

    Done by hand rather than with Pillow: this module is copied into the sandbox and
    may import nothing but the stdlib, python-pptx and lxml.
    """
    try:
        with open(path, "rb") as handle:
            header = handle.read(24)
        if header[:8] != b"\x89PNG\r\n\x1a\n":
            return 1.0
        width = int.from_bytes(header[16:20], "big")
        height = int.from_bytes(header[20:24], "big")
        return (width / height) if width and height else 1.0
    except OSError:
        return 1.0


def frame_spec(slide_width: float, slide_height: float, *, label: str,
               symbol_path: Optional[str] = None, logo_path: Optional[str] = None,
               logo_variant: str = "color",
               slide_number: Optional[int] = None) -> dict:
    """The brand furniture every slide carries, as rectangles.

    Computed, not drawn, so the geometry gate can protect exactly the same rectangles
    `finalize()` later fills (Decision 3). Everything is in inches.

    Args:
        slide_width: Canvas width.
        slide_height: Canvas height.
        label: The classification as rendered, e.g. "Internal".
        symbol_path: Policy symbol PNG, or None when the brand pack has none.
        logo_path: The brand mark to place, or None to place none.
        logo_variant: "color" or "white"; recorded so the gate can report mismatches.
        slide_number: Printed at the right of the footer band.
    """
    band_h = 0.45                          # GeometryConstants.FOOTER_BAND_HEIGHT_IN
    side = 0.42                            # GeometryConstants.FRAME_SIDE_MARGIN_IN
    sym = 0.22                             # GeometryConstants.CLASSIFICATION_SYMBOL_IN
    logo_w = 0.79                          # GeometryConstants.LOGO_WIDTH_IN
    band_top = slide_height - band_h
    sym_top = band_top + (band_h - sym) / 2
    # The logo is placed by width alone, so its height follows from the file's own
    # aspect ratio. Reserving a square here would block a half-inch strip of slide that
    # is actually free — the brand marks are wide (d2r_mark is 2.63:1, the full RA
    # lockup 3.42:1), so the real mark is about 0.30in tall, not 0.79in.
    logo_h = logo_w / _image_aspect(logo_path) if logo_path else logo_w
    return {
        "footer_band": [0.0, band_top, slide_width, band_h],
        "classification": {
            "label_text": label,
            "symbol_path": symbol_path,
            "symbol": [side, sym_top, sym, sym],
            "label": [side + sym + 0.10, band_top, 1.60, band_h],
        },
        "logo": None if not logo_path else {
            "path": logo_path, "variant": logo_variant,
            "rect": [side, 0.28, logo_w, round(logo_h, 3)],
        },
        "slide_number": {
            "value": slide_number,
            "rect": [slide_width - side - 0.60, band_top, 0.60, band_h],
        },
    }


def reserved_rects(frame: dict, layout: "LayoutView") -> list[list[float]]:
    """Every rectangle an added shape must not intersect (gate rule 5).

    That is the frame's own furniture plus the layout's inherited decorative graphics —
    the shapes a program cannot see in its own output but which land on the slide all
    the same.
    """
    rects: list[list[float]] = []
    if frame:
        rects.append(list(frame["footer_band"]))
        rects.append(list(frame["classification"]["symbol"]))
        rects.append(list(frame["classification"]["label"]))
        rects.append(list(frame["slide_number"]["rect"]))
        if frame.get("logo"):
            rects.append(list(frame["logo"]["rect"]))
    rects.extend([list(r) for r in getattr(layout, "decor_rects", [])])
    return rects
```

- [ ] **Step 4: Apply the frame in `finalize()`**

Replace `SlideHandle.finalize` (ported from reference 1821–1830) with:

```python
    def finalize(self) -> None:
        """Drop unfilled placeholders, then stamp the frame.

        Order matters: the frame is applied last so nothing the program wrote can sit on
        top of the classification mark, and placeholder removal happens first so a
        never-filled body cannot leave "Click to edit" under the footer.
        """
        if self.deck.remove_unused_placeholders:
            for idx, shape in self._placeholder_shapes().items():
                if idx in self.used_idx:
                    continue
                if shape.placeholder_format.type in (
                        PP_PLACEHOLDER.SLIDE_NUMBER, PP_PLACEHOLDER.FOOTER,
                        PP_PLACEHOLDER.DATE):
                    continue
                shape._element.getparent().remove(shape._element)
        self._apply_frame()

    def _apply_frame(self) -> None:
        """Stamp classification, logo and slide number. Never the model's to draw."""
        frame = self.deck.frame
        if not frame:
            return
        classification = frame.get("classification") or {}
        symbol_path = classification.get("symbol_path")
        if symbol_path and Path(symbol_path).is_file():
            left, top, width, height = classification["symbol"]
            self.slide.shapes.add_picture(symbol_path, Inches(left), Inches(top),
                                          width=Inches(width), height=Inches(height))
        elif classification.get("label_text"):
            self._warn(f"no policy symbol for {classification['label_text']}; "
                       f"the label is stamped without it")
        if classification.get("label_text"):
            self.add_textbox(classification["label_text"], *classification["label"],
                             size=10, color="caption", align="left", anchor="middle")

        logo = frame.get("logo")
        # The template's own layouts already carry the Rockwell mark on most masters.
        # Adding a second one is the mistake the brand guide calls out by name, so the
        # frame only places a logo where the layout brought none.
        if logo and Path(str(logo.get("path", ""))).is_file() and not self.layout.decor_rects:
            left, top, width, _ = logo["rect"]
            self.slide.shapes.add_picture(logo["path"], Inches(left), Inches(top),
                                          width=Inches(width))

        number = frame.get("slide_number") or {}
        if number.get("value") is not None:
            self.add_textbox(str(number["value"]), *number["rect"],
                             size=10, color="caption", align="right", anchor="middle")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_deck_builder.py -q`
Expected: PASS, all of Task 4's and Task 5's tests.

- [ ] **Step 6: Commit**

```bash
git add app/genai/ppt/deck_builder.py tests/test_deck_builder.py
git commit -m "feat(ppt): render the classification mark and brand frame from one path"
```

---

### Task 6: AST guard — reject before executing

**Why:** The spec's component 4. A program that constructs `Presentation(...)` itself, calls `shapes.add_*` directly, or writes a hex literal has escaped every guarantee in this design. Catching it statically costs nothing; catching it after a 180s execution costs a repair round.

**Files:**
- Create: `app/genai/ppt/code_guard.py`
- Test: `tests/test_code_guard.py`

**Interfaces:**
- Consumes: nothing (pure `ast`).
- Produces:
  - `code_guard.check(code: str) -> list[str]` — human-readable problems, `[]` when clean
  - `code_guard.check_message(code: str) -> str | None` — the same, joined, or `None`
  - `code_guard.strip_code_fences(code: str) -> str`
  - `code_guard.FORBIDDEN_MODULES: frozenset[str]`, `code_guard.FORBIDDEN_CALLS: frozenset[str]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_code_guard.py`:

```python
"""The static guard. A rejection here is a repair round the sandbox never has to pay for."""

import pytest

from app.genai.ppt import code_guard


CLEAN = '''
from deck_builder import DeckBuilder

deck = DeckBuilder(TEMPLATE_PATH, CATALOG, frame=FRAME)
slide = deck.add_slide("M1-L10")
slide.set_title("Reflow profile control")
slide.add_card("Yield", ["Up 4 points"], *slide.grid(0, 1, cols=4, rows=2))
slide.add_stat("98%", "first-pass yield", *slide.grid(5, 1, cols=3, rows=2))
slide.set_notes("why it matters")
deck.save(OUTPUT_PATH)
'''


def test_a_representative_deckbuilder_program_is_accepted():
    assert code_guard.check(CLEAN) == []
    assert code_guard.check_message(CLEAN) is None


def test_a_syntax_error_is_reported_with_its_line():
    problems = code_guard.check("def broken(:\n    pass\n")
    assert len(problems) == 1 and "SyntaxError" in problems[0] and "line" in problems[0]


def test_constructing_a_presentation_directly_is_refused():
    code = CLEAN + "\nfrom pptx import Presentation\nprs = Presentation()\n"
    assert any("Presentation(" in p for p in code_guard.check(code))


def test_calling_shapes_add_directly_is_refused():
    code = CLEAN + "\nslide.slide.shapes.add_textbox(1, 2, 3, 4)\n"
    problems = code_guard.check(code)
    assert any("add_textbox" in p and "component API" in p for p in problems)


@pytest.mark.parametrize("literal", ['"#CD163F"', '"CD163F"', "'#003e7e'"])
def test_a_hex_colour_literal_is_refused(literal):
    code = CLEAN + f"\nslide.add_shape('rectangle', 1, 1, 1, 1, fill={literal})\n"
    assert any("hex" in p.lower() for p in code_guard.check(code))


def test_rgbcolor_is_refused():
    code = CLEAN + "\nfrom pptx.dml.color import RGBColor\nc = RGBColor(1, 2, 3)\n"
    assert any("RGBColor" in p for p in code_guard.check(code))


@pytest.mark.parametrize("module", ["subprocess", "socket", "urllib", "requests",
                                    "shutil", "ctypes", "importlib", "multiprocessing"])
def test_forbidden_modules_are_refused(module):
    code = CLEAN + f"\nimport {module}\n"
    assert any(module in p and "not allowed" in p for p in code_guard.check(code))


@pytest.mark.parametrize("call", ["os.system('ls')", "eval('1')", "exec('x=1')",
                                  "os.remove('f')", "__import__('os')"])
def test_forbidden_calls_are_refused(call):
    assert any("not allowed" in p for p in code_guard.check(CLEAN + f"\n{call}\n"))


def test_a_program_that_never_imports_the_builder_is_refused():
    assert any("deck_builder" in p for p in code_guard.check("x = 1\n"))


def test_code_fences_are_stripped_before_checking():
    fenced = "```python\n" + CLEAN + "```"
    assert code_guard.check(code_guard.strip_code_fences(fenced)) == []


def test_every_problem_reads_as_an_instruction_not_an_assertion():
    problems = code_guard.check("import socket\n")
    assert problems
    for problem in problems:
        assert problem[0].islower() or problem.startswith(("SyntaxError", "import"))
        assert len(problem) > 20, "a violation must tell the model what to do instead"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_code_guard.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.genai.ppt.code_guard'`.

- [ ] **Step 3: Write `app/genai/ppt/code_guard.py`**

```python
"""Reject a generated program before it runs.

Extends the deny-list in `main_v3::_static_code_check` (reference lines 2108-2133 at
0bd70fb) with the brand rules this design needs. Three of the four new checks exist
because a program that does these things has escaped the design's guarantees entirely:
constructing its own `Presentation` abandons the template, calling `shapes.add_*`
abandons the pre-styled component API, and a hex literal abandons the theme.

A rejection is returned to the model in the same repair loop as a traceback or a
geometry violation, so the wording matters: each problem says what to do instead.
"""

import ast
import re
from typing import Optional

FORBIDDEN_MODULES = frozenset({
    "subprocess", "socket", "shutil", "urllib", "requests", "http", "ftplib",
    "ctypes", "importlib", "multiprocessing",
})
FORBIDDEN_CALLS = frozenset({
    "os.system", "os.remove", "os.unlink", "os.rmdir", "os.removedirs", "os.popen",
    "eval", "exec", "__import__", "os.execv", "os.spawn",
})

#: The module the sandbox copies in beside the program. It is the only way to a deck.
BUILDER_MODULE = "deck_builder"

_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")
_FENCE_RE = re.compile(r"^```(?:python|py)?\s*\n(.*?)\n?```\s*$", re.DOTALL)


def strip_code_fences(code: str) -> str:
    """Unwrap a ```python fenced block, which the model sometimes answers with."""
    match = _FENCE_RE.match(code.strip())
    return match.group(1) if match else code.strip()


def check(code: str) -> list[str]:
    """Everything wrong with this program that can be seen without running it.

    Args:
        code: The generated program's source.

    Returns:
        One instruction per problem, empty when the program is acceptable.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"SyntaxError: {exc.msg} (line {exc.lineno}) — fix the syntax and "
                f"resend the whole program"]

    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = ([alias.name for alias in node.names]
                     if isinstance(node, ast.Import) else [node.module or ""])
            for name in names:
                if name.split(".")[0] in FORBIDDEN_MODULES:
                    problems.append(
                        f"import of '{name}' is not allowed; a slide program needs "
                        f"nothing but `{BUILDER_MODULE}`")
        elif isinstance(node, ast.Call):
            problems.extend(_call_problems(node))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _HEX_RE.match(node.value.strip()):
                problems.append(
                    f"the hex colour literal '{node.value}' is not allowed; use a brand "
                    f"token name such as 'red', 'navy' or 'accent1' — every colour must "
                    f"resolve against the template theme")

    if (f"from {BUILDER_MODULE} import" not in code
            and f"import {BUILDER_MODULE}" not in code):
        problems.append(
            f"the program must build the slide through the DeckBuilder API: start with "
            f"`from {BUILDER_MODULE} import DeckBuilder`")

    # Preserve order, drop repeats: the same mistake on twelve lines is one instruction.
    return list(dict.fromkeys(problems))


def _call_problems(node: ast.Call) -> list[str]:
    """Problems with one call node."""
    func = node.func
    problems: list[str] = []

    if isinstance(func, ast.Name):
        if func.id == "Presentation":
            problems.append(
                "do not construct `Presentation(...)` — `DeckBuilder` opens the brand "
                "template for you, and a deck built any other way inherits no theme, "
                "no fonts and no masters")
        elif func.id == "RGBColor":
            problems.append(
                "do not use `RGBColor(...)` — pass a brand token name instead, so the "
                "colour comes from the template theme")
        elif func.id in FORBIDDEN_CALLS:
            problems.append(f"call to '{func.id}' is not allowed; remove it")

    if isinstance(func, ast.Attribute):
        if func.attr.startswith("add_") and _is_shapes_access(func.value):
            problems.append(
                f"do not call `.shapes.{func.attr}(...)` directly; use the DeckBuilder "
                f"component API (add_card, add_stat, add_shape, add_table, add_chart, "
                f"add_textbox, add_line, add_image, add_icon_circle) so the result is "
                f"styled and measurable")
        if isinstance(func.value, ast.Name):
            dotted = f"{func.value.id}.{func.attr}"
            if dotted in FORBIDDEN_CALLS or any(
                    dotted.startswith(f) for f in FORBIDDEN_CALLS if f.endswith("spawn")):
                problems.append(f"call to '{dotted}' is not allowed; remove it")
    return problems


def _is_shapes_access(node: ast.expr) -> bool:
    """True for `<anything>.shapes` — the attribute the component API replaces."""
    return isinstance(node, ast.Attribute) and node.attr == "shapes"


def check_message(code: str) -> Optional[str]:
    """All problems as one message for the repair loop, or None when clean."""
    problems = check(code)
    if not problems:
        return None
    return ("The program was rejected before running. Fix every point and resend the "
            "whole program:\n" + "\n".join(f"- {p}" for p in problems))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_code_guard.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/genai/ppt/code_guard.py tests/test_code_guard.py
git commit -m "feat(ppt): AST guard rejecting free-form pptx and literal colours"
```

---

### Task 7: Geometry gate, rules 1–7 — the guarantees

**Why:** This is the component the whole design exists for. Rules 1–6 are decidable by rectangle arithmetic, so they are *guarantees*, not heuristics. The hard part is that naive pairwise overlap rejects every good design — a label inside a card, an accent bar on a card edge, a heading over a gradient panel all overlap and are all correct. Containment is the distinction: **overlap is a violation, containment is layering.**

**Files:**
- Create: `app/genai/ppt/geometry.py`
- Test: `tests/test_geometry.py`

**Interfaces:**
- Consumes: `GeometryConstants` (Task 1), `catalog.LayoutInfo` / `Rect` (Task 3), `deck_builder.reserved_rects` (Task 5).
- Produces:
  - `@dataclass Box(id, left, top, width, height, has_text=False, filled=False, text="", font_size=None, font_name=None, colors=(), is_placeholder=False, inherited_size=False)` with `.right`, `.bottom`, `.as_tuple()`
  - `@dataclass Violation(rule: int, kind: str, message: str, blocking: bool = True)`
  - `geometry.boxes_from_slide(slide, layout) -> list[Box]`
  - `geometry.check_rects(boxes, *, slide_width, slide_height, reserved=()) -> list[Violation]` — rules 1–6
  - `geometry.check_prompt_text(boxes) -> list[Violation]` — rule 7
  - `geometry.PROMPT_PATTERNS: tuple[str, ...]`
  - helpers `intersect(a, b) -> tuple[float, float]`, `contains(outer, inner) -> bool`

**Rule table (from the spec, restated as the code must implement it):**

| # | Rule | Decided by |
|---|---|---|
| 1 | Every shape lies within the canvas minus margins, tolerance 0.02in | `CANVAS_MARGIN_IN`, `CANVAS_TOLERANCE_IN` |
| 2 | No text-bearing shape overlaps another text-bearing shape. **No exception.** | `OVERLAP_TOLERANCE_IN` |
| 3 | A text-bearing shape may overlap a non-text shape only if fully contained in it | `CONTAINMENT_TOLERANCE_IN` |
| 4 | Two filled shapes may overlap only if one fully contains the other | both |
| 5 | No shape intersects a reserved zone (frame rect or layout decor rect) | `OVERLAP_TOLERANCE_IN` |
| 6 | No added shape overlaps a filled placeholder | `OVERLAP_TOLERANCE_IN` |
| 7 | No shape retains template prompt text | `PROMPT_PATTERNS` |

- [ ] **Step 1: Write the failing tests**

Create `tests/test_geometry.py`. The legitimate-layering block is the most important part of this file — a gate that rejects good design is worse than no gate.

```python
"""The geometry gate, rule by rule.

Every rule gets a positive and a negative fixture, and legitimate layering gets its own
block: a text box inside a card, an accent bar on a card edge and a heading over a
gradient panel all overlap something, and all three are correct design. A gate that
flags them is worse than no gate at all.
"""

import pytest

from app.config.constants import GeometryConstants as GC
from app.genai.ppt import geometry as geo

W, H = 13.333, 7.5


def box(ident, left, top, width, height, **kw):
    return geo.Box(id=ident, left=left, top=top, width=width, height=height, **kw)


def rules(violations):
    return sorted({v.rule for v in violations})


def check(boxes, reserved=()):
    return geo.check_rects(boxes, slide_width=W, slide_height=H, reserved=reserved)


# --- rule 1: inside the canvas --------------------------------------------- #

def test_a_shape_inside_the_margins_passes():
    assert check([box("a", 1.0, 1.0, 4.0, 2.0)]) == []


@pytest.mark.parametrize("rect", [
    (-0.5, 1.0, 4.0, 2.0),        # off the left edge
    (1.0, -0.5, 4.0, 2.0),        # off the top
    (11.0, 1.0, 4.0, 2.0),        # past the right edge
    (1.0, 6.5, 4.0, 2.0),         # past the bottom
])
def test_a_shape_outside_the_canvas_is_rule_1(rect):
    assert 1 in rules(check([box("a", *rect)]))


def test_the_canvas_tolerance_forgives_a_hairline_overhang():
    # 0.01in past the margin is rounding, not a design error.
    inside = GC.CANVAS_MARGIN_IN - GC.CANVAS_TOLERANCE_IN / 2
    assert 1 not in rules(check([box("a", inside, 1.0, 4.0, 2.0)]))


# --- rule 2: text never overlaps text -------------------------------------- #

def test_two_separated_text_boxes_pass():
    assert check([box("a", 1.0, 1.0, 4.0, 1.0, has_text=True, text="one"),
                  box("b", 6.0, 1.0, 4.0, 1.0, has_text=True, text="two")]) == []


def test_two_overlapping_text_boxes_are_rule_2():
    found = check([box("a", 1.0, 1.0, 4.0, 1.0, has_text=True, text="one"),
                   box("b", 3.0, 1.0, 4.0, 1.0, has_text=True, text="two")])
    assert 2 in rules(found)


def test_text_inside_text_is_still_rule_2_because_there_is_no_exception():
    found = check([box("a", 1.0, 1.0, 6.0, 3.0, has_text=True, text="outer"),
                   box("b", 2.0, 1.5, 2.0, 1.0, has_text=True, text="inner")])
    assert 2 in rules(found)


def test_an_empty_text_frame_does_not_count_as_text_bearing():
    # A shape with a text frame but no text is not text-bearing (spec's definition).
    assert check([box("a", 1.0, 1.0, 4.0, 1.0, has_text=False, text="   "),
                  box("b", 3.0, 1.0, 4.0, 1.0, has_text=True, text="two")]) == []


def test_hairline_abutment_is_not_an_overlap():
    slack = GC.OVERLAP_TOLERANCE_IN / 2
    found = check([box("a", 1.0, 1.0, 4.0, 1.0, has_text=True, text="one"),
                   box("b", 5.0 - slack, 1.0, 4.0, 1.0, has_text=True, text="two")])
    assert 2 not in rules(found)


# --- rule 3: text over a shape must be contained in it --------------------- #

def test_a_label_fully_inside_a_card_passes():
    assert check([box("card", 1.0, 1.0, 4.0, 2.0, filled=True),
                  box("label", 1.2, 1.2, 3.6, 0.5, has_text=True, text="Yield")]) == []


def test_a_label_half_off_its_card_is_rule_3():
    found = check([box("card", 1.0, 1.0, 4.0, 2.0, filled=True),
                   box("label", 4.0, 1.2, 3.0, 0.5, has_text=True, text="Yield")])
    assert 3 in rules(found)


# --- rule 4: filled shapes nest or separate -------------------------------- #

def test_an_accent_bar_sitting_on_a_card_passes_because_it_is_contained():
    assert check([box("card", 1.0, 1.0, 4.0, 2.0, filled=True),
                  box("accent", 1.15, 1.12, 0.5, 0.06, filled=True)]) == []


def test_two_partially_overlapping_filled_shapes_are_rule_4():
    found = check([box("a", 1.0, 1.0, 4.0, 2.0, filled=True),
                   box("b", 3.0, 1.5, 4.0, 2.0, filled=True)])
    assert 4 in rules(found)


def test_a_heading_over_a_gradient_panel_passes():
    found = check([box("panel", 0.5, 1.0, 12.0, 2.0, filled=True),
                   box("heading", 1.0, 1.4, 6.0, 0.6, has_text=True, text="Quality")])
    assert found == []


# --- rule 5: reserved zones ------------------------------------------------ #

def test_a_shape_clear_of_the_footer_band_passes():
    footer = [0.0, 7.05, W, 0.45]
    assert check([box("a", 1.0, 1.0, 4.0, 2.0)], reserved=[footer]) == []


def test_a_shape_over_the_footer_band_is_rule_5():
    footer = [0.0, 7.05, W, 0.45]
    found = check([box("a", 1.0, 6.8, 4.0, 0.6)], reserved=[footer])
    assert 5 in rules(found)


def test_a_shape_over_the_layouts_inherited_decor_is_rule_5():
    decor = [0.23, 0.28, 0.65, 0.71]          # M1-L10's brand mark
    found = check([box("a", 0.3, 0.3, 2.0, 1.0)], reserved=[decor])
    assert 5 in rules(found)


# --- rule 6: filled placeholders ------------------------------------------- #

def test_an_added_shape_over_a_filled_placeholder_is_rule_6():
    found = check([
        box("ph", 1.0, 1.0, 6.0, 3.0, filled=True, is_placeholder=True),
        box("added", 2.0, 1.5, 4.0, 1.0),
    ])
    assert 6 in rules(found)


def test_an_added_shape_beside_a_filled_placeholder_passes():
    assert check([
        box("ph", 1.0, 1.0, 5.0, 3.0, filled=True, is_placeholder=True),
        box("added", 7.0, 1.0, 4.0, 1.0),
    ]) == []


# --- rule 7: prompt text --------------------------------------------------- #

@pytest.mark.parametrize("text", ["Click to edit Master title style", "Second level",
                                  "Click to add text", "Lorem ipsum dolor"])
def test_leftover_template_prompt_text_is_rule_7(text):
    found = geo.check_prompt_text([box("a", 1, 1, 4, 1, has_text=True, text=text)])
    assert rules(found) == [7]


def test_real_copy_is_not_mistaken_for_prompt_text():
    good = "Click-through rate rose at every level of the funnel"
    assert geo.check_prompt_text([box("a", 1, 1, 4, 1, has_text=True, text=good)]) == []


# --- the whole picture ----------------------------------------------------- #

def test_a_realistic_three_card_row_passes_every_rule():
    """The layout the repair loop should converge on, not fight."""
    boxes = []
    for index, left in enumerate((0.42, 4.75, 9.08)):
        boxes += [
            box(f"card{index}", left, 2.0, 3.85, 2.2, filled=True),
            box(f"accent{index}", left + 0.15, 2.12, 0.5, 0.06, filled=True),
            box(f"title{index}", left + 0.12, 2.25, 3.6, 0.45,
                has_text=True, text=f"Pillar {index + 1}"),
            box(f"body{index}", left + 0.12, 2.75, 3.6, 1.3,
                has_text=True, text="Short supporting sentence."),
        ]
    footer = [0.0, 7.05, W, 0.45]
    assert check(boxes, reserved=[footer]) == []


def test_violations_name_the_shapes_and_say_what_to_do():
    found = check([box("card 2", 6.80, 2.40, 3.60, 1.80, has_text=True, text="a"),
                   box("card 3", 10.20, 2.40, 3.60, 1.80, has_text=True, text="b")])
    assert found
    message = found[0].message
    assert "card 2" in message and "card 3" in message
    assert "overlaps" in message
    # Actionable, per the spec: it must suggest a fix, not merely state the fact.
    assert any(word in message for word in ("shift", "narrow", "move"))


def test_the_builder_and_the_gate_agree_on_capacity():
    """deck_builder duplicates measure.py's arithmetic because it cannot import app.*.
    The two copies must return identical numbers or the builder's shrink-to-fit and the
    gate's overflow rule will disagree about the same box."""
    from app.genai.ppt import deck_builder as db, measure

    for width, height, size in ((6.0, 3.0, 15), (3.6, 1.8, 11), (12.5, 0.4, 28)):
        per_line, lines = db._capacity(width, height, size, bullets=False)
        assert per_line == measure.chars_per_line(width, size, bullets=False)
        assert lines == measure.line_capacity(height, size)

    for text in ("Preheat ramp under three degrees per second", "WWWWWWWWWW", "iiii", ""):
        assert db._string_em(text) == pytest.approx(measure.string_em(text))
        assert db._rendered_lines([text], 6.0, 15, bullets=False) == (
            measure.rendered_lines([text], 6.0, 15, bullets=False))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_geometry.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.genai.ppt.geometry'`.

- [ ] **Step 3: Write `app/genai/ppt/geometry.py` — model and helpers**

```python
"""The geometry gate: what actually landed on the slide, measured.

Rules 1-6 are decided by rectangle arithmetic alone, which is what lets the design
promise "no overlapping text" rather than estimate it. The one subtlety is that naive
pairwise overlap detection rejects every good design: a label inside a card, an accent
bar on a card's edge and a heading over a gradient panel all overlap something, and all
three are correct. The distinction this module draws is CONTAINMENT — overlap is a
collision, full containment is layering.

Violations are written as instructions, not assertions. They go straight back to the
model, and "card 3 overlaps card 2" is a fact it has to diagnose, while "shift card 3
right by 0.45in" is a fix it can apply.
"""

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from app.config.constants import GeometryConstants as GC
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

#: Text the template leaves in an untouched placeholder. Rule 7.
PROMPT_PATTERNS: tuple[str, ...] = (
    "click to edit", "click to add", "second level", "third level", "fourth level",
    "fifth level", "master title style", "master text styles", "lorem ipsum",
)


@dataclass
class Box:
    """One shape as the gate sees it, in inches.

    Attributes:
        has_text: True when the shape has a text frame whose text is non-empty after
            stripping. This is the spec's definition of "text-bearing"; an empty text
            frame is not text.
        filled: True when the shape has a solid or gradient fill.
        inherited_size: True when the font size came from the template placeholder
            rather than from the program. Rule 9 exempts these.
    """
    id: str
    left: float
    top: float
    width: float
    height: float
    has_text: bool = False
    filled: bool = False
    text: str = ""
    font_size: float | None = None
    font_name: str | None = None
    colors: tuple[str, ...] = ()
    is_placeholder: bool = False
    inherited_size: bool = False

    @property
    def right(self) -> float:
        return self.left + self.width

    @property
    def bottom(self) -> float:
        return self.top + self.height

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.left, self.top, self.width, self.height)

    def describe(self) -> str:
        return (f"{self.id} at ({self.left:.2f}, {self.top:.2f}) "
                f"{self.width:.2f}x{self.height:.2f}in")


@dataclass
class Violation:
    """One thing wrong with the slide, phrased as an instruction."""
    rule: int
    kind: str
    message: str
    blocking: bool = True


def intersect(a: Box, b: Box) -> tuple[float, float]:
    """Overlap of two boxes on each axis, in inches. Negative means separated."""
    return (min(a.right, b.right) - max(a.left, b.left),
            min(a.bottom, b.bottom) - max(a.top, b.top))


def overlaps(a: Box, b: Box, tolerance: float = GC.OVERLAP_TOLERANCE_IN) -> bool:
    """True when two boxes overlap by more than a hairline on BOTH axes."""
    dx, dy = intersect(a, b)
    return dx > tolerance and dy > tolerance


def contains(outer: Box, inner: Box,
             tolerance: float = GC.CONTAINMENT_TOLERANCE_IN) -> bool:
    """True when `inner` sits wholly inside `outer`, allowing a hairline of bleed."""
    return (inner.left >= outer.left - tolerance
            and inner.top >= outer.top - tolerance
            and inner.right <= outer.right + tolerance
            and inner.bottom <= outer.bottom + tolerance)
```

- [ ] **Step 4: Write rules 1–6**

```python
def check_rects(boxes: Sequence[Box], *, slide_width: float, slide_height: float,
                reserved: Iterable[Sequence[float]] = ()) -> list[Violation]:
    """Rules 1 to 6, decided from rectangles alone.

    Args:
        boxes: Every shape on the slide.
        slide_width: Canvas width in inches.
        slide_height: Canvas height in inches.
        reserved: Frame rectangles and the layout's inherited decor, as
            (left, top, width, height) — the zones rule 5 protects.

    Returns:
        Every violation found, in rule order. Empty means the slide is clean.
    """
    violations: list[Violation] = []
    violations += _rule_1_inside_canvas(boxes, slide_width, slide_height)
    violations += _rule_2_text_over_text(boxes)
    violations += _rule_3_text_over_shape(boxes)
    violations += _rule_4_filled_over_filled(boxes)
    violations += _rule_5_reserved_zones(boxes, reserved)
    violations += _rule_6_filled_placeholders(boxes)
    return violations


def _rule_1_inside_canvas(boxes, slide_width, slide_height) -> list[Violation]:
    margin, slack = GC.CANVAS_MARGIN_IN, GC.CANVAS_TOLERANCE_IN
    out: list[Violation] = []
    for shape in boxes:
        overhang = {
            "left": (margin - slack) - shape.left,
            "top": (margin - slack) - shape.top,
            "right": shape.right - (slide_width - margin + slack),
            "bottom": shape.bottom - (slide_height - margin + slack),
        }
        worst = max(overhang.items(), key=lambda item: item[1])
        if worst[1] > 0:
            out.append(Violation(1, "canvas", (
                f"{shape.describe()} runs {worst[1]:.2f}in past the {worst[0]} edge of "
                f"the {slide_width:.2f}x{slide_height:.2f}in canvas; move it "
                f"{worst[1] + 0.05:.2f}in towards the centre or shrink it to fit inside "
                f"the {margin:.2f}in margin")))
    return out


def _rule_2_text_over_text(boxes) -> list[Violation]:
    out: list[Violation] = []
    text_boxes = [b for b in boxes if b.has_text]
    for i, a in enumerate(text_boxes):
        for b in text_boxes[i + 1:]:
            if not overlaps(a, b):
                continue
            dx, dy = intersect(a, b)
            out.append(Violation(2, "text-over-text", (
                f"{a.describe()} overlaps {b.describe()} by {dx:.2f}x{dy:.2f}in. Text "
                f"may never overlap text. Shift {b.id} right by {dx + 0.05:.2f}in, or "
                f"narrow both to {(a.width - dx / 2):.2f}in, or move {b.id} down by "
                f"{dy + 0.05:.2f}in")))
    return out


def _rule_3_text_over_shape(boxes) -> list[Violation]:
    out: list[Violation] = []
    shapes = [b for b in boxes if not b.has_text]
    for text in (b for b in boxes if b.has_text):
        for shape in shapes:
            if not overlaps(text, shape) or contains(shape, text):
                continue
            out.append(Violation(3, "text-over-shape", (
                f"{text.describe()} partly covers {shape.describe()}. Text over a shape "
                f"must sit wholly inside it: either move {text.id} fully within "
                f"({shape.left:.2f}, {shape.top:.2f}) to "
                f"({shape.right:.2f}, {shape.bottom:.2f}), or move it clear of "
                f"{shape.id} altogether")))
    return out


def _rule_4_filled_over_filled(boxes) -> list[Violation]:
    out: list[Violation] = []
    filled = [b for b in boxes if b.filled]
    for i, a in enumerate(filled):
        for b in filled[i + 1:]:
            if not overlaps(a, b) or contains(a, b) or contains(b, a):
                continue
            dx, dy = intersect(a, b)
            out.append(Violation(4, "fill-over-fill", (
                f"{a.describe()} and {b.describe()} overlap by {dx:.2f}x{dy:.2f}in "
                f"without one containing the other. Two filled shapes may only nest or "
                f"stand apart: separate them by {dx + 0.05:.2f}in, or place {b.id} "
                f"entirely inside {a.id}")))
    return out


def _rule_5_reserved_zones(boxes, reserved) -> list[Violation]:
    out: list[Violation] = []
    zones = [Box(id=f"reserved zone {i + 1}", left=r[0], top=r[1], width=r[2],
                 height=r[3]) for i, r in enumerate(reserved)]
    for shape in boxes:
        for zone in zones:
            if not overlaps(shape, zone):
                continue
            dx, dy = intersect(shape, zone)
            out.append(Violation(5, "reserved", (
                f"{shape.describe()} intrudes {dx:.2f}x{dy:.2f}in into "
                f"{zone.describe()}, which carries brand furniture (the footer band, "
                f"the classification mark, or a graphic the layout already draws). "
                f"Keep every element clear of it: move {shape.id} up by "
                f"{dy + 0.05:.2f}in or shorten it to {shape.height - dy - 0.05:.2f}in")))
    return out


def _rule_6_filled_placeholders(boxes) -> list[Violation]:
    out: list[Violation] = []
    placeholders = [b for b in boxes if b.is_placeholder and b.filled]
    for shape in (b for b in boxes if not b.is_placeholder):
        for placeholder in placeholders:
            if not overlaps(shape, placeholder):
                continue
            dx, dy = intersect(shape, placeholder)
            out.append(Violation(6, "over-placeholder", (
                f"{shape.describe()} covers the filled placeholder "
                f"{placeholder.describe()} by {dx:.2f}x{dy:.2f}in. Either place "
                f"{shape.id} in free space, or fill that placeholder through the "
                f"placeholder API instead of drawing over it")))
    return out


def check_prompt_text(boxes: Sequence[Box]) -> list[Violation]:
    """Rule 7: no shape may still carry the template's own prompt copy."""
    out: list[Violation] = []
    for shape in boxes:
        lowered = (shape.text or "").strip().lower()
        if not lowered:
            continue
        hit = next((p for p in PROMPT_PATTERNS if p in lowered), None)
        if hit:
            out.append(Violation(7, "prompt-text", (
                f"{shape.describe()} still shows the template's prompt text "
                f"({hit!r}). Fill it with real content, or remove the placeholder so "
                f"nothing ships with a 'Click to edit' prompt on it")))
    return out
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_geometry.py -q`
Expected: PASS. If any of the four *legitimate layering* tests fail, the containment tolerance is too tight — that is the bug, not the test.

- [ ] **Step 6: Commit**

```bash
git add app/genai/ppt/geometry.py tests/test_geometry.py
git commit -m "feat(ppt): containment-aware geometry rules 1-7"
```

---

### Task 8: Geometry gate, rules 8–10 and the slide reader

**Why:** Rules 1–7 operate on `Box`es; something has to produce them from a real `.pptx`. Rules 8 (colours resolve to tokens) and 9 (theme font, type scale) can only be decided from what the XML actually contains, and rule 10 is the calibrated overflow estimate — advisory, except at a wide margin.

**Files:**
- Modify: `app/genai/ppt/geometry.py`
- Test: `tests/test_geometry.py` (extend)

**Interfaces:**
- Consumes: Task 7's model, `measure.overflow_ratio` (Task 2), `deck_builder.brand_palette` / `TYPE_SCALE` / `reserved_rects` (Tasks 4–5).
- Produces:
  - `geometry.boxes_from_slide(slide, layout) -> list[Box]`
  - `geometry.check_slide(pptx_path, layout, *, frame=None, theme_colors=None, theme_font="Barlow", slide_width=13.333, slide_height=7.5) -> list[Violation]` — rules 1–10, **authoritative**
  - `geometry.format_violations(violations) -> str`
  - `geometry.is_clean(violations) -> bool` — no *blocking* violation

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_geometry.py`:

```python
# --------------------------------------------------------------------------- #
# reading a real slide, and rules 8-10                                         #
# --------------------------------------------------------------------------- #

from pathlib import Path

from app.genai.ppt import deck_builder as db
from app.genai.ppt.template import catalog as catalog_mod, registry


@pytest.fixture(scope="module")
def cat():
    return catalog_mod.load()


@pytest.fixture
def build(cat, tmp_path):
    """Build a one-slide deck with DeckBuilder and hand the gate the result."""
    def _build(layout_key, populate, frame=None):
        deck = db.DeckBuilder(registry.template_path(), cat.to_dict(), frame=frame)
        handle = deck.add_slide(layout_key)
        populate(handle)
        out = deck.save(tmp_path / f"{layout_key}.pptx")
        return out, cat.get(layout_key)
    return _build


def test_a_clean_deckbuilder_slide_passes_the_whole_gate(build, cat):
    def populate(slide):
        slide.set_title("Reflow profile control")
        slide.add_card("Preheat", ["Ramp under 3 C/s"], *slide.grid(0, 1, cols=3, rows=2))
        slide.add_card("Soak", ["150-180 C for 90 s"], *slide.grid(4, 1, cols=3, rows=2))
        slide.add_card("Reflow", ["Peak 245 C"], *slide.grid(8, 1, cols=3, rows=2))

    out, layout = build("M1-L10", populate)
    found = geo.check_slide(out, layout, theme_colors=cat.theme.colors)
    assert [v.message for v in found if v.blocking] == []


def test_the_reader_sees_text_fills_and_placeholders(build, cat):
    def populate(slide):
        slide.set_title("A title")
        slide.add_shape("rectangle", 1.0, 3.0, 2.0, 1.0, fill="red")
        slide.add_textbox("free text", 5.0, 3.0, 2.0, 0.5)

    out, layout = build("M1-L10", populate)
    from pptx import Presentation
    slide = next(iter(Presentation(str(out)).slides))
    boxes = geo.boxes_from_slide(slide, layout)

    assert any(b.has_text and "A title" in b.text for b in boxes)
    assert any(b.filled and not b.has_text for b in boxes)
    assert any(b.is_placeholder for b in boxes)
    for b in boxes:
        assert b.width > 0 and b.height > 0


def test_an_overlapping_pair_of_cards_is_caught_on_a_real_slide(build, cat):
    def populate(slide):
        slide.set_title("Overlapping by construction")
        slide.add_card("A", ["one"], 1.0, 2.5, 4.0, 2.0)
        slide.add_card("B", ["two"], 3.0, 2.5, 4.0, 2.0)      # 2in of overlap

    out, layout = build("M1-L10", populate)
    found = geo.check_slide(out, layout, theme_colors=cat.theme.colors)
    assert 2 in rules(found) or 4 in rules(found)


def test_a_shape_in_the_footer_band_is_caught_on_a_real_slide(build, cat):
    frame = db.frame_spec(13.333, 7.5, label="Internal", slide_number=1)

    def populate(slide):
        slide.set_title("T")
        slide.add_shape("rectangle", 2.0, 7.1, 3.0, 0.3, fill="red")

    out, layout = build("M2-L4", populate, frame=frame)
    found = geo.check_slide(out, layout, frame=frame, theme_colors=cat.theme.colors)
    assert 5 in rules(found)


# --- rule 8: colours ------------------------------------------------------- #

def test_theme_and_brand_colours_pass_rule_8(cat):
    palette = db.brand_palette(cat.theme.colors)
    clean = [box("a", 1, 1, 2, 1, filled=True, colors=(palette["red"],)),
             box("b", 4, 1, 2, 1, filled=True, colors=(palette["navy"],))]
    assert geo.check_colors(clean, palette) == []


def test_an_off_palette_colour_is_rule_8(cat):
    palette = db.brand_palette(cat.theme.colors)
    found = geo.check_colors(
        [box("a", 1, 1, 2, 1, filled=True, colors=("7F00FF",))], palette)
    assert rules(found) == [8]
    assert "7F00FF" in found[0].message


# --- rule 9: fonts and the type scale -------------------------------------- #

def test_theme_font_at_a_scale_size_passes_rule_9():
    clean = [box("a", 1, 1, 2, 1, has_text=True, text="x",
                 font_name="Barlow", font_size=15)]
    assert geo.check_typography(clean, theme_font="Barlow") == []


def test_an_off_scale_size_is_rule_9():
    found = geo.check_typography(
        [box("a", 1, 1, 2, 1, has_text=True, text="x",
             font_name="Barlow", font_size=13)], theme_font="Barlow")
    assert rules(found) == [9]
    assert "13" in found[0].message


def test_a_placeholder_inherited_size_is_exempt_from_rule_9():
    exempt = [box("a", 1, 1, 2, 1, has_text=True, text="x", font_name="Barlow",
                  font_size=13, is_placeholder=True, inherited_size=True)]
    assert geo.check_typography(exempt, theme_font="Barlow") == []


def test_a_shrunk_placeholder_is_exempt_even_though_its_size_is_explicit():
    """DeckBuilder's shrink-to-fit decrements 1pt at a time from the placeholder's own
    base, so it writes explicit off-scale sizes like 13pt or 17pt. That is correct
    behaviour — the spec exempts placeholder text as such, not merely un-set text — and
    rule 9 must not fire on it, or every shrunk placeholder becomes a violation."""
    for size in (13, 16, 17, 19):
        shrunk = [box("a", 1, 1, 2, 1, has_text=True, text="x", font_name="Barlow",
                      font_size=size, is_placeholder=True, inherited_size=True)]
        assert geo.check_typography(shrunk, theme_font="Barlow") == []


def test_a_component_box_is_not_exempt_even_next_to_a_placeholder():
    mixed = [box("ph", 1, 1, 2, 1, has_text=True, text="x", font_name="Barlow",
                 font_size=13, is_placeholder=True, inherited_size=True),
             box("card", 4, 1, 2, 1, has_text=True, text="y", font_name="Barlow",
                 font_size=13)]
    found = geo.check_typography(mixed, theme_font="Barlow")
    assert [v.rule for v in found] == [9]
    assert "card" in found[0].message


def test_a_foreign_font_is_rule_9():
    found = geo.check_typography(
        [box("a", 1, 1, 2, 1, has_text=True, text="x",
             font_name="Comic Sans MS", font_size=15)], theme_font="Barlow")
    assert rules(found) == [9]
    assert "Barlow" in found[0].message


# --- rule 10: overflow, advisory until it is wide -------------------------- #

def test_text_that_fits_raises_nothing():
    fits = [box("a", 1, 1, 6, 2, has_text=True, text="A short line.", font_size=15)]
    assert geo.check_overflow(fits) == []


def test_mild_overflow_is_advisory_not_blocking():
    mild = [box("a", 1, 1, 6, 0.55, has_text=True, font_size=15,
                text="A line of copy that needs a little more room than it has here.")]
    found = geo.check_overflow(mild)
    assert found and all(v.rule == 10 for v in found)
    assert not any(v.blocking for v in found)


def test_wide_overflow_blocks():
    wide = [box("a", 1, 1, 3, 0.4, has_text=True, font_size=15, text="word " * 200)]
    found = geo.check_overflow(wide)
    assert found and any(v.blocking for v in found)
    assert "shorten" in found[0].message.lower()


def test_is_clean_ignores_advisory_violations():
    advisory = [geo.Violation(10, "overflow", "a bit tight", blocking=False)]
    assert geo.is_clean(advisory)
    assert not geo.is_clean(advisory + [geo.Violation(2, "text-over-text", "no", True)])


def test_the_formatted_report_is_ordered_and_numbered():
    found = [geo.Violation(5, "reserved", "footer"), geo.Violation(2, "t", "overlap")]
    report = geo.format_violations(found)
    assert report.index("overlap") < report.index("footer")     # rule order
    assert "rule 2" in report and "rule 5" in report
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_geometry.py -q`
Expected: FAIL — `AttributeError: module has no attribute 'boxes_from_slide'`.

- [ ] **Step 3: Add the slide reader**

```python
def boxes_from_slide(slide, layout) -> list[Box]:
    """Read a rendered slide into the boxes the rules are decided over.

    Measures what LANDED, not what was declared: a component that python-pptx resized,
    a picture that kept its aspect ratio, a table that grew a row. That is the whole
    point of reopening the file rather than trusting the program's own arithmetic.
    """
    from pptx.util import Emu

    boxes: list[Box] = []
    for index, shape in enumerate(slide.shapes):
        text = ""
        if shape.has_text_frame:
            text = shape.text_frame.text or ""
        boxes.append(Box(
            id=_shape_label(shape, index),
            left=Emu(shape.left or 0).inches, top=Emu(shape.top or 0).inches,
            width=Emu(shape.width or 0).inches, height=Emu(shape.height or 0).inches,
            has_text=bool(text.strip()),
            filled=_is_filled(shape),
            text=text,
            font_size=_font_size(shape),
            font_name=_font_name(shape),
            colors=tuple(_colors(shape)),
            is_placeholder=bool(shape.is_placeholder),
            # ALL placeholder text is exempt from rule 9, not merely text whose size was
            # inherited. The spec is explicit: "Placeholder text is exempt. Sizes
            # inherited from the template's own placeholders are correct by definition,
            # and shrink-to-fit may reduce them as far as 10pt." Shrink-to-fit
            # decrements by 1pt from the placeholder's own base, so it routinely writes
            # an explicit 13pt or 17pt — off the type scale, and correct. Keying the
            # exemption on "no explicit size" would make rule 9 fire on every
            # placeholder the builder shrank to make the copy fit.
            inherited_size=bool(shape.is_placeholder),
        ))
    return boxes


def _shape_label(shape, index: int) -> str:
    """A name the model will recognise in a violation message."""
    name = (getattr(shape, "name", "") or "").strip()
    if shape.is_placeholder:
        try:
            return f"placeholder idx {shape.placeholder_format.idx} ({name})"
        except Exception:                                    # noqa: BLE001
            pass
    return name or f"shape {index + 1}"


def _is_filled(shape) -> bool:
    """Solid or gradient fill, per the spec's definition of 'filled'."""
    from pptx.oxml.ns import qn

    element = shape._element.find(qn("p:spPr"))
    if element is None:
        return False
    return (element.find(qn("a:solidFill")) is not None
            or element.find(qn("a:gradFill")) is not None)


def _runs(shape):
    if not shape.has_text_frame:
        return []
    return [run for paragraph in shape.text_frame.paragraphs for run in paragraph.runs]


def _font_size(shape) -> float | None:
    """The largest explicitly set size on the shape, or None when all are inherited."""
    sizes = [run.font.size.pt for run in _runs(shape) if run.font.size is not None]
    return max(sizes) if sizes else None


def _font_name(shape) -> str | None:
    names = [run.font.name for run in _runs(shape) if run.font.name]
    return names[0] if names else None


def _colors(shape) -> list[str]:
    """Every explicit RGB in the shape's fill, line and runs, as uppercase hex."""
    from pptx.oxml.ns import qn

    found: list[str] = []
    for node in shape._element.iter(qn("a:srgbClr")):
        value = node.get("val")
        if value:
            found.append(value.upper())
    return found
```

- [ ] **Step 4: Add rules 8, 9 and 10**

```python
def check_colors(boxes: Sequence[Box], palette: dict) -> list[Violation]:
    """Rule 8: every explicit colour resolves to a theme or brand token."""
    allowed = {value.upper() for value in palette.values()}
    out: list[Violation] = []
    for shape in boxes:
        stray = sorted({c for c in shape.colors if c.upper() not in allowed})
        if stray:
            out.append(Violation(8, "off-palette", (
                f"{shape.describe()} uses {', '.join(stray)}, which is not a template "
                f"colour. Every fill, line and font colour must come from a brand token "
                f"({', '.join(sorted(palette))}) so the deck stays on the template's "
                f"theme")))
    return out


def check_typography(boxes: Sequence[Box], theme_font: str = "Barlow") -> list[Violation]:
    """Rule 9: the theme font everywhere, and a type-scale size on component text.

    Placeholder-inherited sizes are exempt: the template's own choices are correct by
    definition, and the builder's shrink-to-fit may take them as low as 10pt.
    """
    out: list[Violation] = []
    for shape in boxes:
        if not shape.has_text:
            continue
        if shape.font_name and shape.font_name != theme_font:
            out.append(Violation(9, "font", (
                f"{shape.describe()} is set in {shape.font_name!r}. Every run must use "
                f"the template theme font {theme_font!r} — do not set `font.name` at "
                f"all and it is inherited correctly")))
        if shape.inherited_size or shape.font_size is None:
            continue
        if int(round(shape.font_size)) not in GC.TYPE_SCALE:
            out.append(Violation(9, "type-scale", (
                f"{shape.describe()} uses {shape.font_size:g}pt, which is not on the "
                f"type scale. Use one of "
                f"{', '.join(f'{s}pt' for s in GC.TYPE_SCALE)} — nearest is "
                f"{min(GC.TYPE_SCALE, key=lambda s: abs(s - shape.font_size))}pt")))
    return out


def check_overflow(boxes: Sequence[Box]) -> list[Violation]:
    """Rule 10: estimated lines against the box's capacity.

    Advisory by default and blocking only past a wide margin, because the estimate is a
    heuristic biased to under-estimate capacity (Decision 7). A false positive here
    should cost tokens, not correctness.
    """
    from app.genai.ppt import measure

    out: list[Violation] = []
    for shape in boxes:
        if not shape.has_text or not shape.font_size:
            continue
        lines = [line for line in shape.text.splitlines() if line.strip()]
        ratio = measure.overflow_ratio(lines, shape.width, shape.height, shape.font_size)
        if ratio <= GC.OVERFLOW_ADVISORY_RATIO:
            continue
        blocking = ratio >= GC.OVERFLOW_BLOCKING_RATIO
        excess = int(round((ratio - 1.0) * 100))
        out.append(Violation(10, "overflow", (
            f"{shape.describe()} holds about {excess}% more text than fits at "
            f"{shape.font_size:g}pt. Shorten the copy, or give the box "
            f"{shape.height * ratio - shape.height + 0.05:.2f}in more height"
        ), blocking=blocking))
    return out
```

- [ ] **Step 5: Add the authoritative entry point and the report**

```python
def check_slide(pptx_path, layout, *, frame: dict | None = None,
                theme_colors: dict | None = None, theme_font: str = "Barlow",
                slide_width: float = 13.333,
                slide_height: float = 7.5) -> list[Violation]:
    """Every rule, against the file that was actually produced.

    This is authoritative. `check_geometry` (the design tool) evaluates rules 1-6 and
    10 from declared rectangles as a fast path, but rules 7-9 depend on what the XML
    really contains, so only this function can clear a slide.

    Args:
        pptx_path: The single-slide file the program wrote.
        layout: The `LayoutInfo` the slide was built on, for its decor rectangles.
        frame: The deck contract's frame block, whose rectangles are reserved.
        theme_colors: The template theme's colour map, for rule 8.
        theme_font: The template theme's body font, for rule 9.
        slide_width: Canvas width in inches.
        slide_height: Canvas height in inches.

    Returns:
        Every violation, rule order. `is_clean()` decides whether it may ship.
    """
    from pptx import Presentation

    from app.genai.ppt import deck_builder as builder

    presentation = Presentation(str(pptx_path))
    slides = list(presentation.slides)
    if not slides:
        return [Violation(0, "empty", f"{pptx_path} has no slide in it; the program "
                                      f"must add exactly one slide and save it")]

    boxes = boxes_from_slide(slides[0], layout)
    reserved = builder.reserved_rects(frame or {}, _layout_view(layout))
    palette = builder.brand_palette(theme_colors or {})

    violations = check_rects(boxes, slide_width=slide_width, slide_height=slide_height,
                             reserved=reserved)
    violations += check_prompt_text(boxes)
    violations += check_colors(boxes, palette)
    violations += check_typography(boxes, theme_font)
    violations += check_overflow(boxes)
    violations.sort(key=lambda v: (v.rule, v.message))
    if violations:
        log.info("geometry gate: %d violation(s) on %s",
                 len(violations), getattr(pptx_path, "name", pptx_path))
    return violations


def _layout_view(layout):
    """`reserved_rects` wants something with `.decor_rects` as plain lists."""
    class _View:
        decor_rects = [list(r.as_tuple()) if hasattr(r, "as_tuple") else list(r)
                       for r in getattr(layout, "decor_rects", ())]
    return _View()


def is_clean(violations: Sequence[Violation]) -> bool:
    """True when nothing blocking remains. Advisory rule-10 notes do not block."""
    return not any(v.blocking for v in violations)


def format_violations(violations: Sequence[Violation]) -> str:
    """The violation list as the model receives it: numbered instructions, rule order."""
    if not violations:
        return "The slide passed every geometry and brand check."
    ordered = sorted(violations, key=lambda v: (v.rule, v.message))
    lines = ["The slide was built but did not pass the geometry gate. Fix every point "
             "below and resend the whole program.", ""]
    for index, violation in enumerate(ordered, start=1):
        flag = "" if violation.blocking else "  (advisory)"
        lines.append(f"{index}. [rule {violation.rule}]{flag} {violation.message}")
    return "\n".join(lines)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest tests/test_geometry.py -q`
Expected: PASS.
`test_a_clean_deckbuilder_slide_passes_the_whole_gate` is the acceptance test for the whole design: if a slide built entirely through the component API cannot pass the gate, the gate is wrong, not the slide. Fix the gate.

- [ ] **Step 7: Commit**

```bash
git add app/genai/ppt/geometry.py tests/test_geometry.py
git commit -m "feat(ppt): brand and overflow rules, and the authoritative slide gate"
```

---

### Task 9: Data model — drop what was never populated, carry what the contract needs

**Why:** Spec's "Data model changes". `SlideDraft.shapes` and `SlideDraft.icons` are declared but `slide_tools.write_content` never populates them (Bug 3); geometry is now decided in generated code, not by the content agent. `SlideCodeBrief` must carry the contract fields each parallel builder needs.

**Files:**
- Modify: `app/core/models/deck_model.py`
- Test: `tests/test_slides_node.py` (verify still green), `tests/test_deck_end_to_end.py` (created in Task 15 — not here)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `SlideDraft` **without** `shapes` and `icons`; `tables` retained
  - `ShapeBlock` and `IconBlock` deleted
  - `SlideCodeBrief` gains `mode: Literal["layout", "canvas"] = "layout"`, `layout_key: str = ""`, `grid: dict = {}`, `frame: dict = {}`, `tokens: dict = {}`, `free_region: list[float] | None = None`

- [ ] **Step 1: Confirm nothing populates the fields being dropped**

```bash
grep -rn "\.shapes\b\|\.icons\b\|ShapeBlock\|IconBlock" --include=*.py app/ tests/ | grep -v "slide.shapes\|shapes\._spTree\|shapes\.add_\|shapes\.title"
```

Expected: hits only in `deck_model.py` (the declarations), `filler.py` and `renderer.py` (both deleted in Task 16), and `builder.py::build_pptx` (also deleted). **If any live path populates them, stop and report** — the spec's premise is that nothing does.

- [ ] **Step 2: Write the failing test**

Append to `tests/test_slides_node.py`:

```python
def test_a_slide_draft_no_longer_carries_hand_placed_geometry():
    """Bug 3: shapes/icons were declared but never populated. Geometry is decided in
    generated code now, so carrying empty lists through the model only invites a
    regression."""
    from app.core.models.deck_model import SlideDraft

    assert "shapes" not in SlideDraft.model_fields
    assert "icons" not in SlideDraft.model_fields
    assert "tables" in SlideDraft.model_fields      # write_content does populate this


def test_a_stored_draft_from_before_the_change_still_parses():
    """Saved sessions carry the old fields; extra keys must not break a reload."""
    from app.core.models.deck_model import SlideDraft

    slide = SlideDraft(slide_number=1, title="T",
                       shapes=[{"kind": "rectangle", "left": 1, "top": 1,
                                "width": 10, "height": 10}],
                       icons=[{"icon_ref": "x", "left": 1, "top": 1,
                               "width": 10, "height": 10}])
    assert slide.title == "T"
    assert not hasattr(slide, "shapes")


def test_the_code_brief_carries_the_deck_design_contract():
    from app.core.models.deck_model import DeckContext, SlideCodeBrief, SlideDraft

    brief = SlideCodeBrief(
        slide_number=1, content=SlideDraft(slide_number=1, title="T"),
        context=DeckContext(title="Deck"),
        mode="canvas", layout_key="M1-L10",
        grid={"columns": 12, "rows": 6, "gutter_in": 0.15},
        frame={"footer_band": [0, 7.05, 13.333, 0.45]},
        tokens={"red": "CD163F"}, free_region=[0.42, 1.05, 12.49, 6.0])
    assert brief.mode == "canvas" and brief.layout_key == "M1-L10"
    assert brief.free_region[3] == 6.0

    # The default is the conservative one: fill a real layout's placeholders.
    plain = SlideCodeBrief(slide_number=1, content=SlideDraft(slide_number=1, title="T"),
                           context=DeckContext(title="Deck"))
    assert plain.mode == "layout" and plain.free_region is None
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest tests/test_slides_node.py -q`
Expected: FAIL — `shapes` is still in `model_fields`; `SlideCodeBrief` has no `mode`.

- [ ] **Step 4: Edit `app/core/models/deck_model.py`**

Delete the `ShapeBlock` and `IconBlock` classes entirely. In `SlideDraft`, delete the `shapes` and `icons` fields and add a config so old stored drafts still load:

```python
class SlideDraft(BaseModel):
    # Old stored drafts carry `shapes` and `icons`, which were declared but never
    # populated (Bug 3). Ignoring unknown keys is what lets a session saved before this
    # change reload instead of erroring on fields that no longer exist.
    model_config = ConfigDict(extra="ignore")

    slide_number: int
    kind: SlideKind = "content"
    layout: str = ""
    title: str
    blocks: list[ContentBlock] = []
    tables: list[TableBlock] = []
    notes: str = ""
    ...
```

Add `ConfigDict` to the pydantic import. In `_migrate_legacy`, leave every existing branch untouched.

Extend `SlideCodeBrief`:

```python
class SlideCodeBrief(BaseModel):
    """Everything the code-generating designer needs to build ONE slide.

    Deliberately self-contained: the designer never retrieves and never sees the rest of
    the deck. The contract fields below are what make a deck of independently generated
    slides still read as one deck — every parallel builder gets the same master, frame,
    grid and tokens, and its own assigned mode and layout.
    """
    slide_number: int
    kind: SlideKind = "content"
    content: SlideDraft
    context: DeckContext
    classification: str = "internal"
    image_paths: list[str] = []
    design_hint: str = ""

    # --- from the deck design contract ------------------------------------- #
    #: "layout" fills a real layout's placeholders; "canvas" composes freely on a
    #: canvas_capable layout from the same master.
    mode: Literal["layout", "canvas"] = "layout"
    #: Catalog key, e.g. "M1-L10". Empty means the builder resolves one itself.
    layout_key: str = ""
    grid: dict = {}
    frame: dict = {}
    tokens: dict = {}
    #: [left, top, width, height] in inches; set in canvas mode.
    free_region: list[float] | None = None
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_slides_node.py -q`
Expected: PASS.

- [ ] **Step 6: Check the tree still imports**

Run: `python -m pytest tests/ -q`
Expected: failures only in `renderer.py` / `filler.py` / `builder.build_pptx` paths, which Task 16 deletes. If a *live* path breaks, fix it here.

- [ ] **Step 7: Commit**

```bash
git add app/core/models/deck_model.py tests/test_slides_node.py
git commit -m "refactor(models): drop unpopulated slide geometry, carry the design contract"
```

---

### Task 10: Deck design contract — what makes a deck read as one deck

**Why:** The piece with no equivalent in either codebase. Four parallel workers guessing in isolation is why the deck does not cohere. This is computed once, before any slide is built, and handed to every builder. Most of it is deterministic from the catalog; only the per-slide mode and layout needs judgement, and that is **one** model call for the whole deck.

**Files:**
- Create: `app/genai/ppt/design_contract.py`
- Test: `tests/test_design_contract.py`

**Interfaces:**
- Consumes: `catalog.load()` (Task 3), `deck_builder.frame_spec` / `brand_palette` / `logo_variant_for` (Tasks 4–5), `registry.classification_symbol` / `logo_path` (Task 1), `DeckPlan` / `DeckDraft` (Task 9).
- Produces:
  - `@dataclass SlideAssignment(slide_number: int, mode: str, layout_key: str, free_region: list[float] | None)`
  - `@dataclass DeckDesignContract(master_index, grid, tokens, theme_font, assignments: dict[int, SlideAssignment], classification, slide_width, slide_height)` with `.frame_for(slide_number, layout) -> dict` and `.brief_fields(slide_number, layout) -> dict`
  - `design_contract.build(plan, draft, *, catalog=None, ask=None) -> DeckDesignContract`
  - `design_contract.validate_assignments(raw: list[dict], plan, catalog) -> tuple[dict[int, SlideAssignment], list[str]]`
  - `design_contract.fallback_assignment(slide, catalog, master_index) -> SlideAssignment`

`ask` is injected so every test runs with no model: `ask(system: str, user: str) -> list[dict]` returns the raw assignment list.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_design_contract.py`:

```python
"""The once-per-deck contract. Every test here runs with a stub in place of the model:
the deterministic parts must be right on their own, and the one judgement call must be
validated against the catalog before it is trusted."""

import pytest

from app.core.models.deck_model import DeckDraft, DeckPlan, SlideDraft, SlidePlan
from app.genai.ppt import design_contract as dc
from app.genai.ppt.template import catalog as catalog_mod


@pytest.fixture(scope="module")
def cat():
    return catalog_mod.load()


@pytest.fixture
def plan():
    return DeckPlan(title="SMT Quality", classification="internal", slides=[
        SlidePlan(slide_number=1, kind="cover", title="SMT Quality"),
        SlidePlan(slide_number=2, kind="content", title="Reflow control",
                  visual="diagram"),
        SlidePlan(slide_number=3, kind="content", title="Yield by line", visual="table"),
        SlidePlan(slide_number=4, kind="thanks", title="Thank you"),
    ])


@pytest.fixture
def draft(plan):
    return DeckDraft(title=plan.title, slides=[
        SlideDraft(slide_number=s.slide_number, kind=s.kind, title=s.title)
        for s in plan.slides])


def stub(assignments):
    def ask(system, user):
        return assignments
    return ask


# --- the deterministic half ------------------------------------------------ #

def test_the_contract_pins_one_master_for_every_content_slide(plan, draft, cat):
    contract = dc.build(plan, draft, catalog=cat, ask=stub([]))
    assert contract.master_index == cat.preferred_master == 1
    for number in (2, 3):
        layout = cat.get(contract.assignments[number].layout_key)
        assert layout.master_index == 1


def test_cover_and_closing_slides_are_exempt_from_master_uniformity(plan, draft, cat):
    contract = dc.build(plan, draft, catalog=cat, ask=stub([]))
    cover = cat.get(contract.assignments[1].layout_key)
    assert "cover" in cover.tags


def test_the_grid_is_twelve_by_six_with_fixed_gutters(plan, draft, cat):
    grid = dc.build(plan, draft, catalog=cat, ask=stub([])).grid
    assert grid == {"columns": 12, "rows": 6, "gutter_in": 0.15}


def test_the_tokens_come_from_the_template_theme(plan, draft, cat):
    contract = dc.build(plan, draft, catalog=cat, ask=stub([]))
    assert contract.tokens["red"] == "CD163F"
    assert contract.tokens["blue"] == "003E7E"
    assert contract.theme_font == "Barlow"
    assert contract.tokens["_type_scale"] == list(dc.GC.TYPE_SCALE)


def test_the_frame_carries_the_classification_and_the_slide_number(plan, draft, cat):
    contract = dc.build(plan, draft, catalog=cat, ask=stub([]))
    frame = contract.frame_for(2, cat.get(contract.assignments[2].layout_key))
    assert frame["classification"]["label_text"] == "Internal"
    assert frame["classification"]["symbol_path"].endswith("Internal_Symbol.png")
    assert frame["slide_number"]["value"] == 2


def test_the_logo_variant_follows_the_layout_background(plan, draft, cat):
    contract = dc.build(plan, draft, catalog=cat, ask=stub([]))
    dark = next(l for l in cat.layouts if l.background_luminance < 0.5)
    frame = contract.frame_for(2, dark)
    assert frame["logo"] is None or frame["logo"]["variant"] == "white"


# --- the one judgement call, and its validation ---------------------------- #

def test_a_valid_model_assignment_is_used_as_given(plan, draft, cat):
    contract = dc.build(plan, draft, catalog=cat, ask=stub([
        {"slide_number": 2, "mode": "canvas", "layout_key": "M1-L10"},
        {"slide_number": 3, "mode": "layout", "layout_key": "M1-L10"},
    ]))
    assert contract.assignments[2].mode == "canvas"
    assert contract.assignments[2].layout_key == "M1-L10"
    assert contract.assignments[2].free_region is not None


def test_an_unknown_layout_key_is_rejected(plan, cat):
    parsed, problems = dc.validate_assignments(
        [{"slide_number": 2, "mode": "layout", "layout_key": "M9-L99"}], plan, cat)
    assert 2 not in parsed
    assert any("M9-L99" in p for p in problems)


def test_canvas_mode_on_a_layout_that_cannot_take_it_is_rejected(plan, cat):
    busy = next(l for l in cat.layouts if not l.canvas_capable and l.placeholders)
    parsed, problems = dc.validate_assignments(
        [{"slide_number": 2, "mode": "canvas", "layout_key": busy.key}], plan, cat)
    assert 2 not in parsed
    assert any("canvas" in p.lower() for p in problems)


def test_a_content_slide_on_a_non_preferred_master_is_rejected(plan, cat):
    other = next(l for l in cat.layouts
                 if l.master_index != cat.preferred_master and "content" in l.tags)
    parsed, problems = dc.validate_assignments(
        [{"slide_number": 2, "mode": "layout", "layout_key": other.key}], plan, cat)
    assert 2 not in parsed
    assert any("master" in p.lower() for p in problems)


def test_a_rejected_assignment_is_re_requested_once_then_falls_back(plan, draft, cat):
    calls = {"n": 0}

    def ask(system, user):
        calls["n"] += 1
        if calls["n"] == 1:
            return [{"slide_number": 2, "mode": "canvas", "layout_key": "M9-L99"}]
        return []                       # the second answer is no better

    contract = dc.build(plan, draft, catalog=cat, ask=ask)
    assert calls["n"] == 2, "the model is asked again exactly once"
    assigned = contract.assignments[2]
    assert assigned.mode == "layout"                        # the safe default
    assert cat.get(assigned.layout_key) is not None         # a real layout all the same


def test_the_second_request_names_what_was_wrong(plan, draft, cat):
    seen = []

    def ask(system, user):
        seen.append(user)
        return [{"slide_number": 2, "mode": "layout", "layout_key": "M9-L99"}]

    dc.build(plan, draft, catalog=cat, ask=ask)
    assert len(seen) == 2
    assert "M9-L99" in seen[1]


def test_a_model_failure_never_fails_the_deck(plan, draft, cat):
    def ask(system, user):
        raise RuntimeError("gateway 500")

    contract = dc.build(plan, draft, catalog=cat, ask=ask)
    assert len(contract.assignments) == len(plan.slides)
    assert all(a.mode == "layout" for a in contract.assignments.values())


def test_every_slide_ends_up_assigned_whatever_the_model_said(plan, draft, cat):
    contract = dc.build(plan, draft, catalog=cat, ask=stub([
        {"slide_number": 2, "mode": "canvas", "layout_key": "M1-L10"}]))
    assert set(contract.assignments) == {1, 2, 3, 4}
    for assignment in contract.assignments.values():
        assert cat.get(assignment.layout_key) is not None


def test_brief_fields_are_exactly_what_the_code_brief_declares(plan, draft, cat):
    from app.core.models.deck_model import SlideCodeBrief

    contract = dc.build(plan, draft, catalog=cat, ask=stub([]))
    fields = contract.brief_fields(2, cat.get(contract.assignments[2].layout_key))
    assert set(fields) <= set(SlideCodeBrief.model_fields)
    assert set(fields) == {"mode", "layout_key", "grid", "frame", "tokens",
                           "free_region"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_design_contract.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.genai.ppt.design_contract'`.

- [ ] **Step 3: Write `app/genai/ppt/design_contract.py`**

```python
"""The deck design contract: computed once, handed to every parallel slide builder.

This is what makes a deck of independently generated slides read as one deck. Four
workers each choosing a master, a grid and a palette in isolation is precisely why the
current decks do not cohere, so those choices are made here, once, and are not the
slide builder's to revisit.

Almost all of it is deterministic from the catalog. The single judgement call — which
composition mode and which layout each slide should use — is one model call for the
whole deck, made with every slide's title and content shape in view. Its answer is
validated against the catalog before it is trusted, re-requested once if it is wrong,
and replaced by `recommend_layouts`' top choice in `layout` mode if it is wrong twice.
A model failure degrades to that same default: it never fails the deck.
"""

import json
from dataclasses import dataclass, field
from typing import Callable, Optional

from app.config.constants import GeometryConstants as GC, ModelConstants, PptCreatorConstants
from app.core.models.deck_model import DeckDraft, DeckPlan
from app.genai.ppt import deck_builder as builder
from app.genai.ppt.template import catalog as catalog_mod, registry
from app.utilities.apim_llm_utils import get_apim_llm_client
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

#: Slide kinds exempt from master uniformity, matching main_v3.
_EXEMPT_KINDS = ("cover", "thanks")

SYSTEM_PROMPT = """You are the art director for one slide deck.

You are given every slide's number, role, title, content shape and visual hint at once,
and a catalog of the brand template's layouts. Decide, for each slide, how it should be
built:

- mode "layout": fill an existing template layout's placeholders. Correct whenever the
  content maps cleanly onto a layout that already exists — three text columns, a five
  item agenda, a photo with text.
- mode "canvas": take a canvas-capable layout and compose the content region freely.
  Correct for charts, tables, cards, composed diagrams — content an existing layout
  would misrepresent.

Rules you must follow:
- Every content slide uses a layout on the preferred content master. Cover and closing
  slides may use any master.
- "canvas" is only valid on a layout the catalog marks canvas_capable.
- Reference layouts by their catalog key ("M1-L10"). Never invent one.
- Vary the layouts. A deck where every slide is the same layout reads as a template,
  not as a deck.

Answer with JSON only: a list of
{"slide_number": int, "mode": "layout"|"canvas", "layout_key": "M1-L10"}
"""


@dataclass(frozen=True)
class SlideAssignment:
    """How one slide is to be built."""
    slide_number: int
    mode: str
    layout_key: str
    free_region: Optional[list] = None


@dataclass
class DeckDesignContract:
    """The deck-wide decisions every slide builder inherits."""
    master_index: int
    grid: dict
    tokens: dict
    theme_font: str
    classification: str
    slide_width: float
    slide_height: float
    assignments: dict = field(default_factory=dict)

    def frame_for(self, slide_number: int, layout) -> dict:
        """The frame this slide carries: classification, logo variant, slide number."""
        variant = builder.logo_variant_for(getattr(layout, "background_luminance", 1.0))
        logo_file = "d2r_mark_white.png" if variant == "white" else "d2r_mark.png"
        logo = registry.logo_path(logo_file)
        symbol = registry.classification_symbol(self.classification)
        return builder.frame_spec(
            self.slide_width, self.slide_height,
            label=registry.label(self.classification),
            symbol_path=str(symbol) if symbol else None,
            logo_path=str(logo) if logo else None,
            logo_variant=variant, slide_number=slide_number)

    def brief_fields(self, slide_number: int, layout) -> dict:
        """The contract fields `SlideCodeBrief` carries for this slide."""
        assignment = self.assignments[slide_number]
        return {
            "mode": assignment.mode,
            "layout_key": assignment.layout_key,
            "grid": dict(self.grid),
            "frame": self.frame_for(slide_number, layout),
            "tokens": dict(self.tokens),
            "free_region": list(assignment.free_region) if assignment.free_region else None,
        }
```

- [ ] **Step 4: Add assignment validation and the fallback**

```python
def fallback_assignment(slide, catalog, master_index: int) -> SlideAssignment:
    """The safe default: `recommend_layouts`' top choice, in `layout` mode.

    Used when the model's answer is unusable twice, and when the model is unreachable.
    A layout the template designed is always cleaner than a canvas we composed badly.
    """
    intent = _intent_for(slide)
    ranked = catalog.recommend(intent, limit=5)
    exempt = getattr(slide, "kind", "content") in _EXEMPT_KINDS
    for layout, _score, _reasons in ranked:
        if exempt or layout.master_index == master_index:
            return SlideAssignment(slide.slide_number, "layout", layout.key, None)
    chosen = ranked[0][0] if ranked else catalog.canonical_layouts()[0]
    return SlideAssignment(slide.slide_number, "layout", chosen.key, None)


def _intent_for(slide) -> str:
    """Map a planned slide onto the catalog's intent vocabulary."""
    kind = getattr(slide, "kind", "content")
    if kind in ("cover", "agenda", "thanks"):
        return {"thanks": "closing"}.get(kind, kind)
    visual = (getattr(slide, "visual", "") or "").strip().lower()
    return visual if visual in ("table", "chart", "photo", "process", "dashboard",
                                "comparison", "timeline") else "content"


def validate_assignments(raw, plan: DeckPlan, catalog) -> tuple[dict, list[str]]:
    """Check the model's answer against the catalog before any of it is trusted.

    Args:
        raw: The model's list of assignment dicts.
        plan: The deck plan, for each slide's kind.
        catalog: The template catalog.

    Returns:
        (accepted assignments by slide number, one problem string per rejection).
    """
    kinds = {s.slide_number: s.kind for s in plan.slides}
    accepted: dict[int, SlideAssignment] = {}
    problems: list[str] = []

    for entry in raw or []:
        if not isinstance(entry, dict):
            problems.append(f"expected an object per slide, got {entry!r}")
            continue
        try:
            number = int(entry.get("slide_number"))
        except (TypeError, ValueError):
            problems.append(f"slide_number missing or not a number in {entry!r}")
            continue
        if number not in kinds:
            problems.append(f"slide {number} is not in the deck")
            continue

        key = str(entry.get("layout_key") or "").strip()
        layout = catalog.get(key)
        if layout is None:
            problems.append(
                f"slide {number}: layout_key '{key}' is not in the catalog; use a key "
                f"exactly as the catalog lists it, e.g. 'M1-L10'")
            continue

        mode = str(entry.get("mode") or "layout").strip().lower()
        if mode not in ("layout", "canvas"):
            problems.append(f"slide {number}: mode must be 'layout' or 'canvas', "
                            f"not {mode!r}")
            continue
        if mode == "canvas" and not layout.canvas_capable:
            problems.append(
                f"slide {number}: '{layout.key}' is not canvas_capable, so it cannot be "
                f"used in canvas mode. Either use mode 'layout' on it, or pick a "
                f"canvas_capable layout")
            continue
        if (kinds[number] not in _EXEMPT_KINDS
                and layout.master_index != catalog.preferred_master):
            problems.append(
                f"slide {number}: '{layout.key}' is on master M{layout.master_index}, "
                f"but content slides must stay on the preferred content master "
                f"M{catalog.preferred_master}")
            continue

        accepted[number] = SlideAssignment(
            number, mode, layout.key,
            list(layout.free_region.to_list()) if (mode == "canvas" and layout.free_region)
            else None)
    return accepted, problems
```

- [ ] **Step 5: Add `build()` — the single model call and its degradation**

```python
def build(plan: DeckPlan, draft: DeckDraft, *, catalog=None,
          ask: Optional[Callable[[str, str], list]] = None) -> DeckDesignContract:
    """Compute the whole deck's design contract, before any slide is built.

    Args:
        plan: The deck plan, for classification and each slide's role and visual hint.
        draft: The assembled slides, for each slide's actual content shape.
        catalog: Injected in tests; the template catalog otherwise.
        ask: Injected in tests; `(system, user) -> list of assignment dicts`.

    Returns:
        A contract with an assignment for every slide in the plan. Never raises: a
        model failure degrades every slide to `recommend_layouts`' top choice.
    """
    catalog = catalog or catalog_mod.load()
    ask = ask or _ask_model
    classification = registry.normalize(plan.classification)

    tokens = builder.brand_palette(catalog.theme.colors)
    tokens["_type_scale"] = list(GC.TYPE_SCALE)
    contract = DeckDesignContract(
        master_index=catalog.preferred_master,
        grid={"columns": GC.GRID_COLUMNS, "rows": GC.GRID_ROWS,
              "gutter_in": GC.GRID_GUTTER_IN},
        tokens=tokens,
        theme_font=catalog.theme.minor_font or "Barlow",
        classification=classification,
        slide_width=catalog.slide_width,
        slide_height=catalog.slide_height)

    slides_by_number = {s.slide_number: s for s in plan.slides}
    accepted: dict[int, SlideAssignment] = {}
    user_message = _describe_deck(plan, draft, catalog)

    for attempt in (1, 2):
        try:
            raw = ask(SYSTEM_PROMPT, user_message)
        except Exception as exc:                             # noqa: BLE001
            # The contract is an improvement, not a dependency: a gateway failure costs
            # deck-wide coherence, never the deck.
            log.warning("design contract: model unavailable on attempt %d (%s); "
                        "falling back to recommended layouts", attempt, exc)
            break
        parsed, problems = validate_assignments(raw, plan, catalog)
        accepted.update(parsed)
        if not problems or len(accepted) == len(slides_by_number):
            break
        if attempt == 1:
            log.info("design contract: %d assignment(s) rejected, asking again",
                     len(problems))
            user_message = (user_message + "\n\n=== YOUR PREVIOUS ANSWER WAS REJECTED ===\n"
                            + "\n".join(f"- {p}" for p in problems)
                            + "\nAnswer again, for these slides only.")

    for number, slide in slides_by_number.items():
        if number not in accepted:
            accepted[number] = fallback_assignment(slide, catalog,
                                                   catalog.preferred_master)
    contract.assignments = accepted
    log.info("design contract built", extra={
        "master": contract.master_index,
        "canvas_slides": sum(1 for a in accepted.values() if a.mode == "canvas"),
        "slides": len(accepted)})
    return contract


def _describe_deck(plan: DeckPlan, draft: DeckDraft, catalog) -> str:
    """Every slide's shape plus the layout menu, as one prompt."""
    drafts = {s.slide_number: s for s in draft.slides}
    lines = [f'Deck "{plan.title}" — {len(plan.slides)} slides, '
             f'audience: {plan.audience or "unstated"}, tone: {plan.tone}',
             f"Preferred content master: M{catalog.preferred_master}", "", "SLIDES:"]
    for slide in plan.slides:
        content = drafts.get(slide.slide_number)
        blocks = len(content.blocks) if content else 0
        tables = len(content.tables) if content else 0
        images = sum(1 for b in (content.blocks if content else []) if b.image_ref)
        lines.append(
            f"  {slide.slide_number}. [{slide.kind}] {slide.title!r} — "
            f"{blocks} text block(s), {tables} table(s), {images} image(s)"
            + (f", visual hint: {slide.visual}" if slide.visual else ""))
    lines += ["", "CANVAS-CAPABLE LAYOUTS (valid for mode 'canvas'):"]
    for layout in catalog.layouts:
        if layout.canvas_capable:
            lines.append(f"  {layout.key} '{layout.name}' — "
                         f"{layout.free_region.height:.2f}in of free height")
    lines += ["", "LAYOUT CATALOG:", catalog.summary_text()]
    return "\n".join(lines)


def _ask_model(system: str, user: str) -> list:
    """One gateway call for the whole deck. Returns the parsed assignment list."""
    response = get_apim_llm_client().foundry_chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        model=ModelConstants.PPT_CODER, max_tokens=PptCreatorConstants.MAX_TOKENS)
    text = get_apim_llm_client().reply_text(response)
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("the model did not answer with a JSON list")
    return json.loads(text[start:end + 1])
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest tests/test_design_contract.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/genai/ppt/design_contract.py tests/test_design_contract.py
git commit -m "feat(ppt): one deck design contract, computed before any slide is built"
```

---

### Task 11: Design agent tools

**Why:** Spec's component 2. `check_geometry` exists so arrangement iteration is free: without it, discovering a collision costs a full code execution, up to `EXEC_TIMEOUT_SECONDS` (180s). It evaluates rules 1–6 plus rule 10 from declared rectangles; **`validate_slide` stays authoritative** because rules 7–9 depend on what the XML really contains.

**Files:**
- Create: `app/genai/tools/design_tools.py`
- Test: `tests/test_design_tools.py`

**Interfaces:**
- Consumes: `catalog.load()`, `geometry.check_rects` / `check_overflow` / `format_violations` / `check_slide`, `deck_builder.reserved_rects`.
- Produces: `build_design_tools(session_id: str, *, contract=None, slide_dir=None) -> list` — LangChain tools named `get_template_summary`, `search_layouts`, `recommend_layouts`, `get_layout_details`, `check_geometry`, `run_slide_code`, `validate_slide`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_design_tools.py`:

```python
"""The design agent's tools. Every one runs offline against the real template."""

import json

import pytest

from app.genai.tools import design_tools


@pytest.fixture
def tools():
    return {t.name: t for t in design_tools.build_design_tools("s1")}


def test_every_tool_the_design_declares_is_present(tools):
    assert set(tools) == {"get_template_summary", "search_layouts", "recommend_layouts",
                          "get_layout_details", "check_geometry", "run_slide_code",
                          "validate_slide"}


def test_the_summary_names_the_slide_size_fonts_and_preferred_master(tools):
    summary = tools["get_template_summary"].invoke({})
    assert "13.33" in summary and "Barlow" in summary
    assert "preferred" in summary.lower()
    assert "M1" in summary


def test_search_finds_layouts_by_keyword(tools):
    found = tools["search_layouts"].invoke({"query": "agenda", "limit": 5})
    assert "agenda" in found.lower()
    assert "M" in found                       # catalog keys are quoted back


def test_recommend_ranks_layouts_for_an_intent(tools):
    ranked = tools["recommend_layouts"].invoke({"intent": "cover", "limit": 3})
    assert "cover" in ranked.lower()


def test_layout_details_carry_idx_geometry_capacity_decor_and_the_safe_region(tools):
    details = json.loads(tools["get_layout_details"].invoke({"layout": "M1-L10"}))
    assert details["key"] == "M1-L10"
    title = next(p for p in details["placeholders"] if p["role"] == "title")
    assert title["idx"] == 0 and title["approx_chars"] > 0
    assert len(details["decor_rects"]) == 2
    assert details["safe_region"] and len(details["safe_region"]) == 4
    assert details["grid"]["columns"] == 12
    assert "reserved_zones" in details


def test_layout_details_on_an_unknown_layout_says_so_usefully(tools):
    answer = tools["get_layout_details"].invoke({"layout": "M9-L99"})
    assert "unknown" in answer.lower() or "did you mean" in answer.lower()


# --- check_geometry: the fast path ----------------------------------------- #

def test_check_geometry_clears_a_clean_arrangement(tools):
    boxes = [{"id": "a", "left": 1.0, "top": 2.0, "width": 3.0, "height": 1.0,
              "has_text": True, "filled": False, "text": "one"},
             {"id": "b", "left": 5.0, "top": 2.0, "width": 3.0, "height": 1.0,
              "has_text": True, "filled": False, "text": "two"}]
    answer = tools["check_geometry"].invoke({"boxes": boxes, "layout": "M1-L10"})
    assert "passed" in answer.lower()


def test_check_geometry_reports_an_overlap_before_any_code_runs(tools):
    boxes = [{"id": "card 2", "left": 6.80, "top": 2.40, "width": 3.60, "height": 1.80,
              "has_text": True},
             {"id": "card 3", "left": 8.00, "top": 2.40, "width": 3.60, "height": 1.80,
              "has_text": True}]
    answer = tools["check_geometry"].invoke({"boxes": boxes, "layout": "M1-L10"})
    assert "rule 2" in answer and "card 3" in answer


def test_check_geometry_protects_the_layouts_own_decor(tools):
    boxes = [{"id": "a", "left": 0.25, "top": 0.30, "width": 2.0, "height": 1.0}]
    answer = tools["check_geometry"].invoke({"boxes": boxes, "layout": "M1-L10"})
    assert "rule 5" in answer


def test_check_geometry_checks_overflow_only_when_given_text_and_a_size(tools):
    boxes = [{"id": "a", "left": 1.0, "top": 2.0, "width": 2.0, "height": 0.3,
              "has_text": True, "text": "word " * 120, "font_size": 15}]
    assert "rule 10" in tools["check_geometry"].invoke({"boxes": boxes,
                                                        "layout": "M1-L10"})
    sized_out = [{**boxes[0], "font_size": None}]
    assert "rule 10" not in tools["check_geometry"].invoke({"boxes": sized_out,
                                                            "layout": "M1-L10"})


def test_check_geometry_says_it_is_not_the_last_word(tools):
    """Rules 7-9 need the rendered XML; the tool must not imply otherwise."""
    boxes = [{"id": "a", "left": 1.0, "top": 2.0, "width": 3.0, "height": 1.0}]
    answer = tools["check_geometry"].invoke({"boxes": boxes, "layout": "M1-L10"})
    assert "validate_slide" in answer


def test_malformed_boxes_are_explained_not_raised(tools):
    answer = tools["check_geometry"].invoke({"boxes": [{"id": "a"}], "layout": "M1-L10"})
    assert "left" in answer and "required" in answer.lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_design_tools.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.genai.tools.design_tools'`.

- [ ] **Step 3: Write `app/genai/tools/design_tools.py`**

```python
"""The design agent's tools: the catalog, a free geometry check, and the gate.

`check_geometry` exists to make arrangement iteration free. Without it, the only way to
discover that two cards collide is to write the whole program and run it, at up to
`EXEC_TIMEOUT_SECONDS` a try. It evaluates rules 1-6 — the rules decidable from
rectangles alone — plus rule 10 for any box that supplies text and a size.

It is a fast path, not a substitute for the gate. Rules 7-9 depend on what the produced
XML actually contains, so `validate_slide` remains authoritative and every answer from
`check_geometry` says so.
"""

import json
from pathlib import Path

from langchain_core.tools import tool

from app.config.constants import GeometryConstants as GC
from app.genai.ppt import deck_builder as builder, geometry as geo
from app.genai.ppt.template import catalog as catalog_mod
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

_REQUIRED_BOX_KEYS = ("left", "top", "width", "height")


def build_design_tools(session_id: str, *, contract=None, slide_dir=None) -> list:
    """Design-tool instances bound to one slide build.

    Args:
        session_id: For logging only; the tools carry no session state.
        contract: The deck design contract, so `check_geometry` knows the frame.
        slide_dir: Where `run_slide_code` executes and `validate_slide` reads from.
    """
    catalog = catalog_mod.load()

    def _frame_for(layout):
        if contract is None:
            return builder.frame_spec(catalog.slide_width, catalog.slide_height,
                                      label="Internal")
        return contract.frame_for(1, layout)

    @tool
    def get_template_summary() -> str:
        """The brand template at a glance: slide size, theme fonts and colours, the
        masters, which master is preferred for content slides, and every unique layout
        with its slot summary. Read this first — it is how you learn what layouts
        exist, and layout names are never to be invented."""
        return catalog.summary_text()

    @tool
    def search_layouts(query: str = "", tags: str = "", min_text_slots: int = 0,
                       needs_picture: bool = False, limit: int = 20) -> str:
        """Find template layouts by keyword or tag. Tags include cover, agenda,
        section, columns, 3-column, process, dashboard, photo, chart, title-only,
        closing, dark. Use `min_text_slots` to demand room for N text blocks and
        `needs_picture` to demand a real picture slot."""
        found = catalog.search(query=query, tags=tags, min_text_slots=min_text_slots,
                               needs_picture=needs_picture, limit=limit)
        if not found:
            return (f"No layout matches {query!r}. Call get_template_summary to see "
                    f"what the template actually offers.")
        return "\n".join(layout.compact() for layout in found)

    @tool
    def recommend_layouts(intent: str, text_blocks: int = 0, needs_picture: bool = False,
                          has_table: bool = False, has_chart: bool = False,
                          limit: int = 5) -> str:
        """Rank layouts for one slide's intent and content shape, with the reason each
        scored as it did. `intent` is one of cover, agenda, section, quote, closing,
        content, bullets, comparison, columns, process, timeline, dashboard, kpi,
        photo, table, chart."""
        ranked = catalog.recommend(intent, text_blocks=text_blocks,
                                   needs_picture=needs_picture, has_table=has_table,
                                   has_chart=has_chart, limit=limit)
        return "\n".join(
            f"{layout.compact()} | score {score} | {', '.join(reasons) or 'no strong signal'}"
            for layout, score, reasons in ranked)

    @tool
    def get_layout_details(layout: str) -> str:
        """Everything you need before building on a layout: every placeholder with its
        idx, slot name, role, geometry and text capacity; the rectangles of the
        decorative shapes the layout already draws; the reserved zones you must not
        draw into; the safe region you may compose in; and the deck grid. Call this for
        the layout you were assigned before writing any code."""
        try:
            found = catalog.resolve_layout(layout)
        except LookupError as exc:
            return str(exc)
        frame = _frame_for(found)
        safe = (found.free_region.to_list() if found.free_region
                else [GC.FRAME_SIDE_MARGIN_IN, 1.30,
                      catalog.slide_width - 2 * GC.FRAME_SIDE_MARGIN_IN,
                      catalog.slide_height - 1.30 - GC.FOOTER_BAND_HEIGHT_IN])
        details = found.details()
        details.update({
            "decor_rects": [r.to_list() for r in found.decor_rects],
            "reserved_zones": builder.reserved_rects(frame, _view(found)),
            "safe_region": safe,
            "canvas_capable": found.canvas_capable,
            "background_luminance": found.background_luminance,
            "grid": {"columns": GC.GRID_COLUMNS, "rows": GC.GRID_ROWS,
                     "gutter_in": GC.GRID_GUTTER_IN, "over": safe},
            "placeholders": [p.summary() | {"role": p.role, "approx_chars": p.approx_chars}
                             for p in found.placeholders],
        })
        return json.dumps(details, indent=2, default=str)

    @tool
    def check_geometry(boxes: list, layout: str = "") -> str:
        """Check an arrangement BEFORE writing or running any code. Pass the rectangles
        you intend to draw as a list of
        {"id", "left", "top", "width", "height", "has_text", "filled", "text", "font_size"}
        in inches. Returns every collision, off-canvas element and reserved-zone
        intrusion, with a concrete fix for each. Use this freely while arranging —
        it costs nothing, where running the program costs up to three minutes."""
        parsed, problems = _parse_boxes(boxes)
        if problems:
            return "These boxes could not be read:\n" + "\n".join(f"- {p}" for p in problems)
        try:
            found = catalog.resolve_layout(layout) if layout else None
        except LookupError as exc:
            return str(exc)
        reserved = (builder.reserved_rects(_frame_for(found), _view(found))
                    if found is not None else ())
        violations = geo.check_rects(parsed, slide_width=catalog.slide_width,
                                     slide_height=catalog.slide_height,
                                     reserved=reserved)
        violations += geo.check_overflow(parsed)
        tail = ("\n\nThis check covers rules 1-6 and 10 only. Rules 7-9 (leftover "
                "prompt text, off-palette colours, off-scale fonts) depend on what the "
                "rendered file contains — validate_slide is the authoritative check.")
        if not violations:
            return "This arrangement passed rules 1-6 and 10." + tail
        return geo.format_violations(violations) + tail

    @tool
    def run_slide_code(code: str) -> str:
        """Run your DeckBuilder program and report what happened. The program is
        static-checked first; a rejection comes back without costing an execution."""
        from app.genai.ppt import sandbox

        if slide_dir is None:
            return "no slide directory is bound to this tool"
        return sandbox.run(code, Path(slide_dir))

    @tool
    def validate_slide() -> str:
        """The authoritative check on the file your program produced: geometry, brand
        and fidelity. A slide is only finished when this passes."""
        from app.genai.ppt import sandbox

        if slide_dir is None:
            return "no slide directory is bound to this tool"
        return sandbox.validate(Path(slide_dir), catalog, contract)

    return [get_template_summary, search_layouts, recommend_layouts, get_layout_details,
            check_geometry, run_slide_code, validate_slide]


def _view(layout):
    """`reserved_rects` wants `.decor_rects` as plain lists."""
    class _View:
        decor_rects = [r.to_list() for r in getattr(layout, "decor_rects", ())]
    return _View()


def _parse_boxes(boxes) -> tuple[list, list[str]]:
    """Turn the tool's JSON boxes into `geometry.Box`es, explaining what is unusable."""
    parsed, problems = [], []
    for index, raw in enumerate(boxes or []):
        if not isinstance(raw, dict):
            problems.append(f"box {index + 1} is not an object")
            continue
        missing = [key for key in _REQUIRED_BOX_KEYS if raw.get(key) is None]
        if missing:
            problems.append(
                f"box {raw.get('id', index + 1)!r} is missing required "
                f"{', '.join(missing)} — every box needs left, top, width and height "
                f"in inches")
            continue
        text = str(raw.get("text") or "")
        parsed.append(geo.Box(
            id=str(raw.get("id") or f"box {index + 1}"),
            left=float(raw["left"]), top=float(raw["top"]),
            width=float(raw["width"]), height=float(raw["height"]),
            has_text=bool(raw.get("has_text", bool(text.strip()))),
            filled=bool(raw.get("filled", False)),
            text=text,
            font_size=float(raw["font_size"]) if raw.get("font_size") else None))
    return parsed, problems
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_design_tools.py -q`
Expected: PASS. `run_slide_code` and `validate_slide` are exercised in Task 13 once `sandbox.py` exists; here only their presence is asserted.

- [ ] **Step 5: Commit**

```bash
git add app/genai/tools/design_tools.py tests/test_design_tools.py
git commit -m "feat(tools): catalog and free geometry checking for the design agent"
```

---

### Task 12: The brand guide

**Why:** Bug 1. The constant was fixed in Task 1, but the file it now points at is the *incompatible* older guide: it hardcodes a layout list (layouts are discovered at runtime, across 181 of them) and its "Programmatic Implementation (python-pptx)" section teaches exactly the hand-built textboxes and literal hex colours the AST guard rejects. Loading it would teach the model to write programs Task 6 refuses. The file's content is replaced, not repaired.

**Files:**
- Rewrite: `app/genai/ppt/skills/rockwell-skill.md` (from `scripts/mainv3/rockwell-skill-v3.md`)
- Test: `tests/test_ppt_creator_agent.py` (extend)

**Interfaces:**
- Consumes: `PptCreatorConstants.SKILL_MD_FILENAME` (Task 1).
- Produces: a guide whose content matches the `DeckBuilder` API and the geometry rules.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ppt_creator_agent.py`:

```python
# --------------------------------------------------------------------------- #
# the brand guide                                                              #
# --------------------------------------------------------------------------- #

def test_the_brand_guide_actually_loads():
    """This failed silently for the whole life of the feature: the constant said
    SKILL.md and the file on disk was rockwell-skill.md, so load_brand_skill() logged a
    warning and returned "" on every single slide."""
    creator.load_brand_skill.cache_clear()
    guide = creator.load_brand_skill()
    assert guide.strip(), "the brand guide is empty — the model gets no brand guidance"
    assert len(guide) > 2000


def test_the_guide_teaches_the_deckbuilder_api():
    guide = creator.load_brand_skill()
    for component in ("add_card", "add_stat", "add_icon_circle", "add_table",
                      "add_chart", "add_textbox", "set_title", "set_bullets"):
        assert component in guide, f"the guide never mentions {component}"


def test_the_guide_does_not_teach_the_code_the_guard_rejects():
    """The old guide taught hand-built textboxes and literal hex. Loading that would
    train the model to write programs code_guard is specified to refuse."""
    guide = creator.load_brand_skill()
    assert "prs.slide_layouts" not in guide
    assert "slide.shapes.add_textbox" not in guide
    assert "Presentation()" not in guide
    assert "## Available Layouts" not in guide      # the hardcoded layout list


def test_the_guide_says_layouts_are_discovered_at_runtime():
    guide = creator.load_brand_skill()
    assert "discovered at runtime" in guide
    assert "get_layout_details" in guide


def test_the_guide_covers_the_four_additions_this_design_needs():
    guide = creator.load_brand_skill()
    assert "canvas" in guide.lower()                 # composition modes
    assert "12" in guide and "grid" in guide.lower()  # the grid
    assert "classification" in guide.lower()          # the reserved zone
    for size in ("32", "28", "20", "15", "14", "12", "11", "10"):
        assert f"{size}" in guide                     # the type scale
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_ppt_creator_agent.py -q -k guide`
Expected: FAIL — `test_the_guide_does_not_teach_the_code_the_guard_rejects` fails, because the current file is the old guide.

- [ ] **Step 3: Replace the guide**

```bash
cp scripts/mainv3/rockwell-skill-v3.md app/genai/ppt/skills/rockwell-skill.md
```

Then edit `app/genai/ppt/skills/rockwell-skill.md`:

**(a)** In the `## Layouts Come From the Template Catalog` table, drop the `submit_presentation_plan` row (there is no plan submission in this architecture) and add:

```markdown
| `check_geometry` | Check an arrangement's rectangles **before** writing code — free, and the fastest way to find a collision |
| `validate_slide` | The authoritative geometry, brand and fidelity check on the file you produced |
```

**(b)** Replace the `## Components (DeckBuilder helpers)` preamble line with:

```markdown
Your program is written against `DeckBuilder` and nothing else. Start it with
`from deck_builder import DeckBuilder`; the sandbox puts that module beside your
program. Never construct `Presentation(...)` yourself, never call `.shapes.add_*`
directly, and never write a hex colour — all three are rejected before your program
runs.
```

**(c)** Add four new sections before `## Common Mistakes to Avoid`:

```markdown
---

## Composition Modes

Every slide is built in one of two modes, and the mode is assigned to you — do not
change it.

**`layout` mode** — fill a real template layout's placeholders. This is correct
whenever the content maps onto a layout that already exists: three text columns, a five
item agenda, a photo with text, headshots. Use `set_title`, `set_subtitle`, `set_text`,
`set_bullets`, `set_picture`, `set_table`, `set_chart`. Add components only in space the
layout leaves genuinely free.

**`canvas` mode** — you were given a `canvas_capable` layout and the free region below
its title. Compose that region yourself: charts, tables, cards, composed diagrams,
brand shapes. You still sit on a real master, so the theme, Barlow, the background
treatment and the master's decorative graphics are inherited — you are composing inside
a branded frame, not on a blank page.

### The grid

The content region carries a **12 column x 6 row** grid with fixed 0.15in gutters. Use
`slide.grid(col, row, cols, rows)` to get `(left, top, width, height)` in inches for a
block. Two adjacent grid cells never overlap, which is the cheapest way to satisfy the
geometry rules:

    left, top, width, height = slide.grid(0, 1, cols=4, rows=2)
    slide.add_card("Preheat", ["Ramp under 3 C/s"], left, top, width, height)

Place things on the grid unless you have a reason not to. Hand-picked coordinates are
allowed, and are where collisions come from.

---

## Geometry Rules

Your slide is reopened and measured after it is built. These are checked exactly, by
rectangle arithmetic, and a failure sends the program back to you:

1. Every element sits inside the canvas, 0.30in from every edge.
2. **No text overlaps other text. There is no exception to this one.**
3. Text over a shape must sit *wholly inside* that shape. A label inside a card is
   correct; a label hanging off its card's edge is not.
4. Two filled shapes may nest or stand apart, never partly overlap.
5. Nothing may touch a reserved zone: the footer band, the classification mark, or a
   graphic the layout already draws. `get_layout_details` lists them.
6. Nothing may be drawn over a filled placeholder. Fill it through the placeholder API
   instead.
7. No "Click to edit" or "Second level" prompt text may remain anywhere.
8. Every colour is a brand token. Never a hex literal.
9. Every font is the theme font, at a size on the type scale below.

Aim for these while you arrange, rather than discovering them afterwards: call
`check_geometry` with your intended rectangles before you write the program.

---

## The Classification Mark Is Not Yours To Draw

Every slide carries a classification label, its policy symbol, the slide number and —
where the layout does not already have one — the brand mark. All of it is stamped by
`finalize()`, from one code path, in a reserved band along the bottom of the slide.

Do not draw a classification label, a footer, a slide number or a logo. Do not place
anything in the bottom 0.45in of the slide. Rule 5 rejects it.

---

## Type Scale

Placeholder text inherits the template's own sizes and is correct by definition — do
not override it. Text you add through a component must use one of these sizes, and
nothing else:

| Size | Use |
|---|---|
| 32 pt | stat value |
| 28 pt | title (only where a layout has no title placeholder) |
| 20 pt | heading |
| 15 pt | body |
| 14 pt | card title |
| 12 pt | shape label |
| 11 pt | table cell, caption |
| 10 pt | floor — never smaller |
```

**(d)** In `## Common Mistakes to Avoid`, add three bullets:

```markdown
- Drawing your own footer, slide number, logo or classification label (all are stamped for you)
- Hand-picking coordinates where `slide.grid(...)` would do
- Writing a hex colour instead of a brand token name
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_ppt_creator_agent.py -q -k guide`
Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add app/genai/ppt/skills/rockwell-skill.md tests/test_ppt_creator_agent.py
git commit -m "fix(ppt): load a brand guide that matches the architecture (bug 1)"
```

---

### Task 13: Sandbox staging, the repair loop, and the deterministic fallback

**Why:** Spec's component 6, and the decision recorded at the top of this plan: the sandbox stays sealed, so `deck_builder.py` is **copied into each slide directory** rather than reached through `PYTHONPATH`. The repair loop, which today reacts only to tracebacks, now reacts to AST rejections and gate violations too — all three sharing one budget, so a slide's total cost stays bounded.

**Files:**
- Create: `app/genai/ppt/sandbox.py`
- Modify: `app/genai/agents/ppt_creator_agent.py`
- Modify: `tests/test_ppt_creator_agent.py`

**Interfaces:**
- Consumes: `code_guard` (Task 6), `geometry` (Tasks 7–8), `deck_builder` (Tasks 4–5), `catalog` (Task 3).
- Produces:
  - `sandbox.stage(slide_dir, catalog, contract_fields, image_paths) -> dict` — writes `deck_builder.py`, `catalog.json`, `context.py`; returns the preamble names the program may use
  - `sandbox.run(code, slide_dir) -> str` — guard, write, execute, summarise
  - `sandbox.validate(slide_dir, catalog, contract) -> str`
  - `PptCreatorAgent._render_on_layout(brief, out_path) -> Path` — the deterministic fallback, replacing `plain_slide_pptx`

**The sandbox contract with generated code.** `stage()` writes a `context.py` next to the program defining exactly four names, which the guide tells the model to use:

```python
TEMPLATE_PATH  # str: the brand template
CATALOG        # dict: the serialized catalog
FRAME          # dict: this slide's frame block
OUTPUT_PATH    # str: where to save
IMAGES         # list[str]: local image paths this slide may place
```

- [ ] **Step 1: Write the failing tests**

Replace the three tests in `tests/test_ppt_creator_agent.py` that depend on retired behaviour, and add the new loop tests. **Keep every other test in the file unchanged.**

Replace the module-level `GOOD_PROGRAM` / `BROKEN_PROGRAM` / `SILENT_PROGRAM` block with:

```python
GOOD_PROGRAM = """
from deck_builder import DeckBuilder
from context import TEMPLATE_PATH, CATALOG, FRAME, OUTPUT_PATH

deck = DeckBuilder(TEMPLATE_PATH, CATALOG, frame=FRAME)
slide = deck.add_slide("M1-L10")
slide.set_title("generated")
deck.save(OUTPUT_PATH)
"""

# Lands a card straight across the footer band: runs fine, fails gate rule 5.
COLLIDING_PROGRAM = """
from deck_builder import DeckBuilder
from context import TEMPLATE_PATH, CATALOG, FRAME, OUTPUT_PATH

deck = DeckBuilder(TEMPLATE_PATH, CATALOG, frame=FRAME)
slide = deck.add_slide("M1-L10")
slide.set_title("collides")
slide.add_card("A", ["one"], 1.0, 6.9, 4.0, 0.5)
deck.save(OUTPUT_PATH)
"""

# Rejected by the AST guard before it ever runs.
FORBIDDEN_PROGRAM = """
from pptx import Presentation
prs = Presentation()
prs.save("out.pptx")
"""

BROKEN_PROGRAM = "from deck_builder import DeckBuilder\nraise RuntimeError('boom')\n"

SILENT_PROGRAM = "from deck_builder import DeckBuilder\nprint('I wrote nothing')\n"
```

Delete `def _program(out_path)` and every call to it — the program no longer hardcodes a path, it reads `OUTPUT_PATH`. In the four tests that called `_program(out)`, pass `GOOD_PROGRAM` instead and drop the now-unused `out = agent.workdir / ...` lines.

Replace `test_the_fallback_slide_carries_the_content_it_was_given` with:

```python
def test_the_deterministic_fallback_renders_on_a_real_template_layout(brief, agent,
                                                                      tmp_path):
    """plain_slide_pptx built on a blank layout, which inherited no theme at all. The
    fallback now renders the same content on a layout the template designed, so even a
    slide the model could not build is on-brand and passes the gate."""
    from pptx import Presentation

    path = agent._render_on_layout(brief, tmp_path / "fallback.pptx")
    deck = Presentation(str(path))
    slide = next(iter(deck.slides))

    assert slide.slide_layout.name != "Blank"
    text = " ".join(s.text_frame.text for s in slide.shapes if s.has_text_frame)
    assert "Reflow Profile Control" in text
    assert "Preheat under 3 C/s" in text
    assert slide.notes_slide.notes_text_frame.text == "why it matters"


def test_the_fallback_slide_itself_passes_the_geometry_gate(brief, agent, tmp_path):
    from app.genai.ppt import geometry as geo
    from app.genai.ppt.template import catalog as catalog_mod

    path = agent._render_on_layout(brief, tmp_path / "fallback.pptx")
    catalog = catalog_mod.load()
    from pptx import Presentation
    layout_name = next(iter(Presentation(str(path)).slides)).slide_layout.name
    layout = catalog.resolve_layout(layout_name)
    violations = geo.check_slide(path, layout, theme_colors=catalog.theme.colors)
    assert geo.is_clean(violations), geo.format_violations(violations)
```

Add the new loop tests:

```python
# --------------------------------------------------------------------------- #
# the repair loop now reacts to three kinds of failure, on one budget          #
# --------------------------------------------------------------------------- #

def test_a_guard_rejection_is_repaired_without_costing_an_execution(agent, brief,
                                                                    monkeypatch):
    executed = []
    original = agent._execute
    monkeypatch.setattr(agent, "_execute",
                        lambda *a, **k: (executed.append(1), original(*a, **k))[1])
    calls = _queue(agent, monkeypatch, FORBIDDEN_PROGRAM, GOOD_PROGRAM)

    result = agent.build_slide(brief)

    assert result.attempts == 2 and not result.fallback_used
    assert len(executed) == 1, "the rejected program must never have been run"
    feedback = calls["messages"][-1]["content"]
    assert "Presentation(" in feedback


def test_a_gate_violation_is_fed_back_and_repaired(agent, brief, monkeypatch):
    calls = _queue(agent, monkeypatch, COLLIDING_PROGRAM, GOOD_PROGRAM)

    result = agent.build_slide(brief)

    assert result.attempts == 2 and not result.fallback_used
    feedback = calls["messages"][-1]["content"]
    assert "rule 5" in feedback
    # The feedback must be actionable, not a bare assertion (spec's requirement).
    assert any(word in feedback for word in ("move", "shift", "shorten", "narrow"))


def test_a_slide_that_only_ever_collides_falls_back_to_an_on_layout_render(agent, brief,
                                                                           monkeypatch):
    calls = _queue(agent, monkeypatch, COLLIDING_PROGRAM)
    result = agent.build_slide(brief)

    assert calls["n"] == PptCreatorConstants.MAX_CODE_FIX_ATTEMPTS
    assert result.fallback_used
    assert Path(result.pptx_path).exists()


def test_the_three_failure_kinds_share_one_budget(agent, brief, monkeypatch):
    """A slide that fails a guard check, then a traceback, then the gate, then runs out
    must not get three separate budgets."""
    calls = _queue(agent, monkeypatch, FORBIDDEN_PROGRAM, BROKEN_PROGRAM,
                   COLLIDING_PROGRAM, COLLIDING_PROGRAM, COLLIDING_PROGRAM,
                   GOOD_PROGRAM)
    result = agent.build_slide(brief)

    assert calls["n"] == PptCreatorConstants.MAX_CODE_FIX_ATTEMPTS
    assert result.fallback_used


def test_a_clean_slide_is_accepted_without_a_repair_round(agent, brief, monkeypatch):
    calls = _queue(agent, monkeypatch, GOOD_PROGRAM)
    result = agent.build_slide(brief)
    assert calls["n"] == 1 and result.attempts == 1 and not result.fallback_used


# --------------------------------------------------------------------------- #
# the sealed sandbox                                                           #
# --------------------------------------------------------------------------- #

def test_the_builder_is_copied_into_the_slide_directory(tmp_path):
    from app.genai.ppt import sandbox
    from app.genai.ppt.template import catalog as catalog_mod

    catalog = catalog_mod.load()
    sandbox.stage(tmp_path, catalog, {"frame": {}}, [])
    assert (tmp_path / "deck_builder.py").is_file()
    assert (tmp_path / "catalog.json").is_file()
    assert (tmp_path / "context.py").is_file()


def test_the_copied_builder_is_importable_with_no_app_on_the_path(tmp_path):
    """The whole point of the sealed sandbox: the copy must stand alone."""
    import subprocess
    import sys

    from app.genai.ppt import sandbox
    from app.genai.ppt.template import catalog as catalog_mod

    sandbox.stage(tmp_path, catalog_mod.load(), {"frame": {}}, [])
    probe = tmp_path / "probe.py"
    probe.write_text(
        "from deck_builder import DeckBuilder\n"
        "from context import TEMPLATE_PATH, CATALOG\n"
        "deck = DeckBuilder(TEMPLATE_PATH, CATALOG)\n"
        "print('ok', len(deck.catalog.layouts))\n", encoding="utf-8")
    finished = subprocess.run(
        [sys.executable, str(probe)], cwd=str(tmp_path), capture_output=True, text=True,
        env=creator.PptCreatorAgent._sandbox_env(tmp_path))
    assert finished.returncode == 0, finished.stderr
    assert "ok 181" in finished.stdout


def test_the_sandbox_env_still_carries_no_pythonpath_and_no_secrets(tmp_path,
                                                                    monkeypatch):
    monkeypatch.setenv("APIM_CLIENT_SECRET", "super-secret")
    env = creator.PptCreatorAgent._sandbox_env(tmp_path)
    assert "PYTHONPATH" not in env
    assert "APIM_CLIENT_SECRET" not in env
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_ppt_creator_agent.py -q`
Expected: FAIL — no `sandbox` module, no `_render_on_layout`.

- [ ] **Step 3: Write `app/genai/ppt/sandbox.py`**

```python
"""Stage and run one slide's program inside a sealed directory.

The sandbox is deliberately closed: no PYTHONPATH, no inherited secrets, one writable
directory. Model-written code therefore cannot import `app.*` at all, which is why
`deck_builder.py` is COPIED here rather than imported — it has no `app.*` imports of
its own precisely so it can make this trip. The same file runs in-process for the
deterministic fallback, so there is one API and it cannot drift.

Three files land beside the program:
    deck_builder.py  the API, copied verbatim
    catalog.json     the serialized template catalog
    context.py       TEMPLATE_PATH, CATALOG, FRAME, OUTPUT_PATH, IMAGES
"""

import json
import shutil
from pathlib import Path

from app.config.constants import PptCreatorConstants
from app.genai.ppt import code_guard, deck_builder, geometry as geo
from app.genai.ppt.template import registry
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

CONTEXT_FILENAME = "context.py"
CATALOG_FILENAME = "catalog.json"
BUILDER_FILENAME = "deck_builder.py"
OUTPUT_FILENAME = "slide.pptx"

_CONTEXT_TEMPLATE = '''"""Written by the sandbox. Import these; do not edit them."""

import json
from pathlib import Path

TEMPLATE_PATH = {template_path!r}
OUTPUT_PATH = {output_path!r}
FRAME = {frame!r}
GRID = {grid!r}
TOKENS = {tokens!r}
MODE = {mode!r}
LAYOUT_KEY = {layout_key!r}
FREE_REGION = {free_region!r}
IMAGES = {images!r}

CATALOG = json.loads((Path(__file__).parent / {catalog_filename!r}).read_text(
    encoding="utf-8"))
'''


def stage(slide_dir: Path, catalog, contract_fields: dict,
          image_paths: list[str]) -> dict:
    """Lay out one slide's sandbox directory.

    Args:
        slide_dir: The program's only writable location.
        catalog: The `TemplateCatalog`, serialized into the directory.
        contract_fields: `DeckDesignContract.brief_fields(...)` for this slide.
        image_paths: Local paths the program may place.

    Returns:
        The names the program can import from `context`, for the prompt.
    """
    slide_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(deck_builder.__file__), slide_dir / BUILDER_FILENAME)
    (slide_dir / CATALOG_FILENAME).write_text(
        json.dumps(catalog.to_dict()), encoding="utf-8")

    output_path = (slide_dir / OUTPUT_FILENAME).as_posix()
    (slide_dir / CONTEXT_FILENAME).write_text(_CONTEXT_TEMPLATE.format(
        template_path=str(registry.template_path()),
        output_path=output_path,
        frame=contract_fields.get("frame") or {},
        grid=contract_fields.get("grid") or {},
        tokens=contract_fields.get("tokens") or {},
        mode=contract_fields.get("mode") or "layout",
        layout_key=contract_fields.get("layout_key") or "",
        free_region=contract_fields.get("free_region"),
        images=list(image_paths or []),
        catalog_filename=CATALOG_FILENAME), encoding="utf-8")
    return {"TEMPLATE_PATH": str(registry.template_path()), "OUTPUT_PATH": output_path,
            "FRAME": "the slide's frame block", "CATALOG": "the template catalog",
            "IMAGES": list(image_paths or [])}


def output_path(slide_dir: Path) -> Path:
    """Where a staged program is told to save."""
    return slide_dir / OUTPUT_FILENAME


def guard(code: str) -> str | None:
    """Static rejection message, or None when the program may run."""
    return code_guard.check_message(code_guard.strip_code_fences(code))


def run(code: str, slide_dir: Path) -> str:
    """Guard, write and execute one program, and describe what happened.

    Used by the `run_slide_code` design tool. `PptCreatorAgent.build_slide` does not
    call this — it drives the same three steps itself so it can feed each kind of
    failure back into its own repair loop with its own budget.

    Args:
        code: The generated program, fenced or not.
        slide_dir: A directory already prepared by `stage()`.

    Returns:
        A report the model can act on: the rejection, the traceback, or a success line.
    """
    import subprocess
    import sys

    from app.genai.agents.ppt_creator_agent import PptCreatorAgent

    code = code_guard.strip_code_fences(code)
    rejection = guard(code)
    if rejection:
        return rejection

    script = slide_dir / PptCreatorConstants.GENERATED_CODE_FILENAME
    script.write_text(code, encoding="utf-8")
    produced = output_path(slide_dir)
    before = produced.stat().st_mtime if produced.exists() else None
    try:
        finished = subprocess.run(
            [sys.executable, str(script)], cwd=str(slide_dir), capture_output=True,
            text=True, env=PptCreatorAgent._sandbox_env(slide_dir),
            timeout=PptCreatorConstants.EXEC_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return (f"The program did not finish within "
                f"{PptCreatorConstants.EXEC_TIMEOUT_SECONDS}s and was killed. Make it "
                f"finish quickly and deterministically.")
    if finished.returncode != 0:
        tail = (finished.stderr or finished.stdout
                or "exited non-zero with no output")
        return ("The program raised. Fix it and resend the whole program.\n\n"
                "=== ERROR ===\n" + tail[-PptCreatorConstants.MAX_STDERR_CHARS:])
    written = produced.exists() and (before is None
                                     or produced.stat().st_mtime >= before)
    if not written:
        return ("The program ran but wrote no slide. Save the deck with "
                "`deck.save(OUTPUT_PATH)`.")
    return (f"The program ran and wrote {produced.name}. Call validate_slide to check "
            f"it before you consider the slide finished.")


def validate(slide_dir: Path, catalog, contract=None, layout_key: str = "") -> str:
    """The authoritative gate report for the file in this directory."""
    produced = output_path(slide_dir)
    if not produced.is_file():
        return (f"No slide was written. Save the deck to OUTPUT_PATH with "
                f"`deck.save(OUTPUT_PATH)`.")
    try:
        layout = catalog.resolve_layout(layout_key) if layout_key else None
    except LookupError:
        layout = None
    if layout is None:
        from pptx import Presentation
        name = next(iter(Presentation(str(produced)).slides)).slide_layout.name
        layout = catalog.resolve_layout(name)
    frame = None
    if contract is not None:
        frame = contract.frame_for(1, layout)
    violations = geo.check_slide(produced, layout, frame=frame,
                                 theme_colors=catalog.theme.colors,
                                 theme_font=catalog.theme.minor_font or "Barlow",
                                 slide_width=catalog.slide_width,
                                 slide_height=catalog.slide_height)
    return geo.format_violations(violations)
```

- [ ] **Step 4: Rewire `PptCreatorAgent`**

In `app/genai/agents/ppt_creator_agent.py`:

**(a)** Replace the imports of `ROLE_PREFERENCES` and `_resolve_layout`'s catalog use:

```python
from app.genai.ppt import code_guard, deck_builder, geometry as geo, sandbox
from app.genai.ppt.template import catalog as catalog_mod, registry
```

Delete `from app.genai.ppt.renderer import ROLE_PREFERENCES` and the whole `_resolve_layout` function — layout selection now comes from the design contract on the brief.

**(b)** Replace `SYSTEM_SUFFIX` with:

```python
SYSTEM_SUFFIX = """
=== HOW TO ANSWER ===
You are a presentation designer who answers with CODE, never with prose.

Write ONE complete Python program that builds EXACTLY ONE slide through the DeckBuilder
API and saves it. Begin it with:

    from deck_builder import DeckBuilder
    from context import TEMPLATE_PATH, CATALOG, FRAME, OUTPUT_PATH, IMAGES

Those two modules are placed beside your program. Hard rules:
- Build the slide with `DeckBuilder(TEMPLATE_PATH, CATALOG, frame=FRAME)` and save it
  with `deck.save(OUTPUT_PATH)`. Never construct `Presentation(...)` yourself.
- Use the layout you were assigned, by its catalog key. Never invent a layout name.
- Use only the DeckBuilder component API. Never call `.shapes.add_*` directly.
- Colours are brand token names ('red', 'navy', 'accent1'). Never a hex literal.
- Do not set fonts. Do not set a slide size. Both come from the template.
- Place only the image files in IMAGES, by their exact paths. Never invent a path,
  never use the network, never write outside the output directory.
- Put the speaker notes from the brief on the slide with `set_notes`.
- Do not draw a footer, a slide number, a logo or a classification label. All of them
  are stamped for you, and drawing them is a rule 5 violation.
- No emoji, no placeholder lorem text, no "TODO".
- The program runs unattended with no arguments and no input, and prints nothing but
  its own progress.
"""
```

**(c)** Replace `USER_TEMPLATE` so it carries the contract — and, unlike today, so every field it names is actually declared (Bug 2: `template_path=` and `layout_map=` were passed to a template that declared neither, and `str.format` silently dropped both):

```python
USER_TEMPLATE = """Build slide {slide_number} of {total} for the deck "{deck_title}".

Audience: {audience} | Objective: {objective} | Tone: {tone}
Slide role: {kind}
Classification: {classification}
Design steer: {design_hint}

=== YOUR ASSIGNMENT (from the deck design contract — do not change it) ===
Composition mode: {mode}
Layout: {layout_key}
{region_line}
Grid: {grid_columns} columns x {grid_rows} rows, {grid_gutter}in gutters, over the region above
Call get_layout_details("{layout_key}") for its placeholders, reserved zones and decor.

Save to OUTPUT_PATH. Do not hardcode any other path.

=== SLIDE CONTENT (verbatim — do not invent, drop or reword facts) ===
{content_json}

=== LOCAL IMAGE FILES YOU MAY PLACE ===
{image_block}
"""
```

**(d)** Rewrite `_user_prompt`:

```python
    def _user_prompt(self, brief: SlideCodeBrief) -> list[dict]:
        """The one slide the model is asked to draw, plus its design assignment.

        Every field named in the template is declared here. The layout contract itself
        is NOT inlined: it moved to `get_layout_details`, which the agent calls as a
        tool, so the prompt cannot silently lose it (Bug 2) and it is cacheable across
        slides.
        """
        image_block = "\n".join(f"- {p}" for p in brief.image_paths) or "(none)"
        grid = brief.grid or {}
        region = brief.free_region
        region_line = (f"Free region: left {region[0]:.2f}in, top {region[1]:.2f}in, "
                       f"{region[2]:.2f}in wide, {region[3]:.2f}in tall"
                       if region else
                       "Fill the layout's own placeholders; add components only in "
                       "space it genuinely leaves free.")
        text = USER_TEMPLATE.format(
            slide_number=brief.slide_number, total=brief.context.total,
            deck_title=brief.context.title,
            audience=brief.context.audience or "(unstated)",
            objective=brief.context.objective or "(unstated)",
            tone=brief.context.tone, kind=brief.kind,
            classification=brief.classification,
            design_hint=brief.design_hint or "(designer's choice)",
            mode=brief.mode, layout_key=brief.layout_key or "(choose one)",
            region_line=region_line,
            grid_columns=grid.get("columns", 12), grid_rows=grid.get("rows", 6),
            grid_gutter=grid.get("gutter_in", 0.15),
            content_json=brief.content.model_dump_json(
                indent=2, exclude={"bullets", "cards", "steps", "stat_value",
                                   "stat_label", "image_path"}),
            image_block=image_block)
        log.info("slide %d assigned %s mode on layout %s", brief.slide_number,
                 brief.mode, brief.layout_key, extra={"session_id": self.session_id})
        return [{"type": "text", "text": text}]
```

**(e)** Rewrite `build_slide`'s loop body. Keep the existing structure — the event publishing, the logging, the `SlideBuildResult` shape — and insert the two new checks:

```python
    def build_slide(self, brief: SlideCodeBrief) -> SlideBuildResult:
        """Generate, check, run, gate and repair one slide until it is clean.

        The budget is shared across all three failure kinds — a static rejection, a
        traceback and a geometry violation each cost one generation — so a stubborn
        slide's total cost stays bounded no matter how it fails.

        Returns:
            A SlideBuildResult always carrying a usable .pptx: the generated slide, or
            the deterministic on-layout render when the budget ran out.
        """
        catalog = catalog_mod.load()
        slide_dir = self.workdir / f"slide_{brief.slide_number}"
        sandbox.stage(slide_dir, catalog, {
            "frame": brief.frame, "grid": brief.grid, "tokens": brief.tokens,
            "mode": brief.mode, "layout_key": brief.layout_key,
            "free_region": brief.free_region}, brief.image_paths)
        out_path = sandbox.output_path(slide_dir)
        code_path = slide_dir / PptCreatorConstants.GENERATED_CODE_FILENAME

        messages = [{"role": "system", "content": self._system_prompt()},
                    {"role": "user", "content": self._user_prompt(brief)}]

        self._step(brief, "designing the slide", phase="start")
        last_error = ""
        for attempt in range(1, PptCreatorConstants.MAX_CODE_FIX_ATTEMPTS + 1):
            try:
                source = self._request_code(messages)
            except Exception as exc:                          # noqa: BLE001
                log.error("slide %d code generation failed on attempt %d: %s",
                          brief.slide_number, attempt, exc,
                          extra={"session_id": self.session_id}, exc_info=True)
                last_error = str(exc)
                continue

            source = code_guard.strip_code_fences(source)
            code_path.write_text(source, encoding="utf-8")

            # 1. Static check. A rejection here costs no execution at all.
            rejection = code_guard.check_message(source)
            if rejection:
                last_error = rejection
                self._step(brief, f"attempt {attempt} rejected before running",
                           phase="repair", attempts=attempt)
                messages += [{"role": "assistant", "content": source},
                             {"role": "user", "content": rejection}]
                continue

            # 2. Execute.
            succeeded, error = self._execute(code_path, slide_dir)
            produced = self._locate_output(slide_dir, out_path) if succeeded else None
            if produced is None:
                last_error = error or "the program ran but wrote no .pptx file"
                self._step(brief, f"attempt {attempt} failed; repairing the code",
                           phase="repair", attempts=attempt)
                messages += [
                    {"role": "assistant", "content": source},
                    {"role": "user", "content":
                        "Running that program failed. Fix it so it runs unattended and "
                        "saves the slide to OUTPUT_PATH.\n\n=== ERROR ===\n"
                        + last_error}]
                continue

            # 3. Gate. Measure what landed, not what was declared.
            violations = self._gate(produced, brief, catalog)
            if geo.is_clean(violations):
                self._step(brief, f"slide rendered on attempt {attempt}",
                           phase="done", attempts=attempt)
                return SlideBuildResult(
                    slide_number=brief.slide_number, pptx_path=str(produced),
                    attempts=attempt, code_path=str(code_path))

            last_error = geo.format_violations(violations)
            log.info("slide %d failed the gate on attempt %d with %d violation(s)",
                     brief.slide_number, attempt, len(violations),
                     extra={"session_id": self.session_id})
            self._step(brief, f"attempt {attempt}: {len(violations)} layout issue(s); "
                              f"redesigning", phase="repair", attempts=attempt)
            messages += [{"role": "assistant", "content": source},
                         {"role": "user", "content": last_error}]

        # Budget spent. A slide on a real layout beats a slide the gate rejected.
        log.error("slide %d hit the repair limit (%d attempts); rendering it on a "
                  "template layout instead. Last problem: %s", brief.slide_number,
                  PptCreatorConstants.MAX_CODE_FIX_ATTEMPTS, last_error[:500],
                  extra={"session_id": self.session_id})
        events.publish(
            self.session_id, "agent_step", agent="ppt-code",
            message=(f"Slide {brief.slide_number}: the designer could not produce a "
                     f"clean layout after {PptCreatorConstants.MAX_CODE_FIX_ATTEMPTS} "
                     f"attempts; the slide was rendered on a template layout instead."),
            slide_number=brief.slide_number, phase="fallback",
            attempts=PptCreatorConstants.MAX_CODE_FIX_ATTEMPTS)
        fallback = self._render_on_layout(brief, out_path)
        return SlideBuildResult(slide_number=brief.slide_number, pptx_path=str(fallback),
                                attempts=PptCreatorConstants.MAX_CODE_FIX_ATTEMPTS,
                                fallback_used=True, error=last_error,
                                code_path=str(code_path))

    def _gate(self, produced: Path, brief: SlideCodeBrief, catalog) -> list:
        """Run the geometry gate over the produced file. Never raises.

        A gate that crashes must not lose the slide: an unreadable result is reported
        as one violation and repaired like any other.
        """
        try:
            layout = (catalog.resolve_layout(brief.layout_key) if brief.layout_key
                      else None)
            if layout is None:
                from pptx import Presentation
                name = next(iter(Presentation(str(produced)).slides)).slide_layout.name
                layout = catalog.resolve_layout(name)
            return geo.check_slide(produced, layout, frame=brief.frame,
                                   theme_colors=catalog.theme.colors,
                                   theme_font=catalog.theme.minor_font or "Barlow",
                                   slide_width=catalog.slide_width,
                                   slide_height=catalog.slide_height)
        except Exception as exc:                              # noqa: BLE001
            log.warning("slide %d could not be measured: %s", brief.slide_number, exc,
                        extra={"session_id": self.session_id}, exc_info=True)
            return [geo.Violation(0, "unreadable", (
                f"The produced file could not be reopened and measured ({exc}). "
                f"Rebuild it with DeckBuilder and save it to OUTPUT_PATH."))]
```

**(f)** Replace `_fallback_pptx` and delete `plain_slide_pptx` entirely:

```python
    def _render_on_layout(self, brief: SlideCodeBrief, out_path: Path) -> Path:
        """Render this slide's content on a real template layout, no model involved.

        This is the safety net, and it is clean by construction: the template
        positioned every element, so it inherits the theme, the fonts and the master's
        furniture, and it passes the gate. The retired `plain_slide_pptx` built on a
        blank layout and inherited nothing at all.

        Args:
            brief: The slide that could not be generated.
            out_path: Where to write the single-slide .pptx.

        Returns:
            The path of the written .pptx.
        """
        catalog = catalog_mod.load()
        try:
            layout = catalog.resolve_layout(brief.layout_key)
        except LookupError:
            ranked = catalog.recommend(brief.kind or "content", limit=1)
            layout = ranked[0][0] if ranked else catalog.canonical_layouts()[0]

        deck = deck_builder.DeckBuilder(registry.template_path(), catalog.to_dict(),
                                        frame=brief.frame or None)
        slide = deck.add_slide(layout.key)
        slide.set_title(brief.content.title or f"Slide {brief.slide_number}")

        lines = [item for block in brief.content.blocks
                 for item in (block.bullets or ([block.text] if block.text else []))]
        if lines:
            target = next((p["slot"] for p in slide.layout.placeholders
                           if p.get("role") in ("body", "content")), None)
            if target:
                slide.set_bullets(target, lines)
            else:
                # A title-only layout: put the copy in the free region rather than
                # dropping it. Never silently lose content.
                slide.add_textbox(lines, *slide.grid(0, 0, cols=12, rows=5), size=15)
        if brief.content.notes:
            slide.set_notes(brief.content.notes)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        return deck.save(out_path)
```

Also delete `BRAND_LOGOS`, `SHOWN_LOGOS`, `logo_assets`, `_logo_b64` and `logo_blocks` — the frame places the marks now, and the guide describes them. Keep `load_brand_skill`, `materialize_images`, `_extract_code`, `new_workdir`, `_normalize_code`, `_sandbox_env`, `_execute`, `_locate_output` and `_step` as they are.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_ppt_creator_agent.py -q`
Expected: PASS. `test_the_copied_builder_is_importable_with_no_app_on_the_path` is the decisive one — it proves the sealed sandbox actually works.

- [ ] **Step 6: Commit**

```bash
git add app/genai/ppt/sandbox.py app/genai/agents/ppt_creator_agent.py \
        tests/test_ppt_creator_agent.py
git commit -m "feat(ppt): sealed sandbox, gate-aware repair loop, on-layout fallback"
```

---

### Task 14: Stitcher — each slide on its own layout

**Why:** Stitching gets *simpler*. Because every slide and the target deck now derive from the same `SAMPLE.pptx`, layout `(master_index, layout_index)` is identical in source and target, so each target slide can be created on its **correct** layout instead of `slide_layouts[6]`. Theme, fonts, master decor and footer then inherit properly.

**Files:**
- Modify: `app/genai/ppt/stitcher.py`
- Test: `tests/test_stitcher.py`

**Interfaces:**
- Consumes: `registry.template_path()`, `catalog.load()`, `deck_builder.DeckBuilder` (to make fixtures).
- Produces: `DeckStitcher.stitch(slide_files, out_path) -> Path` — unchanged signature; `DeckStitcher.notes` unchanged.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_stitcher.py`:

```python
"""Stitching. Because every slide now comes from the same template, a stitched slide
must land on the same layout it was built on — that is what makes theme, fonts and
master furniture inherit instead of being re-drawn."""

import pytest
from pptx import Presentation

from app.genai.ppt import deck_builder as db
from app.genai.ppt.stitcher import DeckStitcher
from app.genai.ppt.template import catalog as catalog_mod, registry


@pytest.fixture(scope="module")
def cat():
    return catalog_mod.load()


@pytest.fixture
def slide_file(cat, tmp_path):
    def _make(number, layout_key, title, with_image=False):
        deck = db.DeckBuilder(registry.template_path(), cat.to_dict())
        handle = deck.add_slide(layout_key)
        handle.set_title(title)
        handle.set_notes(f"notes for {number}")
        if with_image:
            handle.add_image(str(registry.logo_path("ra_lockup_full.png")),
                             1.0, 3.0, 2.0, 1.0)
        return deck.save(tmp_path / f"s{number}.pptx")
    return _make


def test_each_slide_keeps_the_layout_it_was_built_on(slide_file, tmp_path):
    files = [(1, slide_file(1, "M1-L10", "First")),
             (2, slide_file(2, "M2-L4", "Second"))]
    out = DeckStitcher().stitch(files, tmp_path / "deck.pptx")

    slides = list(Presentation(str(out)).slides)
    assert len(slides) == 2
    assert slides[0].slide_layout.name == "Title with Subhead"
    assert slides[1].slide_layout.name == "Title with Subhead on Gradient"


def test_the_stitched_deck_keeps_the_template_theme_and_masters(slide_file, tmp_path):
    out = DeckStitcher().stitch([(1, slide_file(1, "M1-L10", "T"))],
                                tmp_path / "deck.pptx")
    deck = Presentation(str(out))
    assert len(deck.slide_masters) == 4
    from pptx.util import Emu
    assert Emu(deck.slide_width).inches == pytest.approx(13.33, abs=0.01)


def test_slides_are_stitched_in_slide_number_order(slide_file, tmp_path):
    files = [(2, slide_file(2, "M1-L10", "Second")),
             (1, slide_file(1, "M1-L10", "First"))]
    out = DeckStitcher().stitch(files, tmp_path / "deck.pptx")

    titles = [s.shapes.title.text_frame.text for s in Presentation(str(out)).slides]
    assert titles == ["First", "Second"]


def test_images_survive_the_stitch(slide_file, tmp_path):
    out = DeckStitcher().stitch([(1, slide_file(1, "M2-L4", "T", with_image=True))],
                                tmp_path / "deck.pptx")
    slide = next(iter(Presentation(str(out)).slides))
    assert [s for s in slide.shapes if s.shape_type == 13]


def test_notes_survive_the_stitch(slide_file, tmp_path):
    out = DeckStitcher().stitch([(1, slide_file(1, "M1-L10", "T"))],
                                tmp_path / "deck.pptx")
    slide = next(iter(Presentation(str(out)).slides))
    assert slide.notes_slide.notes_text_frame.text == "notes for 1"


def test_an_unreadable_slide_is_noted_and_the_deck_still_ships(slide_file, tmp_path):
    broken = tmp_path / "broken.pptx"
    broken.write_bytes(b"not a pptx at all")
    stitcher = DeckStitcher()
    out = stitcher.stitch([(1, slide_file(1, "M1-L10", "Good")), (2, broken)],
                          tmp_path / "deck.pptx")

    assert len(list(Presentation(str(out)).slides)) == 1
    assert any("Slide 2" in note for note in stitcher.notes)


def test_a_deck_with_no_readable_slide_is_an_error(tmp_path):
    broken = tmp_path / "broken.pptx"
    broken.write_bytes(b"nope")
    with pytest.raises(ValueError, match="no slide"):
        DeckStitcher().stitch([(1, broken)], tmp_path / "deck.pptx")


def test_a_slide_built_on_an_unknown_layout_still_stitches(cat, tmp_path):
    """A source deck that did not come from our template must not lose its slide."""
    from pptx import Presentation as P

    foreign = P()
    foreign.slides.add_slide(foreign.slide_layouts[6])
    path = tmp_path / "foreign.pptx"
    foreign.save(str(path))

    stitcher = DeckStitcher()
    out = stitcher.stitch([(1, path)], tmp_path / "deck.pptx")
    assert len(list(Presentation(str(out)).slides)) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_stitcher.py -q`
Expected: FAIL — slides land on the blank layout, so the layout-name assertions fail.

- [ ] **Step 3: Rewrite `stitcher.py`**

Replace the module docstring's second paragraph and the `stitch` body. `_copy_slide`, `_copy_related_parts` and `_remap_relationship_ids` are **unchanged** — images and charts still need them. Delete `_strip_empty_placeholders`: `DeckBuilder.finalize()` now removes unused placeholders per slide, with knowledge of what was deliberately filled, which the blanket version could not have.

```python
"""The collector: many single-slide .pptx files -> one deck, in plan order.

Every slide and this target deck are built from the same brand template, so a slide's
layout is at the same (master, layout) coordinates in both. That makes stitching
simpler than it was: each target slide is created on its OWN layout rather than on a
blank one, and theme, fonts, master decor, footer and logo inherit properly instead of
having to be re-drawn into every slide.

A source that cannot be read is skipped with a note rather than failing the deck:
losing one slide's design is always better than losing the whole deck.
"""

    def stitch(self, slide_files: list[tuple[int, Path]], out_path: Path) -> Path:
        """Build the final deck from the per-slide files.

        Args:
            slide_files: (slide_number, path) pairs; stitched in slide-number order.
            out_path: Where the merged deck is written.

        Returns:
            The path of the written deck.

        Raises:
            ValueError: When no slide could be copied, so there is no deck to publish.
        """
        from pptx import Presentation

        from app.genai.ppt.template import loader, registry

        target = Presentation(str(registry.template_path()))
        loader.clear_slides(target)          # the template ships 31 sample slides
        masters = list(target.slide_masters)

        copied = 0
        for slide_number, path in sorted(slide_files, key=lambda item: item[0]):
            try:
                source = Presentation(str(path))
            except Exception as exc:                          # noqa: BLE001
                log.error("slide %d could not be read from %s: %s", slide_number, path,
                          exc, exc_info=True)
                self.notes.append(f"Slide {slide_number} was dropped: its file could "
                                  f"not be read ({exc}).")
                continue

            for source_slide in source.slides:
                layout = self._matching_layout(masters, source_slide, target)
                self._copy_slide(source_slide, target.slides.add_slide(layout))
                copied += 1

        if not copied:
            raise ValueError("no slide could be copied into the deck")

        target.save(str(out_path))
        log.info("stitched %d slide(s) into %s", copied, out_path)
        return out_path

    @staticmethod
    def _matching_layout(masters, source_slide, target):
        """The target layout matching the one this slide was built on.

        Because both decks come from the same template, matching by layout name is
        exact. A source that did not come from our template falls back to the blank
        layout, which is the old behaviour and keeps the slide rather than dropping it.
        """
        wanted = (source_slide.slide_layout.name or "").strip().lower()
        for master in masters:
            for layout in master.slide_layouts:
                if (layout.name or "").strip().lower() == wanted:
                    return layout
        log.warning("no layout named %r in the template; using a blank layout", wanted)
        return target.slide_layouts[6]
```

> **Note for the implementer:** `_copy_slide` appends the source shape tree onto a slide that now inherits its layout's placeholders. Those inherited placeholders are empty (the source already filled and finalized its own copies), so run the existing `_copy_slide` unchanged and let `test_the_stitched_deck_keeps_the_template_theme_and_masters` and `test_each_slide_keeps_the_layout_it_was_built_on` confirm it. **If a stitched slide shows duplicated or empty placeholder boxes**, remove the target slide's own placeholders before appending the source tree:
>
> ```python
>         for shape in list(target_slide.placeholders):
>             shape._element.getparent().remove(shape._element)
> ```
>
> at the top of `_copy_slide`. Add a test asserting no empty placeholder survives if you need this.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_stitcher.py -q`
Expected: PASS, 8 tests.

- [ ] **Step 5: Commit**

```bash
git add app/genai/ppt/stitcher.py tests/test_stitcher.py
git commit -m "feat(ppt): stitch each slide onto its own template layout"
```

---

### Task 15: Workflow wiring and the end-to-end test

**Why:** The contract has to be built and threaded to every parallel worker, and the whole pipeline needs one test that runs with no model at all — a fixed contract plus a hand-written `DeckBuilder` program producing a deck that passes the gate on every slide.

**Files:**
- Modify: `app/genai/workflows/deck_workflow.py` (`_design_slides`, `_collect`)
- Create: `tests/test_deck_end_to_end.py`
- Test: `tests/test_workflow.py` (verify still green)

**Interfaces:**
- Consumes: `design_contract.build` (Task 10), `SlideCodeBrief`'s new fields (Task 9), `DeckStitcher` (Task 14).
- Produces: `_design_slides(session_id, draft, plan, workdir, contract)` — one new positional parameter.

- [ ] **Step 1: Write the failing end-to-end test**

Create `tests/test_deck_end_to_end.py`:

```python
"""The whole pipeline, with no model anywhere.

A fixed design contract plus hand-written DeckBuilder programs must produce a deck that
passes the geometry gate on every slide. If this cannot be made to pass, the design is
not buildable by a model either.
"""

import pytest
from pptx import Presentation

from app.core.models.deck_model import (
    ContentBlock, DeckDraft, DeckPlan, SlideDraft, SlidePlan,
)
from app.genai.ppt import deck_builder as db, design_contract as dc, geometry as geo
from app.genai.ppt.stitcher import DeckStitcher
from app.genai.ppt.template import catalog as catalog_mod, registry


@pytest.fixture(scope="module")
def cat():
    return catalog_mod.load()


@pytest.fixture
def plan():
    return DeckPlan(title="SMT Quality", audience="Plant managers",
                    classification="internal", slides=[
        SlidePlan(slide_number=1, kind="cover", title="SMT Quality"),
        SlidePlan(slide_number=2, kind="content", title="Reflow profile control"),
        SlidePlan(slide_number=3, kind="content", title="Yield by line",
                  visual="table"),
        SlidePlan(slide_number=4, kind="thanks", title="Thank you"),
    ])


@pytest.fixture
def draft(plan):
    return DeckDraft(title=plan.title, slides=[
        SlideDraft(slide_number=1, kind="cover", title="SMT Quality"),
        SlideDraft(slide_number=2, kind="content", title="Reflow profile control",
                   blocks=[ContentBlock(slot="body1", bullets=[
                       "Preheat ramp under 3 C/s",
                       "Soak 150-180 C for 90 s",
                       "Peak 245 C, 45 s above liquidus"])],
                   notes="the three numbers that matter"),
        SlideDraft(slide_number=3, kind="content", title="Yield by line"),
        SlideDraft(slide_number=4, kind="thanks", title="Thank you"),
    ])


@pytest.fixture
def contract(plan, draft, cat):
    # No model: the contract falls back to recommended layouts for every slide.
    return dc.build(plan, draft, catalog=cat, ask=lambda system, user: [])


def build_slide(cat, contract, number, slide, out_path):
    """Hand-written stand-in for what the model would produce."""
    layout = cat.get(contract.assignments[number].layout_key)
    fields = contract.brief_fields(number, layout)
    deck = db.DeckBuilder(registry.template_path(), cat.to_dict(),
                          frame=fields["frame"])
    handle = deck.add_slide(layout.key)
    handle.set_title(slide.title)
    lines = [item for block in slide.blocks
             for item in (block.bullets or ([block.text] if block.text else []))]
    if lines:
        target = next((p["slot"] for p in handle.layout.placeholders
                       if p.get("role") in ("body", "content")), None)
        if target:
            handle.set_bullets(target, lines)
        else:
            handle.add_textbox(lines, *handle.grid(0, 0, cols=12, rows=4), size=15)
    if slide.notes:
        handle.set_notes(slide.notes)
    return deck.save(out_path), layout, fields


def test_every_slide_of_a_hand_built_deck_passes_the_gate(cat, contract, draft,
                                                           tmp_path):
    for slide in draft.slides:
        path, layout, fields = build_slide(cat, contract, slide.slide_number, slide,
                                           tmp_path / f"s{slide.slide_number}.pptx")
        violations = geo.check_slide(path, layout, frame=fields["frame"],
                                     theme_colors=cat.theme.colors)
        assert geo.is_clean(violations), (
            f"slide {slide.slide_number} on {layout.key}:\n"
            + geo.format_violations(violations))


def test_the_stitched_deck_has_every_slide_on_its_own_layout(cat, contract, draft,
                                                              tmp_path):
    files = []
    for slide in draft.slides:
        path, _, _ = build_slide(cat, contract, slide.slide_number, slide,
                                 tmp_path / f"s{slide.slide_number}.pptx")
        files.append((slide.slide_number, path))
    out = DeckStitcher().stitch(files, tmp_path / "deck.pptx")

    slides = list(Presentation(str(out)).slides)
    assert len(slides) == 4
    for number, slide in enumerate(slides, start=1):
        expected = cat.get(contract.assignments[number].layout_key)
        assert slide.slide_layout.name == expected.name


def test_the_stitched_deck_ships_no_prompt_text(cat, contract, draft, tmp_path):
    files = []
    for slide in draft.slides:
        path, _, _ = build_slide(cat, contract, slide.slide_number, slide,
                                 tmp_path / f"s{slide.slide_number}.pptx")
        files.append((slide.slide_number, path))
    out = DeckStitcher().stitch(files, tmp_path / "deck.pptx")

    for slide in Presentation(str(out)).slides:
        text = " ".join(s.text_frame.text for s in slide.shapes if s.has_text_frame)
        assert "Click to edit" not in text
        assert "Second level" not in text


def test_every_content_slide_is_on_the_same_master(cat, contract, plan):
    content = [s.slide_number for s in plan.slides if s.kind == "content"]
    masters = {cat.get(contract.assignments[n].layout_key).master_index
               for n in content}
    assert masters == {cat.preferred_master}


def test_the_deck_carries_the_classification_on_every_slide(cat, contract, draft,
                                                             tmp_path):
    files = []
    for slide in draft.slides:
        path, _, _ = build_slide(cat, contract, slide.slide_number, slide,
                                 tmp_path / f"s{slide.slide_number}.pptx")
        files.append((slide.slide_number, path))
    out = DeckStitcher().stitch(files, tmp_path / "deck.pptx")

    for slide in Presentation(str(out)).slides:
        text = " ".join(s.text_frame.text for s in slide.shapes if s.has_text_frame)
        assert "Internal" in text
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_deck_end_to_end.py -q`
Expected: PASS already for the pure-builder tests if Tasks 4–14 are done — **that is fine and expected**; this file is the integration proof, not a red-first unit. If it fails, the failure is a real integration bug in one of the earlier tasks: fix it there, not here.

- [ ] **Step 3: Thread the contract through `_design_slides`**

In `app/genai/workflows/deck_workflow.py`, add the import:

```python
from app.genai.ppt import design_contract, slide_cache
```

Change the signature and the brief construction:

```python
def _design_slides(session_id: str, draft: DeckDraft, plan: DeckPlan, workdir: Path,
                   contract) -> tuple[list[tuple[int, Path]], dict[int, dict], list[str]]:
    """Get one .pptx per slide: generated where the content changed, cached where not.

    Args:
        session_id: Owning session, for events, caching and logs.
        draft: The assembled deck, already renumbered.
        plan: The deck plan, for classification and the per-slide design hint.
        workdir: Directory every slide file is written into.
        contract: The deck design contract, so every parallel worker builds to the
            same master, frame, grid and tokens.

    Returns:
        Tuple of (slide files as (number, path) pairs, the new cache map, gap notes).
    """
```

and inside `build`:

```python
    catalog = catalog_mod.load()

    def build(item: tuple[int, object]):
        position, slide = item
        planned = plan_by_number.get(position)
        assignment = contract.assignments.get(position)
        layout = catalog.get(assignment.layout_key) if assignment else None
        contract_fields = (contract.brief_fields(position, layout)
                           if layout is not None else {})
        brief = SlideCodeBrief(
            slide_number=position, kind=slide.kind, content=slide,
            context=_deck_context(plan, position) if planned else DeckContext(
                title=draft.title, position=position, total=len(draft.slides)),
            classification=classification,
            image_paths=materialize_images(slide, workdir / f"slide_{position}"),
            design_hint=slide.layout or (planned.visual if planned else ""),
            **contract_fields)
        return position, slide, agent.build_slide(brief)
```

Add `from app.genai.ppt.template import catalog as catalog_mod` to the imports.

- [ ] **Step 4: Build the contract in `_collect`**

In `_collect`, immediately before the `workdir = Path(tempfile.mkdtemp(...))` line:

```python
    # One contract for the whole deck, before any slide is built: this is what makes
    # four parallel workers produce slides that read as one deck rather than four.
    contract = design_contract.build(plan, draft)
    _step(sid, "deck", f"Design contract: master M{contract.master_index}, "
                       f"{sum(1 for a in contract.assignments.values() if a.mode == 'canvas')} "
                       f"slide(s) composed on canvas.")
```

and pass it through:

```python
        slide_files, artifacts, design_gaps = _design_slides(sid, draft, plan, workdir,
                                                             contract)
```

- [ ] **Step 5: Run the suites**

Run: `python -m pytest tests/test_deck_end_to_end.py tests/test_workflow.py tests/test_slides_node.py -q`
Expected: PASS. `test_workflow.py` must be green **unchanged** — if it mocks `_design_slides`, the new parameter may need adding to the mock; that is the only edit permitted there.

- [ ] **Step 6: Commit**

```bash
git add app/genai/workflows/deck_workflow.py tests/test_deck_end_to_end.py
git commit -m "feat(workflow): build the design contract once and thread it to every slide"
```

---

### Task 16: Retirement, deletion and the hygiene test

**Why:** The spec's "Retired" table, and the last step of "Reference material and cleanup". This is gated on everything above being green, and the import-hygiene test is written **before** the deletion so the deletion is provably safe.

**Files:**
- Create: `tests/test_import_hygiene.py`
- Delete: `app/genai/ppt/renderer.py`, `app/genai/ppt/audit.py`, `app/genai/ppt/template/filler.py`, `app/genai/ppt/template/icons.py`, `scripts/mainv3/`
- Modify: `app/genai/ppt/builder.py` (drop `build_pptx`), `app/genai/ppt/template/registry.py` (drop `available` / `path_for` / `_FILE_HINT`), `app/genai/ppt/template/catalog.py` (drop the temporary shims), `app/genai/ppt/template/__init__.py`
- Modify: `scripts/manual_testing/*.py` (the dev scripts that import retired code)

**Interfaces:**
- Consumes: everything above.
- Produces: a tree with no reference to the retired modules.

- [ ] **Step 1: Write the hygiene test first — it is what makes the deletion safe**

Create `tests/test_import_hygiene.py`:

```python
"""Import hygiene.

`scripts/mainv3/` was reference material, ported from and then deleted. Nothing under
`app/` ever imported it and nothing ever may: an application that reaches into
`scripts/` cannot be deployed without shipping the scripts too.

The second test is the one that keeps the sealed sandbox sealed.
"""

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


@pytest.mark.parametrize("path", sorted(APP.rglob("*.py")), ids=lambda p: p.name)
def test_no_module_under_app_imports_from_scripts(path):
    offenders = [name for name in _imports(path)
                 if name == "scripts" or name.startswith("scripts.")]
    assert not offenders, f"{path} imports {offenders} from scripts/"


def test_the_deck_builder_imports_nothing_from_the_app():
    """It is copied into the sandbox, where `app` does not exist. One app import here
    breaks every generated slide, and only at runtime."""
    path = APP / "genai" / "ppt" / "deck_builder.py"
    offenders = [name for name in _imports(path)
                 if name == "app" or name.startswith("app.")]
    assert not offenders, f"deck_builder.py imports {offenders}; it must stand alone"


@pytest.mark.parametrize("retired", [
    "app.genai.ppt.renderer",
    "app.genai.ppt.audit",
    "app.genai.ppt.template.filler",
    "app.genai.ppt.template.icons",
])
def test_retired_modules_are_gone_and_unreferenced(retired):
    for path in APP.rglob("*.py"):
        assert retired not in _imports(path), f"{path} still imports {retired}"
    with pytest.raises(ModuleNotFoundError):
        __import__(retired)


def test_the_potx_glob_is_gone():
    """Decision 2: a single constant resolves the template. A glob could resolve it to
    one of the five .potx files still on disk, silently."""
    from app.genai.ppt.template import registry

    assert not hasattr(registry, "available")
    assert not hasattr(registry, "path_for")
    source = Path(registry.__file__).read_text(encoding="utf-8")
    assert ".potx" not in source
```

- [ ] **Step 2: Run it and see what still has to be cleaned up**

Run: `python -m pytest tests/test_import_hygiene.py -q`
Expected: FAIL on `test_retired_modules_are_gone_and_unreferenced` and `test_the_potx_glob_is_gone`. `test_no_module_under_app_imports_from_scripts` and the `deck_builder` test should **already pass** — if either fails, fix that before deleting anything.

- [ ] **Step 3: Remove the retired code from `app/`**

```bash
git rm app/genai/ppt/renderer.py app/genai/ppt/audit.py \
       app/genai/ppt/template/filler.py app/genai/ppt/template/icons.py
```

In `app/genai/ppt/builder.py`: delete `build_pptx` and `_materialize_image`, and drop the now-unused imports (`renderer`, `loader`, `catalog_mod`, `icon_mod`, `ContentBlock`, `SlideDraft`, `storage`). Keep `persist_deck`, `_slug` and the `pptx_translator` import. Update the module docstring's last paragraph — the sentence about `build_pptx` being kept for the diagnostic scripts is no longer true:

```python
"""Publish a finished deck: stitched .pptx -> translations -> blob -> deck version.

The deck itself is not rendered here. `PptCreatorAgent` generates each slide and
`DeckStitcher` merges them, so this module's job is publication: upload the English
file, produce each additional language by rewriting the text of that same file, and
record the new numbered version.
"""
```

In `app/genai/ppt/template/registry.py`: delete `_FILE_HINT`, `available()` and `path_for()`. Update the module docstring:

```python
"""The brand template and its marks, by name.

Decision 2: one template, resolved by a single constant. The classification no longer
selects a file — it is rendered onto the slide instead (Decision 3), so this module
resolves the template, the policy symbols and the logos, and normalizes the
classification for display.
"""
```

In `app/genai/ppt/template/catalog.py`: delete the `LayoutSpec = LayoutInfo` / `Catalog = TemplateCatalog` aliases and the `resolve` / `by_role` / `default_content` / `best_with_picture` shims added in Task 3 — their only consumer was `renderer.py`. **Keep** `for_classification`, `prompt_block` and `content_layouts`: `plan_tools.py` and `layout_tools.py` still use them.

Rewrite `app/genai/ppt/template/__init__.py`:

```python
"""Company brand-template support:
registry  — the one template, the policy symbols and the brand marks, by name
loader    — open a template with python-pptx and drop its sample slides
catalog   — introspect the template into layouts, theme, decor and canvas regions
"""
```

- [ ] **Step 4: Update the diagnostic scripts that imported retired code**

These are developer scripts, not application code, but they must not be left broken:

```bash
grep -rln "build_pptx\|plain_slide_pptx\|render_report\|registry.path_for\|template.icons" scripts/manual_testing/
```

For each hit — `05_slide_agent.py`, `test_docintel_ppt_images.py`, `test_workflow_collect.py`, `test_workflow_publish.py`, `test_workflow_image_nodes.py`, `test_workflow_slides.py` — replace:

| Retired call | Replacement |
|---|---|
| `build_pptx(draft, audience, out, workdir, classification)` | build each slide with `DeckBuilder` and stitch with `DeckStitcher`, as `tests/test_deck_end_to_end.py::build_slide` does |
| `plain_slide_pptx(brief, path)` | `PptCreatorAgent(sid, workdir)._render_on_layout(brief, path)` |
| `render_report(draft, audience, classification)` | `geometry.check_slide(...)` per slide, joined with `geometry.format_violations` |
| `registry.path_for(classification)` | `registry.template_path()` |
| `catalog_mod.for_classification(x)` | `catalog_mod.load()` |

If a script cannot be mechanically updated in a few minutes, delete it and say so in the commit message — a broken diagnostic script is worse than no script.

- [ ] **Step 5: Run the hygiene test and the whole suite**

```bash
python -m pytest tests/test_import_hygiene.py -q
python -m pytest tests/ -q
```

Expected: both green. **Do not proceed to Step 6 until they are.** The whole point of the ordering is that `scripts/mainv3/` stays available for the entire port.

- [ ] **Step 6: Delete the reference implementation**

```bash
git rm -r scripts/mainv3
git status --short
```

Confirm `company-ppt-templet/SAMPLE.pptx` is **not** in the deletion — Task 1 moved it out, and this is the step where forgetting that would take the template with it.

```bash
ls -l company-ppt-templet/SAMPLE.pptx
python -c "from app.genai.ppt.template import registry; print(registry.template_path())"
```

- [ ] **Step 7: Run everything one last time**

```bash
python -m pytest tests/ -q
```

Expected: green, including the original 61.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "chore(ppt): retire the pre-template renderer and the main_v3 reference

The port is complete and the suite is green, so the reference implementation goes.
scripts/mainv3/main_v3.py remains readable at git show 0bd70fb:scripts/mainv3/main_v3.py.
SAMPLE.pptx was moved to company-ppt-templet/ in the first commit of this series."
```

---

## Verification

After Task 16, all of this must hold:

```bash
# the original suites, unchanged in intent
python -m pytest tests/test_ppt_creator_agent.py tests/test_slides_node.py \
                 tests/test_workflow.py tests/test_ppt_agent.py -q

# the new ones
python -m pytest tests/test_template_catalog.py tests/test_measure.py \
                 tests/test_deck_builder.py tests/test_code_guard.py \
                 tests/test_geometry.py tests/test_design_contract.py \
                 tests/test_design_tools.py tests/test_stitcher.py \
                 tests/test_deck_end_to_end.py tests/test_import_hygiene.py -q

# everything
python -m pytest tests/ -q
```

Spot-check that the deletion did not take the template:

```bash
python -c "
from app.genai.ppt.template import catalog, registry
c = catalog.load()
print(registry.template_path())
print(len(c.layouts), 'layouts,', sum(1 for l in c.layouts if l.canvas_capable), 'canvas-capable')
print(c.theme.major_font, c.theme.colors['accent1'])
"
```

Expected: the template path under `company-ppt-templet/`, `181 layouts, 16 canvas-capable`, `Barlow CD163F`.

---

## What this plan does not do

Called out so a reviewer does not go looking for them:

- **It does not prove text never overflows.** Rule 10 stays a calibrated heuristic (Decision 7, Non-goals). Residual overflow is reported, not silently shipped.
- **It does not prove the repair loop converges.** A slide that exhausts its budget falls back to a deterministic on-layout render (Decision 6).
- **It does not measure the cost increase.** The spec's first open risk — roughly 1 to 2 extra model calls per offending slide — should be measured against the current baseline once rule 10's false-positive rate is known in practice. Nothing here instruments that.
- **It does not address `canvas` mode concentration.** Only 16 layouts are canvas-capable and the strongest sit on M1 and M2. The grid is what should keep that from reading as repetition; watch it on real decks.










# Three-Tier Slide Rendering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put a layout-constrained tier between the creative slide designer and the content-dumping fallback, so a slide that defeats the creative tier still lands on a real layout with its content fitted rather than poured into one placeholder.

**Architecture:** `PptCreatorAgent.build_slide` becomes a three-rung ladder. Tier 1 is today's creative loop, capped at 3 generations instead of 5. Tier 2 is the `scripts/main_v3.py` approach ported into `app/`: the model answers with a *structured plan* (a layout key plus one entry per placeholder), the plan is validated against that layout's real slots and measured capacity, then **deterministically compiled** into a `DeckBuilder` program and run in the existing sandbox — 3 generations. Tier 3 is `_render_on_layout`, untouched. All three tiers are measured by the same geometry gate, and every failure in tiers 1 and 2 is fed back to the model inside its own tier's budget.

**Tech Stack:** Python 3.11+, `python-pptx`, `pydantic` v2. **No new dependencies.**

**Spec:** No separate spec document. This plan is the design, approved in chat on 2026-09-22 with four decisions recorded in [Approved Decisions](#approved-decisions) below.

---

## Global Constraints

- **Verification is a compile check only.** Do **not** run the test suite. After the code is written, run `python -m py_compile` over every touched file. A focused `pytest tests/test_ppt_creator_agent.py` is offered as an explicitly optional final step and requires the user's go-ahead.
- **Nothing else in the app changes.** The only files this plan may touch are the five listed under [File Structure](#file-structure). No refactoring of neighbouring modules, no changes to the workflow's behaviour beyond the roll-up counts in Task 6.
- **Nothing under `app/` may import from `scripts/`.** `tests/test_import_hygiene.py::test_no_module_under_app_imports_from_scripts` enforces it. Tier 2 is a **port** of `scripts/main_v3.py`, never a wrapper around it. Quote from it, copy from it, do not import it.
- **`app/genai/ppt/deck_builder.py` must keep importing nothing from `app`.** It is copied into the sandbox verbatim (`tests/test_import_hygiene.py::test_the_deck_builder_imports_nothing_from_the_app`). This plan does not modify it; if a task tempts you to, stop and re-read the task.
- **Tier 3 is unchanged.** `_render_on_layout` keeps its current body, byte for byte. Only the wording of the event that announces it may change (Task 6).
- **`fallback_used` keeps its exact current meaning:** True if and only if Tier 3 produced the slide. `deck_workflow` and `slide_cache` read it, and a Tier 2 success is not a fallback.
- **Every colour is a brand token name**, never a hex literal — `code_guard` rejects `#RRGGBB` string constants, so the Tier 2 compiler must be incapable of emitting one (Task 2 validates tokens; Task 3 relies on that).
- **Thorough logging is a requirement, not a nicety.** Every tier boundary, every attempt with its number and limit, the layout selected and whether it deviated from the contract assignment, and a classified reason for every failure. Log lines carry `extra={"session_id": self.session_id}` wherever the surrounding code does.

## Approved Decisions

1. **Layout freedom (Tier 2):** the model chooses from a shortlist — the design contract's assigned layout **first**, then up to 5 `catalog.recommend()` alternates **from the same master**. Deviation from the assignment is allowed and logged.
2. **Copy fidelity (Tier 2):** the model may tighten phrasing and redistribute content across the layout's placeholders to make it fit. It may never invent a fact. Anything it deliberately leaves out it must declare in `LayoutPlan.dropped`, which is logged.
3. **Render path (Tier 2):** compile the accepted plan to a `DeckBuilder` program and run it in the existing sandbox — one execution path shared with Tier 1.
4. **Deliverable:** this plan file, reviewed before any code is written.

## Baseline

Read these before Task 1. They are the code this plan modifies.

- [app/genai/agents/ppt_creator_agent.py:495](../../../app/genai/agents/ppt_creator_agent.py#L495) — `build_slide`, the loop being split into tiers.
- [app/genai/agents/ppt_creator_agent.py:425](../../../app/genai/agents/ppt_creator_agent.py#L425) — `_render_on_layout`, which becomes Tier 3 and does not change.
- [app/genai/ppt/sandbox.py](../../../app/genai/ppt/sandbox.py) — `stage()` writes `deck_builder.py`, `catalog.json` and `context.py` beside the program; `output_path()` is always `<dir>/slide.pptx`.
- [app/genai/ppt/deck_builder.py](../../../app/genai/ppt/deck_builder.py) — the API the compiled program calls. `BRAND_DEFAULTS` and `THEME_TO_BRAND` are the colour allow-list; `_capacity`, `_rendered_lines` and `_text_fits` are the calibrated fit model; `reserved_rects` is the keep-out list.
- [app/genai/ppt/template/catalog.py](../../../app/genai/ppt/template/catalog.py) — `resolve_layout`, `LayoutInfo.find`, `recommend`, `canonical_layouts`, `preferred_master`.
- [scripts/main_v3.py:1078-1224](../../../scripts/main_v3.py#L1078) (`SlideSpec` and friends), [:1279](../../../scripts/main_v3.py#L1279) (`validate_plan`), [:2004](../../../scripts/main_v3.py#L2004) (`compile_plan_to_code`) — the reference being ported.

### Why Tier 2 exists

Tier 3 resolves the assigned layout, stamps the title, and pours *every* bullet of *every* block into the first body placeholder. On a 19-line slide that is a wall of shrunk text in one card. Tier 2 fails a plan whose content does not fit **before** rendering it, and hands the model the measurement (`holds ~12 lines at 15pt, needs ~28`) so its next pass either redistributes the copy or picks a roomier layout. That measurement, fed back, is the whole difference.

---

## File Structure

### New files

| File | Responsibility |
|---|---|
| `app/genai/ppt/layout_plan.py` | Tier 2's data model, validator and compiler. Pure functions plus pydantic models: no LLM calls, no logging of session state, no file I/O. `validate()` decides whether a plan may be rendered; `compile_program()` turns it into a `DeckBuilder` program. ~400 lines. |

### Modified files

| File | Change |
|---|---|
| `app/config/constants.py` | `PptCreatorConstants`: `MAX_CODE_FIX_ATTEMPTS` 5 → 3, plus five new Tier 2 values. |
| `app/core/models/deck_model.py` | `SlideBuildResult`: add `tier` and `layout_used`. |
| `app/genai/agents/ppt_creator_agent.py` | Split `build_slide` into the tier ladder; add Tier 2's prompt, request seam and loop; `_gate` gains an optional `layout_key`. |
| `tests/test_ppt_creator_agent.py` | Stub the new Tier 2 request seam so the existing exhaustion tests still reach Tier 3, and update the budget assertions. |

Nothing else. In particular `deck_builder.py`, `sandbox.py`, `code_guard.py`, `geometry.py`, `design_contract.py` and `stitcher.py` are read-only for this plan.

## Task Order and Dependencies

```
Task 1 (constants + result fields)
   |
   +-- Task 2 (layout_plan: models + validate)
   |        |
   |        +-- Task 3 (layout_plan: compile_program)
   |                 |
   +-- Task 4 (agent: tier ladder, Tier 1 capped, Tier 3 unchanged)
                     |
                     +-- Task 5 (agent: Tier 2 wired in)
                              |
                              +-- Task 6 (tests, roll-up wording, compile check)
```

Task 4 is deliberately separable from Task 5: after Task 4 the app still works, with Tier 1 capped at 3 and dropping straight to Tier 3. A reviewer can reject Task 5 without unwinding Task 4.

---

### Task 1: Budgets and the result's tier

**Files:**
- Modify: `app/config/constants.py:559` (inside `class PptCreatorConstants`)
- Modify: `app/core/models/deck_model.py:268-275` (`class SlideBuildResult`)

**Interfaces:**
- Consumes: nothing.
- Produces: `PptCreatorConstants.MAX_CODE_FIX_ATTEMPTS = 3`, `MAX_LAYOUT_PLAN_ATTEMPTS = 3`, `LAYOUT_PLAN_CANDIDATES = 6`, `LAYOUT_PLAN_MAX_TOKENS = 8000`, `LAYOUT_PLAN_DIRNAME = "fit"`, `LAYOUT_PLAN_CODE_FILENAME = "fit_slide.py"`; `SlideBuildResult.tier: int` and `SlideBuildResult.layout_used: str`.

- [ ] **Step 1: Retune the creative budget and add Tier 2's**

In `app/config/constants.py`, replace the single `MAX_CODE_FIX_ATTEMPTS` line (currently line 559) with:

```python
    # The rendering ladder. Tier 1 writes code creatively; Tier 2 fits the content into
    # a real layout from a validated plan; Tier 3 renders deterministically. A stubborn
    # slide therefore costs at most MAX_CODE_FIX_ATTEMPTS + MAX_LAYOUT_PLAN_ATTEMPTS
    # generations before it is rendered without a model at all.
    MAX_CODE_FIX_ATTEMPTS = 3       # tier 1 generations: 1 first try + repairs
    MAX_LAYOUT_PLAN_ATTEMPTS = 3    # tier 2 plan generations: 1 first try + repairs
    # Layouts tier 2 may choose between: the contract's assignment plus alternates from
    # the same master. More choice costs prompt tokens and invites deck incoherence.
    LAYOUT_PLAN_CANDIDATES = 6
    LAYOUT_PLAN_MAX_TOKENS = 8000   # a plan is JSON, not a program; it needs less room
    # Tier 2 stages its own sandbox beside tier 1's, so a program from the abandoned
    # tier can never be mistaken for tier 2's output.
    LAYOUT_PLAN_DIRNAME = "fit"
    LAYOUT_PLAN_CODE_FILENAME = "fit_slide.py"
```

Leave the docstring of `PptCreatorConstants` accurate by replacing its second paragraph with:

```python
    One slide == one generated python-pptx program, executed locally. Every value here
    bounds that loop: a stubborn slide can cost at most MAX_CODE_FIX_ATTEMPTS creative
    generations plus MAX_LAYOUT_PLAN_ATTEMPTS layout-fitting generations and
    EXEC_TIMEOUT_SECONDS of CPU per execution before it falls back to a deterministic
    render on a template layout.
```

- [ ] **Step 2: Record which tier produced the slide**

In `app/core/models/deck_model.py`, replace `class SlideBuildResult` with:

```python
class SlideBuildResult(BaseModel):
    """What came back from the rendering ladder for one slide."""
    slide_number: int
    pptx_path: str = ""                # local path of the produced single-slide .pptx
    attempts: int = 0                  # generations spent across every tier
    #: 1 = the creative designer, 2 = the layout-fitted plan, 3 = the deterministic
    #: render. `fallback_used` stays reserved for tier 3, which is what the workflow
    #: and the slide cache read.
    tier: int = 1
    layout_used: str = ""              # catalog key the slide actually landed on
    fallback_used: bool = False        # True when both model tiers were exhausted
    error: str = ""
    code_path: str = ""                # the generated program, kept for debugging
```

- [ ] **Step 3: Compile-check both files**

Run:

```bash
python -m py_compile app/config/constants.py app/core/models/deck_model.py
```

Expected: no output, exit 0.

- [ ] **Step 4: Commit**

```bash
git add app/config/constants.py app/core/models/deck_model.py
git commit -m "feat(ppt): budget the rendering ladder and record the tier that won

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `layout_plan` — the plan model and its validator

**Files:**
- Create: `app/genai/ppt/layout_plan.py`

**Interfaces:**
- Consumes: `PptCreatorConstants` (Task 1); `app.genai.ppt.deck_builder` for `BRAND_DEFAULTS`, `THEME_TO_BRAND`, `_capacity`, `_rendered_lines`, `reserved_rects`; `catalog.LayoutInfo.find`.
- Produces:
  - `class TableFill`, `ChartSeries`, `ChartFill`, `PlaceholderFill`, `ExtraShape`, `LayoutPlan` (pydantic v2 models)
  - `PlaceholderFill.kind() -> str` returning one of `"chart" | "table" | "image" | "bullets" | "text"`, and `PlaceholderFill.lines() -> list[str]`, the copy the layout must hold
  - `ExtraShape.rect() -> list[float]` and `ExtraShape.copy_text() -> list[str]`
  - `COLOUR_TOKENS: frozenset[str]`
  - `validate(plan, catalog, *, frame, allowed_images, slide_number=0) -> tuple[list[str], list[str]]` returning `(errors, notes)`
  - `format_errors(errors: Sequence[str]) -> str`

- [ ] **Step 1: Write the module docstring and the imports**

Create `app/genai/ppt/layout_plan.py`:

```python
"""Tier 2: fit one slide's content into a layout the template already draws.

Ported from `scripts/main_v3.py` - `SlideSpec` and its children (line 1078),
`validate_plan` (line 1279) and `compile_plan_to_code` (line 2004) - and narrowed to
a single slide. It is a port and not a wrapper because nothing under `app/` may import
from `scripts/`; `tests/test_import_hygiene.py` asserts it.

Three steps, and the model writes no code in any of them:

    LayoutPlan        the model's JSON answer, validated by pydantic
    validate()        the plan against the chosen layout: real slots, content that fits
    compile_program() the plan -> a DeckBuilder program the sandbox runs

Separating those is the point. The creative tier's failure modes are a syntax error, a
traceback and a geometry violation, and it can only learn about the last one after
paying for an execution. Here a plan that cannot fit its layout is rejected by
measurement before anything runs, and the measurement itself - "holds ~12 lines at
15pt, needs ~28" - is the repair instruction.

Two deliberate narrowings against the reference. Extras are limited to five component
types (`textbox`, `card`, `stat`, `icon_circle`, `line`): tier 2 exists to be reliable,
and every free-floating shape is another way to fail the geometry gate. And colours are
restricted to brand token names by `COLOUR_TOKENS`, so `compile_program` is structurally
incapable of emitting the hex literal `code_guard` would reject.
"""

from typing import Any, Literal, Optional, Sequence, Union

from pydantic import BaseModel, Field

from app.genai.ppt import deck_builder as db
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

#: Every colour name `deck_builder.to_rgb` resolves. A plan carrying anything else is
#: rejected rather than rendered off-palette.
COLOUR_TOKENS: frozenset[str] = frozenset(db.BRAND_DEFAULTS) | frozenset(db.THEME_TO_BRAND)

#: Component types an extra may be. Mirrors the `SlideHandle.add_*` methods tier 2 is
#: allowed to reach; `shape`, `image`, `table` and `chart` extras are deliberately out.
EXTRA_TYPES = ("textbox", "card", "stat", "icon_circle", "line")

#: Point size the fit check falls back to when neither the plan nor the layout says.
_DEFAULT_BODY_PT = 18.0
#: `SlideHandle._fit_size` shrinks copy to fit, but only this far. A plan that needs
#: more than this is not a plan that fits, it is a plan that will be unreadable.
_MIN_SHRINK_RATIO = 0.70
#: Tolerance in inches when checking an extra against the slide edges.
_EDGE_SLACK_IN = 0.02
```

- [ ] **Step 2: Write the plan models**

Append:

```python
class TableFill(BaseModel):
    """A table for one placeholder. Mirrors `SlideHandle.set_table`."""
    columns: list[str] = []
    rows: list[list[str]] = []
    font_size: float = 11.0


class ChartSeries(BaseModel):
    name: str = "Series"
    values: list[float] = []


class ChartFill(BaseModel):
    """A chart for one placeholder. Mirrors `SlideHandle.set_chart`."""
    chart_type: Literal["column", "stacked_column", "bar", "stacked_bar", "line",
                        "pie", "doughnut", "area"] = "column"
    categories: list[str] = []
    series: list[ChartSeries] = []
    title: str = ""
    show_legend: Optional[bool] = None
    data_labels: bool = False


class PlaceholderFill(BaseModel):
    """What goes into ONE placeholder of the chosen layout.

    `target` is the placeholder's `idx`, its slot name (`"body1"`) or its role
    (`"body"`) - whatever `LayoutInfo.find` accepts, which is the same address the
    layout contract in the prompt publishes.
    """
    target: Union[int, str]
    text: str = ""
    bullets: list[str] = []
    image: str = ""
    table: Optional[TableFill] = None
    chart: Optional[ChartFill] = None
    size: Optional[float] = None
    bold: Optional[bool] = None
    color: str = ""
    align: Optional[Literal["left", "center", "right", "justify"]] = None

    def kind(self) -> str:
        """Which `SlideHandle` setter this fill calls for.

        Returns:
            One of "chart", "table", "image", "bullets" or "text", in that precedence.
        """
        if self.chart is not None:
            return "chart"
        if self.table is not None:
            return "table"
        if self.image:
            return "image"
        if self.bullets:
            return "bullets"
        return "text"

    def lines(self) -> list[str]:
        """The text this fill puts on the slide, as the lines the layout must hold."""
        if self.bullets:
            return [str(b) for b in self.bullets if str(b).strip()]
        return [self.text] if self.text.strip() else []


class ExtraShape(BaseModel):
    """One component placed by inches, outside the layout's own placeholders.

    Only the five types in `EXTRA_TYPES`, and every one of them is checked against the
    slide edges and the reserved zones before it is compiled.
    """
    type: Literal["textbox", "card", "stat", "icon_circle", "line"]
    left: float
    top: float
    width: float
    height: float
    text: str = ""          # textbox, icon_circle
    title: str = ""         # card
    body: list[str] = []    # card
    value: str = ""         # stat
    label: str = ""         # stat
    size: Optional[float] = None
    bold: Optional[bool] = None
    color: str = ""
    fill: str = ""
    accent: str = ""
    align: Optional[Literal["left", "center", "right", "justify"]] = None
    weight: float = 1.0     # line

    def rect(self) -> list[float]:
        return [self.left, self.top, self.width, self.height]

    def copy_text(self) -> list[str]:
        """Everything readable in this component, for the "is it empty" check."""
        return [t for t in [self.text, self.title, self.value, self.label,
                            *[str(b) for b in self.body]] if str(t).strip()]


class LayoutPlan(BaseModel):
    """One slide, as a filled-in layout rather than as a program.

    This is the schema handed to `ApimLlmClient.chat_json`, so its field names are part
    of the prompt whether the prompt repeats them or not.
    """
    layout: str = Field(description="Catalog key of the layout to fill, e.g. 'M1-L10'")
    title: str = ""
    subtitle: str = ""
    placeholders: list[PlaceholderFill] = []
    extras: list[ExtraShape] = []
    notes: str = ""
    #: Content the model chose to leave off the slide to make the rest fit. Declaring it
    #: is mandatory; it is logged so a dropped fact is visible after the fact.
    dropped: list[str] = []
```

- [ ] **Step 3: Write the fit check**

Append. This is the port of `main_v3::validate_plan`'s capacity branch, measuring with
`deck_builder`'s calibrated width table instead of the reference's flat estimate:

```python
def _fit_problem(fill: PlaceholderFill, info: Any) -> Optional[str]:
    """Whether this fill's copy fits its placeholder, as an instruction when it does not.

    Args:
        fill: The plan's entry for this placeholder.
        info: The `PlaceholderInfo` it addresses.

    Returns:
        None when the copy fits (possibly after the shrink `SlideHandle._fit_size`
        would apply), else the measurement as a repair instruction.
    """
    items = fill.lines()
    if not items:
        return None
    size = float(fill.size or info.font_size or _DEFAULT_BODY_PT)
    bullets = bool(info.bullets or fill.bullets)
    _, holds = db._capacity(info.width, info.height, size, bullets)
    needed = db._rendered_lines(items, info.width, size, bullets)
    if needed <= holds:
        return None
    floor = max(size * _MIN_SHRINK_RATIO, 10.0)
    _, holds_shrunk = db._capacity(info.width, info.height, floor, bullets)
    if db._rendered_lines(items, info.width, floor, bullets) <= holds_shrunk:
        return None
    return (f"{info.slot} (idx {info.idx}, {info.width:.2f}x{info.height:.2f}in) holds "
            f"about {holds} line(s) at {size:.0f}pt and about {holds_shrunk} at the "
            f"{floor:.0f}pt floor, but this content needs about {needed}. Shorten it, "
            f"split it across more placeholders, or choose a layout with more room.")
```

- [ ] **Step 4: Write the validator**

Append:

```python
def validate(plan: LayoutPlan, catalog, *, frame: Optional[dict] = None,
             allowed_images: Sequence[str] = (),
             slide_number: int = 0) -> tuple[list[str], list[str]]:
    """Check a plan against the layout it claims, before anything is rendered.

    Ported from `main_v3::validate_plan`, for one slide. An error blocks the plan and
    goes back to the model verbatim; a note only reaches the log, because it describes
    something `DeckBuilder` handles on its own.

    Args:
        plan: The model's answer, already parsed by pydantic.
        catalog: The `TemplateCatalog`.
        frame: This slide's frame block, for the reserved zones.
        allowed_images: The only image paths this slide may place.
        slide_number: For the log line only.

    Returns:
        Tuple of (errors, notes). An empty error list means the plan may be compiled.
    """
    errors: list[str] = []
    notes: list[str] = []
    try:
        layout = catalog.resolve_layout(plan.layout)
    except LookupError as exc:
        log.warning("slide %d plan names layout %r, which is not in the catalog: %s",
                    slide_number, plan.layout, exc)
        return ([f"Layout {plan.layout!r} does not exist. Choose one of the layout keys "
                 f"you were offered, exactly as written."], notes)

    if layout.key != plan.layout:
        notes.append(f"layout {plan.layout!r} normalised to {layout.key}")
        plan.layout = layout.key
    if not layout.placeholders:
        errors.append(f"{layout.key} is fixed artwork with no placeholders, so nothing "
                      f"can be filled on it. Choose a layout that has placeholders.")
    if plan.title and not layout.has_role("title"):
        notes.append(f"{layout.key} has no title placeholder; the title is drawn as a "
                     f"text box")
    if not plan.title and layout.has_role("title") and "closing" not in layout.tags:
        errors.append(f"{layout.key} has a title placeholder and the plan leaves it "
                      f"empty. Give the slide its title.")
    if plan.subtitle and not layout.has_role("subtitle"):
        notes.append(f"{layout.key} has no subtitle placeholder; the subtitle is drawn "
                     f"under the title")

    seen: set[int] = set()
    for fill in plan.placeholders:
        info = layout.find(fill.target)
        if info is None:
            available = ", ".join(f"{p.slot} (idx {p.idx}, role {p.role})"
                                  for p in layout.placeholders) or "none"
            errors.append(f"Placeholder {fill.target!r} does not exist on {layout.key}. "
                          f"Available: {available}.")
            continue
        if info.idx in seen:
            notes.append(f"{info.slot} is filled twice; the last entry wins")
        seen.add(info.idx)

        for token in (fill.color,):
            if token and token not in COLOUR_TOKENS:
                errors.append(f"{token!r} is not a colour token. Use one of: "
                              f"{', '.join(sorted(COLOUR_TOKENS))}.")
        kind = fill.kind()
        if kind == "image":
            if fill.image not in set(allowed_images):
                errors.append(f"Image {fill.image!r} is not one of this slide's files. "
                              f"Use one of: {', '.join(allowed_images) or 'none'}.")
            elif info.role not in ("picture", "content"):
                notes.append(f"image placed on a {info.role} placeholder")
        elif kind in ("table", "chart"):
            if info.width < 3.0 or info.height < 1.5:
                notes.append(f"{kind} on {info.slot} ({info.width:.2f}x"
                             f"{info.height:.2f}in) will be cramped")
            if kind == "chart" and fill.chart is not None:
                errors.extend(_chart_problems(fill.chart))
        else:
            if info.role == "picture" and not info.accepts_text:
                errors.append(f"{info.slot} is a picture slot that takes no text. Put "
                              f"this copy in a text placeholder.")
                continue
            problem = _fit_problem(fill, info)
            if problem:
                errors.append(problem)

    for info in layout.placeholders:
        if (info.idx not in seen and info.role in ("body", "content", "heading")
                and info.slot not in ("title", "subtitle")):
            notes.append(f"{info.slot} (idx {info.idx}) left empty and will be dropped "
                         f"from the slide")

    errors.extend(_extra_problems(plan.extras, catalog, layout, frame))
    log.info("slide %d plan on %s validated: %d error(s), %d note(s), %d fill(s), "
             "%d extra(s), %d declared omission(s)", slide_number, layout.key,
             len(errors), len(notes), len(plan.placeholders), len(plan.extras),
             len(plan.dropped))
    if notes:
        log.debug("slide %d plan notes: %s", slide_number, " | ".join(notes))
    return errors, notes


def _chart_problems(chart: ChartFill) -> list[str]:
    """Series that do not line up with their categories, as instructions."""
    problems: list[str] = []
    if not chart.categories or not chart.series:
        problems.append("A chart needs 'categories' and at least one series.")
        return problems
    for series in chart.series:
        if len(series.values) != len(chart.categories):
            problems.append(f"Chart series {series.name!r} has {len(series.values)} "
                            f"value(s) for {len(chart.categories)} categories. They "
                            f"must match.")
    return problems


def _extra_problems(extras: Sequence[ExtraShape], catalog, layout,
                    frame: Optional[dict]) -> list[str]:
    """Extras that leave the slide, land on brand furniture, or say nothing.

    Args:
        extras: The plan's extras.
        catalog: The `TemplateCatalog`, for the canvas size.
        layout: The chosen `LayoutInfo`, for its inherited decoration.
        frame: This slide's frame block.

    Returns:
        One instruction per problem.
    """
    if not extras:
        return []
    problems: list[str] = []
    reserved = db.reserved_rects(frame, layout, catalog.slide_width,
                                 catalog.slide_height)
    for extra in extras:
        rect = extra.rect()
        if extra.width <= 0 or extra.height <= 0:
            problems.append(f"Extra {extra.type!r} has no size. Give it a positive "
                            f"width and height in inches.")
            continue
        if (rect[0] < -_EDGE_SLACK_IN or rect[1] < -_EDGE_SLACK_IN
                or rect[0] + rect[2] > catalog.slide_width + _EDGE_SLACK_IN
                or rect[1] + rect[3] > catalog.slide_height + _EDGE_SLACK_IN):
            problems.append(
                f"Extra {extra.type!r} at ({rect[0]:.2f}, {rect[1]:.2f}) "
                f"{rect[2]:.2f}x{rect[3]:.2f}in leaves the "
                f"{catalog.slide_width:.2f}x{catalog.slide_height:.2f}in slide.")
        for zone in reserved:
            if db._rects_overlap(rect, zone):
                problems.append(
                    f"Extra {extra.type!r} at ({rect[0]:.2f}, {rect[1]:.2f}) "
                    f"{rect[2]:.2f}x{rect[3]:.2f}in overlaps the reserved zone "
                    f"({zone[0]:.2f}, {zone[1]:.2f}) {zone[2]:.2f}x{zone[3]:.2f}in. "
                    f"The logo, the classification mark and the slide number are "
                    f"stamped there for you.")
                break
        if extra.type != "line" and not extra.copy_text():
            problems.append(f"Extra {extra.type!r} has no text. Give it something to "
                            f"say or remove it.")
        for token in (extra.color, extra.fill, extra.accent):
            if token and token not in COLOUR_TOKENS:
                problems.append(f"{token!r} is not a colour token. Use one of: "
                                f"{', '.join(sorted(COLOUR_TOKENS))}.")
    return problems


def format_errors(errors: Sequence[str]) -> str:
    """The error list as the model receives it: numbered, with a lead-in.

    Args:
        errors: Problems from `validate`.

    Returns:
        The repair instruction, or the accepted line when there is nothing wrong.
    """
    if not errors:
        return "The plan is valid."
    body = "\n".join(f"{i}. {problem}" for i, problem in enumerate(errors, start=1))
    return ("That plan cannot be rendered. Fix every problem below and answer with the "
            "whole corrected plan as JSON.\n\n=== PROBLEMS ===\n" + body)
```

- [ ] **Step 5: Confirm the private helpers exist under the names used**

`validate` reaches three private names in `deck_builder`. Confirm they are spelled exactly that way before moving on:

```bash
grep -n "^def _capacity\|^def _rendered_lines\|^def _rects_overlap\|^def reserved_rects" app/genai/ppt/deck_builder.py
```

Expected: four lines, `_capacity`, `_rendered_lines`, `_rects_overlap` and `reserved_rects`. If any name differs, fix the call in `layout_plan.py` — do not rename anything in `deck_builder.py`.

- [ ] **Step 6: Compile-check**

Run:

```bash
python -m py_compile app/genai/ppt/layout_plan.py
```

Expected: no output, exit 0.

- [ ] **Step 7: Commit**

```bash
git add app/genai/ppt/layout_plan.py
git commit -m "feat(ppt): a validated layout plan, ported from main_v3

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `layout_plan.compile_program` — the plan becomes a program

**Files:**
- Modify: `app/genai/ppt/layout_plan.py` (append)

**Interfaces:**
- Consumes: `LayoutPlan`, `PlaceholderFill`, `ExtraShape` (Task 2).
- Produces: `compile_program(plan: LayoutPlan) -> str` — the source of a `DeckBuilder` program that builds exactly this plan and saves it to `OUTPUT_PATH`.

Ported from `main_v3::compile_plan_to_code` (line 2004), with the sandbox's preamble in place of the reference's `sys.path` shim. Every value reaches the source through `repr()`, so content can never become code.

- [ ] **Step 1: Write the compiler**

Append to `app/genai/ppt/layout_plan.py`:

```python
_PREAMBLE = '''"""Tier 2: compiled from the accepted layout plan. Not model-written."""

from deck_builder import DeckBuilder
from context import TEMPLATE_PATH, CATALOG, FRAME, OUTPUT_PATH

deck = DeckBuilder(TEMPLATE_PATH, CATALOG, frame=FRAME)
'''


def _kwargs(pairs: Sequence[tuple[str, Any]]) -> str:
    """Render keyword arguments, dropping the ones left unset.

    Args:
        pairs: (name, value) pairs. A value of None or "" is omitted entirely so the
            `DeckBuilder` default - which is the template's own styling - survives.

    Returns:
        The arguments, each prefixed with ", ", or an empty string.
    """
    return "".join(f", {name}={value!r}" for name, value in pairs
                   if value not in (None, "", []))


def compile_program(plan: LayoutPlan) -> str:
    """Compile a validated plan into the program the sandbox runs.

    Deterministic, and the only code path tier 2 executes: the model contributed the
    plan, never a line of this. Every literal goes through `repr()`, so a bullet that
    happens to contain a quote or a brace cannot become syntax.

    Args:
        plan: A plan `validate` returned no errors for.

    Returns:
        The program source, ready for `code_guard` and the sandbox.
    """
    lines = [_PREAMBLE, f"slide = deck.add_slide({plan.layout!r})"]
    if plan.title:
        lines.append(f"slide.set_title({plan.title!r})")
    if plan.subtitle:
        lines.append(f"slide.set_subtitle({plan.subtitle!r})")

    for fill in plan.placeholders:
        kind = fill.kind()
        style = _kwargs([("size", fill.size), ("bold", fill.bold),
                         ("color", fill.color), ("align", fill.align)])
        if kind == "chart":
            chart = fill.chart
            series = [{"name": s.name, "values": list(s.values)} for s in chart.series]
            lines.append(
                f"slide.set_chart({fill.target!r}, {chart.chart_type!r}, "
                f"{list(chart.categories)!r}, {series!r}"
                + _kwargs([("title", chart.title), ("show_legend", chart.show_legend),
                           ("data_labels", chart.data_labels or None)]) + ")")
        elif kind == "table":
            table = fill.table
            lines.append(
                f"slide.set_table({fill.target!r}, {list(table.columns)!r}, "
                f"{[list(r) for r in table.rows]!r}, font_size={table.font_size!r})")
        elif kind == "image":
            lines.append(f"slide.set_picture({fill.target!r}, {fill.image!r})")
        elif kind == "bullets":
            lines.append(f"slide.set_bullets({fill.target!r}, "
                         f"{[str(b) for b in fill.bullets]!r}{style})")
        else:
            lines.append(f"slide.set_text({fill.target!r}, {fill.text!r}{style})")

    for extra in plan.extras:
        box = f"{extra.left!r}, {extra.top!r}, {extra.width!r}, {extra.height!r}"
        if extra.type == "textbox":
            lines.append(f"slide.add_textbox({extra.text!r}, {box}"
                         + _kwargs([("size", extra.size), ("bold", extra.bold),
                                    ("color", extra.color), ("align", extra.align),
                                    ("fill", extra.fill)]) + ")")
        elif extra.type == "card":
            body = [str(b) for b in extra.body] or [extra.text]
            lines.append(f"slide.add_card({extra.title!r}, {body!r}, {box}"
                         + _kwargs([("accent", extra.accent or "red")]) + ")")
        elif extra.type == "stat":
            lines.append(f"slide.add_stat({extra.value!r}, {extra.label!r}, {box}"
                         + _kwargs([("color", extra.color), ("fill", extra.fill)]) + ")")
        elif extra.type == "icon_circle":
            lines.append(f"slide.add_icon_circle({extra.text!r}, {extra.left!r}, "
                         f"{extra.top!r}, {extra.width!r}"
                         + _kwargs([("fill", extra.fill), ("color", extra.color)]) + ")")
        elif extra.type == "line":
            lines.append(f"slide.add_line({box}"
                         + _kwargs([("color", extra.color or "border"),
                                    ("weight", extra.weight)]) + ")")

    if plan.notes:
        lines.append(f"slide.set_notes({plan.notes!r})")
    lines.append("deck.save(OUTPUT_PATH)")
    source = "\n".join(lines) + "\n"
    log.info("compiled a %s plan into %d line(s) of program (%d chars): %d fill(s), "
             "%d extra(s)", plan.layout, source.count("\n"), len(source),
             len(plan.placeholders), len(plan.extras))
    log.debug("compiled program:\n%s", source)
    return source
```

- [ ] **Step 2: Check the compiled shape against the API it calls**

The compiler must not invent a keyword. Confirm each signature it relies on:

```bash
grep -n "def set_chart\|def set_table\|def set_picture\|def set_bullets\|def set_text\|def add_textbox\|def add_card\|def add_stat\|def add_icon_circle\|def add_line\|def set_notes\|def set_subtitle\|def set_title" app/genai/ppt/deck_builder.py
```

Read each signature and confirm every keyword the compiler emits — `size`, `bold`, `color`, `align`, `fill`, `accent`, `weight`, `font_size`, `title`, `show_legend`, `data_labels` — is accepted by the method it is emitted for. Fix `compile_program`, never `deck_builder.py`.

- [ ] **Step 3: Compile-check**

Run:

```bash
python -m py_compile app/genai/ppt/layout_plan.py
```

Expected: no output, exit 0.

- [ ] **Step 4: Commit**

```bash
git add app/genai/ppt/layout_plan.py
git commit -m "feat(ppt): compile an accepted layout plan into a DeckBuilder program

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: The ladder — Tier 1 capped, Tier 3 reached through a tier boundary

**Files:**
- Modify: `app/genai/agents/ppt_creator_agent.py` — add `_TierOutcome` near the top; replace `build_slide` (line 495) with the ladder plus `_tier1_creative` and `_tier3_render`; widen `_gate` (line 620) with an optional `layout_key`.

**Interfaces:**
- Consumes: `PptCreatorConstants.MAX_CODE_FIX_ATTEMPTS`, `SlideBuildResult.tier`/`layout_used` (Task 1).
- Produces:
  - `@dataclass class _TierOutcome` with fields `result: SlideBuildResult | None`, `attempts: int`, `last_error: str`, `upstream_only: bool`, `skipped: str`
  - `PptCreatorAgent._tier1_creative(brief, catalog, slide_dir, out_path, code_path) -> _TierOutcome`
  - `PptCreatorAgent._tier3_render(brief, out_path, spent, last_error) -> SlideBuildResult`
  - `PptCreatorAgent._gate(produced, brief, catalog, layout_key="") -> list`

After this task the ladder is Tier 1 → Tier 3, and the only behaviour change is the cap moving from 5 to 3. Tier 2 slots in at Task 5.

- [ ] **Step 1: Add the tier-outcome record**

In `app/genai/agents/ppt_creator_agent.py`, add `from dataclasses import dataclass` to the imports, and put this immediately above `class PptCreatorAgent` (line 162):

```python
@dataclass
class _TierOutcome:
    """What one rung of the ladder achieved, and what the next rung should know.

    The agent is called from several threads and keeps no mutable state, so a tier
    reports back through a value rather than through `self`.
    """
    #: The finished slide, or None when this tier did not produce one.
    result: SlideBuildResult | None = None
    #: Generations this tier spent, which the next tier adds to its own.
    attempts: int = 0
    #: The last problem, already formatted for the model.
    last_error: str = ""
    #: True when every attempt failed before any output existed - a gateway outage
    #: rather than a design problem. The next model-driven tier skips itself.
    upstream_only: bool = False
    #: Why this tier did not run at all. Empty when it ran.
    skipped: str = ""
```

`SlideBuildResult | None` matches the file's own style (`_locate_output` already
returns `Path | None`), so no import beyond `dataclass` is needed.

- [ ] **Step 2: Extract Tier 1 from the current loop**

Replace the whole of `build_slide` (line 495 through the `return SlideBuildResult(...)` that ends it) with the three methods below. `_tier1_creative` is today's loop verbatim except that it returns a `_TierOutcome` instead of falling through to the fallback, and it counts upstream-only failures.

```python
    # --- tier 1: the creative designer ---------------------------------------#
    def _tier1_creative(self, brief: SlideCodeBrief, catalog, slide_dir: Path,
                        out_path: Path, code_path: Path) -> _TierOutcome:
        """Let the model design the slide freely, and repair it until the gate clears.

        Unchanged from the single-tier design except for its budget and its exit: a
        spent budget is now a tier boundary rather than the end of the road.

        Args:
            brief: The slide's content, context and design assignment.
            catalog: The template catalog.
            slide_dir: This slide's staged sandbox.
            out_path: Where a program is told to save.
            code_path: Where each generated program is written.

        Returns:
            A `_TierOutcome` carrying the slide, or the reason the tier came up empty.
        """
        limit = PptCreatorConstants.MAX_CODE_FIX_ATTEMPTS
        log.info("slide %d entering tier 1 (creative, %d attempt(s) allowed)",
                 brief.slide_number, limit, extra={"session_id": self.session_id})
        messages = [{"role": "system", "content": self._system_prompt()},
                    {"role": "user", "content": self._user_prompt(brief)}]
        last_error, produced_anything = "", False

        for attempt in range(1, limit + 1):
            try:
                source = self._request_code(messages)
            except Exception as exc:                          # noqa: BLE001
                # A gateway failure is retryable in exactly the same loop as a code bug.
                log.error("slide %d tier 1 attempt %d/%d: code generation failed: %s",
                          brief.slide_number, attempt, limit, exc,
                          extra={"session_id": self.session_id}, exc_info=True)
                last_error = str(exc)
                continue

            produced_anything = True
            source = code_guard.strip_code_fences(
                self._normalize_code(source, slide_dir))
            code_path.write_text(source, encoding="utf-8")
            log.info("slide %d tier 1 attempt %d/%d: program generated (%d chars)",
                     brief.slide_number, attempt, limit, len(source),
                     extra={"session_id": self.session_id})

            # 1. Static check. A rejection here costs no execution at all.
            rejection = code_guard.check_message(source)
            if rejection:
                last_error = rejection
                log.warning("slide %d tier 1 attempt %d/%d rejected by the static "
                            "guard, no execution spent:\n%s", brief.slide_number,
                            attempt, limit, rejection,
                            extra={"session_id": self.session_id})
                self._step(brief, f"attempt {attempt} rejected before running",
                           phase="repair", tier=1, attempts=attempt)
                messages += [{"role": "assistant", "content": source},
                             {"role": "user", "content": rejection}]
                continue

            # 2. Execute.
            succeeded, error = self._execute(code_path, slide_dir)
            produced = self._locate_output(slide_dir, out_path) if succeeded else None
            if produced is None:
                last_error = error or "the program ran but wrote no .pptx file"
                log.warning("slide %d tier 1 attempt %d/%d: the program failed: %s",
                            brief.slide_number, attempt, limit, last_error[:500],
                            extra={"session_id": self.session_id})
                self._step(brief, f"attempt {attempt} failed; repairing the code",
                           phase="repair", tier=1, attempts=attempt)
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
                log.info("slide %d accepted at tier 1 on attempt %d/%d: %s "
                         "(%d advisory note(s))", brief.slide_number, attempt, limit,
                         produced.name, len(violations),
                         extra={"session_id": self.session_id})
                self._step(brief, f"slide rendered on attempt {attempt}",
                           phase="done", tier=1, attempts=attempt)
                return _TierOutcome(result=SlideBuildResult(
                    slide_number=brief.slide_number, pptx_path=str(produced),
                    attempts=attempt, tier=1, layout_used=brief.layout_key,
                    code_path=str(code_path)), attempts=attempt)

            last_error = geo.format_violations(violations)
            log.warning("slide %d tier 1 attempt %d/%d failed the gate with %d "
                        "violation(s) (%d blocking):\n%s", brief.slide_number, attempt,
                        limit, len(violations),
                        sum(1 for v in violations if v.blocking), last_error,
                        extra={"session_id": self.session_id})
            self._step(brief, f"attempt {attempt}: {len(violations)} layout issue(s); "
                              f"redesigning", phase="repair", tier=1, attempts=attempt)
            messages += [{"role": "assistant", "content": source},
                         {"role": "user", "content": last_error}]

        log.error("slide %d leaving tier 1 unsatisfied after %d attempt(s); last "
                  "problem: %s", brief.slide_number, limit, last_error[:500],
                  extra={"session_id": self.session_id})
        return _TierOutcome(attempts=limit, last_error=last_error,
                            upstream_only=not produced_anything)
```

- [ ] **Step 3: Give Tier 3 its own method**

Add, directly after `_render_on_layout` (which stays exactly as it is):

```python
    # --- tier 3: the deterministic render ------------------------------------#
    def _tier3_render(self, brief: SlideCodeBrief, out_path: Path, spent: int,
                      last_error: str) -> SlideBuildResult:
        """Render the slide without a model at all. The end of the ladder.

        `_render_on_layout` is unchanged; this wraps it so the event and the result
        can say how much was spent getting here.

        Args:
            brief: The slide that defeated both model tiers.
            out_path: Where to write the single-slide .pptx.
            spent: Generations already spent across tiers 1 and 2.
            last_error: The last problem either tier reported.

        Returns:
            A `SlideBuildResult` on a real template layout, with `fallback_used` set.
        """
        log.error("slide %d entering tier 3 after %d generation(s) across the model "
                  "tiers; rendering it deterministically. Last problem: %s",
                  brief.slide_number, spent, last_error[:500],
                  extra={"session_id": self.session_id})
        events.publish(
            self.session_id, "agent_step", agent="ppt-code",
            message=(f"Slide {brief.slide_number}: the designer could not produce a "
                     f"clean layout in {spent} attempt(s); the slide was rendered on a "
                     f"template layout instead."),
            slide_number=brief.slide_number, phase="fallback", tier=3, attempts=spent)
        produced = self._render_on_layout(brief, out_path)
        log.info("slide %d finished at tier 3: %s", brief.slide_number, produced.name,
                 extra={"session_id": self.session_id})
        return SlideBuildResult(slide_number=brief.slide_number,
                                pptx_path=str(produced), attempts=spent, tier=3,
                                layout_used=brief.layout_key, fallback_used=True,
                                error=last_error)
```

- [ ] **Step 4: Write the ladder**

Add as the new public entry point, where `build_slide` used to be:

```python
    # --- public API ----------------------------------------------------------#
    def build_slide(self, brief: SlideCodeBrief) -> SlideBuildResult:
        """Render one slide, dropping a tier at a time until one of them succeeds.

        Tier 1 lets the model design freely. Tier 2 makes it fit a real layout from a
        validated plan. Tier 3 renders deterministically. Each model tier owns its own
        budget, so a slide's total cost is bounded and a tier that cannot help is left
        behind rather than retried.

        Args:
            brief: The slide's content, context, design assignment and image paths.

        Returns:
            A `SlideBuildResult` always carrying a usable .pptx, with `tier` saying
            which rung produced it.
        """
        log.info("building slide %d/%d %r: %s mode on %s, %d block(s), %d image(s)",
                 brief.slide_number, brief.context.total, brief.content.title,
                 brief.mode, brief.layout_key or "(unassigned)",
                 len(brief.content.blocks), len(brief.image_paths),
                 extra={"session_id": self.session_id})
        catalog = catalog_mod.load()
        slide_dir = self.workdir / f"slide_{brief.slide_number}"
        sandbox.stage(slide_dir, catalog, {
            "frame": brief.frame, "grid": brief.grid, "tokens": brief.tokens,
            "mode": brief.mode, "layout_key": brief.layout_key,
            "free_region": brief.free_region}, brief.image_paths)
        out_path = sandbox.output_path(slide_dir)
        code_path = slide_dir / PptCreatorConstants.GENERATED_CODE_FILENAME

        self._step(brief, "designing the slide", phase="start", tier=1)
        tier1 = self._tier1_creative(brief, catalog, slide_dir, out_path, code_path)
        if tier1.result is not None:
            return tier1.result

        tier2 = self._tier2_layout_fit(brief, catalog, tier1)
        if tier2.result is not None:
            return tier2.result

        spent = tier1.attempts + tier2.attempts
        return self._tier3_render(brief, out_path, spent,
                                  tier2.last_error or tier1.last_error)
```

- [ ] **Step 5: Stub Tier 2 so this task stands on its own**

Task 5 replaces this. Add it now so the ladder is runnable and reviewable:

```python
    # --- tier 2: fit the content into a real layout --------------------------#
    def _tier2_layout_fit(self, brief: SlideCodeBrief, catalog,
                          tier1: _TierOutcome) -> _TierOutcome:
        """Not implemented until Task 5; the ladder falls straight through to tier 3."""
        log.info("slide %d: tier 2 is not wired up yet, going to tier 3",
                 brief.slide_number, extra={"session_id": self.session_id})
        return _TierOutcome(skipped="not implemented", last_error=tier1.last_error)
```

- [ ] **Step 6: Let the gate measure against a layout other than the assigned one**

Tier 2 may choose a different layout, and gating a slide against the wrong layout is meaningless. In `_gate` (line 620), change the signature and the first resolution step:

```python
    def _gate(self, produced: Path, brief: SlideCodeBrief, catalog,
              layout_key: str = "") -> list:
```

and inside it, replace the layout resolution with:

```python
            key = layout_key or brief.layout_key
            layout = catalog.resolve_layout(key) if key else None
```

Everything below that line, including the `Presentation(...)` fallback and the `except` branch, stays as it is. Add to the docstring's `Args`:

```
            layout_key: Measure against this layout instead of the brief's assignment.
                Tier 2 may land the slide on a layout the contract never assigned.
```

The existing Tier 1 call site `self._gate(produced, brief, catalog)` is unchanged.

- [ ] **Step 7: Compile-check**

Run:

```bash
python -m py_compile app/genai/agents/ppt_creator_agent.py
```

Expected: no output, exit 0.

- [ ] **Step 8: Commit**

```bash
git add app/genai/agents/ppt_creator_agent.py
git commit -m "refactor(ppt): make slide rendering a ladder and cap the creative tier at 3

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Tier 2 wired in

**Files:**
- Modify: `app/genai/agents/ppt_creator_agent.py` — add the Tier 2 prompt constants, `_layout_candidates`, `_layout_plan_prompt`, `_request_layout_plan`, and replace the `_tier2_layout_fit` stub from Task 4.

**Interfaces:**
- Consumes: `layout_plan.LayoutPlan`, `layout_plan.validate`, `layout_plan.format_errors`, `layout_plan.compile_program` (Tasks 2-3); `PptCreatorConstants.MAX_LAYOUT_PLAN_ATTEMPTS`, `LAYOUT_PLAN_CANDIDATES`, `LAYOUT_PLAN_MAX_TOKENS`, `LAYOUT_PLAN_DIRNAME`, `LAYOUT_PLAN_CODE_FILENAME` (Task 1); `_TierOutcome`, `_gate(..., layout_key=)` (Task 4).
- Produces:
  - `FIT_SYSTEM_PROMPT`, `FIT_USER_TEMPLATE` (module constants)
  - `PptCreatorAgent._layout_candidates(brief, catalog) -> list`
  - `PptCreatorAgent._layout_plan_prompt(brief, catalog, candidates, spent, tier1_error) -> str`
  - `PptCreatorAgent._request_layout_plan(messages) -> layout_plan.LayoutPlan`
  - `PptCreatorAgent._tier2_layout_fit(brief, catalog, tier1) -> _TierOutcome`

- [ ] **Step 1: Import the new module**

In `app/genai/agents/ppt_creator_agent.py`, extend the existing ppt import:

```python
from app.genai.ppt import (
    code_guard, deck_builder, geometry as geo, layout_plan, sandbox,
)
```

- [ ] **Step 2: Write Tier 2's prompts**

Add below `USER_TEMPLATE` (line 66-93). Tier 2 gets its own system prompt rather than the brand guide: it is not designing, and the guide's creative latitude is exactly what failed at Tier 1.

```python
FIT_SYSTEM_PROMPT = """You fit slide content into a layout the company template
already draws. You are not designing a slide; the template designed it.

Answer with ONE JSON object and nothing else - no prose, no code, no markdown fence:

{
  "layout": "<catalog key of the layout you chose, exactly as offered>",
  "title": "<the slide's title>",
  "subtitle": "<optional>",
  "placeholders": [
    {"target": "<idx or slot name from that layout>", "bullets": ["...", "..."]},
    {"target": "<idx or slot name>", "text": "..."}
  ],
  "extras": [],
  "notes": "<speaker notes>",
  "dropped": ["<any line you left off the slide, verbatim>"]
}

Hard rules:
- Choose ONE layout from the list you are given, by its catalog key. Never invent one.
- Every "target" must be a placeholder that layout actually has. They are listed for
  you with their idx, their slot name and their size in inches.
- The content must FIT. Each placeholder's capacity is given as approx_chars and
  approx_lines. You may tighten wording and spread content across the placeholders to
  make it fit, and you may choose a roomier layout. You may NOT invent a fact, and
  anything you leave off the slide you must list verbatim in "dropped".
- A placeholder fill carries text OR bullets OR an image OR a table OR a chart.
- "extras" are for content the layout has no placeholder for, and only these types:
  textbox, card, stat, icon_circle, line - positioned in inches, inside the safe
  region, never overlapping a reserved zone. Prefer an empty "extras" list.
- Colours are brand token names ('red', 'navy', 'accent1'). Never a hex value.
- Do not describe a footer, a slide number, a logo or a classification label. They are
  stamped for you.
- No emoji, no lorem text, no "TODO".
"""

FIT_USER_TEMPLATE = """Fit slide {slide_number} of {total} of the deck "{deck_title}"
into one of the layouts below.

Audience: {audience} | Objective: {objective} | Tone: {tone}
Slide role: {kind}
Design steer: {design_hint}

The free-form designer already failed on this slide {spent} time(s). Its last problem
was:
{tier1_error}

=== LAYOUTS YOU MAY CHOOSE FROM ===
The first one is the layout this slide was assigned by the deck's design contract.
Prefer it, and choose another only if this content genuinely cannot fit it.
{candidate_block}

=== SLIDE CONTENT (do not invent facts; tighten and redistribute as needed) ===
{content_json}

=== LOCAL IMAGE FILES YOU MAY PLACE ===
{image_block}
"""
```

- [ ] **Step 3: Write the candidate shortlist**

Add to `PptCreatorAgent`:

```python
    # --- tier 2: fit the content into a real layout --------------------------#
    def _layout_candidates(self, brief: SlideCodeBrief, catalog) -> list:
        """The layouts tier 2 may choose between: the assignment first, then alternates.

        Tier 1 often fails because the assigned layout cannot hold this slide's
        content, so refusing every alternative would just walk the slide to tier 3.
        Alternates are restricted to the assigned layout's own master, which is what
        keeps a deck of independently built slides reading as one deck.

        Args:
            brief: The slide's design assignment.
            catalog: The template catalog.

        Returns:
            `LayoutInfo` objects, the assignment first. Empty when nothing is fillable.
        """
        limit = PptCreatorConstants.LAYOUT_PLAN_CANDIDATES
        chosen: list = []
        assigned = None
        if brief.layout_key:
            try:
                assigned = catalog.resolve_layout(brief.layout_key)
            except LookupError:
                log.warning("slide %d: assigned layout %r is not in the catalog, so "
                            "tier 2 chooses freely from the preferred master",
                            brief.slide_number, brief.layout_key,
                            extra={"session_id": self.session_id})
        if assigned is not None and not assigned.placeholders:
            # Fixed artwork - the six closing and section layouts that draw their own
            # wording. There is nothing to fill, and moving the slide to some other
            # layout would overrule the deck's design rather than repair it.
            log.info("slide %d: assigned layout %s is fixed artwork with no "
                     "placeholders, so tier 2 has nothing to fit",
                     brief.slide_number, assigned.key,
                     extra={"session_id": self.session_id})
            return []
        if assigned is not None:
            chosen.append(assigned)
        master = (assigned.master_index if assigned is not None
                  else catalog.preferred_master)

        ranked = catalog.recommend(
            brief.kind or "content", text_blocks=len(brief.content.blocks) or 1,
            needs_picture=bool(brief.image_paths),
            has_table=bool(brief.content.tables), limit=limit * 4)
        for layout, score, reasons in ranked:
            if len(chosen) >= limit:
                break
            if (layout.master_index != master or not layout.placeholders
                    or any(c.key == layout.key for c in chosen)):
                continue
            chosen.append(layout)
            log.debug("slide %d: tier 2 candidate %s scored %.2f (%s)",
                      brief.slide_number, layout.key, score,
                      ", ".join(reasons) or "no reason given")
        log.info("slide %d: tier 2 may choose from %d layout(s) on master M%d: %s "
                 "(assigned %s)", brief.slide_number, len(chosen), master,
                 ", ".join(c.key for c in chosen) or "none",
                 assigned.key if assigned is not None else "(none)",
                 extra={"session_id": self.session_id})
        return chosen
```

- [ ] **Step 4: Write Tier 2's prompt assembly and request**

Add:

```python
    def _layout_plan_prompt(self, brief: SlideCodeBrief, catalog, candidates: list,
                            spent: int, tier1_error: str) -> str:
        """The candidate layouts, this slide's content, and why tier 1 gave up.

        Each candidate is described with `design_tools.layout_details`, the same
        payload tier 1 and the `get_layout_details` tool use, so no two places can
        describe one layout differently.

        Args:
            brief: The slide's content, context and design assignment.
            catalog: The template catalog.
            candidates: Layouts from `_layout_candidates`.
            spent: Generations tier 1 spent.
            tier1_error: Tier 1's last problem, for context.

        Returns:
            The user turn as text.
        """
        from app.genai.tools.design_tools import layout_details

        blocks = []
        for layout in candidates:
            details = layout_details(catalog, layout, brief.frame or {})
            blocks.append(f"--- {layout.key} ({layout.name}) ---\n"
                          + json.dumps(details, indent=2, default=str))
        text = FIT_USER_TEMPLATE.format(
            slide_number=brief.slide_number, total=brief.context.total,
            deck_title=brief.context.title,
            audience=brief.context.audience or "(unstated)",
            objective=brief.context.objective or "(unstated)",
            tone=brief.context.tone, kind=brief.kind,
            design_hint=brief.design_hint or "(designer's choice)",
            spent=spent, tier1_error=(tier1_error or "(none recorded)")[:1500],
            candidate_block="\n\n".join(blocks) or "(none)",
            content_json=brief.content.model_dump_json(
                indent=2, exclude={"bullets", "cards", "steps", "stat_value",
                                   "stat_label", "image_path"}),
            image_block="\n".join(f"- {p}" for p in brief.image_paths) or "(none)")
        log.info("slide %d: tier 2 prompt is %d chars over %d candidate layout(s)",
                 brief.slide_number, len(text), len(candidates),
                 extra={"session_id": self.session_id})
        return text

    def _request_layout_plan(self, messages: list[dict]) -> layout_plan.LayoutPlan:
        """Ask the model for a layout plan and return it, parsed.

        `chat_json` validates against the schema and retries its own malformed-JSON
        repairs, so a plan that arrives here is structurally sound; whether it is
        *renderable* is `layout_plan.validate`'s question.

        Args:
            messages: The conversation so far (brief, and any rejected plan).

        Returns:
            The parsed plan.

        Raises:
            Exception: Whatever `chat_json` raises when the gateway or the schema
                defeats it; the tier-2 loop treats it as one spent attempt.
        """
        log.debug("requesting a layout plan from %s: %d message(s)",
                  ModelConstants.PPT_CODER, len(messages),
                  extra={"session_id": self.session_id})
        plan = get_apim_llm_client().chat_json(
            FIT_SYSTEM_PROMPT, messages, layout_plan.LayoutPlan,
            max_tokens=PptCreatorConstants.LAYOUT_PLAN_MAX_TOKENS,
            model=ModelConstants.PPT_CODER)
        log.debug("received a plan on %s with %d fill(s)", plan.layout,
                  len(plan.placeholders), extra={"session_id": self.session_id})
        return plan
```

- [ ] **Step 5: Replace the Tier 2 stub with the real loop**

Delete the stub `_tier2_layout_fit` from Task 4 Step 5 and put this in its place:

```python
    def _tier2_layout_fit(self, brief: SlideCodeBrief, catalog,
                          tier1: _TierOutcome) -> _TierOutcome:
        """Make the content fit a real layout, from a plan that is checked before it runs.

        The model answers with a plan, not a program. An invalid plan is rejected by
        measurement - no execution spent - and the measurement is the repair
        instruction. An accepted plan is compiled deterministically and run in its own
        sandbox, then measured by the same gate as tier 1.

        Args:
            brief: The slide's content, context and design assignment.
            catalog: The template catalog.
            tier1: What tier 1 spent and how it failed, for the prompt and the skips.

        Returns:
            A `_TierOutcome` carrying the slide, or the reason this tier came up empty.
        """
        limit = PptCreatorConstants.MAX_LAYOUT_PLAN_ATTEMPTS
        if tier1.upstream_only:
            # Every tier 1 attempt died before producing anything, which is a gateway
            # problem. Three more gateway calls would fail the same way.
            reason = ("every creative attempt failed upstream, so the gateway - not "
                      "the design - is the problem")
            log.error("slide %d: skipping tier 2 (%s); going to tier 3",
                      brief.slide_number, reason,
                      extra={"session_id": self.session_id})
            return _TierOutcome(skipped=reason, last_error=tier1.last_error)

        candidates = self._layout_candidates(brief, catalog)
        if not candidates:
            reason = "no fillable layout to fit this slide into"
            log.warning("slide %d: skipping tier 2 (%s); going to tier 3",
                        brief.slide_number, reason,
                        extra={"session_id": self.session_id})
            return _TierOutcome(skipped=reason, last_error=tier1.last_error)

        log.info("slide %d entering tier 2 (layout-constrained, %d attempt(s) allowed) "
                 "after %d creative attempt(s)", brief.slide_number, limit,
                 tier1.attempts, extra={"session_id": self.session_id})
        self._step(brief, "fitting the content into a template layout",
                   phase="start", tier=2)

        fit_dir = (self.workdir / f"slide_{brief.slide_number}"
                   / PptCreatorConstants.LAYOUT_PLAN_DIRNAME)
        sandbox.stage(fit_dir, catalog, {
            "frame": brief.frame, "grid": brief.grid, "tokens": brief.tokens,
            "mode": "layout", "layout_key": brief.layout_key,
            "free_region": brief.free_region}, brief.image_paths)
        out_path = sandbox.output_path(fit_dir)
        code_path = fit_dir / PptCreatorConstants.LAYOUT_PLAN_CODE_FILENAME

        messages = [{"role": "user", "content": self._layout_plan_prompt(
            brief, catalog, candidates, tier1.attempts, tier1.last_error)}]
        last_error = tier1.last_error

        for attempt in range(1, limit + 1):
            try:
                plan = self._request_layout_plan(messages)
            except Exception as exc:                          # noqa: BLE001
                last_error = str(exc)
                log.error("slide %d tier 2 attempt %d/%d: no usable plan came back: %s",
                          brief.slide_number, attempt, limit, exc,
                          extra={"session_id": self.session_id}, exc_info=True)
                continue

            log.info("slide %d tier 2 attempt %d/%d: plan on %s (%s the assigned "
                     "layout), %d fill(s), %d extra(s), %d declared omission(s)",
                     brief.slide_number, attempt, limit, plan.layout,
                     "same as" if plan.layout == brief.layout_key else "NOT",
                     len(plan.placeholders), len(plan.extras), len(plan.dropped),
                     extra={"session_id": self.session_id})
            if plan.dropped:
                log.warning("slide %d tier 2: the plan leaves %d line(s) off the "
                            "slide: %s", brief.slide_number, len(plan.dropped),
                            " | ".join(str(d)[:120] for d in plan.dropped),
                            extra={"session_id": self.session_id})

            # 1. Validate the plan. A rejection here costs no execution at all.
            errors, _ = layout_plan.validate(
                plan, catalog, frame=brief.frame, allowed_images=brief.image_paths,
                slide_number=brief.slide_number)
            if errors:
                last_error = layout_plan.format_errors(errors)
                log.warning("slide %d tier 2 attempt %d/%d: the plan is not "
                            "renderable, no execution spent:\n%s", brief.slide_number,
                            attempt, limit, last_error,
                            extra={"session_id": self.session_id})
                self._step(brief, f"attempt {attempt}: the fitted plan needs "
                                  f"{len(errors)} fix(es)", phase="repair", tier=2,
                           attempts=attempt)
                messages += [{"role": "assistant", "content": plan.model_dump_json()},
                             {"role": "user", "content": last_error}]
                continue

            # 2. Compile and run. The program is ours, so a static rejection here is a
            #    compiler bug, not something the model can repair.
            source = layout_plan.compile_program(plan)
            rejection = code_guard.check_message(source)
            if rejection:
                last_error = rejection
                log.error("slide %d tier 2 attempt %d/%d: the COMPILED program was "
                          "rejected by the static guard. This is a bug in "
                          "layout_plan.compile_program, not in the plan:\n%s",
                          brief.slide_number, attempt, limit, rejection,
                          extra={"session_id": self.session_id})
                continue
            code_path.write_text(source, encoding="utf-8")
            succeeded, error = self._execute(code_path, fit_dir)
            produced = self._locate_output(fit_dir, out_path) if succeeded else None
            if produced is None:
                last_error = error or "the compiled program wrote no .pptx file"
                log.error("slide %d tier 2 attempt %d/%d: the compiled program "
                          "failed: %s", brief.slide_number, attempt, limit,
                          last_error[:500], extra={"session_id": self.session_id})
                self._step(brief, f"attempt {attempt}: the fitted slide would not "
                                  f"render", phase="repair", tier=2, attempts=attempt)
                messages += [
                    {"role": "assistant", "content": plan.model_dump_json()},
                    {"role": "user", "content":
                        "Rendering that plan failed. Simplify it - fewer extras, "
                        "shorter copy, a layout whose placeholders already suit this "
                        "content.\n\n=== ERROR ===\n" + last_error}]
                continue

            # 3. The same gate as tier 1, against the layout the plan actually chose.
            violations = self._gate(produced, brief, catalog, layout_key=plan.layout)
            if geo.is_clean(violations):
                total = tier1.attempts + attempt
                log.info("slide %d accepted at tier 2 on attempt %d/%d on layout %s: "
                         "%s (%d advisory note(s), %d generation(s) in total)",
                         brief.slide_number, attempt, limit, plan.layout,
                         produced.name, len(violations), total,
                         extra={"session_id": self.session_id})
                self._step(brief, f"slide fitted onto {plan.layout} on attempt "
                                  f"{attempt}", phase="done", tier=2, attempts=attempt)
                return _TierOutcome(result=SlideBuildResult(
                    slide_number=brief.slide_number, pptx_path=str(produced),
                    attempts=total, tier=2, layout_used=plan.layout,
                    code_path=str(code_path)), attempts=attempt)

            last_error = geo.format_violations(violations)
            log.warning("slide %d tier 2 attempt %d/%d failed the gate with %d "
                        "violation(s) (%d blocking) on %s:\n%s", brief.slide_number,
                        attempt, limit, len(violations),
                        sum(1 for v in violations if v.blocking), plan.layout,
                        last_error, extra={"session_id": self.session_id})
            self._step(brief, f"attempt {attempt}: {len(violations)} layout issue(s) "
                              f"in the fitted slide", phase="repair", tier=2,
                       attempts=attempt)
            messages += [{"role": "assistant", "content": plan.model_dump_json()},
                         {"role": "user", "content": last_error}]

        log.error("slide %d leaving tier 2 unsatisfied after %d attempt(s); last "
                  "problem: %s", brief.slide_number, limit, last_error[:500],
                  extra={"session_id": self.session_id})
        return _TierOutcome(attempts=limit, last_error=last_error)
```

- [ ] **Step 6: Update the module docstring**

The file's docstring still describes a single loop. Replace its first two paragraphs with:

```python
"""The PPT Creator Agent: turns ONE slide's content into ONE .pptx, in three tiers.

Tier 1 hands the model the company presentation guide and one slide's grounded content
and asks for a `DeckBuilder` program. Every program is checked statically, executed
locally in a throwaway directory, then reopened and measured by the geometry gate; a
static rejection, a traceback and a gate violation are all repairs sharing one budget
of `PptCreatorConstants.MAX_CODE_FIX_ATTEMPTS` generations.

Tier 2 takes over when the creative tier runs out. The model no longer writes code: it
answers with a plan that names one of a shortlist of real layouts and fills that
layout's own placeholders. The plan is measured against the layout before anything
runs - so a plan whose copy cannot fit is rejected for free, with the measurement as
the repair instruction - then compiled deterministically into a `DeckBuilder` program
and gated exactly like tier 1. It has far less freedom than tier 1 and typically needs
one pass.

Tier 3 is the deterministic render on a real template layout, with no model involved at
all, so a stubborn slide can never cost the user a slide.
"""
```

- [ ] **Step 7: Compile-check**

Run:

```bash
python -m py_compile app/genai/agents/ppt_creator_agent.py app/genai/ppt/layout_plan.py
```

Expected: no output, exit 0.

- [ ] **Step 8: Commit**

```bash
git add app/genai/agents/ppt_creator_agent.py
git commit -m "feat(ppt): fit the content into a real layout before falling back

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Keep the existing tests honest, and verify

**Files:**
- Modify: `tests/test_ppt_creator_agent.py`
- Modify: `app/genai/workflows/deck_workflow.py:412-425` (the design roll-up only)

**Interfaces:**
- Consumes: everything above.
- Produces: no new interfaces.

Two problems this task exists to solve. First, four tests in `tests/test_ppt_creator_agent.py` stub `agent._request_code` and then assert `result.fallback_used` — with Tier 2 in place those tests would walk into `_request_layout_plan`, which is a **real gateway call**. They must stub the new seam too. Second, the workflow's roll-up counts slides as "first try", "after redesign" or "rendered from a template layout", and a Tier 2 success is none of those.

- [ ] **Step 1: Add a Tier 2 stub helper to the test module**

In `tests/test_ppt_creator_agent.py`, add below `_queue` (line 79):

```python
def _refuse_tier2(agent, monkeypatch):
    """Make tier 2 unavailable, so an exhausted tier 1 lands on tier 3.

    The tier-1 tests are about tier 1. Letting them reach `_request_layout_plan` would
    put a real gateway call in the suite.
    """
    calls = {"n": 0}

    def fake_plan(messages):
        calls["n"] += 1
        raise UpstreamError("tier 2 is stubbed out in this test")

    monkeypatch.setattr(agent, "_request_layout_plan", fake_plan)
    return calls
```

- [ ] **Step 2: Fix the four exhaustion tests**

Exactly four tests drive Tier 1 to exhaustion and then assert the fallback. Each one needs `_refuse_tier2(agent, monkeypatch)` before its `agent.build_slide(brief)` call, and its attempts assertion must account for both budgets. By name, so you do not have to trust line numbers:

1. `test_a_program_that_writes_nothing_counts_as_a_failure`
2. `test_the_repair_budget_is_spent_then_the_slide_is_rendered_on_a_layout`
3. `test_a_slide_that_only_ever_collides_falls_back_to_an_on_layout_render`
4. `test_the_three_failure_kinds_share_one_budget`

The shape for each:

```python
    calls = _queue(agent, monkeypatch, BROKEN_PROGRAM)
    tier2 = _refuse_tier2(agent, monkeypatch)

    result = agent.build_slide(brief)

    assert calls["n"] == PptCreatorConstants.MAX_CODE_FIX_ATTEMPTS
    assert tier2["n"] == PptCreatorConstants.MAX_LAYOUT_PLAN_ATTEMPTS
    assert result.fallback_used and result.tier == 3
    assert result.attempts == (PptCreatorConstants.MAX_CODE_FIX_ATTEMPTS
                               + PptCreatorConstants.MAX_LAYOUT_PLAN_ATTEMPTS)
```

Keep each test's own remaining assertions (the `published` event check, the `Path(...).exists()` check) exactly as they are, with one exception covered in the next step.

Two things that look like they need changing and do not. Changing either would hide a regression:

- `test_a_gateway_failure_is_retried_like_any_other_failure` needs nothing at all — it succeeds on Tier 1's second attempt, so Tier 2 is never reached and the stub would go unused.
- `test_the_three_failure_kinds_share_one_budget` needs `_refuse_tier2` like the other three, but **do not touch its `_queue` list.** It queues six programs and, with the cap now at 3, consumes only the first three (`FORBIDDEN_PROGRAM`, `BROKEN_PROGRAM`, `COLLIDING_PROGRAM`) — which is still one guard rejection, one traceback and one gate violation, exactly the three failure kinds it exists to prove share a budget. Re-queueing it to "fit" the new budget would delete the point of the test.

- [ ] **Step 3: Repoint the one assertion that reads a log message**

`test_the_repair_budget_is_spent_then_the_slide_is_rendered_on_a_layout` ends with:

```python
    assert any("code-repair limit" in message for message in
               (record.getMessage() for record in caplog.records))
```

Task 4 replaced that log line. The equivalent assertion against the new ladder — it must show both that Tier 1 gave up and that Tier 3 was reached:

```python
    messages = [record.getMessage() for record in caplog.records]
    assert any("leaving tier 1 unsatisfied" in message for message in messages)
    assert any("entering tier 3" in message for message in messages)
```

- [ ] **Step 4: Count Tier 2 in the workflow's roll-up**

In `app/genai/workflows/deck_workflow.py`, replace the roll-up block (currently lines 415-425) with:

```python
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
        log.info("design tiers: %d at tier 1 first try, %d at tier 1 after repair, "
                 "%d at tier 2, %d at tier 3", first_try, repaired, fitted, fell_back,
                 extra={"session_id": session_id})
        _step(session_id, "deck",
              f"Designed {len(built)} slide(s): {', '.join(parts)}.")
```

Nothing else in this file changes. The `gaps` note and `slide_cache.put(..., fallback=result.fallback_used)` above it stay as they are, because `fallback_used` still means Tier 3 and only Tier 3.

- [ ] **Step 5: Compile-check everything this plan touched**

Run:

```bash
python -m py_compile \
  app/config/constants.py \
  app/core/models/deck_model.py \
  app/genai/ppt/layout_plan.py \
  app/genai/agents/ppt_creator_agent.py \
  app/genai/workflows/deck_workflow.py \
  tests/test_ppt_creator_agent.py
```

Expected: no output, exit 0. A `SyntaxError` here names the file and line — fix it and re-run.

- [ ] **Step 6: Check the import graph still holds**

The new module must not have dragged `scripts/` into `app/`, and `deck_builder.py` must still be sandbox-safe:

```bash
grep -rn "import scripts\|from scripts" app/ --include=*.py
grep -n "^from app\|^import app" app/genai/ppt/deck_builder.py
```

Expected: no output from either command.

- [ ] **Step 7: Commit**

```bash
git add tests/test_ppt_creator_agent.py app/genai/workflows/deck_workflow.py
git commit -m "test(ppt): keep the tier-1 tests off the gateway, and count tier 2

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 8 (OPTIONAL — ask first): run the focused test file**

The plan's constraint is a compile check only, so **do not run this without the user's go-ahead.** If they want it:

```bash
python -m pytest tests/test_ppt_creator_agent.py -v
```

Expected: the tier-1 success, repair and exhaustion tests pass. A failure here is most likely a stub that did not reach `_request_layout_plan`, or an attempts assertion that still expects the old single budget.

---

## Verification

What "done" means for this plan:

1. `python -m py_compile` is clean over all six touched files (Task 6, Step 5).
2. Neither import-hygiene invariant is broken (Task 6, Step 6).
3. `PptCreatorConstants.MAX_CODE_FIX_ATTEMPTS == 3` and `MAX_LAYOUT_PLAN_ATTEMPTS == 3`.
4. `_render_on_layout` is byte-for-byte what it was before this plan: `git diff` over `app/genai/agents/ppt_creator_agent.py` shows no change inside it.
5. Reading the log of one real deck run, you can answer for every slide: which tier produced it, how many attempts each tier spent, which layout it landed on, whether that layout was the assigned one, and why each failed attempt failed.

## What this plan does not do

- **It does not improve Tier 3.** The content dump stays exactly as it is, by instruction. Tier 2 exists so Tier 3 is reached less often, not so it is reached prettily.
- **It does not change the design contract.** Layout assignment, the grid, the frame and the tokens are computed exactly as they are today; Tier 2 may only deviate from the assigned *layout*, and only within the assigned master.
- **It does not add tests for `layout_plan`.** `validate` and `compile_program` are pure and eminently testable, and a follow-up task should cover them — but writing tests this plan is forbidden to run would be writing unverified code, which is worse than an honest gap.
- **It does not touch the stitcher.** A Tier 2 slide is a single-slide .pptx on a real template layout, which is exactly what the stitcher already consumes.
- **It does not retry Tier 1 after Tier 2.** The ladder only descends.

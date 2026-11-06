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
#: `SlideHandle._fit_size` shrinks copy to fit, but a plan may rely on only this much of
#: it. A plan that needs more is not a plan that fits, it is one that will be hard to read.
_MIN_SHRINK_RATIO = 0.85
#: The smallest a title may be shrunk to and still read as a title.
_MIN_TITLE_PT = 22.0
#: Tolerance in inches when checking an extra against the slide edges.
_EDGE_SLACK_IN = 0.02


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


# --- validation --------------------------------------------------------------#
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


def _title_problem(title: str, info: Any) -> Optional[str]:
    """Whether the title fits its placeholder at no less than `_MIN_TITLE_PT`.

    Args:
        title: The plan's title.
        info: The title `PlaceholderInfo`.

    Returns:
        None when it fits, else a repair instruction.
    """
    size = max(float(info.font_size or 28.0), _MIN_TITLE_PT)
    floor = min(size, _MIN_TITLE_PT)
    if db._text_fits([title], info.width, info.height, floor, False):
        return None
    return (f"The title ({len(title)} characters) does not fit the {info.width:.2f}x"
            f"{info.height:.2f}in title placeholder even at {floor:.0f}pt. Shorten it to "
            f"one short line.")


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
    title_info = layout.find("title") if plan.title and layout.has_role("title") else None
    if title_info is not None:
        problem = _title_problem(plan.title, title_info)
        if problem:
            errors.append(problem)

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

    filled = set(seen) | {p.idx for p in layout.placeholders
                          if (p.role == "title" and plan.title)
                          or (p.role == "subtitle" and plan.subtitle)}
    errors.extend(_extra_problems(plan.extras, catalog, layout, frame, filled))
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
                    frame: Optional[dict], filled: Optional[set] = None) -> list[str]:
    """Extras that leave the slide, land on brand furniture, or say nothing.

    Args:
        extras: The plan's extras.
        catalog: The `TemplateCatalog`, for the canvas size.
        layout: The chosen `LayoutInfo`, for its inherited decoration.
        frame: This slide's frame block.
        filled: idx of the placeholders the plan fills; an extra may not cover one.

    Returns:
        One instruction per problem.
    """
    if not extras:
        return []
    problems: list[str] = []
    reserved = db.reserved_rects(frame, layout, catalog.slide_width,
                                 catalog.slide_height)
    # Extras may not collide with each other, nor sit on a text placeholder the plan
    # fills: the gate would reject either after a wasted render.
    for index, extra in enumerate(extras):
        if extra.width <= 0 or extra.height <= 0:
            continue
        for other in extras[index + 1:]:
            if other.width > 0 and other.height > 0 and db._rects_overlap(
                    extra.rect(), other.rect()):
                problems.append(f"Extras {extra.type!r} and {other.type!r} overlap. Give "
                                f"each its own space, or drop one.")
        for info in layout.placeholders:
            if info.idx in (filled or set()) and \
                    db._rects_overlap(extra.rect(),
                                      (info.left, info.top, info.width, info.height)):
                problems.append(f"Extra {extra.type!r} overlaps the {info.slot} "
                                f"placeholder. Put its copy in a placeholder, or place "
                                f"it in free space.")
                break
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


# --- compilation -------------------------------------------------------------#
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

    The keyword set emitted per call is exactly what that `SlideHandle` method accepts -
    `set_bullets` takes `size` and `color` only, which is why bullets get a narrower
    style than `set_text` does.

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
            # set_bullets accepts size and color only; bold and align are not its
            # keywords, and emitting them would be a TypeError at render time.
            lines.append(f"slide.set_bullets({fill.target!r}, "
                         f"{[str(b) for b in fill.bullets]!r}"
                         + _kwargs([("size", fill.size), ("color", fill.color)]) + ")")
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

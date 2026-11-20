"""The design agent's tools: the catalog, a free geometry check, and the gate.

`check_geometry` exists to make arrangement iteration free. Without it, the only way to
discover that two cards collide is to write the whole program and run it, at up to
`EXEC_TIMEOUT_SECONDS` a try. It evaluates rules 1-6 - the rules decidable from
rectangles alone - plus rule 10 for any box that supplies text and a size.

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

#: Where the free region starts on a layout the catalog measured none for.
_FALLBACK_REGION_TOP_IN = 1.30


def layout_details(catalog, layout, frame: dict) -> dict:
    """Everything a program needs to know about the layout it was assigned.

    Shared by the `get_layout_details` tool and by `PptCreatorAgent`'s prompt, which
    inlines it. One payload, so the two can never describe the same layout differently.

    Args:
        catalog: The template catalog.
        layout: The `LayoutInfo` being described.
        frame: This slide's frame block, for the reserved zones.

    Returns:
        The layout's placeholders, decor, reserved zones, safe region and the grid.
    """
    safe = (layout.free_region.to_list() if layout.free_region
            else [GC.FRAME_SIDE_MARGIN_IN, _FALLBACK_REGION_TOP_IN,
                  catalog.slide_width - 2 * GC.FRAME_SIDE_MARGIN_IN,
                  catalog.slide_height - _FALLBACK_REGION_TOP_IN
                  - GC.FOOTER_BAND_HEIGHT_IN])
    if layout.free_region is None:
        log.debug("%s has no measured free region; falling back to the %.2fin band "
                  "between the header and the footer", layout.key,
                  safe[3])
    details = layout.details()
    details.update({
        "decor_rects": [r.to_list() for r in layout.decor_rects],
        "reserved_zones": builder.reserved_rects(
            frame, layout, catalog.slide_width, catalog.slide_height),
        "safe_region": safe,
        "canvas_capable": layout.canvas_capable,
        "background_luminance": layout.background_luminance,
        "grid": {"columns": GC.GRID_COLUMNS, "rows": GC.GRID_ROWS,
                 "gutter_in": GC.GRID_GUTTER_IN, "over": safe},
        "placeholders": [dict(p.summary(), role=p.role, approx_chars=p.approx_chars)
                         for p in layout.placeholders],
    })
    log.debug("described %s: %d placeholder(s), %d decor rect(s), %d reserved zone(s), "
              "safe region %.2fx%.2fin, canvas_capable=%s", layout.key,
              len(details["placeholders"]), len(details["decor_rects"]),
              len(details["reserved_zones"]), safe[2], safe[3], layout.canvas_capable)
    return details


def build_design_tools(session_id: str, *, contract=None, slide_dir=None) -> list:
    """Design-tool instances bound to one slide build.

    Args:
        session_id: For logging only; the tools carry no session state.
        contract: The deck design contract, so `check_geometry` knows the frame.
        slide_dir: Where `run_slide_code` executes and `validate_slide` reads from.

    Returns:
        The seven LangChain tools, in the order the prompt introduces them.
    """
    catalog = catalog_mod.load()
    log.info("design tools bound for session %s: contract=%s, slide_dir=%s",
             session_id, "yes" if contract is not None else "no", slide_dir or "none")

    def _frame_for(layout):
        if contract is None:
            # No contract means no classification and no slide number, so the reserved
            # zones are the generic ones. Fine for a catalog query, wrong for a gate.
            log.debug("no contract bound; using a generic Internal frame for %s",
                      getattr(layout, "key", "?"))
            return builder.frame_spec(catalog.slide_width, catalog.slide_height,
                                      label="Internal")
        return contract.frame_for(1, layout)

    @tool
    def get_template_summary() -> str:
        """The brand template at a glance: slide size, theme fonts and colours, the
        masters, which master is preferred for content slides, and every unique layout
        with its slot summary. Read this first — it is how you learn what layouts
        exist, and layout names are never to be invented."""
        summary = catalog.summary_text()
        log.info("tool get_template_summary -> %d chars", len(summary))
        return summary

    @tool
    def search_layouts(query: str = "", tags: str = "", min_text_slots: int = 0,
                       needs_picture: bool = False, limit: int = 20) -> str:
        """Find template layouts by keyword or tag. Tags include cover, agenda,
        section, columns, 3-column, process, dashboard, photo, chart, title-only,
        closing, dark. Use `min_text_slots` to demand room for N text blocks and
        `needs_picture` to demand a real picture slot."""
        found = catalog.search(query=query, tags=tags, min_text_slots=min_text_slots,
                               needs_picture=needs_picture, limit=limit)
        log.info("tool search_layouts(query=%r, tags=%r, min_text_slots=%d, "
                 "needs_picture=%s) -> %d match(es)", query, tags, min_text_slots,
                 needs_picture, len(found))
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
        log.info("tool recommend_layouts(intent=%r, text_blocks=%d, picture=%s, "
                 "table=%s, chart=%s) -> %s", intent, text_blocks, needs_picture,
                 has_table, has_chart,
                 [f"{l.key}:{s}" for l, s, _ in ranked] or "nothing")
        if not ranked:
            log.warning("no layout ranks for intent %r; the model is left to pick by "
                        "hand", intent)
            return (f"Nothing ranks for intent {intent!r}. Call get_template_summary "
                    f"and pick a layout by hand.")
        return "\n".join(
            f"{layout.compact()} | score {score} | "
            f"{', '.join(reasons) or 'no strong signal'}"
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
            log.warning("tool get_layout_details(%r) -> unknown layout: %s",
                        layout, exc)
            return str(exc)
        log.info("tool get_layout_details(%r) -> %s", layout, found.key)
        return json.dumps(layout_details(catalog, found, _frame_for(found)),
                          indent=2, default=str)

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
            log.info("tool check_geometry: %d of %d box(es) unreadable, nothing "
                     "checked", len(problems), len(boxes or []))
            return ("These boxes could not be read:\n"
                    + "\n".join(f"- {p}" for p in problems))
        try:
            found = catalog.resolve_layout(layout) if layout else None
        except LookupError as exc:
            log.warning("tool check_geometry: unknown layout %r: %s", layout, exc)
            return str(exc)
        if found is None:
            # Without a layout there are no decor rects and no frame, so rule 5 has
            # nothing to protect and the answer is more permissive than the gate.
            log.debug("tool check_geometry called with no layout; rule 5 has no "
                      "reserved zones to check against")
        reserved = (builder.reserved_rects(_frame_for(found), found,
                                           catalog.slide_width, catalog.slide_height)
                    if found is not None else ())
        violations = geo.check_rects(parsed, slide_width=catalog.slide_width,
                                     slide_height=catalog.slide_height,
                                     reserved=reserved)
        violations += geo.check_overflow(parsed)
        tail = ("\n\nThis check covers rules 1-6 and 10 only. Rules 7-9 (leftover "
                "prompt text, off-palette colours, off-scale fonts) depend on what the "
                "rendered file contains — validate_slide is the authoritative check.")
        log.info("tool check_geometry: %d box(es) on %s -> %d violation(s)",
                 len(parsed), found.key if found is not None else "(no layout)",
                 len(violations))
        if not violations:
            return "This arrangement passed rules 1-6 and 10." + tail
        return geo.format_violations(violations) + tail

    @tool
    def run_slide_code(code: str) -> str:
        """Run your DeckBuilder program and report what happened. The program is
        static-checked first; a rejection comes back without costing an execution."""
        from app.genai.ppt import sandbox

        if slide_dir is None:
            log.error("tool run_slide_code called with no slide directory bound; the "
                      "program cannot be executed")
            return "no slide directory is bound to this tool"
        log.info("tool run_slide_code: %d chars in %s", len(code or ""), slide_dir)
        return sandbox.run(code, Path(slide_dir))

    @tool
    def validate_slide() -> str:
        """The authoritative check on the file your program produced: geometry, brand
        and fidelity. A slide is only finished when this passes."""
        from app.genai.ppt import sandbox

        if slide_dir is None:
            log.error("tool validate_slide called with no slide directory bound; "
                      "there is no produced file to gate")
            return "no slide directory is bound to this tool"
        log.info("tool validate_slide: gating whatever is in %s", slide_dir)
        return sandbox.validate(Path(slide_dir), catalog, contract)

    return [get_template_summary, search_layouts, recommend_layouts, get_layout_details,
            check_geometry, run_slide_code, validate_slide]


def _parse_boxes(boxes) -> tuple[list, list]:
    """Turn the tool's JSON boxes into `geometry.Box`es, explaining what is unusable.

    Args:
        boxes: The raw list the model passed.

    Returns:
        (parsed boxes, one problem string per unusable entry).
    """
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
        try:
            parsed.append(geo.Box(
                id=str(raw.get("id") or f"box {index + 1}"),
                left=float(raw["left"]), top=float(raw["top"]),
                width=float(raw["width"]), height=float(raw["height"]),
                has_text=bool(raw.get("has_text", bool(text.strip()))),
                filled=bool(raw.get("filled", False)),
                is_picture=bool(raw.get("is_picture", False)),
                text=text,
                font_size=float(raw["font_size"]) if raw.get("font_size") else None))
        except (TypeError, ValueError) as exc:
            log.debug("box %r rejected: %s", raw.get("id", index + 1), exc)
            problems.append(
                f"box {raw.get('id', index + 1)!r} has a non-numeric left, top, width, "
                f"height or font_size — every measurement is a number of inches")
    log.debug("parsed %d of %d box(es) from the tool call",
              len(parsed), len(boxes or []))
    return parsed, problems

"""Lay out the run folder for the whole-deck program.

Same idea as the per-slide sandbox (``app.genai.ppt.sandbox``): the program can import
nothing from the app, so everything it needs is placed beside it -

    deck_builder.py  the DeckBuilder API, copied verbatim (the same file classic uses)
    deck_kit.py      WholeDeck: per-slide layouts, frames and answer reveal
    catalog.json     the template catalog
    context.py       TEMPLATE_PATH, OUTPUT_PATH, GRID, TOKENS, CATALOG and SLIDES

SLIDES is one entry per slide, from the deck design contract the classic engine also
uses: the layout, the composition mode and free region, the frame, whether it is a quiz
answer slide, and the image files it may place.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from app.config.constants import SkillDeckConstants as C
from app.genai.ppt import deck_builder
from app.genai.ppt.template import registry
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

CATALOG_FILENAME = "catalog.json"
CONTEXT_FILENAME = "context.py"
BUILDER_FILENAME = "deck_builder.py"
KIT_SOURCE = Path(__file__).resolve().parent / C.KIT_FILENAME

_CONTEXT = '''"""Written by the skill deck agent. Import these; do not edit them."""

import json
from pathlib import Path

TEMPLATE_PATH = {template_path!r}
OUTPUT_PATH = {output_path!r}
GRID = {grid!r}
TOKENS = {tokens!r}
SLIDES = {slides!r}

CATALOG = json.loads((Path(__file__).parent / {catalog!r}).read_text(encoding="utf-8"))
'''


def _family(slide) -> str:
    for name in C.FAMILY_FIELDS:
        if getattr(slide, name, None):
            return name
    return ""


def _fallback_key(slide, catalog, contract) -> str:
    try:
        from app.genai.ppt import design_contract
        return design_contract.fallback_assignment(slide, catalog,
                                                   contract.master_index).layout_key
    except Exception as exc:                # noqa: BLE001 - any layout beats no deck
        log.warning("fallback layout lookup failed (%s); using the first canonical", exc)
        return catalog.canonical_layouts()[0].key


def slide_specs(draft, contract, catalog, image_paths: dict | None = None) -> list[dict]:
    """One entry per slide: where and how the program builds it.

    Quiz slides share one layout, and so do FAQ slides (the first slide of the family
    decides), so a family reads as one design - as in the classic engine.

    Args:
        draft: The assembled DeckDraft (slides already renumbered).
        contract: The deck design contract (``design_contract.build``).
        catalog: The template catalog.
        image_paths: Local image files per slide number.

    Returns:
        The SLIDES list written into context.py.
    """
    specs, family_keys = [], {}
    for number, slide in enumerate(draft.slides, start=1):
        assignment = contract.assignments.get(number)
        key = assignment.layout_key if assignment else ""
        family = _family(slide)
        if family:
            key = family_keys.setdefault(family, key)
        layout = catalog.get(key) if key else None
        if layout is None:
            key = _fallback_key(slide, catalog, contract)
            layout = catalog.get(key)
            log.warning("slide %d: no usable assigned layout; using %s", number, key)
        same = assignment is not None and assignment.layout_key == key
        quiz = getattr(slide, "quiz", None)
        specs.append({
            "number": number,
            "kind": getattr(slide, "kind", "content"),
            "layout_key": key,
            "mode": assignment.mode if same else "layout",
            "free_region": (list(assignment.free_region)
                            if same and assignment.free_region else None),
            "frame": contract.frame_for(number, layout),
            "reveal": bool(quiz is not None and getattr(quiz, "reveal", False)),
            "images": [str(p) for p in (image_paths or {}).get(number, [])],
        })
    return specs


def stage(run_dir: Path, catalog, specs: list[dict], contract) -> Path:
    """Write the run folder. Returns the path the program must save to."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(deck_builder.__file__, run_dir / BUILDER_FILENAME)
    shutil.copyfile(KIT_SOURCE, run_dir / C.KIT_FILENAME)
    high = any(getattr(catalog.get(s["layout_key"]), "high_creativity", False) for s in specs)
    serialized = json.dumps(catalog.to_dict(include_high_creativity=high))
    (run_dir / CATALOG_FILENAME).write_text(serialized, encoding="utf-8")
    output = (run_dir / C.RAW_DECK_FILENAME).as_posix()
    (run_dir / CONTEXT_FILENAME).write_text(_CONTEXT.format(
        template_path=str(registry.template_path()), output_path=output,
        grid=dict(getattr(contract, "grid", {}) or {}),
        tokens=dict(getattr(contract, "tokens", {}) or {}), slides=specs,
        catalog=CATALOG_FILENAME), encoding="utf-8")
    log.info("whole-deck sandbox staged at %s: %d slide(s), %d layout(s), catalog %d bytes",
             run_dir, len(specs), len({s["layout_key"] for s in specs}), len(serialized))
    return Path(output)


def layout_info(catalog, key: str, frame: dict) -> dict:
    """The assigned layout as the classic designer's prompt describes it."""
    layout = catalog.get(key)
    try:
        from app.genai.tools.design_tools import layout_details
        return layout_details(catalog, layout, frame)
    except ImportError:                     # light environments (tests): the basics
        return layout.details()

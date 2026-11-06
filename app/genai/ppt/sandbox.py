"""Stage and run one slide's program inside a sealed directory.

The sandbox is deliberately closed: no PYTHONPATH, no inherited secrets, one writable
directory. Model-written code therefore cannot import `app.*` at all, which is why
`deck_builder.py` is COPIED here rather than imported - it has no `app.*` imports of
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
from typing import Optional

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
REVEAL_ANSWERS = {reveal!r}

CATALOG = json.loads((Path(__file__).parent / {catalog_filename!r}).read_text(
    encoding="utf-8"))
'''


def stage(slide_dir: Path, catalog, contract_fields: dict,
          image_paths: list, reveal_answers: bool = False) -> dict:
    """Lay out one slide's sandbox directory.

    Args:
        slide_dir: The program's only writable location.
        catalog: The `TemplateCatalog`, serialized into the directory.
        contract_fields: `DeckDesignContract.brief_fields(...)` for this slide.
        image_paths: Local paths the program may place.
        reveal_answers: Stage a quiz ANSWER slide: `DeckBuilder.save` finds the marker
            and highlights the correct choice, so the question's program runs as is.

    Returns:
        The names the program can import from `context`, for the prompt.
    """
    slide_dir.mkdir(parents=True, exist_ok=True)
    marker = slide_dir / deck_builder.REVEAL_MARKER_FILENAME
    if reveal_answers:
        marker.write_text("1", encoding="utf-8")
    elif marker.exists():
        marker.unlink()
    builder_source = Path(deck_builder.__file__)
    shutil.copyfile(builder_source, slide_dir / BUILDER_FILENAME)
    # Only a slide built on the High Creativity master sees that master's layouts;
    # every other sandbox gets exactly the catalog it always did.
    choices = list(contract_fields.get("layout_choices") or [])
    assigned = catalog.get(contract_fields.get("layout_key") or "")
    high_creativity = bool(choices) or bool(getattr(assigned, "high_creativity", False))
    serialized = json.dumps(catalog.to_dict(include_high_creativity=high_creativity))
    (slide_dir / CATALOG_FILENAME).write_text(serialized, encoding="utf-8")
    log.debug("staged %s: deck_builder.py (%d bytes) and catalog.json (%d bytes, "
              "%d layouts)", slide_dir, builder_source.stat().st_size,
              len(serialized), len(catalog.layouts))

    produced = (slide_dir / OUTPUT_FILENAME).as_posix()
    (slide_dir / CONTEXT_FILENAME).write_text(_CONTEXT_TEMPLATE.format(
        template_path=str(registry.template_path()),
        output_path=produced,
        frame=contract_fields.get("frame") or {},
        grid=contract_fields.get("grid") or {},
        tokens=contract_fields.get("tokens") or {},
        mode=contract_fields.get("mode") or "layout",
        layout_key=contract_fields.get("layout_key") or "",
        free_region=contract_fields.get("free_region"),
        images=list(image_paths or []),
        reveal=bool(reveal_answers),
        catalog_filename=CATALOG_FILENAME), encoding="utf-8")
    if choices:
        # Appended, never templated in: a context.py without choices stays byte-identical.
        with open(slide_dir / CONTEXT_FILENAME, "a", encoding="utf-8") as handle:
            handle.write("\n# Layouts this slide may be built on (pass one key to "
                         "deck.add_slide).\n"
                         f"LAYOUT_CHOICES = {choices!r}\n")
        log.info("sandbox %s offers %d High Creativity layout(s): %s", slide_dir,
                 len(choices), ", ".join(c.get("key", "?") for c in choices))
    log.info("sandbox staged at %s: mode=%s layout=%s frame=%s images=%d",
             slide_dir, contract_fields.get("mode") or "layout",
             contract_fields.get("layout_key") or "(unassigned)",
             "yes" if contract_fields.get("frame") else "no", len(image_paths or []))
    missing = [p for p in (image_paths or []) if not Path(str(p)).is_file()]
    if missing:
        # The program is told it may place these; a path that is not on disk becomes a
        # warning inside DeckBuilder and an image silently absent from the slide.
        log.warning("%d image path(s) staged but not on disk: %s",
                    len(missing), ", ".join(str(p) for p in missing[:4]))
    return {"TEMPLATE_PATH": str(registry.template_path()), "OUTPUT_PATH": produced,
            "FRAME": "the slide's frame block", "CATALOG": "the template catalog",
            "IMAGES": list(image_paths or [])}


def output_path(slide_dir: Path) -> Path:
    """Where a staged program is told to save."""
    return slide_dir / OUTPUT_FILENAME


def guard(code: str) -> Optional[str]:
    """Static rejection message, or None when the program may run."""
    rejection = code_guard.check_message(code_guard.strip_code_fences(code))
    log.debug("static guard: %s", "rejected" if rejection else "accepted")
    return rejection


def run(code: str, slide_dir: Path) -> str:
    """Guard, write and execute one program, and describe what happened.

    Used by the `run_slide_code` design tool. `PptCreatorAgent.build_slide` does not
    call this - it drives the same three steps itself so it can feed each kind of
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
        log.info("run_slide_code: rejected before execution, no subprocess started")
        return rejection

    script = slide_dir / PptCreatorConstants.GENERATED_CODE_FILENAME
    script.write_text(code, encoding="utf-8")
    produced = output_path(slide_dir)
    before = produced.stat().st_mtime if produced.exists() else None
    log.info("run_slide_code: executing %s (%d chars, timeout %ss)",
             script, len(code), PptCreatorConstants.EXEC_TIMEOUT_SECONDS)
    try:
        finished = subprocess.run(
            [sys.executable, str(script)], cwd=str(slide_dir), capture_output=True,
            text=True, env=PptCreatorAgent._sandbox_env(slide_dir),
            timeout=PptCreatorConstants.EXEC_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        log.error("run_slide_code: %s did not finish within %ss and was killed",
                  script, PptCreatorConstants.EXEC_TIMEOUT_SECONDS)
        return (f"The program did not finish within "
                f"{PptCreatorConstants.EXEC_TIMEOUT_SECONDS}s and was killed. Make it "
                f"finish quickly and deterministically.")
    if finished.returncode != 0:
        tail = finished.stderr or finished.stdout or "exited non-zero with no output"
        log.error("run_slide_code: %s exited %d\n=== stderr ===\n%s",
                  script, finished.returncode,
                  tail[-PptCreatorConstants.MAX_STDERR_CHARS:])
        return ("The program raised. Fix it and resend the whole program.\n\n"
                "=== ERROR ===\n" + tail[-PptCreatorConstants.MAX_STDERR_CHARS:])
    if finished.stdout.strip():
        log.debug("run_slide_code: program stdout: %s", finished.stdout.strip()[:800])
    written = produced.exists() and (before is None
                                     or produced.stat().st_mtime >= before)
    if not written:
        log.warning("run_slide_code: %s exited 0 but wrote no slide at %s",
                    script, produced)
        return ("The program ran but wrote no slide. Save the deck with "
                "`deck.save(OUTPUT_PATH)`.")
    log.info("run_slide_code: wrote %s (%d bytes)",
             produced.name, produced.stat().st_size)
    return (f"The program ran and wrote {produced.name}. Call validate_slide to check "
            f"it before you consider the slide finished.")


def validate(slide_dir: Path, catalog, contract=None, layout_key: str = "") -> str:
    """The authoritative gate report for the file in this directory.

    Args:
        slide_dir: A directory already prepared by `stage()` and run in.
        catalog: The `TemplateCatalog`.
        contract: The deck design contract, for the frame this slide carries.
        layout_key: The assigned layout. Empty resolves it from the file itself.

    Returns:
        The formatted violation report, or the "passed" line.
    """
    produced = output_path(slide_dir)
    if not produced.is_file():
        log.warning("validate_slide: nothing to validate, %s does not exist", produced)
        return ("No slide was written. Save the deck to OUTPUT_PATH with "
                "`deck.save(OUTPUT_PATH)`.")
    layout = _layout_of(produced, catalog, layout_key)
    if layout is None:
        log.error("validate_slide: %s matches no template layout (assigned key %r); "
                  "the slide cannot be gated", produced, layout_key)
        return ("The produced file could not be matched to a template layout. Build "
                "the slide with `deck.add_slide(LAYOUT_KEY)` on the layout you were "
                "assigned.")
    log.info("validate_slide: gating %s on %s", produced.name, layout.key)
    frame = contract.frame_for(1, layout) if contract is not None else None
    violations = geo.check_slide(produced, layout, frame=frame,
                                 theme_colors=catalog.theme.colors,
                                 theme_font=catalog.theme.minor_font or "Barlow",
                                 slide_width=catalog.slide_width,
                                 slide_height=catalog.slide_height)
    log.info("validate_slide: %s -> %d violation(s), %s", produced.name,
             len(violations), "clean" if geo.is_clean(violations) else "blocked")
    return geo.format_violations(violations)


def _layout_of(produced: Path, catalog, layout_key: str = ""):
    """The `LayoutInfo` a produced slide sits on, by key or by its own layout name.

    Args:
        produced: The single-slide .pptx.
        catalog: The `TemplateCatalog`.
        layout_key: The assigned key; tried first.

    Returns:
        The layout, or None when neither the key nor the file resolves to one.
    """
    from pptx import Presentation

    if layout_key:
        try:
            return catalog.resolve_layout(layout_key)
        except LookupError:
            log.warning("assigned layout %r is not in the catalog; falling back to "
                        "the layout name in the produced file", layout_key)
    try:
        slides = list(Presentation(str(produced)).slides)
        if not slides:
            log.warning("%s has no slide, so no layout to resolve", produced)
            return None
        name = slides[0].slide_layout.name
        log.debug("resolving the produced slide's own layout name %r", name)
        return catalog.resolve_layout(name)
    except Exception as exc:                                 # noqa: BLE001
        # An unreadable file or a layout name from some other template: the caller
        # turns a None into an instruction, which is repairable. Raising is not.
        log.warning("could not resolve a layout for %s: %s", produced, exc,
                    exc_info=True)
        return None

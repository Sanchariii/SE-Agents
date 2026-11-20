"""Reference design mode: build the deck on the DESIGN of a PowerPoint the user uploaded.

The user uploads a presentation and asks for its design ("copy the design of the
uploaded PPT"). That deck then gives the new deck its look - its slide layouts,
backgrounds, artwork, colours and fonts - and nothing else:

1. ``claim_from_message`` (triage, start of the turn) records the uploaded deck as the
   session's design reference in the brief. From then on
   ``session_store.latest_docs`` leaves it out, so its text never reaches retrieval,
   planning or the slide writers.
2. ``prepare`` (designer node) downloads it once, strips every slide (only masters,
   layouts and the theme stay, so none of its wording can travel) and checks that it
   has layouts a slide can be built on.
3. The workflow builds the design contract, every slide and the stitched deck inside
   ``registry.using_template(path)``: the catalog, DeckBuilder, the sandbox and the
   stitcher all read the reference instead of the brand template, and every colour
   token resolves to the reference's theme. ``GUIDANCE`` tells the designer to copy
   the design faithfully.

The logo is the one part of the design that is never copied: ``prepare`` also runs
``reference_rebrand.rebrand``, which keeps Rockwell marks, swaps any other company's
logo for Rockwell's and adds Rockwell's to layouts that have none. The stamped frame
(classification label and slide number) stays on every slide, as on any deck the app
publishes.
"""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from app.config.constants import ReferenceDesignConstants as C
from app.genai.ppt.reference_rebrand import RebrandReport
from app.utilities.logger_util import get_logger

log = get_logger(__name__)


def _any(words) -> str:
    return "(?:" + "|".join(words) + ")"


_COPY_DESIGN = re.compile(r"\b" + _any(C.COPY_WORDS) + r"[\w\s'/,-]{0,%d}?\b"
                          % C.MAX_GAP_CHARS + _any(C.DESIGN_WORDS), re.I)
_REFERENCE = re.compile(r"\b" + _any(C.REFERENCE_WORDS), re.I)
_DROP = re.compile(C.DROP_PATTERN, re.I)


def asks_to_copy_design(message: str) -> bool:
    """True when the message asks for the design of an uploaded deck, e.g. "copy the
    design of the uploaded PPT" or "use the attached template's look and feel"."""
    text = message or ""
    return bool(_COPY_DESIGN.search(text) and _REFERENCE.search(text))


def asks_to_drop_design(message: str) -> bool:
    """True when the message asks to go back to the Rockwell template."""
    return bool(_DROP.search(message or ""))


def is_deck(record) -> bool:
    return str(getattr(record, "filename", "")).lower().endswith(C.EXTENSIONS)


def pick_reference(records) -> object | None:
    """The uploaded deck to copy the design of.

    Of the latest uploaded decks: the one whose file name says it is a reference
    ("template", "design", ...), else the most recently uploaded one.
    """
    decks = [r for r in records if getattr(r, "is_latest", True) and is_deck(r)
             and str(getattr(r, "status", "")).split(".")[-1] != "failed"]
    if not decks:
        return None
    named = [r for r in decks
             if any(hint in r.filename.lower() for hint in C.NAME_HINTS)]
    pool = named or decks
    return max(pool, key=lambda r: (str(getattr(r, "uploaded_at", "") or ""),
                                    getattr(r, "version", 0)))


def record_of(record) -> dict:
    return {"doc_id": record.doc_id, "version": record.version,
            "filename": record.filename, "sha256": record.sha256,
            "storage_path": record.storage_path}


def claim_from_message(session_id: str, message: str) -> str:
    """Record (or drop) the session's design reference from what the user asked.

    Runs at the start of triage, before anything reads the session's documents, so
    the claimed deck is already out of the content for the rest of the turn.

    Returns:
        A one-line note for the progress panel, or "" when nothing changed.
    """
    from app.core.handlers.session_store import session_store
    from app.genai.agents.brief import DeckBrief

    brief = DeckBrief.load(session_id)
    if brief.design_reference and asks_to_drop_design(message):
        name = brief.design_reference.get("filename", "the reference")
        brief.design_reference = {}
        brief.save(session_id)
        log.info("reference design dropped (%s); back to the brand template", name,
                 extra={"session_id": session_id})
        return f"Stopped copying the design of {name}; using the Rockwell template."
    if not asks_to_copy_design(message):
        return ""
    chosen = pick_reference(session_store.all_docs(session_id))
    if chosen is None:
        log.info("the user asked to copy a design but no PowerPoint is uploaded",
                 extra={"session_id": session_id})
        return ("You asked to copy a design, but no PowerPoint file is uploaded - "
                "upload the reference .pptx and ask again.")
    record = record_of(chosen)
    if (brief.design_reference.get("doc_id"), brief.design_reference.get("version")) == (
            record["doc_id"], record["version"]):
        return ""
    brief.design_reference = record
    brief.save(session_id)
    log.info("reference design claimed: %s v%s (sha %s); it is design only, never "
             "content", record["filename"], record["version"], record["sha256"][:12],
             extra={"session_id": session_id})
    return (f"Copying the design of {record['filename']} - its layouts, colours and "
            "fonts only; none of its text is used.")


@dataclass
class Prepared:
    """A reference deck ready to build on."""
    path: Path
    filename: str
    sha256: str
    #: What happened to the reference's logos.
    rebrand: RebrandReport = field(default_factory=RebrandReport)

    @property
    def cache_key(self) -> str:
        """Appended to every slide's cache key, so a slide built on another design is
        never reused."""
        return f"|ref:{self.sha256[:12]}:v{C.PREPARED_VERSION}"


def strip_slides(data: bytes, out_path: Path, suffix: str = ".pptx") -> None:
    """Save the deck with every slide removed: masters, layouts and theme only.

    Removing the slides is what guarantees none of the reference's wording or
    pictures can reach the new deck, and it keeps the template small.
    """
    from app.genai.ppt.template import loader

    if data[:2] != b"PK":
        raise ValueError("it is not a PowerPoint (.pptx) file")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    raw = out_path.with_name(out_path.stem + ".source" + suffix)
    raw.write_bytes(data)
    try:
        prs = loader.open_presentation(str(raw))       # .potx files open too
        loader.clear_slides(prs)
        partial = out_path.with_name(out_path.stem + ".part.pptx")
        prs.save(str(partial))
        partial.replace(out_path)
    finally:
        raw.unlink(missing_ok=True)


def rebrand_file(path: Path) -> RebrandReport:
    """Put Rockwell's logo on the prepared reference at ``path``, in place.

    The catalog is read first so a replacement mark can be the white lockup where the
    layout's background is dark.

    Raises:
        Whatever reading or saving the deck raises; ``prepare`` turns that into the
        brand-template fallback, because a reference that might still carry another
        company's logo must not be built on.
    """
    from app.genai.ppt import reference_rebrand
    from app.genai.ppt.template import catalog as catalog_mod, loader

    catalog = catalog_mod.load(str(path))

    def luminance_of(master_index: int, layout_index: int | None) -> float:
        if layout_index is not None:
            layout = catalog.get(f"M{master_index}-L{layout_index}")
            return layout.background_luminance if layout else 1.0
        values = [l.background_luminance for l in catalog.layouts
                  if l.master_index == master_index]
        return sum(values) / len(values) if values else 1.0

    def shade_of(master_index: int, shape) -> float | None:
        if not catalog.themes:
            return None
        theme = catalog.themes[min(master_index, len(catalog.themes) - 1)]
        return catalog_mod.shape_luminance(shape, theme)

    prs = loader.open_presentation(str(path))
    report = reference_rebrand.rebrand(prs, luminance_of, shade_of=shade_of)
    prs.save(str(path))
    return report


def problems(path: Path) -> list[str]:
    """Why the prepared deck cannot be built on ([] when it can)."""
    from app.genai.ppt.template import catalog as catalog_mod

    catalog = catalog_mod.load(str(path))
    found = []
    if min(catalog.slide_width, catalog.slide_height) < C.MIN_SLIDE_IN:
        found.append(f"its slides are only {catalog.slide_width:.1f} x "
                     f"{catalog.slide_height:.1f} in")
    if not any(layout.has_role("title") for layout in catalog.layouts):
        found.append("none of its layouts has a title placeholder")
    if not any(layout.has_role("content") or layout.canvas_capable
               for layout in catalog.layouts):
        found.append("none of its layouts has room for content")
    return found


def prepare(session_id: str, reference: dict, read_bytes=None,
            cache_dir: Path | None = None) -> tuple[Prepared | None, str]:
    """Download the session's reference deck and make it a template to build on.

    Args:
        session_id: For logs.
        reference: ``DeckBrief.design_reference``.
        read_bytes: Injected in tests; ``storage_util.read_file`` otherwise.
        cache_dir: Injected in tests; a folder under the system temp dir otherwise.

    Returns:
        (the prepared template or None, a note for the user - "" when all went well).
        Never raises: a reference that cannot be used costs the design, not the deck,
        which is then built on the brand template.
    """
    if not reference:
        return None, ""
    name = reference.get("filename") or "the reference deck"
    digest = reference.get("sha256") or ""
    folder = cache_dir or Path(tempfile.gettempdir()) / C.CACHE_DIRNAME
    try:
        target = folder / (f"{hashlib.sha1(digest.encode()).hexdigest()[:16]}"
                           f"-v{C.PREPARED_VERSION}.pptx")
        report_path = target.with_suffix(".json")
        if target.is_file():
            report = RebrandReport.from_dict(
                json.loads(report_path.read_text()) if report_path.is_file() else {})
        else:
            if read_bytes is None:
                from app.utilities import storage_util
                read_bytes = storage_util.read_file
            data = read_bytes(reference["storage_path"])
            if len(data) > C.MAX_BYTES:
                raise ValueError(f"it is larger than {C.MAX_BYTES // (1024 * 1024)} MB")
            actual = hashlib.sha256(data).hexdigest()
            if digest and actual != digest:
                raise ValueError("the stored file does not match the uploaded one")
            digest = digest or actual
            # Built under a work name and moved into place only when complete, so a
            # failure part-way never leaves a copy without Rockwell's logo in the cache.
            work = target.with_name(target.stem + ".work.pptx")
            try:
                strip_slides(data, work, Path(name).suffix.lower() or ".pptx")
                report = rebrand_file(work)
                report_path.write_text(json.dumps(report.to_dict()))
                work.replace(target)
            finally:
                work.unlink(missing_ok=True)
        found = problems(target)
        if found:
            raise ValueError("; ".join(found))
    except Exception as exc:                                 # noqa: BLE001
        log.warning("reference design %s cannot be used (%s); building on the brand "
                    "template", name, exc, exc_info=True,
                    extra={"session_id": session_id})
        return None, (f"The design of {name} could not be used ({exc}); the deck was "
                      "built on the Rockwell template instead.")
    log.info("reference design ready: %s -> %s", name, target.name,
             extra={"session_id": session_id})
    return Prepared(path=target, filename=name, sha256=digest, rebrand=report), ""


def with_guidance(params: dict) -> dict:
    """Add the reference-design direction to one slide's generation parameters,
    above the planner's instructions, without changing the input."""
    updated = dict(params)
    existing = updated.get("planner_special_instructions") or ""
    updated["planner_special_instructions"] = "\n\n".join(
        part for part in (C.GUIDANCE, existing) if part)
    return updated

"""Manual end-to-end probe for Document Intelligence figure-to-PPT embedding.

Run from the repository root:
python -m scripts.manual_testing.test_docintel_ppt_images --input "C:\path\source.pdf"

The script performs:
1. Document Intelligence extraction
2. Figure storage verification
3. Real-template picture-slot verification
4. PPT-agent handoff verification
5. PPTX build and embedded-media verification
"""

import argparse
import mimetypes
import sys
import tempfile
import zipfile
from pathlib import Path

from app.core.models.deck_model import ContentBlock, DeckDraft, SlideDraft
from app.core.models.document_model import DocumentRecord
from app.genai.agents.writer import repair_slide
from app.genai.ingestion.azure.doc_intelligence import DocIntelligenceExtractor
from app.genai.ppt.template import catalog as catalog_mod
from app.genai.ppt.template import registry
from app.utilities import storage_util as storage


SESSION_ID = "manual-docintel-ppt-image"
DOC_ID = "manual-figure-document"
DOCUMENT_VERSION = 1
DEFAULT_CLASSIFICATION = "internal"


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for the probe.

    Returns:
        Parsed input document, classification, and output PPTX path.
    """
    parser = argparse.ArgumentParser(
        description="Verify Document Intelligence figure extraction and PPT insertion."
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="A PDF, DOCX, PPTX, XLSX, PNG, JPEG, TIFF, or BMP source document.",
    )
    parser.add_argument(
        "--classification",
        default=DEFAULT_CLASSIFICATION,
        help="Template classification: internal, public, confidential, restricted, or cobranded.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/docintel-figure-probe.pptx"),
        help="Where the generated PPTX should be written.",
    )
    return parser.parse_args()


def make_record(source: Path) -> DocumentRecord:
    """Build a storage-shaped document record for Document Intelligence.

    Args:
        source: Existing document used as extractor input.

    Returns:
        Document metadata with a storage path matching normal uploads.

    Raises:
        ValueError: If the source MIME type cannot be determined.
    """
    mime_type, _ = mimetypes.guess_type(source.name)
    if mime_type is None:
        raise ValueError(f"Cannot determine MIME type for {source.name}")

    return DocumentRecord(
        session_id=SESSION_ID,
        doc_id=DOC_ID,
        version=DOCUMENT_VERSION,
        filename=source.name,
        mime_type=mime_type,
        sha256="manual-probe",
        storage_path=f"{SESSION_ID}/{DOC_ID}/v{DOCUMENT_VERSION}/{source.name}",
    )


def extract_first_figure(record: DocumentRecord, source_bytes: bytes) -> str:
    """Extract a document and return the first persisted figure storage path.

    Args:
        record: Metadata for the source document.
        source_bytes: Raw source-document bytes.

    Returns:
        The extracted figure's storage path.

    Raises:
        RuntimeError: If Document Intelligence finds no downloadable figures.
    """
    print("\nPHASE 1 - Extract figures with Document Intelligence")
    units = DocIntelligenceExtractor().extract(source_bytes, record)
    figures = [unit for unit in units if unit.kind == "image_description"]

    print(f"Extracted {len(units)} total unit(s), including {len(figures)} figure(s).")
    for figure in figures:
        print(
            f"  figure: section={figure.section!r}, "
            f"path={figure.meta.get('frame_path')!r}, "
            f"caption={figure.text[:100]!r}"
        )

    if not figures:
        raise RuntimeError(
            "No image_description units were produced. "
            "Use a source that contains a detected figure or screenshot."
        )

    figure_path = str(figures[0].meta.get("frame_path") or "")
    if not figure_path:
        raise RuntimeError("The figure unit has no meta['frame_path'].")

    return figure_path


def verify_stored_figure(figure_path: str) -> None:
    """Confirm that the extracted figure can be read from configured storage.

    Args:
        figure_path: Blob/local-storage path emitted by Document Intelligence.

    Raises:
        RuntimeError: If the figure was not persisted or is empty.
    """
    print("\nPHASE 2 - Verify the stored figure")
    figure_bytes = storage.read_file(figure_path)

    if not figure_bytes:
        raise RuntimeError(f"Stored figure is empty: {figure_path}")

    print(f"Stored figure is readable: {figure_path} ({len(figure_bytes)} bytes)")


def find_picture_layout(classification: str):
    """Find a real template layout that the catalog can use for images.

    Args:
        classification: Requested company-template classification.

    Returns:
        The catalog and one image-capable layout.

    Raises:
        RuntimeError: If the actual template exposes no recognized picture slot.
    """
    print("\nPHASE 3 - Inspect actual template picture slots")
    normalized = registry.normalize(classification)
    catalog = catalog_mod.for_classification(normalized)
    picture_layout = catalog.best_with_picture(text_slots_needed=1)

    layouts_with_images = [
        layout for layout in catalog.content_layouts if layout.picture_slots
    ]
    print(f"Template: {registry.template_path()}")
    print(f"Image-capable layouts found: {len(layouts_with_images)}")

    for layout in layouts_with_images:
        print(
            f"  {layout.name}: "
            f"{', '.join(slot.slot for slot in layout.picture_slots)}"
        )

    if picture_layout is None:
        raise RuntimeError(
            "The real template has no cataloged picture placeholder. "
            "This is a template-catalog issue: OBJECT placeholders are currently "
            "classified as body slots and cannot receive images."
        )

    print(f"Selected image layout: {picture_layout.name}")
    return catalog, normalized, picture_layout


def verify_agent_handoff(catalog, picture_layout, figure_path: str) -> SlideDraft:
    """Show whether repair_slide auto-selects extracted figure paths.

    Args:
        catalog: Parsed company-template catalog.
        picture_layout: Layout containing a picture slot.
        figure_path: Valid stored figure path from Document Intelligence.

    Returns:
        A repaired slide with an explicit image reference for the render test.
    """
    print("\nPHASE 4 - Verify PPT-agent image handoff")

    body_slot = picture_layout.text_slots[0]
    picture_slot = picture_layout.picture_slots[0]

    # This reflects the current live agent behavior: it knows a figure exists,
    # but it was never given the actual storage path to put into image_ref.
    hidden_asset_slide = SlideDraft(
        slide_number=1,
        layout=picture_layout.name,
        title="Figure handoff probe",
        blocks=[ContentBlock(slot=body_slot.slot, bullets=["Figure should appear here."])],
    )
    repaired_hidden_asset = repair_slide(
        hidden_asset_slide,
        catalog,
        keyframes=[figure_path],
    )
    hidden_asset_inserted = any(
        block.image_ref for block in repaired_hidden_asset.blocks
    )
    print(f"Implicit extracted-image selection: {hidden_asset_inserted}")

    if not hidden_asset_inserted:
        print(
            "  EXPECTED CURRENT FAILURE: repair_slide does not add an offered "
            "figure unless image_ref was already supplied by the agent."
        )

    # This proves whether renderer/builder can embed the same valid asset.
    explicit_asset_slide = SlideDraft(
        slide_number=1,
        layout=picture_layout.name,
        title="Figure embedding probe",
        blocks=[
            ContentBlock(slot=body_slot.slot, bullets=["Document Intelligence figure."]),
            ContentBlock(slot=picture_slot.slot, image_ref=figure_path),
        ],
    )
    repaired_explicit_asset = repair_slide(
        explicit_asset_slide,
        catalog,
        keyframes=[figure_path],
    )

    if not any(block.image_ref == figure_path for block in repaired_explicit_asset.blocks):
        raise RuntimeError(
            "repair_slide removed an explicit valid image_ref. "
            "The issue is before PPT rendering."
        )

    print(f"Explicit image reference retained: {figure_path}")
    return repaired_explicit_asset


def build_and_verify_pptx(
    classification: str,
    picture_layout,
    slide: SlideDraft,
    output_path: Path,
) -> None:
    """Build a real PPTX and verify that its ZIP package contains image media.

    Args:
        classification: Template classification used to build the presentation.
        picture_layout: Image-capable layout selected from the real template.
        slide: Repaired slide containing an explicit image reference.
        output_path: Persistent destination for the generated presentation.

    Raises:
        RuntimeError: If the resulting PPTX has no embedded media item.
    """
    print("\nPHASE 5 - Build PPTX and inspect embedded media")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    from app.core.models.deck_model import DeckContext, SlideCodeBrief
    from app.genai.agents.ppt_creator_agent import PptCreatorAgent

    brief = SlideCodeBrief(
        slide_number=slide.slide_number, kind=slide.kind, content=slide,
        context=DeckContext(title="Document Intelligence Figure Probe",
                            position=1, total=1),
        classification=classification)

    with tempfile.TemporaryDirectory(prefix="docintel_ppt_probe_") as temp_dir:
        PptCreatorAgent("manual-docintel", Path(temp_dir))._render_on_layout(
            brief, output_path)

    with zipfile.ZipFile(output_path) as package:
        media_files = [
            name for name in package.namelist() if name.startswith("ppt/media/")
        ]

    print(f"Generated: {output_path.resolve()}")
    print(f"Embedded media files: {media_files}")

    if not media_files:
        raise RuntimeError(
            "The PPTX was created but contains no ppt/media entries. "
            "Check builder materialization logs and template placeholder handling."
        )


def main() -> int:
    """Run the phased Document Intelligence-to-PPT image diagnostic.

    Returns:
        Zero when every phase succeeds; one after the first diagnostic failure.
    """
    args = parse_args()
    source = args.input.resolve()

    if not source.is_file():
        print(f"ERROR: input file does not exist: {source}", file=sys.stderr)
        return 1

    try:
        record = make_record(source)
        figure_path = extract_first_figure(record, source.read_bytes())
        verify_stored_figure(figure_path)

        catalog, classification, picture_layout = find_picture_layout(
            args.classification
        )
        explicit_image_slide = verify_agent_handoff(
            catalog,
            picture_layout,
            figure_path,
        )
        build_and_verify_pptx(
            classification,
            picture_layout,
            explicit_image_slide,
            args.output,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"\nFAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print("\nPASSED: extraction, storage, template slot, and PPT embedding succeeded.")
    print(
        "If Phase 4 reported the expected current failure, the remaining fix is "
        "to pass extracted figure paths into the PPT-agent write/repair flow."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
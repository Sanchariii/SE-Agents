"""Azure Document Intelligence (prebuilt-layout) extractor: text + tables + figures.

Emits text/table units in reading order and, for each detected figure, downloads
the cropped image from the service, stores it in blob, captions it with AOAI
vision, and emits an image_description unit whose meta carries the blob path so the
deck builder can place the real figure on a slide (same channel as video keyframes).
"""

import base64

from app.config.constants import AzureConstants, IngestionConstants
from app.config.settings import get_settings
from app.core.models.document_model import DocumentRecord, ExtractedUnit
from app.utilities import storage_util as storage
from app.utilities.clients_util import  get_doc_intelligence_client
from app.utilities.apim_llm_utils import get_apim_llm_client
from app.config.constants import IngestionConstants, ModelConstants
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

# prebuilt-layout handles these; legacy binary .doc/.ppt are not supported.
_MIME_TYPES = (
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "image/png", "image/jpeg", "image/tiff", "image/bmp",
)

_CAPTION_PROMPT = (
    "Describe this figure from a document for a training knowledge base: "
    "transcribe any text/labels verbatim and explain what the diagram, chart, "
    "or screenshot conveys. Be concise (under 120 words).")


def _base_dir(record: DocumentRecord) -> str:
    return record.storage_path.rsplit("/", 1)[0]


def _caption(image_bytes: bytes, mime: str = "image/png") -> str:
    log.info(f"captioning figure in document intelligence {len(image_bytes)} bytes", extra={"size": len(image_bytes), "mime": mime})
    b64 = base64.b64encode(image_bytes).decode()
    client = get_apim_llm_client()

    resp = client.chat_completion(
        deployment=ModelConstants.DEFAULT,
        max_tokens=IngestionConstants.IMAGE_DESCRIPTION_MAX_TOKENS,
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "Describe this image for a training deck, "
                                        "including any visible text, labels, and diagrams."},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ]}] )

    text = client.reply_text(resp).strip()
    return text


def _table_to_text(table) -> str:
    log.info(f"converting table to text {table.row_count}x{table.column_count}", extra={"rows": table.row_count, "cols": table.column_count})
    grid: dict[int, dict[int, str]] = {}
    for cell in table.cells:
        grid.setdefault(cell.row_index, {})[cell.column_index] = (cell.content or "").strip()
    lines = [" | ".join(grid[r].get(c, "") for c in range(table.column_count))
             for r in sorted(grid)]
    return "\n".join(line for line in lines if line.strip(" |"))


class DocIntelligenceExtractor:
    name = "doc_intelligence_extractor"
    mime_types = _MIME_TYPES

    def extract(self, data: bytes, record: DocumentRecord) -> list[ExtractedUnit]:
        # Lazy import: SDK is optional until this extractor is actually enabled.
        from azure.ai.documentintelligence.models import (
            AnalyzeDocumentRequest, AnalyzeOutputOption,
        )
        client = get_doc_intelligence_client()

        log.info(f"analyzing document with Azure Document Intelligence {len(data)} bytes", extra={"size": len(data), "mime": record.mime_type})

        poller = client.begin_analyze_document(
            AzureConstants.DOC_INTELLIGENCE_MODEL,
            AnalyzeDocumentRequest(bytes_source=data),
            output=[AnalyzeOutputOption.FIGURES],
        )
        result = poller.result()
        result_id = (poller.details or {}).get("operation_id")

        units: list[ExtractedUnit] = []

        # --- Text: paragraphs in reading order, grouped by section heading ---
        section = record.filename
        buf: list[str] = []

        def flush() -> None:
            if buf:
                units.append(ExtractedUnit(text="\n".join(buf), section=section))
                buf.clear()

        for para in (result.paragraphs or []):
            text = (para.content or "").strip()
            if not text:
                continue
            if getattr(para, "role", None) in ("title", "sectionHeading"):
                flush()
                section = text
            else:
                buf.append(text)
        flush()

        if not units and (result.content or "").strip():
            units.append(ExtractedUnit(text=result.content.strip(), section=record.filename))

        # --- Tables ---
        for i, table in enumerate(result.tables or [], start=1):
            page = table.bounding_regions[0].page_number if table.bounding_regions else None
            table_text = _table_to_text(table)
            if table_text:
                units.append(ExtractedUnit(text=table_text, section=f"table {i}",
                                           page=page, kind="table"))

        # --- An image upload: the whole file is the figure ---
        # Its figure crops would be near-duplicates of the same photo, so they are skipped.
        is_image = (record.mime_type or "").startswith("image/")
        if is_image:
            caption = ""
            if AzureConstants.DOC_INTELLIGENCE_CAPTION_FIGURES:
                try:
                    caption = _caption(data, record.mime_type)
                except Exception as exc:        # noqa: BLE001
                    log.warning("image upload %s caption failed: %s", record.filename, exc)
            units.append(ExtractedUnit(
                text=caption or f"Image {record.filename}",
                section=record.filename, kind="image_description",
                meta={"frame_path": record.storage_path}))
            log.info("image upload %s kept as one placeable image; figure crops skipped",
                     record.filename)

        # --- Figures: download crop -> blob -> caption -> unit ---
        if result_id and not is_image:
            for i, figure in enumerate(result.figures or [], start=1):
                fid = getattr(figure, "id", None)
                if not fid:
                    continue
                page = (figure.bounding_regions[0].page_number
                        if figure.bounding_regions else None)
                try:
                    stream = client.get_analyze_result_figure(
                        model_id=AzureConstants.DOC_INTELLIGENCE_MODEL,
                        result_id=result_id, figure_id=fid)
                    img_bytes = b"".join(stream)
                except Exception as exc:            
                    log.warning("figure %s fetch failed: %s", fid, exc)
                    continue
                rel = f"{_base_dir(record)}/figures/figure_{i:03d}.png"
                storage.save_bytes(rel, img_bytes)
                caption = ""
                if AzureConstants.DOC_INTELLIGENCE_CAPTION_FIGURES:
                    try:
                        caption = _caption(img_bytes)
                    except Exception as exc:        
                        log.warning("figure %s caption failed: %s", fid, exc)
                units.append(ExtractedUnit(
                    text=caption or f"Figure {i} (page {page})",
                    section=f"figure {i}", page=page, kind="image_description",
                    meta={"frame_path": rel, "figure_id": fid, "page": page}))

        log.info(f"doc-intelligence extracted {len(units)} units", extra={
            "doc_id": record.doc_id,
            "text_units": sum(1 for u in units if u.kind == "text"),
            "tables": sum(1 for u in units if u.kind == "table"),
            "figures": sum(1 for u in units if u.kind == "image_description")})
        return units
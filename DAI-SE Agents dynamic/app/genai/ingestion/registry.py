"""MIME -> extractor routing.

Three tiers, resolved once at import and highest tier wins its MIME types:

1. ``extractors/``  deterministic parsers (text, csv, pdf, docx, xlsx, pptx) — always on.
2. ``local/``       processing we run ourselves because no managed service is wired up
                    yet (today: video → frames + transcript). See ``local/README.md``.
3. ``azure/``       managed Azure services (today: Document Intelligence).

Moving a workload from local to Azure means adding the extractor under ``azure/``
and listing it in ``_AZURE_TIER`` — no other module needs to change.
"""

from app.config.settings import get_settings
from app.genai.ingestion.base import ExtractorTool
from app.genai.ingestion.extractors.documents import (
    CsvExtractor, DocxExtractor, PdfExtractor, PptxExtractor, TextExtractor, XlsxExtractor,
)
from app.utilities.logger_util import get_logger

log = get_logger(__name__)


def _deterministic_tier() -> list[ExtractorTool]:
    return [TextExtractor(), PdfExtractor(), DocxExtractor(),
            PptxExtractor(), XlsxExtractor(), CsvExtractor()]


def _local_tier() -> list[ExtractorTool]:
    """Locally processed media. Needs AOAI vision (images) and ffmpeg/OpenCV (video)."""
    try:
        from app.genai.ingestion.extractors.media import ImageExtractor, VideoExtractor
        return [ImageExtractor(), VideoExtractor()]
    except Exception as exc:                        # noqa: BLE001
        log.warning("local media extractors unavailable: %s", exc)
        return []


def _azure_tier() -> list[ExtractorTool]:
    """Managed Azure services. Each entry overrides the tiers below for its MIME types."""
    tools: list[ExtractorTool] = []
    s = get_settings()
    if s.use_doc_intelligence and s.azure_document_intelligence_endpoint:
        try:
            from app.genai.ingestion.azure.doc_intelligence import DocIntelligenceExtractor
            tools.append(DocIntelligenceExtractor())
        except Exception as exc:                    # noqa: BLE001
            log.warning("doc-intelligence extractor unavailable: %s", exc)
    if s.use_video_indexer and s.video_indexer_account_id:
        try:
            from app.genai.ingestion.azure.video_indexer import VideoIndexerExtractor
            tools.append(VideoIndexerExtractor())
        except Exception as exc:                    # noqa: BLE001
            log.warning("video-indexer extractor unavailable: %s", exc)
    return tools


def _build_routes() -> dict[str, ExtractorTool]:
    routes: dict[str, ExtractorTool] = {}
    for tool in (*_deterministic_tier(), *_local_tier(), *_azure_tier()):
        for mime in tool.mime_types:
            routes[mime] = tool
    return routes


_BY_MIME: dict[str, ExtractorTool] | None = None


def _routes() -> dict[str, ExtractorTool]:
    global _BY_MIME
    if _BY_MIME is None:
        _BY_MIME = _build_routes()
        log.info("extraction routes ready", extra={"mime_types": len(_BY_MIME)})
    return _BY_MIME


def get_extractor(mime_type: str) -> ExtractorTool:
    tool = _routes().get(mime_type)
    if tool is None:
        raise ValueError(f"No extractor for MIME type: {mime_type}. "
                         f"Supported: {', '.join(sorted(_routes()))}")
    return tool


def supported_mime_types() -> list[str]:
    return sorted(_routes())

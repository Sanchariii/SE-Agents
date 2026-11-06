"""Extractor tool contract: bytes in -> list[ExtractedUnit] out (MIME-routed)."""

from typing import Protocol
from app.core.models.document_model import DocumentRecord, ExtractedUnit


class ExtractorTool(Protocol):
    name: str
    mime_types: tuple[str, ...]

    def extract(self, data: bytes, record: DocumentRecord) -> list[ExtractedUnit]: ...
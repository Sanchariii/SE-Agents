"""Build the Canonical Knowledge Model (index-ready chunks) from units."""

from app.config.constants import IngestionConstants
from app.core.models.document_model import CkM, CkMChunk, DocumentRecord, ExtractedUnit


def _split_long(text: str) -> list[str]:

    if len(text) <= IngestionConstants.MAX_CHUNK_CHARS:
        return [text]
    parts, current = [], ""
    
    for para in text.split("\n"):
        candidate = f"{current}\n{para}".strip() if current else para
        if len(candidate) > IngestionConstants.MAX_CHUNK_CHARS and current:
            parts.append(current); current = para
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def build_ckm(record: DocumentRecord, units: list[ExtractedUnit]) -> CkM:
    chunks: list[CkMChunk] = []
    counter = 0
    buffer: ExtractedUnit | None = None

    def emit(unit: ExtractedUnit) -> None:
        nonlocal counter
        for piece in _split_long(unit.text):
            counter += 1
            chunks.append(CkMChunk(
                chunk_id=f"{record.doc_id}-v{record.version}-{counter:04d}",
                doc_id=record.doc_id, version=record.version, section=unit.section,
                page=unit.page, kind=unit.kind, text=piece, meta=unit.meta))

    for unit in units:
        if buffer is not None:
            if (buffer.kind == unit.kind == "text" and not buffer.meta
                    and len(buffer.text) < IngestionConstants.MIN_CHUNK_CHARS):
                buffer = ExtractedUnit(text=f"{buffer.text}\n{unit.text}",
                                       section=buffer.section or unit.section,
                                       page=buffer.page, kind=buffer.kind)
                continue
            emit(buffer)
        buffer = unit
    if buffer is not None:
        emit(buffer)

    return CkM(doc_id=record.doc_id, version=record.version, filename=record.filename,
               mime_type=record.mime_type, chunks=chunks)
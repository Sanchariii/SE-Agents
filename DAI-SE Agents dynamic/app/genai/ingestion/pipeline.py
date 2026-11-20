"""storage_path -> extractor -> CkM -> summary -> index. Parallel per document."""

import asyncio
import json
import time

from app.config.constants import (
    AnalyticsStatus, EventTypes, GuardrailConstants, IngestionConstants,
)
from app.core.handlers.session_store import session_store
from app.core.models.document_model import CkM, DocStatus, DocumentRecord
from app.genai.ingestion.chunking import build_ckm
from app.genai.ingestion.registry import get_extractor
from app.genai.ingestion.summarizer import summarize
from app.genai.guardrails import GuardrailViolation, check_content
from app.genai.retrieval import indexer
from app.utilities import events_util as events, storage_util as storage
from app.utilities.analytics import ingestion_run
from app.utilities.logger_util import get_logger

log = get_logger(__name__)


def _progress(record: DocumentRecord, message: str, **data) -> None:
    """Stream ingestion state on the session's event bus (watch via /stream)."""
    events.publish(record.session_id, "ingest_step", agent="ingestion",
                   message=message, filename=record.filename,
                   doc_id=record.doc_id, version=record.version,
                   status=record.status.value, **data)


def process_document(record: DocumentRecord) -> DocumentRecord:
    record.status = DocStatus.extracting
    session_store.update_doc(record)
    _progress(record, f"Reading {record.filename}…")
    log.info("ingestion started for %s (doc_id=%s, mime=%s, storage_path=%s)",
             record.filename, record.doc_id, record.mime_type, record.storage_path)
    with ingestion_run(record.session_id, document_id=record.doc_id) as span:
        try:
            log.info("downloading %s from blob storage", record.filename)
            data = storage.read_file(record.storage_path)
            span.bytes_processed = len(data)
            log.info("downloaded %s (%.1f MB); running extractor",
                     record.filename, len(data) / (1024 * 1024))
            units = get_extractor(record.mime_type).extract(data, record)
            _progress(record, f"{record.filename}: parsed {len(units)} section(s)")
            ckm: CkM = build_ckm(record, units)
            span.chunks_created = len(ckm.chunks)
            log.info("screening extracted source for %s (%d chunks)",
                     record.filename, len(ckm.chunks))
            for offset in range(0, len(ckm.chunks), GuardrailConstants.SOURCE_BATCH_CHUNKS):
                check_content(record.session_id, json.dumps({
                    "filename": ckm.filename, "mime_type": ckm.mime_type,
                    "chunks": [chunk.model_dump() for chunk in ckm.chunks[
                        offset:offset + GuardrailConstants.SOURCE_BATCH_CHUNKS]],
                }), "sources")
            _progress(record, f"{record.filename}: {len(ckm.chunks)} chunk(s), summarizing…",
                      chunks=len(ckm.chunks))
            ckm.summary = summarize(ckm)
            session_store.save_ckm(ckm)
            record.status = DocStatus.extracted
            if indexer.search_configured():
                _progress(record, f"{record.filename}: indexing {len(ckm.chunks)} chunk(s)…",
                          chunks=len(ckm.chunks))
                indexer.index_ckm(record.session_id, ckm)
                record.status = DocStatus.indexed
            else:
                log.warning("AI Search not configured; skipping indexing")
            log.info("ingestion complete for %s: status=%s, chunks=%s",
                     record.filename, record.status.value, len(ckm.chunks))
            events.publish(record.session_id, "ingest_done", agent="ingestion",
                           message=f"{record.filename}: {record.status.value}",
                           filename=record.filename, doc_id=record.doc_id,
                           version=record.version, status=record.status.value,
                           chunks=len(ckm.chunks))
        except Exception as exc:                       # noqa: BLE001
            record.status = DocStatus.failed
            record.error = str(exc)
            span.status = AnalyticsStatus.FAILED
            if isinstance(exc, GuardrailViolation):
                events.publish(record.session_id, EventTypes.GUARDRAIL, agent="ingestion",
                               code=exc.outcome, phase=exc.phase, message=exc.detail,
                               doc_id=record.doc_id, version=record.version)
            log.error("extraction failed for %s (storage_path=%s): %s",
                      record.filename, record.storage_path, exc, exc_info=True)
            events.publish(record.session_id, "ingest_error", agent="ingestion",
                           message=f"{record.filename}: failed — {exc}",
                           filename=record.filename, status=record.status.value)
    session_store.update_doc(record)
    return record


async def ingest_documents(records: list[DocumentRecord]) -> list[DocumentRecord]:
    log.info("ingest_documents: starting parallel ingestion for %s doc(s): %s",
             len(records), [r.filename for r in records])
    results = list(await asyncio.gather(
        *(asyncio.to_thread(process_document, r) for r in records)))
    log.info("ingest_documents: all %s doc(s) finished", len(results))
    return results


_DONE_STATES = {DocStatus.indexed, DocStatus.extracted}


def ensure_session_indexed(session_id: str) -> dict:
    """Block until every latest document is indexed/extracted before planning.

    Waits out ingestion already running in the background (status 'extracting'),
    emitting a single 'in process' event, then extracts anything the background
    run never finished ('uploaded'/'failed') in this thread.
    """
    docs = session_store.latest_docs(session_id)
    pending = [d for d in docs
               if d.status not in _DONE_STATES and d.status != DocStatus.upload_in_progress]
    if not pending:
        return {"waited": False, "indexed": len(docs), "processed": 0, "failed": []}

    events.publish(session_id, "agent_step", agent="extraction",
                   message="Ingestion/indexing in process — waiting for all documents "
                           "to be indexed before planning…",
                   pending=[d.filename for d in pending])

    deadline = time.monotonic() + IngestionConstants.WAIT_TIMEOUT_SECONDS
    while any(d.status == DocStatus.extracting for d in session_store.latest_docs(session_id)):
        if time.monotonic() >= deadline:
            log.warning("ingestion wait timed out; proceeding", extra={"session_id": session_id})
            break
        time.sleep(IngestionConstants.POLL_INTERVAL_SECONDS)

    # Extract whatever the background run never completed. Leave a doc still
    # 'extracting' (timed out) to its own thread, and skip 'upload_in_progress' —
    # the client hasn't finished its direct-to-blob upload, so there's no blob to read.
    processed = 0
    for d in session_store.latest_docs(session_id):
        if d.status in _DONE_STATES or d.status in (DocStatus.extracting,
                                                    DocStatus.upload_in_progress):
            continue
        process_document(d)
        processed += 1

    final = session_store.latest_docs(session_id)
    indexed = sum(1 for d in final if d.status in _DONE_STATES)
    failed = [d.filename for d in final if d.status == DocStatus.failed]
    events.publish(session_id, "agent_step", agent="extraction",
                   message=f"Documents ready: {indexed}/{len(final)} indexed"
                           + (f"; failed: {', '.join(failed)}" if failed else "") + ".",
                   indexed=indexed, total=len(final), failed=failed)
    return {"waited": True, "indexed": indexed, "processed": processed, "failed": failed}
"""Uploads: validate, persist to storage, register in the session, kick off ingestion.

Two upload paths land in the same place:
- classic multipart: the API receives the bytes, saves them, then ingests;
- direct-to-blob (presigned): the client uploads straight to Blob via a SAS URL and
  the API only registers then verifies — this bypasses the ingress body-size limit
  that rejects large files (e.g. video) with HTTP 413.
"""

from datetime import datetime, timedelta, timezone

from app.config.constants import StorageConstants
from app.config.exceptions import AppError, NotFoundError, UnsupportedMediaTypeError
from app.core.handlers.session_store import session_store
from app.core.models.document_model import (
    DocStatus, DocumentRecord, UploadCompleteItem, UploadRegisterItem, UploadTicket,
)
from app.genai.ingestion.pipeline import ingest_documents
from app.genai.ingestion.registry import supported_mime_types
from app.utilities import events_util as events, storage_util as storage
from app.utilities.files_util import detect_mime, sha256_bytes
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

# Statuses past a successful upload — a repeat 'complete' must not re-ingest these.
_PROCESSED_STATES = {DocStatus.extracting, DocStatus.extracted, DocStatus.indexed}


def validate_upload(filename: str, data: bytes) -> str:
    """Reject empty, oversized and unsupported files before anything is persisted."""
    if not data:
        raise AppError(f"Empty file: {filename}")
    limit = StorageConstants.MAX_UPLOAD_BYTES
    if len(data) > limit:
        raise AppError(f"{filename} exceeds the {limit // (1024 * 1024)} MB upload limit")
    try:
        return detect_mime(filename)
    except ValueError as exc:
        raise UnsupportedMediaTypeError(str(exc), supported_mime_types()) from exc



def register_upload(session_id: str, filename: str, mime: str, data: bytes) -> DocumentRecord:
    """Persist an already-validated upload; storage/registration errors bubble up."""
    record = session_store.register_upload(
        session_id=session_id, filename=filename, mime_type=mime,
        sha256=sha256_bytes(data), storage_path="pending")
    record.storage_path = storage.save_upload(
        session_id, record.doc_id, record.version, filename, data)
    session_store.update_doc(record)
    return record


def validate_registration(filename: str, size: int | None) -> str:
    """Boundary check for a presigned upload, before any bytes exist: the extension
    must map to a supported MIME type and any client-declared size must be sane.

    Args:
        filename: original client filename (drives extension-based MIME detection).
        size: client-declared byte count, or None if unknown.

    Returns:
        The detected MIME type.

    Raises:
        AppError: empty or oversized declared size.
        UnsupportedMediaTypeError: unknown/unsupported extension.
    """
    if size is not None:
        if size <= 0:
            raise AppError(f"Empty file: {filename}")
        limit = StorageConstants.MAX_UPLOAD_BYTES
        if size > limit:
            raise AppError(f"{filename} exceeds the {limit // (1024 * 1024)} MB upload limit")
    try:
        return detect_mime(filename)
    except ValueError as exc:
        raise UnsupportedMediaTypeError(str(exc), supported_mime_types()) from exc


def register_direct_upload(session_id: str, item: UploadRegisterItem) -> UploadTicket:
    """Register a to-be-uploaded file and hand back a presigned URL for it.

    The record is created up front in status ``upload_in_progress`` and only flips to
    ``uploaded`` once the client finishes and calls complete — so nothing is ever
    orphaned: an abandoned upload is exactly a record still ``upload_in_progress``.

    Args:
        session_id: owning session.
        item: filename plus an optional declared size.

    Returns:
        An UploadTicket carrying the doc id/version and the write SAS URL.

    Raises:
        AppError: invalid size, or Blob presigning isn't available in this environment.
        UnsupportedMediaTypeError: unsupported file type.
    """
    mime = validate_registration(item.filename, item.size)
    # Reserve the doc id + version first (versions by filename, same as multipart).
    record = session_store.register_upload(
        session_id=session_id, filename=item.filename, mime_type=mime,
        sha256="", storage_path="pending")
    record.storage_path = storage.upload_blob_path(
        session_id, record.doc_id, record.version, item.filename)
    record.status = DocStatus.upload_in_progress
    session_store.update_doc(record)

    ttl = StorageConstants.UPLOAD_SAS_TTL_MINUTES
    upload_url = storage.upload_sas_url(record.storage_path, expiry_minutes=ttl)
    if not upload_url:
        # No Blob / no signing key => we cannot offer a direct upload. Fail loudly so
        # the client falls back to the multipart endpoint instead of silently hanging.
        record.status = DocStatus.failed
        record.error = "direct-to-blob upload unavailable"
        session_store.update_doc(record)
        log.error("presign failed for %s in session %s (Blob not configured/no key)",
                  item.filename, session_id)
        raise AppError("Direct-to-blob upload is not available in this environment")

    log.info("registered direct upload %s v%s (%s) in session %s",
             record.doc_id, record.version, item.filename, session_id)
    return UploadTicket(
        doc_id=record.doc_id, version=record.version, filename=record.filename,
        storage_path=record.storage_path, upload_url=upload_url, expires_in=ttl * 60)


def _verify_and_mark_uploaded(record: DocumentRecord) -> DocumentRecord:
    """Confirm a presigned upload landed in Blob and flip the record to ``uploaded``.

    Streams the same ``ingest_step``/``ingest_error`` events the multipart path emits, so
    the client sees the verify → start-ingestion handoff live on the session stream.

    Idempotent: a file already being/having been ingested is returned untouched; the
    actual blob size is enforced here (not the client's declared size) so an oversized
    or empty upload is rejected and its blob removed.
    """
    if record.status in _PROCESSED_STATES:
        return record                              # already past upload; nothing to redo

    # Announce receipt immediately so the stream lights up the moment 'complete' lands.
    events.publish(record.session_id, "ingest_step", agent="ingestion",
                   message=f"Upload received: {record.filename}. Verifying in storage…",
                   filename=record.filename, doc_id=record.doc_id,
                   version=record.version, status=record.status.value)

    size = storage.blob_size(record.storage_path)
    if size is None:
        events.publish(record.session_id, "ingest_error", agent="ingestion",
                       message=f"{record.filename}: not found in storage — the upload "
                               f"did not complete. Please re-upload.",
                       filename=record.filename, status=DocStatus.failed.value)
        raise NotFoundError(f"{record.filename}: no uploaded file found in storage")
    limit = StorageConstants.MAX_UPLOAD_BYTES
    if size == 0 or size > limit:
        storage.delete_file(record.storage_path)   # drop the bad blob so it can't linger
        record.status = DocStatus.failed
        record.error = ("uploaded file is empty" if size == 0
                        else f"exceeds the {limit // (1024 * 1024)} MB upload limit")
        session_store.update_doc(record)
        events.publish(record.session_id, "ingest_error", agent="ingestion",
                       message=f"{record.filename}: {record.error}.",
                       filename=record.filename, status=DocStatus.failed.value)
        raise AppError(f"{record.filename}: {record.error}")

    record.status = DocStatus.uploaded
    session_store.update_doc(record)
    events.publish(record.session_id, "ingest_step", agent="ingestion",
                   message=f"{record.filename} verified in storage; starting ingestion…",
                   filename=record.filename, doc_id=record.doc_id,
                   version=record.version, status=record.status.value)
    log.info("verified direct upload %s v%s (%s bytes) in session %s",
             record.doc_id, record.version, size, record.session_id)
    return record


def complete_uploads(session_id: str, items: list[UploadCompleteItem]) -> list[DocumentRecord]:
    """Finish presigned uploads: verify each blob exists and mark it uploaded.

    Args:
        session_id: owning session.
        items: doc id/version pairs the client finished uploading.

    Returns:
        The verified DocumentRecords (status ``uploaded``, or unchanged if already
        being ingested). The caller ingests those now in ``uploaded`` status.

    Raises:
        NotFoundError: a referenced upload was never registered or never landed.
        AppError: an uploaded blob is empty or oversized.
    """
    records: list[DocumentRecord] = []
    for item in items:
        record = session_store.get_doc(session_id, item.doc_id, item.version)
        if record is None:
            raise NotFoundError(f"upload not registered: {item.doc_id} v{item.version}")
        records.append(_verify_and_mark_uploaded(record))
    return records


def cleanup_abandoned_uploads(session_id: str,
                              older_than_minutes: int = StorageConstants.UPLOAD_SAS_TTL_MINUTES
                              ) -> list[str]:
    """Remove uploads that were registered but never completed (the SAS expired without
    a finish call). Because every direct upload is registered as ``upload_in_progress``
    before the URL is handed out, an abandoned one is simply a stale record in that
    status — delete its (possibly partial) blob and drop the record.

    Args:
        session_id: session to sweep.
        older_than_minutes: only reap records registered before this age.

    Returns:
        Human-readable ids of the removed uploads.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)
    removed: list[str] = []
    for r in session_store.all_docs(session_id):
        if r.status == DocStatus.upload_in_progress and r.uploaded_at < cutoff:
            storage.delete_file(r.storage_path)
            session_store.delete_doc(session_id, r.doc_id, r.version)
            removed.append(f"{r.filename} ({r.doc_id} v{r.version})")
    if removed:
        log.info("cleaned up %s abandoned upload(s) in session %s: %s",
                 len(removed), session_id, ", ".join(removed))
    return removed


def abort_upload(session_id: str, doc_id: str, version: int | None = None) -> list[int]:
    """Clean up a failed direct-to-blob upload on the client's say-so: delete the
    (possibly partial) blob and drop the registry record, so nothing is left
    half-registered or orphaned in storage.

    Only ``upload_in_progress`` records are abortable — a document that already
    finished uploading and began ingestion must go through delete_document (which also
    purges the search index). This makes the endpoint safe to call blindly on any
    upload error without risking a completed document.

    Args:
        session_id: owning session.
        doc_id: the document whose upload failed.
        version: a specific version, or None to abort every in-progress version.

    Returns:
        The versions that were removed (empty when nothing matched).
    """
    targets = [d for d in session_store.all_docs(session_id)
               if d.doc_id == doc_id and d.status == DocStatus.upload_in_progress
               and (version is None or d.version == version)]
    removed: list[int] = []
    for d in targets:
        session_store.delete_doc(session_id, d.doc_id, d.version)
        storage.delete_file(d.storage_path)            # idempotent: fine if the blob never landed
        removed.append(d.version)
    if removed:
        log.info("aborted failed upload %s v%s in session %s", doc_id, removed, session_id)
    return removed


def sweep_abandoned_uploads() -> int:
    """Reap abandoned uploads across every session — the body of the scheduled job.

    Enumerates sessions from the registry (cheap Postgres read) and runs the per-session
    cleanup for each, isolating failures so one bad session can't stop the sweep.

    Returns:
        Total number of abandoned uploads removed this pass.
    """
    from app.core.handlers.session_registry_handler import session_registry_handler
    total = 0
    for session_id in session_registry_handler.list_all_sessions():
        try:
            total += len(cleanup_abandoned_uploads(session_id))
        except Exception as exc:                       # noqa: BLE001
            log.warning("sweep: cleanup failed for session %s: %s", session_id, exc)
    if total:
        log.info("sweep: reaped %s abandoned upload(s) across all sessions", total)
    else:
        log.info("sweep: no abandoned uploads found across all sessions")
    return total




async def process_uploads(records: list[DocumentRecord]) -> list[DocumentRecord]:
    return await ingest_documents(records)


def build_response(session_id: str, processed: list[DocumentRecord]) -> dict:
    return {
        "session_id": session_id,
        "documents": [
            {"doc_id": r.doc_id, "version": r.version, "filename": r.filename,
             "status": r.status.value, "error": r.error,
             "chunks": len(c.chunks) if (c := session_store.get_ckm(r.doc_id, r.version)) else 0,
             "summary": c.summary if c else None}
            for r in processed],
    }


def delete_document(session_id: str, doc_id: str, version: int | None = None) -> list[int]:
    """Remove a document everywhere: session state, blob storage, and the search index."""
    removed = session_store.delete_doc(session_id, doc_id, version)
    if not removed:
        return []
    for record in removed:
        storage.delete_file(record.storage_path)      # uploads container is the default
    from app.genai.retrieval.indexer import purge_document, search_configured
    if search_configured():
        purge_document(session_id, doc_id)
    return [r.version for r in removed]


def delete_session(session_id: str) -> dict:
    """Full cascade delete: Redis state, uploads, decks, Postgres rows, Search chunks."""
    from app.core.handlers.chat_message_handler import chat_message_handler
    from app.core.handlers.document_registry_handler import document_registry_handler
    from app.core.handlers.deck_version_handler import deck_version_handler
    from app.core.handlers.workflow_event_handler import workflow_event_handler
    from app.genai.retrieval.indexer import purge_session, search_configured
    from app.utilities import events_util as events

    state = session_store.delete_session(session_id)
    events.clear(session_id)

    storage.delete_prefix(f"{session_id}/", container=StorageConstants.CONTAINER_UPLOADS)
    storage.delete_prefix(f"{session_id}/", container=StorageConstants.CONTAINER_DECKS)
    deck_version_handler.delete_all_for_session(session_id)
    documents_removed = document_registry_handler.delete_all_for_session(session_id)
    messages_removed = chat_message_handler.delete_all_for_session(session_id)
    events_removed = workflow_event_handler.delete_all_for_session(session_id)
    from app.core.handlers.workflow_run_handler import workflow_run_handler
    workflow_run_handler.delete_all_for_session(session_id)
    if search_configured():
        purge_session(session_id)

    return {"session_id": session_id, "cleared": True,
            "documents_removed": documents_removed, "decks_removed": len(state.decks),
            "messages_removed": messages_removed, "events_removed": events_removed}

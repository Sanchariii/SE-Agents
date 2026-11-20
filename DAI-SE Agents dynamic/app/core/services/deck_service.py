"""Deck endpoints: sessions, source documents, the agent run, versions and history.

This is the whole deck surface. It used to be an HTTP proxy in one service calling
routes in another; both halves now live in this process, so each handler calls the
domain code directly — one less network hop, one less serialisation round-trip, and
one place where authorisation is enforced.
"""

import asyncio

from fastapi import APIRouter, BackgroundTasks, Depends, File, Request, UploadFile
from fastapi.responses import Response, StreamingResponse

from app.config.exceptions import AppError, NotFoundError
from app.config.constants import (
    Roles, RouteTags, RunErrorCodes, RunStatus, SessionMemoryConstants, StorageConstants,
)
from app.core.dependencies.auth import (
    require_auth, require_role, require_session_access, require_session_owner_role,
    require_session_role,
)
from app.core.handlers import document_handler, run_supervisor
from app.core.handlers.chat_message_handler import chat_message_handler
from app.core.handlers.deck_version_handler import deck_version_handler
from app.core.handlers.session_registry_handler import session_registry_handler
from app.core.handlers.session_store import session_store
from app.core.handlers.workflow_event_handler import workflow_event_handler
from app.core.handlers.workflow_run_handler import workflow_run_handler
from app.core.services import run_stream
from app.core.models.document_model import (
    DocStatus, UploadCompleteRequest, UploadRegisterRequest, UploadRegisterResponse,
)
from app.core.models.question_model import describe_answers, validate_answers
from app.core.models.session_model import UpdateSessionTitleRequest
from app.core.models.deck_version_model import (
    DeckVersionFeedback, SubmitDeckVersionFeedbackRequest,
)
from app.utilities import storage_util as storage
from app.utilities.llm_errors import message_for
from app.utilities.logger_util import get_logger
from app.utilities.metrics_util import Metrics, incr

log = get_logger(__name__)
deck_router = APIRouter(prefix="/decks", tags=[RouteTags.DECKS])

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
_PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


# --------------------------------------------------------------------------- #
# Sessions                                                                     #
# --------------------------------------------------------------------------- #

@deck_router.get("/sessions")
async def list_sessions(claims: dict = Depends(require_auth)):
    """The caller's own projects, persisted server-side so they survive a cache clear."""
    return {"sessions": session_registry_handler.list_detailed_for_user(int(claims["sub"]))}


@deck_router.post("/sessions", status_code=201)
async def register_session(body: dict, claims: dict = Depends(require_auth)):
    session_id = (body or {}).get("session_id")
    if not session_id:
        raise AppError("session_id is required")
    session_registry_handler.record(int(claims["sub"]), session_id, (body or {}).get("title"))
    return {"session_id": session_id}


@deck_router.patch("/{session_id}/title")
async def update_session_title(
    session_id: str,
    body: UpdateSessionTitleRequest,
    claims: dict = Depends(require_session_owner_role(Roles.MEMBER)),
):
    """Rename one of the caller's own projects."""
    user_id = int(claims["sub"])
    # Blocking Postgres write: keep it off the event loop.
    updated = await asyncio.to_thread(
        session_registry_handler.update_title, user_id, session_id, body.title)
    if not updated:
        raise NotFoundError("session not found")
    return {"session_id": session_id, "title": body.title}


@deck_router.delete("/{session_id}")
async def delete_session(session_id: str,
                         claims: dict = Depends(require_session_role(Roles.MEMBER))):
    session_registry_handler.remove(int(claims["sub"]), session_id)
    return await asyncio.to_thread(document_handler.delete_session, session_id)


# --------------------------------------------------------------------------- #
# Source documents                                                             #
# --------------------------------------------------------------------------- #

@deck_router.post("/{session_id}/documents")
async def upload_documents(session_id: str, background: BackgroundTasks,
                           files: list[UploadFile] = File(...),
                           claims: dict = Depends(require_role(Roles.MEMBER))):
    """Validate and store every file, then extract/index them in the background so a
    slow source (e.g. video) can never time out the request. Clients follow progress
    on this session's /stream."""
    user_id = int(claims["sub"])
    # An unknown session id is a legitimate new project; someone else's is not.
    if session_registry_handler.claimed_by_other(user_id, session_id):
        raise NotFoundError("session not found")
    session_registry_handler.record(user_id, session_id)

    records = []
    for f in files:
        data = await f.read()
        mime = document_handler.validate_upload(f.filename or "", data)
        records.append(await asyncio.to_thread(
            document_handler.register_upload, session_id, f.filename or "", mime, data))

    background.add_task(document_handler.process_uploads, records)
    return document_handler.build_response(session_id, records)


@deck_router.post("/{session_id}/documents/register")
async def register_uploads(session_id: str, body: UploadRegisterRequest,
                           claims: dict = Depends(require_role(Roles.MEMBER))):
    """Reserve a document per file and return a short-lived write SAS URL for each, so
    the client uploads straight to Blob and large files never hit the API's ingress
    body-size limit (the cause of HTTP 413). Each file is recorded as
    'upload_in_progress' before its URL is issued, so nothing is ever orphaned."""
    if not body.files:
        raise AppError("files is required")
    user_id = int(claims["sub"])
    # An unknown session id is a legitimate new project; someone else's is not.
    if session_registry_handler.claimed_by_other(user_id, session_id):
        raise NotFoundError("session not found")
    session_registry_handler.record(user_id, session_id)

    tickets = [await asyncio.to_thread(document_handler.register_direct_upload, session_id, item)
               for item in body.files]
    return UploadRegisterResponse(session_id=session_id, uploads=tickets).model_dump()


@deck_router.post("/{session_id}/documents/complete")
async def complete_uploads(session_id: str, body: UploadCompleteRequest,
                           background: BackgroundTasks,
                           _: dict = Depends(require_session_role(Roles.MEMBER))):
    """Finish a presigned upload: confirm each file is in Blob, mark it uploaded, then
    extract/index in the background — same downstream flow as a multipart upload."""
    if not body.documents:
        raise AppError("documents is required")
    records = await asyncio.to_thread(
        document_handler.complete_uploads, session_id, body.documents)
    to_ingest = [r for r in records if r.status == DocStatus.uploaded]
    if to_ingest:
        log.info("upload complete: scheduling ingestion for %s doc(s) in session %s: %s",
                 len(to_ingest), session_id, [r.filename for r in to_ingest])
        background.add_task(document_handler.process_uploads, to_ingest)
    else:
        log.info("upload complete: nothing to ingest in session %s (already processing?)",
                 session_id)
    return document_handler.build_response(session_id, records)


@deck_router.get("/{session_id}/documents")
async def list_documents(session_id: str, latest_only: bool = True,
                         _: dict = Depends(require_session_access)):
    docs = await asyncio.to_thread(
    session_store.latest_docs if latest_only else session_store.all_docs, session_id)
    return {"session_id": session_id, "documents": [d.model_dump() for d in docs]}


@deck_router.delete("/{session_id}/documents/{doc_id}")
async def delete_document(session_id: str, doc_id: str, version: int | None = None,
                          _: dict = Depends(require_session_role(Roles.MEMBER))):
    removed = await asyncio.to_thread(
        document_handler.delete_document, session_id, doc_id, version)
    if not removed:
        raise NotFoundError("document not found")
    return {"session_id": session_id, "doc_id": doc_id, "removed_versions": removed}


@deck_router.post("/{session_id}/documents/{doc_id}/abort")
async def abort_upload(session_id: str, doc_id: str, version: int | None = None,
                       _: dict = Depends(require_session_role(Roles.MEMBER))):
    """Client-reported upload failure: delete the (possibly partial) blob and the
    registry record so a failed direct-to-blob upload leaves nothing orphaned. Only
    acts on in-progress uploads, so it's safe to call on any upload error."""
    aborted = await asyncio.to_thread(
        document_handler.abort_upload, session_id, doc_id, version)
    if not aborted:
        raise NotFoundError("no in-progress upload found")
    return {"session_id": session_id, "doc_id": doc_id, "aborted_versions": aborted}


@deck_router.post("/{session_id}/documents/cleanup-abandoned")
async def cleanup_abandoned_uploads(session_id: str,
                                    _: dict = Depends(require_session_role(Roles.MEMBER))):
    """Reap presigned uploads that were registered but never completed (their SAS
    expired). These are exactly the records still 'upload_in_progress', so cleanup is
    just deleting their (partial) blobs and dropping the records."""
    removed = await asyncio.to_thread(
        document_handler.cleanup_abandoned_uploads, session_id)
    return {"session_id": session_id, "removed": removed}

# --------------------------------------------------------------------------- #
# Agent run (SSE)                                                             #
# --------------------------------------------------------------------------- #

def _client_request_id(body: dict) -> str | None:
    """The client's idempotency key for a submission, validated and bounded."""
    value = body.get("client_request_id")
    if value is None or value == "":
        return None
    value = str(value).strip()
    if not value or len(value) > 128:
        raise AppError("client_request_id must be 1-128 characters")
    return value


def _stream(gen) -> StreamingResponse:
    return StreamingResponse(gen, media_type="text/event-stream", headers=_SSE_HEADERS)


@deck_router.post("/{session_id}/invoke")
async def invoke(session_id: str, body: dict,
                 claims: dict = Depends(require_session_role(Roles.MEMBER))):
    """Submit a message and stream its run until 'done'.

    At most one run is active per session (enforced atomically across pods). If one is
    already active, or this exact submission (same ``client_request_id``) was already
    received, no new work starts: the stream attaches to that run and replays it. The
    first frame, ``run_started``, says which happened and carries the ``run_id``.
    """
    message = (body.get("message") or "").strip()
    # Structured answers (e.g. the creativity slider) are validated before
    # any work starts; a turn may carry answers alone, with no typed message.
    answers = validate_answers(body.get("answers"))
    answers_only = bool(answers) and not message
    if answers_only:
        message = describe_answers(answers)
    if not message:
        raise AppError("message is required")
    refresh = bool(body.get("refresh_content"))
    client_request_id = _client_request_id(body)
    # Must run before add_message: that write recreates the Redis key and hides the miss.
    await asyncio.to_thread(session_store.restore_if_missing, session_id)
    try:
        run, outcome = await asyncio.to_thread(
            run_supervisor.acquire, session_id, str(claims.get("sub") or "") or None,
            client_request_id)
    except AppError:
        raise
    except Exception:                               # noqa: BLE001 - fail closed
        # Without the run registry the one-run-per-session guarantee cannot hold, so
        # no work starts; the client gets a clean terminal event, not a 500 mid-stream.
        log.error("run admission failed for session %s", session_id, exc_info=True)
        code = RunErrorCodes.SERVICE_UNAVAILABLE
        return _stream(run_stream.single_event_stream({
            "type": "done", "agent": "supervisor", "message": "Workflow not started",
            "data": {"status": RunStatus.FAILED, "error_code": code,
                     "error": message_for(code), "retryable": True},
            "session_id": session_id, "run_id": None}))

    if outcome == run_supervisor.AcquireOutcome.STARTED:
        # Only a run that actually starts records the user's turn; an attach or a
        # resent request must not add a second copy of the message.
        await asyncio.to_thread(
            session_store.add_message, session_id, "user", message, None, run.run_id)
        if answers:
            run_supervisor.start(run, message, refresh, answers=answers,
                                 answers_only=answers_only)
        else:
            run_supervisor.start(run, message, refresh)
    return _stream(run_stream.run_stream(
        session_id, run.run_id, after=0,
        first=run_stream.connection_event(run, outcome)))


@deck_router.post("/{session_id}/stop")
async def stop(session_id: str, body: dict | None = None,
               claims: dict = Depends(require_session_role(Roles.MEMBER))):
    """Stop the session's running workflow (an accidental or unwanted message).

    The stop is cooperative: the run halts at its next checkpoint (at most one
    in-flight model call later), the session's plan/draft are rolled back to their
    state before the run, and the run's stream ends with ``done`` (status CANCELLED).
    A run already saving its deck (``completing``) finishes normally.
    """
    run_id = ((body or {}).get("run_id") or None)
    outcome, run = await asyncio.to_thread(
        run_supervisor.request_stop, session_id, run_id,
        str(claims.get("sub") or "") or None)
    return {"session_id": session_id, "status": outcome,
            "run_id": run.run_id if run else run_id,
            "run": run.public() if run else None}


@deck_router.get("/{session_id}/runs/current")
async def current_run(session_id: str, _: dict = Depends(require_session_access)):
    """The session's active run (if any) and its latest run: the authoritative state."""
    try:
        await asyncio.to_thread(run_supervisor.reap, session_id)
    except Exception:                               # noqa: BLE001 - reads still work
        log.warning("stale-run reap failed for session %s", session_id, exc_info=True)
    active = await asyncio.to_thread(workflow_run_handler.get_active, session_id)
    latest = await asyncio.to_thread(workflow_run_handler.get_latest, session_id)
    return {"session_id": session_id,
            "active": active.public() if active else None,
            "latest": latest.public() if latest else None}


async def _session_run(session_id: str, run_id: str):
    run = await asyncio.to_thread(workflow_run_handler.get, run_id)
    if run is None or run.session_id != session_id:
        raise NotFoundError("run not found")
    return run


@deck_router.get("/{session_id}/runs/{run_id}")
async def get_run(session_id: str, run_id: str,
                  _: dict = Depends(require_session_access)):
    """One run's authoritative status."""
    return (await _session_run(session_id, run_id)).public()


@deck_router.get("/{session_id}/runs/{run_id}/events")
async def run_events(session_id: str, run_id: str, request: Request,
                     after_event_index: int = 0,
                     _: dict = Depends(require_session_access)):
    """Resume one run's stream: replay events after an index, then follow live.

    ``Last-Event-ID`` (sent automatically by a reconnecting ``EventSource``) wins over
    ``after_event_index``. A run that already ended replays its events and closes.
    """
    await _session_run(session_id, run_id)
    after = max(0, after_event_index)
    header = request.headers.get("last-event-id")
    if header and header.isdigit():
        after = int(header)
    if after:
        incr(Metrics.STREAM_RECONNECTS)
    return _stream(run_stream.run_stream(session_id, run_id, after=after))


@deck_router.get("/{session_id}/stream")
async def stream(session_id: str, _: dict = Depends(require_session_access)):
    """Deprecated: prefer ``/runs/{run_id}/events``.

    While a run is active this attaches to it (replay + live, run-scoped). Otherwise it
    follows the session's live events (e.g. ingestion progress) with keepalives.
    """
    active = None
    try:
        active = await asyncio.to_thread(workflow_run_handler.get_active, session_id)
    except Exception:                               # noqa: BLE001
        log.warning("could not look up the active run for session %s", session_id,
                    exc_info=True)
    if active is not None:
        return _stream(run_stream.run_stream(session_id, active.run_id, after=0))
    return _stream(run_stream.session_stream(session_id))


# --------------------------------------------------------------------------- #
# Chat history                                                                 #
# --------------------------------------------------------------------------- #

@deck_router.get("/{session_id}/messages")
async def messages(session_id: str, limit: int | None = None,
                   _: dict = Depends(require_session_access)):
    """Chat history for a session, from Postgres (the durable system of record) so the
    thread and each final response survive a Redis eviction/restart or a cleared cache.

    `require_session_access` is the ownership guard: a caller may read only a session
    they own (admins may read any). Anyone else gets 404 — so one user can never read
    another user's conversation, and a guessed session id can't confirm it even exists.
    """
    
    try:
        history = await asyncio.to_thread(
            chat_message_handler.list_messages, session_id, limit)
    except Exception:                               # noqa: BLE001
        log.error("failed to load durable chat messages for session %s",
                  session_id, exc_info=True)
        history = []

    if history:
        # The durable row also carries the session-restore snapshot (plan, draft,
        # content, slide artifacts). That is internal recovery state for a Redis loss,
        # so it is stripped before the turn reaches the client or the timeline.
        for message in history:
            if message.result:
                message.result.pop(SessionMemoryConstants.RESULT_KEY, None)
        timeline_messages = history
        message_payload = [
            message.model_dump(mode="json", exclude={"run_id"}) for message in history
        ]
    else:
        # Sessions predating durable chat persistence still use Redis as a working copy.
        redis_history = await asyncio.to_thread(
            session_store.get_messages, session_id, limit)
        timeline_messages = redis_history
        message_payload = [
            {
                "role": message.role,
                "content": message.content,
                "result": message.result,
                "created_at": message.ts.isoformat(),
            }
            for message in redis_history
        ]

    run_ids = [
        message.run_id for message in timeline_messages if message.run_id is not None
    ]
    # Without a message limit, include event-only runs too (for example, when the
    # best-effort chat write failed but the event batch succeeded).
    event_run_ids = run_ids if limit else None
    try:
        workflow_events = await asyncio.to_thread(
            workflow_event_handler.list_events, session_id, event_run_ids)
    except Exception:                               # noqa: BLE001
        log.error("failed to load workflow events for session %s",
                  session_id, exc_info=True)
        workflow_events = []
    timeline = workflow_event_handler.build_timeline(
        timeline_messages, workflow_events)
    # Each turn carries its run's authoritative status, so a stopped (CANCELLED) or
    # still-running turn can be shown as such after a refresh.
    try:
        run_statuses = await asyncio.to_thread(
            workflow_run_handler.statuses, session_id, run_ids)
    except Exception:                               # noqa: BLE001
        log.warning("could not load run statuses for session %s", session_id,
                    exc_info=True)
        run_statuses = {}
    for payload, message in zip(message_payload, timeline_messages):
        payload["run_status"] = run_statuses.get(message.run_id) if message.run_id else None
    timeline_payload = []
    for item in timeline:
        dumped = item.model_dump(mode="json")
        if dumped.get("run_id"):
            dumped["run_status"] = run_statuses.get(dumped["run_id"])
        timeline_payload.append(dumped)
    return {
        "session_id": session_id,
        "messages": message_payload,
        "timeline": timeline_payload,
    }


# --------------------------------------------------------------------------- #
# Deck versions                                                                #
# --------------------------------------------------------------------------- #

@deck_router.get("/{session_id}/versions")
async def list_versions(session_id: str, _: dict = Depends(require_session_access)):
    versions = deck_version_handler.list_versions(session_id)
    return {"session_id": session_id, "versions": [v.model_dump() for v in versions]}


@deck_router.post(
    "/{session_id}/versions/{deck_version}/feedback",
    response_model=DeckVersionFeedback,
    status_code=201,
)
async def submit_version_feedback(
    session_id: str,
    deck_version: int,
    body: SubmitDeckVersionFeedbackRequest,
    _: dict = Depends(require_session_owner_role(Roles.MEMBER)),
) -> DeckVersionFeedback:
    """Store the session owner's single feedback submission for a deck version.

    Args:
        session_id: Session that owns the deck.
        deck_version: Generated version receiving feedback.
        body: Required rating and optional comment.
        _: Claims validated for strict ownership and member access.

    Returns:
        The newly persisted feedback.
    """
    return await asyncio.to_thread(
        deck_version_handler.submit_feedback,
        session_id,
        deck_version,
        body.thumbs_up,
        body.comment,
    )


@deck_router.get(
    "/{session_id}/versions/{deck_version}/feedback",
    response_model=DeckVersionFeedback,
)
async def get_version_feedback(
    session_id: str,
    deck_version: int,
    _: dict = Depends(require_session_access),
) -> DeckVersionFeedback:
    """Return feedback already submitted for a generated deck version.

    Args:
        session_id: Session that owns the deck.
        deck_version: Generated version whose feedback is requested.
        _: Claims validated for session access.

    Returns:
        Stored feedback for the deck version.
    """
    return await asyncio.to_thread(
        deck_version_handler.get_feedback, session_id, deck_version)


@deck_router.get("/{session_id}/versions/{deck_version}/download")
async def download_version(session_id: str, deck_version: int, language: str | None = None,
                          _: dict = Depends(require_session_access)):
    version = deck_version_handler.get_version(session_id, deck_version)
    if version is None:
        raise NotFoundError("deck version not found")
    path = version.files.get(language) if language else None
    if language and path is None:
        raise NotFoundError(f"deck version has no '{language}' translation")
    data = await asyncio.to_thread(deck_version_handler.download, version, path)
    filename = (path or version.storage_path).rsplit("/", 1)[-1]
    return Response(content=data, media_type=_PPTX_MIME,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@deck_router.get("/{session_id}/versions/{deck_version}/download-url")
async def download_version_url(session_id: str, deck_version: int, language: str | None = None,
                              _: dict = Depends(require_session_access)):
    """A short-lived, read-only SAS URL to the .pptx in Blob storage, so the client can
    fetch it directly instead of streaming every byte back through this service."""
    version = deck_version_handler.get_version(session_id, deck_version)
    if version is None:
        raise NotFoundError("deck version not found")
    path = version.files.get(language) if language else None
    if language and path is None:
        raise NotFoundError(f"deck version has no '{language}' translation")
    url = await asyncio.to_thread(
        storage.read_sas_url, path or version.storage_path,
        StorageConstants.CONTAINER_DECKS, StorageConstants.DECK_SAS_TTL_MINUTES)
    if url is None:
        raise AppError("Blob storage is not configured for direct SAS download")
    return {"session_id": session_id, "deck_version": deck_version, "language": language,
            "url": url, "expires_in": StorageConstants.DECK_SAS_TTL_MINUTES * 60}


@deck_router.delete("/{session_id}/versions/{deck_version}")
async def delete_version(session_id: str, deck_version: int,
                         _: dict = Depends(require_session_role(Roles.MEMBER))):
    if not await asyncio.to_thread(deck_version_handler.delete_version, session_id, deck_version):
        raise NotFoundError("deck version not found")
    return {"session_id": session_id, "deck_version": deck_version, "deleted": True}

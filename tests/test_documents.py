"""Session store, document validation and search-filter construction."""

import pytest

from app.config.exceptions import AppError, NotFoundError, UnsupportedMediaTypeError
from app.core.handlers import document_handler
from app.core.models.document_model import (
    DocStatus, UploadCompleteItem, UploadRegisterItem,
)
from app.genai.retrieval.filters import odata_str
from app.utilities.files_util import detect_mime, sha256_bytes


# --------------------------------------------------------------------------- #
# Session store                                                                #
# --------------------------------------------------------------------------- #

def test_new_session_starts_empty(session_store):
    state = session_store.get_session("s1")
    assert state.docs == [] and state.messages == [] and state.plan is None


def test_reupload_of_same_filename_versions_up(session_store):
    first = session_store.register_upload("s1", "guide.pdf", "application/pdf", "a", "p1")
    second = session_store.register_upload("s1", "guide.pdf", "application/pdf", "b", "p2")
    assert first.doc_id == second.doc_id
    assert (first.version, second.version) == (1, 2)


def test_only_the_newest_version_is_latest(session_store):
    session_store.register_upload("s1", "guide.pdf", "application/pdf", "a", "p1")
    session_store.register_upload("s1", "guide.pdf", "application/pdf", "b", "p2")
    latest = session_store.latest_docs("s1")
    assert [d.version for d in latest] == [2]
    assert len(session_store.all_docs("s1")) == 2


def test_different_filenames_get_different_doc_ids(session_store):
    a = session_store.register_upload("s1", "a.pdf", "application/pdf", "1", "p")
    b = session_store.register_upload("s1", "b.pdf", "application/pdf", "2", "p")
    assert a.doc_id != b.doc_id


def test_same_filename_in_different_sessions_is_isolated(session_store):
    a = session_store.register_upload("s1", "a.pdf", "application/pdf", "1", "p")
    b = session_store.register_upload("s2", "a.pdf", "application/pdf", "1", "p")
    assert a.doc_id != b.doc_id
    assert len(session_store.all_docs("s1")) == 1


def test_deleting_a_document_returns_the_removed_versions(session_store):
    rec = session_store.register_upload("s1", "a.pdf", "application/pdf", "1", "p")
    removed = session_store.delete_doc("s1", rec.doc_id)
    assert [r.version for r in removed] == [1]
    assert session_store.all_docs("s1") == []


def test_deleting_an_unknown_document_is_a_no_op(session_store):
    assert session_store.delete_doc("s1", "nope") == []


def test_messages_round_trip_and_respect_limit(session_store):
    for i in range(5):
        session_store.add_message("s1", "user", f"m{i}")
    assert len(session_store.get_messages("s1")) == 5
    assert [m.content for m in session_store.get_messages("s1", limit=2)] == ["m3", "m4"]


def test_assistant_message_caches_the_final_response_metadata(session_store):
    result = {"reply": "Deck ready", "deck_updated": True, "deck_version": 1}

    session_store.add_message(
        "s1", "assistant", "Deck ready", result=result, run_id="run-1")

    cached = session_store.get_messages("s1")
    assert cached[0].result == result
    assert cached[0].run_id == "run-1"


def test_messages_restore_from_postgres_after_a_redis_miss(session_store, monkeypatch):
    """Agent context should use Redis normally, but recover durable history after a
    Redis eviction or process restart and cache it for the next planning/triage read."""
    from app.core.handlers.chat_message_handler import chat_message_handler
    from app.core.models.chat_message_model import PersistedChatMessage

    calls: list[str] = []

    def persisted_history(session_id: str):
        calls.append(session_id)
        return [
            PersistedChatMessage(
                session_id=session_id, run_id="run-1", role="user", content="first"),
            PersistedChatMessage(
                session_id=session_id, run_id="run-1", role="assistant", content="second"),
        ]

    monkeypatch.setattr(chat_message_handler, "list_messages", persisted_history)

    assert [m.content for m in session_store.get_messages("s1")] == ["first", "second"]
    assert [m.content for m in session_store.get_messages("s1", limit=1)] == ["second"]
    assert all(m.run_id == "run-1" for m in session_store.get_messages("s1"))
    assert calls == ["s1"]


def _durable_sources(monkeypatch, memory=None, docs=None, history=None, versions=None):
    """Point every Postgres source a restore reads at fixed values, counting reads."""
    from app.core.handlers import session_store as module

    reads: list[str] = []

    def source(name, value):
        def read(session_id, *a, **k):
            reads.append(name)
            return value
        return read

    monkeypatch.setattr(module.chat_message_handler, "latest_session_memory",
                        source("memory", memory))
    monkeypatch.setattr(module.chat_message_handler, "list_messages",
                        source("history", history or []))
    monkeypatch.setattr(module.document_registry_handler, "list_documents",
                        source("docs", docs or []))
    monkeypatch.setattr(module.deck_version_handler, "list_versions",
                        source("versions", versions or []))
    return reads


def test_a_flushed_session_is_rebuilt_from_postgres(session_store, monkeypatch):
    from app.core.models.deck_version_model import DeckVersion

    memory = {"version": 1, "plan": {"title": "T"}, "plan_status": "confirmed",
              "draft": {"title": "T", "slides": [{"slide_number": 1, "title": "A"}]},
              "content": {"plan": {}, "slides": []},
              "slide_artifacts": {"1": {"path": "p", "hash": "h", "fallback": False}}}
    versions = [DeckVersion(session_id="s1", deck_version=n, storage_path=f"v{n}")
                for n in (1, 2)]
    _durable_sources(monkeypatch, memory=memory, versions=versions)

    assert session_store.restore_if_missing("s1") is True

    assert session_store.get_plan("s1") == ({"title": "T"}, "confirmed")
    assert session_store.get_draft("s1") == memory["draft"]
    assert session_store.get_slide_artifacts("s1") == memory["slide_artifacts"]
    # Numbering continues from Postgres instead of restarting at v1.
    assert session_store.add_deck("s1", "v3").deck_version == 3


def test_a_live_session_is_never_read_from_postgres(session_store, monkeypatch):
    session_store.set_plan("s1", {"title": "live"})
    reads = _durable_sources(monkeypatch, memory={"plan": {"title": "stale"}})

    assert session_store.restore_if_missing("s1") is False
    assert reads == []
    assert session_store.get_plan("s1")[0] == {"title": "live"}


def test_reading_documents_first_does_not_block_the_plan_restore(session_store, monkeypatch):
    """The UI lists documents before it invokes; that must restore the plan too."""
    memory = {"version": 1, "plan": {"title": "T"}, "plan_status": "draft"}
    _durable_sources(monkeypatch, memory=memory)

    session_store.latest_docs("s1")

    assert session_store.get_plan("s1")[0] == {"title": "T"}


def test_one_failing_source_does_not_block_the_others(session_store, monkeypatch):
    from app.core.handlers import session_store as module
    from app.core.models.chat_message_model import PersistedChatMessage

    _durable_sources(monkeypatch, history=[
        PersistedChatMessage(session_id="s1", role="user", content="hello")])

    def boom(*a, **k):
        raise RuntimeError("table missing")

    monkeypatch.setattr(module.deck_version_handler, "list_versions", boom)

    assert session_store.restore_if_missing("s1") is True
    assert [m.content for m in session_store.get_messages("s1")] == ["hello"]


def test_restored_messages_never_carry_the_snapshot(session_store, monkeypatch):
    from app.core.models.chat_message_model import PersistedChatMessage

    _durable_sources(monkeypatch, history=[PersistedChatMessage(
        session_id="s1", role="assistant", content="ok",
        result={"reply": "ok", "session_memory": {"version": 1}})])

    assert session_store.get_messages("s1")[0].result == {"reply": "ok"}


def test_a_new_session_restores_nothing(session_store, monkeypatch):
    _durable_sources(monkeypatch)

    assert session_store.restore_if_missing("s1") is False
    assert session_store.get_session("s1").plan is None


def test_a_background_doc_update_after_a_flush_still_restores_the_plan(session_store,
                                                                       monkeypatch):
    from app.core.models.document_model import DocumentRecord

    _durable_sources(monkeypatch, memory={"version": 1, "plan": {"title": "T"},
                                          "plan_status": "draft"})
    record = DocumentRecord(session_id="s1", doc_id="d", version=1, filename="a.pdf",
                            mime_type="application/pdf", sha256="x", storage_path="p")

    session_store.update_doc(record)

    assert session_store.get_plan("s1")[0] == {"title": "T"}


def test_plan_status_round_trips(session_store):
    session_store.set_plan("s1", {"title": "T"}, status="confirmed")
    plan, status = session_store.get_plan("s1")
    assert plan["title"] == "T" and status == "confirmed"


def test_deck_versions_increment(session_store):
    assert session_store.add_deck("s1", "p1").deck_version == 1
    assert session_store.add_deck("s1", "p2").deck_version == 2


def test_uploading_marks_sources_changed(session_store):
    assert session_store.sources_changed("s1") is False
    session_store.register_upload("s1", "a.pdf", "application/pdf", "1", "p")
    assert session_store.sources_changed("s1") is True


def test_delete_session_returns_a_snapshot_for_cleanup(session_store):
    session_store.register_upload("s1", "a.pdf", "application/pdf", "1", "p")
    session_store.add_deck("s1", "deck.pptx")
    state = session_store.delete_session("s1")
    assert len(state.docs) == 1 and len(state.decks) == 1
    assert session_store.get_session("s1").docs == []


def test_uploaded_documents_start_in_the_uploaded_state(session_store):
    rec = session_store.register_upload("s1", "a.pdf", "application/pdf", "1", "p")
    assert rec.status is DocStatus.uploaded


# --------------------------------------------------------------------------- #
# Upload validation                                                            #
# --------------------------------------------------------------------------- #

def test_supported_extension_maps_to_a_mime_type():
    assert document_handler.validate_upload("deck.pptx", b"data").endswith("presentationml.presentation")


def test_empty_file_is_rejected():
    with pytest.raises(AppError):
        document_handler.validate_upload("a.pdf", b"")


def test_unsupported_extension_is_rejected_with_the_supported_list():
    with pytest.raises(UnsupportedMediaTypeError) as exc:
        document_handler.validate_upload("malware.exe", b"MZ")
    assert exc.value.status_code == 415
    assert exc.value.supported


def test_oversized_file_is_rejected(monkeypatch):
    from app.config.constants import StorageConstants
    monkeypatch.setattr(StorageConstants, "MAX_UPLOAD_BYTES", 10)
    with pytest.raises(AppError, match="upload limit"):
        document_handler.validate_upload("a.pdf", b"x" * 11)


def test_mime_detection_is_case_insensitive():
    assert detect_mime("REPORT.PDF") == "application/pdf"


def test_sha256_is_stable():
    assert sha256_bytes(b"abc") == sha256_bytes(b"abc")
    assert sha256_bytes(b"abc") != sha256_bytes(b"abd")


# --------------------------------------------------------------------------- #
# Direct-to-blob (presigned) uploads                                          #
# --------------------------------------------------------------------------- #

def _bind_store(monkeypatch, store):
    """Point the handler's module singleton at an isolated in-memory store."""
    monkeypatch.setattr(document_handler, "session_store", store)


def _fake_presign(monkeypatch):
    monkeypatch.setattr(document_handler.storage, "upload_sas_url",
                        lambda path, **kw: f"https://blob/{path}?sig=x")


def test_validate_registration_accepts_a_supported_type():
    assert document_handler.validate_registration("clip.mp4", 1234) == "video/mp4"


def test_validate_registration_rejects_a_zero_size():
    with pytest.raises(AppError):
        document_handler.validate_registration("a.pdf", 0)


def test_validate_registration_rejects_an_oversized_declared_size(monkeypatch):
    from app.config.constants import StorageConstants
    monkeypatch.setattr(StorageConstants, "MAX_UPLOAD_BYTES", 10)
    with pytest.raises(AppError, match="upload limit"):
        document_handler.validate_registration("a.pdf", 11)


def test_validate_registration_rejects_an_unsupported_type():
    with pytest.raises(UnsupportedMediaTypeError):
        document_handler.validate_registration("malware.exe", None)


def test_register_direct_upload_reserves_the_doc_and_returns_a_ticket(monkeypatch, session_store):
    _bind_store(monkeypatch, session_store)
    _fake_presign(monkeypatch)
    ticket = document_handler.register_direct_upload(
        "s1", UploadRegisterItem(filename="clip.mp4", size=999))
    assert ticket.filename == "clip.mp4" and ticket.version == 1
    assert ticket.upload_url.startswith("https://blob/")
    rec = session_store.get_doc("s1", ticket.doc_id, 1)
    assert rec.status is DocStatus.upload_in_progress
    assert rec.storage_path == ticket.storage_path


def test_register_direct_upload_fails_clearly_when_presign_unavailable(monkeypatch, session_store):
    _bind_store(monkeypatch, session_store)
    monkeypatch.setattr(document_handler.storage, "upload_sas_url", lambda path, **kw: None)
    with pytest.raises(AppError, match="not available"):
        document_handler.register_direct_upload("s1", UploadRegisterItem(filename="a.pdf"))
    assert session_store.latest_docs("s1")[0].status is DocStatus.failed


def test_complete_uploads_marks_a_verified_file_uploaded(monkeypatch, session_store):
    _bind_store(monkeypatch, session_store)
    _fake_presign(monkeypatch)
    ticket = document_handler.register_direct_upload("s1", UploadRegisterItem(filename="a.pdf"))
    monkeypatch.setattr(document_handler.storage, "blob_size", lambda path, **kw: 2048)
    [rec] = document_handler.complete_uploads(
        "s1", [UploadCompleteItem(doc_id=ticket.doc_id, version=1)])
    assert rec.status is DocStatus.uploaded


def test_complete_uploads_rejects_a_file_that_never_landed(monkeypatch, session_store):
    _bind_store(monkeypatch, session_store)
    _fake_presign(monkeypatch)
    ticket = document_handler.register_direct_upload("s1", UploadRegisterItem(filename="a.pdf"))
    monkeypatch.setattr(document_handler.storage, "blob_size", lambda path, **kw: None)
    with pytest.raises(NotFoundError):
        document_handler.complete_uploads(
            "s1", [UploadCompleteItem(doc_id=ticket.doc_id, version=1)])


def test_complete_uploads_rejects_and_removes_an_oversized_blob(monkeypatch, session_store):
    _bind_store(monkeypatch, session_store)
    _fake_presign(monkeypatch)
    from app.config.constants import StorageConstants
    monkeypatch.setattr(StorageConstants, "MAX_UPLOAD_BYTES", 10)
    ticket = document_handler.register_direct_upload("s1", UploadRegisterItem(filename="a.pdf"))
    monkeypatch.setattr(document_handler.storage, "blob_size", lambda path, **kw: 999)
    deleted: list[str] = []
    monkeypatch.setattr(document_handler.storage, "delete_file",
                        lambda path, **kw: deleted.append(path))
    with pytest.raises(AppError, match="upload limit"):
        document_handler.complete_uploads(
            "s1", [UploadCompleteItem(doc_id=ticket.doc_id, version=1)])
    assert deleted == [ticket.storage_path]
    assert session_store.get_doc("s1", ticket.doc_id, 1).status is DocStatus.failed


def test_complete_uploads_is_idempotent_for_an_already_processed_doc(monkeypatch, session_store):
    _bind_store(monkeypatch, session_store)
    _fake_presign(monkeypatch)
    ticket = document_handler.register_direct_upload("s1", UploadRegisterItem(filename="a.pdf"))
    rec = session_store.get_doc("s1", ticket.doc_id, 1)
    rec.status = DocStatus.indexed
    session_store.update_doc(rec)

    def _boom(*_a, **_k):
        raise AssertionError("blob_size must not be consulted for a processed doc")
    monkeypatch.setattr(document_handler.storage, "blob_size", _boom)
    [out] = document_handler.complete_uploads(
        "s1", [UploadCompleteItem(doc_id=ticket.doc_id, version=1)])
    assert out.status is DocStatus.indexed


def test_complete_uploads_streams_verify_then_start_events(monkeypatch, session_store):
    from app.utilities import events_util as events
    _bind_store(monkeypatch, session_store)
    _fake_presign(monkeypatch)
    events.clear("s1")
    ticket = document_handler.register_direct_upload("s1", UploadRegisterItem(filename="a.pdf"))
    monkeypatch.setattr(document_handler.storage, "blob_size", lambda path, **kw: 2048)
    document_handler.complete_uploads("s1", [UploadCompleteItem(doc_id=ticket.doc_id, version=1)])
    messages = [e["message"] for e in events.history("s1")]
    assert any("Verifying in storage" in m for m in messages)
    assert any("starting ingestion" in m for m in messages)


def test_complete_uploads_streams_an_error_when_the_blob_is_missing(monkeypatch, session_store):
    from app.utilities import events_util as events
    _bind_store(monkeypatch, session_store)
    _fake_presign(monkeypatch)
    events.clear("s1")
    ticket = document_handler.register_direct_upload("s1", UploadRegisterItem(filename="a.pdf"))
    monkeypatch.setattr(document_handler.storage, "blob_size", lambda path, **kw: None)
    with pytest.raises(NotFoundError):
        document_handler.complete_uploads(
            "s1", [UploadCompleteItem(doc_id=ticket.doc_id, version=1)])
    assert "ingest_error" in [e["type"] for e in events.history("s1")]



def test_cleanup_abandoned_uploads_reaps_stale_in_progress(monkeypatch, session_store):
    from datetime import datetime, timedelta, timezone
    _bind_store(monkeypatch, session_store)
    _fake_presign(monkeypatch)
    ticket = document_handler.register_direct_upload("s1", UploadRegisterItem(filename="a.pdf"))
    rec = session_store.get_doc("s1", ticket.doc_id, 1)
    rec.uploaded_at = datetime.now(timezone.utc) - timedelta(hours=2)   # backdate => abandoned
    session_store.update_doc(rec)
    deleted: list[str] = []
    monkeypatch.setattr(document_handler.storage, "delete_file",
                        lambda path, **kw: deleted.append(path))
    removed = document_handler.cleanup_abandoned_uploads("s1")
    assert len(removed) == 1 and deleted == [ticket.storage_path]
    assert session_store.all_docs("s1") == []


def test_cleanup_abandoned_uploads_keeps_recent_registrations(monkeypatch, session_store):
    _bind_store(monkeypatch, session_store)
    _fake_presign(monkeypatch)
    document_handler.register_direct_upload("s1", UploadRegisterItem(filename="a.pdf"))
    assert document_handler.cleanup_abandoned_uploads("s1") == []
    assert len(session_store.all_docs("s1")) == 1


def test_abort_upload_deletes_the_blob_and_the_record(monkeypatch, session_store):
    _bind_store(monkeypatch, session_store)
    _fake_presign(monkeypatch)
    ticket = document_handler.register_direct_upload("s1", UploadRegisterItem(filename="a.pdf"))
    deleted: list[str] = []
    monkeypatch.setattr(document_handler.storage, "delete_file",
                        lambda path, **kw: deleted.append(path))
    aborted = document_handler.abort_upload("s1", ticket.doc_id, 1)
    assert aborted == [1] and deleted == [ticket.storage_path]
    assert session_store.all_docs("s1") == []


def test_abort_upload_leaves_a_processed_document_untouched(monkeypatch, session_store):
    _bind_store(monkeypatch, session_store)
    _fake_presign(monkeypatch)
    ticket = document_handler.register_direct_upload("s1", UploadRegisterItem(filename="a.pdf"))
    rec = session_store.get_doc("s1", ticket.doc_id, 1)
    rec.status = DocStatus.indexed
    session_store.update_doc(rec)
    deleted: list[str] = []
    monkeypatch.setattr(document_handler.storage, "delete_file",
                        lambda path, **kw: deleted.append(path))
    assert document_handler.abort_upload("s1", ticket.doc_id, 1) == []
    assert deleted == [] and len(session_store.all_docs("s1")) == 1


def test_abort_upload_unknown_doc_is_a_no_op(monkeypatch, session_store):
    _bind_store(monkeypatch, session_store)
    monkeypatch.setattr(document_handler.storage, "delete_file", lambda path, **kw: None)
    assert document_handler.abort_upload("s1", "nope") == []


def test_sweep_abandoned_uploads_reaps_across_sessions(monkeypatch, session_store):
    from datetime import datetime, timedelta, timezone
    from app.core.handlers import session_registry_handler as reg_mod
    _bind_store(monkeypatch, session_store)
    _fake_presign(monkeypatch)
    monkeypatch.setattr(document_handler.storage, "delete_file", lambda path, **kw: None)
    monkeypatch.setattr(reg_mod.session_registry_handler, "list_all_sessions", lambda: ["s1"])
    ticket = document_handler.register_direct_upload("s1", UploadRegisterItem(filename="a.pdf"))
    rec = session_store.get_doc("s1", ticket.doc_id, 1)
    rec.uploaded_at = datetime.now(timezone.utc) - timedelta(hours=2)   # backdate => abandoned
    session_store.update_doc(rec)
    assert document_handler.sweep_abandoned_uploads() == 1
    assert session_store.all_docs("s1") == []


# --------------------------------------------------------------------------- #
# Search filter escaping                                                       #
# --------------------------------------------------------------------------- #

def test_plain_value_is_quoted():
    assert odata_str("sess-1") == "'sess-1'"


def test_embedded_quote_is_escaped_not_dropped():
    assert odata_str("o'brien") == "'o''brien'"


def test_filter_injection_attempt_stays_inside_the_literal():
    """Without escaping this would end the literal and append an OR clause."""
    hostile = "x' or session_id ne '"
    assert odata_str(hostile) == "'x'' or session_id ne '''"

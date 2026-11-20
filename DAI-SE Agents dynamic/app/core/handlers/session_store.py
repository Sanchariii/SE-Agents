"""Per-session state over Redis (chat, docs, plan, decks, CkM).

Keys: session:{id}, ckm:{doc}:{ver}, content:{id}, draft:{id}, sources_changed:{id}.
A re-upload of the same filename versions up; retrieval filters to is_latest.
"""

import hashlib
import threading
from datetime import datetime, timezone
from typing import Optional

from app.config.constants import (
    PlanStatus, RedisKeys, SessionMemoryConstants, SkillReferenceConstants,
)
from app.config.exceptions import AppError
from app.core.handlers.chat_message_handler import chat_message_handler
from app.core.handlers.deck_version_handler import deck_version_handler
from app.core.handlers.document_registry_handler import document_registry_handler
from app.core.models.document_model import (
    ChatMessage, CkM, DeckArtifact, DocumentRecord, SessionState,
)
from app.genai.skills.skill_record import validate_active_skill
from app.utilities.logger_util import get_logger
from app.utilities.redis_util import RedisUtil, get_redis

log = get_logger(__name__)


def _doc_id_for(session_id: str, filename: str) -> str:
    key = f"{session_id}:{filename.strip().lower()}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


class SessionStore:
    def __init__(self, redis: RedisUtil | None = None) -> None:
        self._lock = threading.Lock()
        self._r = redis or get_redis()

    # --- session state ---
    def _load(self, session_id: str) -> SessionState:
        raw = self._r.get(RedisKeys.SESSION.format(session_id=session_id))
        return SessionState.model_validate_json(raw) if raw else SessionState(session_id=session_id)

    def _save(self, state: SessionState) -> None:
        state.updated_at = datetime.now(timezone.utc)
        self._r.set(RedisKeys.SESSION.format(session_id=state.session_id),
                    state.model_dump_json())

    def _restore_docs_if_missing(self, session_id: str) -> list[DocumentRecord]:
        """Return the session's documents, restoring the whole session after a Redis loss.

        Args:
            session_id: The session whose cache may need restoration.

        Returns:
            The cached or restored document records.
        """
        # A docs-only restore would recreate the session key and block the full restore.
        self.restore_if_missing(session_id)
        return self._load(session_id).docs

    @staticmethod
    def _persist_doc(record: DocumentRecord) -> None:
        """Mirror a Redis document record to Postgres without breaking live work.

        Args:
            record: The newly created or status-updated source document.

        Returns:
            None.
        """
        try:
            document_registry_handler.record(record)
        except Exception:                           # noqa: BLE001
            log.error("failed to persist source document %s v%s for session %s",
                      record.doc_id, record.version, record.session_id, exc_info=True)

    def get_session(self, session_id: str) -> SessionState:
        return self._load(session_id)

    def delete_session(self, session_id: str) -> SessionState:
        """Drop all Redis state; the returned snapshot lets the caller clean up
        blob storage, Postgres rows and Search chunks."""
        state = self._load(session_id)
        for r in state.docs:
            self._r.delete(RedisKeys.CKM.format(doc_id=r.doc_id, version=r.version))
        for key in (RedisKeys.SESSION, RedisKeys.CONTENT, RedisKeys.DRAFT,
                    RedisKeys.CRITIQUE, RedisKeys.SOURCES_CHANGED, RedisKeys.BRIEF,
                    RedisKeys.DECK_DESIGN, RedisKeys.ACTIVE_SKILL,
                    RedisKeys.SKILL_REFERENCES):
            self._r.delete(key.format(session_id=session_id))
        return state

    # --- documents ---
    def register_upload(self, session_id: str, filename: str, mime_type: str,
                        sha256: str, storage_path: str) -> DocumentRecord:
        # Recover versions before a new upload is allocated after Redis loss.
        self._restore_docs_if_missing(session_id)
        doc_id = _doc_id_for(session_id, filename)
        with self._lock:
            state = self._load(session_id)
            prior = [r for r in state.docs if r.doc_id == doc_id]
            version = max((r.version for r in prior), default=0) + 1
            for r in prior:
                r.is_latest = False
            record = DocumentRecord(
                session_id=session_id, doc_id=doc_id, version=version,
                filename=filename, mime_type=mime_type, sha256=sha256,
                storage_path=storage_path)
            state.docs.append(record)
            self._save(state)
        self._persist_doc(record)
        self.mark_sources_changed(session_id)
        return record

    def update_doc(self, record: DocumentRecord) -> None:
        # Ingestion runs in the background; without this a status write after a flush
        # recreates the key with one doc and the full restore never runs.
        self.restore_if_missing(record.session_id)
        with self._lock:
            state = self._load(record.session_id)
            state.docs = [record if (r.doc_id == record.doc_id and r.version == record.version)
                          else r for r in state.docs]
            self._save(state)
        self._persist_doc(record)

    def get_doc(self, session_id: str, doc_id: str, version: int) -> Optional[DocumentRecord]:
        for r in self._restore_docs_if_missing(session_id):
            if r.doc_id == doc_id and r.version == version:
                return r
        return None

    def delete_doc(self, session_id: str, doc_id: str, version: int | None = None) -> list[DocumentRecord]:
        """Remove one doc (all versions, or just `version`); caller deletes storage/Search."""
        self._restore_docs_if_missing(session_id)
        with self._lock:
            state = self._load(session_id)
            keys = {(r.doc_id, r.version) for r in state.docs
                   if r.doc_id == doc_id and (version is None or r.version == version)}
            removed = [r for r in state.docs if (r.doc_id, r.version) in keys]
            state.docs = [r for r in state.docs if (r.doc_id, r.version) not in keys]
            self._save(state)
        for r in removed:
            self._r.delete(RedisKeys.CKM.format(doc_id=r.doc_id, version=r.version))
            try:
                document_registry_handler.delete(session_id, r.doc_id, r.version)
            except Exception:                       # noqa: BLE001
                log.error("failed to delete source document %s v%s for session %s from Postgres",
                          r.doc_id, r.version, session_id, exc_info=True)
        if removed:
            self.mark_sources_changed(session_id)
        return removed

    def latest_docs(self, session_id: str) -> list[DocumentRecord]:
        """The latest version of every SOURCE document.

        A deck claimed as the design reference is left out: it gives the deck its look,
        never its content, so retrieval, planning and triage do not see it.
        """
        reference = self.design_reference(session_id)
        return [r for r in self._restore_docs_if_missing(session_id) if r.is_latest
                and not (reference and r.doc_id == reference.get("doc_id"))]

    def design_reference(self, session_id: str) -> dict:
        """The design reference recorded in the session brief ({} when none)."""
        return dict((self.get_brief(session_id) or {}).get("design_reference") or {})

    def all_docs(self, session_id: str) -> list[DocumentRecord]:
        return list(self._restore_docs_if_missing(session_id))

    # --- conversation ---
    def add_message(self, session_id: str, role: str, content: str,
                    result: dict | None = None,
                    run_id: str | None = None) -> ChatMessage:
        """Append a message to the Redis conversation cache.

        Args:
            session_id: The session the message belongs to.
            role: Who produced the message.
            content: Text presented in the chat history.
            result: Final assistant-response metadata, when this is an assistant turn.
            run_id: Workflow invocation associated with the turn, when available.

        Returns:
            The cached message.
        """
        msg = ChatMessage(run_id=run_id, role=role, content=content, result=result)
        with self._lock:
            state = self._load(session_id)
            state.messages.append(msg)
            self._save(state)
        return msg

    def get_messages(self, session_id: str, limit: int | None = None) -> list[ChatMessage]:
        """Return agent conversation context, restoring the whole session after a Redis loss.

        Args:
            session_id: The session whose conversation is needed.
            limit: Optional number of most recent messages to return.

        Returns:
            Messages in chronological order.
        """
        self.restore_if_missing(session_id)
        messages = self._load(session_id).messages
        return messages[-limit:] if limit else messages

    # --- plan ---
    def set_plan(self, session_id: str, plan: dict, status: str = PlanStatus.DRAFT) -> None:
        with self._lock:
            state = self._load(session_id)
            state.plan, state.plan_status = plan, status
            self._save(state)

    def get_plan(self, session_id: str) -> tuple[Optional[dict], str]:
        state = self._load(session_id)
        return state.plan, state.plan_status

    # --- decks ---
    def add_deck(self, session_id: str, storage_path: str,
                 plan_snapshot: dict | None = None) -> DeckArtifact:
        with self._lock:
            state = self._load(session_id)
            deck = DeckArtifact(deck_version=len(state.decks) + 1,
                                storage_path=storage_path, plan_snapshot=plan_snapshot or {})
            state.decks.append(deck)
            self._save(state)
        return deck

    def list_decks(self, session_id: str) -> list[DeckArtifact]:
        return list(self._load(session_id).decks)

    # --- content / draft (agentic RAG + incremental deck) ---
    def save_content(self, session_id: str, bundle: dict) -> None:
        self._r.set_json(RedisKeys.CONTENT.format(session_id=session_id), bundle)

    def get_content(self, session_id: str) -> Optional[dict]:
        return self._r.get_json(RedisKeys.CONTENT.format(session_id=session_id))

    def save_draft(self, session_id: str, draft: dict) -> None:
        self._r.set_json(RedisKeys.DRAFT.format(session_id=session_id), draft)

    def get_draft(self, session_id: str) -> Optional[dict]:
        return self._r.get_json(RedisKeys.DRAFT.format(session_id=session_id))

    # --- deck brief (what the user asked for, gathered across turns) ---
    def set_brief(self, session_id: str, brief: dict) -> None:
        self._r.set_json(RedisKeys.BRIEF.format(session_id=session_id), brief)

    def get_brief(self, session_id: str) -> Optional[dict]:
        return self._r.get_json(RedisKeys.BRIEF.format(session_id=session_id))

    # --- generated slide files (per-slide .pptx cache) ---
    def save_slide_artifacts(self, session_id: str, artifacts: dict) -> None:
        """Store where each slide's generated .pptx lives, so an unchanged slide is
        never regenerated on the next turn.

        Args:
            session_id: Owning session.
            artifacts: {slide_number (str): {"path": storage path, "hash": content hash,
                "fallback": bool}}.

        Returns:
            None.
        """
        self._r.set_json(RedisKeys.SLIDE_ARTIFACTS.format(session_id=session_id), artifacts)

    def get_slide_artifacts(self, session_id: str) -> dict:
        """Return the stored per-slide .pptx map, or an empty map on a first turn.

        Args:
            session_id: Owning session.

        Returns:
            {slide_number (str): {"path": ..., "hash": ..., "fallback": ...}}.
        """
        return self._r.get_json(RedisKeys.SLIDE_ARTIFACTS.format(session_id=session_id)) or {}

    # --- deck-level design context (Quiz / FAQ family layouts) ---
    def save_deck_design(self, session_id: str, context: dict) -> None:
        """Store the family layouts established for this session's deck.

        Args:
            session_id: Owning session.
            context: A `DeckDesignContext` as a JSON-serializable dict.

        Returns:
            None.
        """
        self._r.set_json(RedisKeys.DECK_DESIGN.format(session_id=session_id), context)

    def get_deck_design(self, session_id: str) -> Optional[dict]:
        """Return the stored deck design context, or None before any is established.

        Args:
            session_id: Owning session.

        Returns:
            The `DeckDesignContext` dict, or None.
        """
        return self._r.get_json(RedisKeys.DECK_DESIGN.format(session_id=session_id))

    # --- active skill ---
    def set_active_skill(self, session_id, skill):
        """Pin one validated, immutable skill version to a deck session.

        Args:
            session_id: Owning deck session.
            skill: The skill record to validate and store.

        Returns:
            The normalized record that was stored.
        """
        normalized = validate_active_skill(skill)
        with self._lock:
            self._r.set_json(RedisKeys.ACTIVE_SKILL.format(session_id=session_id),
                             normalized)
        log.info("pinned skill %s v%s to session %s", normalized["skill_id"],
                 normalized["version"], session_id)
        return normalized

    def get_active_skill(self, session_id):
        """Return the validated active skill, or None when the session has none.

        Args:
            session_id: Owning deck session.

        Returns:
            The normalized record, or None.
        """
        value = self._r.get_json(RedisKeys.ACTIVE_SKILL.format(session_id=session_id))
        return validate_active_skill(value) if value is not None else None

    def clear_active_skill(self, session_id):
        """Remove the active skill from a session.

        Args:
            session_id: Owning deck session.
        """
        with self._lock:
            self._r.delete(RedisKeys.ACTIVE_SKILL.format(session_id=session_id))
        log.info("cleared active skill from session %s", session_id)

    def save_skill_reference(self, session_id, reference_id, profile):
        """Keep bounded inspected style metadata separate from factual documents."""
        import json

        if not isinstance(profile, dict):
            raise AppError("skill reference profile must be an object")
        encoded = json.dumps(profile, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":"))
        if len(encoded) > SkillReferenceConstants.MAX_REFERENCE_PROFILE_CHARS:
            raise AppError("skill reference profile exceeds its storage limit")
        key = RedisKeys.SKILL_REFERENCES.format(session_id=session_id)
        with self._lock:
            references = self._r.get_json(key) or {}
            if reference_id not in references and len(references) >= (
                    SkillReferenceConstants.MAX_REFERENCES):
                raise AppError("session has reached its skill reference limit")
            references[reference_id] = {"profile": profile}
            total = sum(len(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
                        for item in references.values())
            if total > SkillReferenceConstants.MAX_SESSION_REFERENCE_CHARS:
                raise AppError("session skill reference profiles exceed their storage limit")
            self._r.set_json(key, references)
        return references[reference_id]

    def get_skill_references(self, session_id):
        """Return this session's inspected style profiles, never another session's."""
        references = self._r.get_json(
            RedisKeys.SKILL_REFERENCES.format(session_id=session_id))
        if references is None:
            return {}
        if not isinstance(references, dict):
            raise AppError("stored skill references are invalid")
        return references

    def get_skill_reference(self, session_id, reference_id):
        """Return one inspected style profile or None when it is not session-owned."""
        return self.get_skill_references(session_id).get(reference_id)

    # --- critic verdict ---
    def save_critique(self, session_id: str, verdict: dict) -> None:
        self._r.set_json(RedisKeys.CRITIQUE.format(session_id=session_id), verdict)

    def get_critique(self, session_id: str) -> Optional[dict]:
        return self._r.get_json(RedisKeys.CRITIQUE.format(session_id=session_id))

    # --- source freshness ---
    def mark_sources_changed(self, session_id: str) -> None:
        self._r.set(RedisKeys.SOURCES_CHANGED.format(session_id=session_id), "1")

    def sources_changed(self, session_id: str) -> bool:
        return self._r.get(RedisKeys.SOURCES_CHANGED.format(session_id=session_id)) == "1"

    def clear_sources_changed(self, session_id: str) -> None:
        self._r.delete(RedisKeys.SOURCES_CHANGED.format(session_id=session_id))

    # --- CkM ---
    def save_ckm(self, ckm: CkM) -> None:
        self._r.set(RedisKeys.CKM.format(doc_id=ckm.doc_id, version=ckm.version),
                    ckm.model_dump_json())

    def get_ckm(self, doc_id: str, version: int) -> Optional[CkM]:
        raw = self._r.get(RedisKeys.CKM.format(doc_id=doc_id, version=version))
        return CkM.model_validate_json(raw) if raw else None

    # --- persistent memory (Postgres fallback for a Redis loss) ---
    def snapshot(self, session_id: str) -> dict:
        """Capture the Redis state a later turn needs, for storage in chat_messages.result.

        Args:
            session_id: The session to capture.

        Returns:
            A JSON-serializable snapshot of plan, draft, content and slide artifacts.
        """
        state = self._load(session_id)
        content = self.get_content(session_id)
        if content and not SessionMemoryConstants.KEEP_CHUNKS:
            content = {**content,
                       "slides": [{**s, "chunks": []} for s in content.get("slides", [])]}
        return {
            "version": SessionMemoryConstants.SNAPSHOT_VERSION,
            "plan": state.plan,
            "plan_status": state.plan_status,
            "draft": self.get_draft(session_id),
            "content": content,
            "slide_artifacts": self.get_slide_artifacts(session_id),
            "brief": self.get_brief(session_id),
            "deck_design": self.get_deck_design(session_id),
            "active_skill": self.get_active_skill(session_id),
            "skill_references": self.get_skill_references(session_id),
        }

    def run_snapshot(self, session_id: str) -> dict:
        """Capture the working state a workflow run may change, taken at run start.

        It is the same shape as :meth:`snapshot` plus the critique and the
        sources-changed flag, so a stopped run can be undone with
        :meth:`restore_snapshot`.

        Args:
            session_id: The session about to run a turn.

        Returns:
            A JSON-serializable pre-run snapshot.
        """
        return {**self.snapshot(session_id),
                "critique": self.get_critique(session_id),
                "sources_changed": self.sources_changed(session_id)}

    def restore_snapshot(self, session_id: str, snap: dict) -> None:
        """Overwrite a session's working state with a pre-run snapshot.

        Unlike :meth:`restore_if_missing`, which only fills gaps after a Redis loss,
        this replaces live values, undoing what a stopped run wrote (plan, draft,
        content, slide cache, critique). Chat messages, documents and decks are left
        alone: a stop never reaches the publish stage, and the user turn is kept.

        Args:
            session_id: The session to roll back.
            snap: A dict from :meth:`run_snapshot`.

        Returns:
            None.
        """
        active_skill = (validate_active_skill(snap["active_skill"])
                        if snap.get("active_skill") is not None else None)
        skill_references = snap.get("skill_references") or {}

        def put(key: str, value) -> None:
            if value:
                self._r.set_json(key.format(session_id=session_id), value)
            else:
                self._r.delete(key.format(session_id=session_id))

        with self._lock:
            state = self._load(session_id)
            state.plan = snap.get("plan")
            state.plan_status = snap.get("plan_status") or PlanStatus.NONE
            self._save(state)
        put(RedisKeys.DRAFT, snap.get("draft"))
        put(RedisKeys.CONTENT, snap.get("content"))
        put(RedisKeys.SLIDE_ARTIFACTS, snap.get("slide_artifacts"))
        put(RedisKeys.BRIEF, snap.get("brief"))
        put(RedisKeys.DECK_DESIGN, snap.get("deck_design"))
        if active_skill is None:
            self.clear_active_skill(session_id)
        else:
            self.set_active_skill(session_id, active_skill)
        references_key = RedisKeys.SKILL_REFERENCES.format(session_id=session_id)
        if skill_references:
            self._r.set_json(references_key, skill_references)
        else:
            self._r.delete(references_key)
        put(RedisKeys.CRITIQUE, snap.get("critique"))
        if snap.get("sources_changed"):
            self.mark_sources_changed(session_id)
        else:
            self.clear_sources_changed(session_id)
        log.info("restored session %s to its pre-run state (plan=%s, draft=%s)",
                 session_id, bool(snap.get("plan")), bool(snap.get("draft")))

    def restore_if_missing(self, session_id: str) -> bool:
        """Rebuild the Redis session from Postgres, only when Redis has lost it.

        Args:
            session_id: The session about to run a turn.

        Returns:
            True if a restore happened, False if Redis already had the session or
            there was nothing durable to restore.
        """
        key = RedisKeys.SESSION.format(session_id=session_id)
        if self._r.get(key) is not None:
            return False                                    # hot path: one GET

        # Each source is read independently so one failing table can't block the rest.
        memory = self._durable(session_id, "session snapshot", {},
                               chat_message_handler.latest_session_memory) or {}
        docs = self._durable(session_id, "source documents", [],
                             document_registry_handler.list_documents, latest_only=False)
        history = self._durable(session_id, "chat history", [],
                                chat_message_handler.list_messages)
        versions = self._durable(session_id, "deck versions", [],
                                 deck_version_handler.list_versions)
        active_skill = (validate_active_skill(memory["active_skill"])
                        if memory.get("active_skill") is not None else None)
        skill_references = memory.get("skill_references") or {}

        if not (memory or docs or history or versions):
            return False                                    # genuinely new session

        # Drop the snapshot from restored messages so Redis doesn't keep a copy on every turn.
        messages = [ChatMessage(
            run_id=m.run_id, role=m.role, content=m.content,
            result={k: v for k, v in (m.result or {}).items()
                    if k != SessionMemoryConstants.RESULT_KEY} or None,
            ts=m.created_at or datetime.now(timezone.utc)) for m in history]
        # ppt_versions is the source of truth for numbering, so the next deck never reuses v1.
        decks = [DeckArtifact(deck_version=v.deck_version, storage_path=v.storage_path,
                              created_at=v.created_at or datetime.now(timezone.utc))
                 for v in versions]

        # The session key is what every other caller tests for, so it must not become
        # visible until the side keys are back too: a concurrent request that sees it
        # would take the early return above and run against a half-restored session.
        with self._lock:
            if self._r.get(key) is not None:
                return False                                # a concurrent request won
            # Side keys are restored only if absent, so a live value is never overwritten.
            if memory.get("draft") and self.get_draft(session_id) is None:
                self.save_draft(session_id, memory["draft"])
            if memory.get("content") and self.get_content(session_id) is None:
                self.save_content(session_id, memory["content"])
            if memory.get("slide_artifacts") and not self.get_slide_artifacts(session_id):
                self.save_slide_artifacts(session_id, memory["slide_artifacts"])
            if memory.get("brief") and self.get_brief(session_id) is None:
                self.set_brief(session_id, memory["brief"])
            if memory.get("deck_design") and self.get_deck_design(session_id) is None:
                self.save_deck_design(session_id, memory["deck_design"])
            if active_skill and self.get_active_skill(session_id) is None:
                self._r.set_json(RedisKeys.ACTIVE_SKILL.format(session_id=session_id),
                                 active_skill)
            if skill_references and not self.get_skill_references(session_id):
                self._r.set_json(
                    RedisKeys.SKILL_REFERENCES.format(session_id=session_id),
                    skill_references)
            self._save(SessionState(
                session_id=session_id, docs=docs, messages=messages, decks=decks,
                plan=memory.get("plan"),
                plan_status=memory.get("plan_status") or PlanStatus.NONE))

        log.info("restored session %s from Postgres: plan=%s, %d slide(s), %d doc(s), "
                 "%d message(s), %d deck version(s), %d cached slide file(s)",
                 session_id, bool(memory.get("plan")),
                 len((memory.get("draft") or {}).get("slides", [])),
                 len(docs), len(messages), len(decks),
                 len(memory.get("slide_artifacts") or {}))
        return True

    @staticmethod
    def _durable(session_id: str, label: str, default, read, *args, **kwargs):
        """Run one Postgres read for a restore, returning `default` if it fails.

        Args:
            session_id: The session being restored, passed first to `read`.
            label: What is being read, for the warning.
            default: Value returned when the read raises.
            read: The handler method to call.
            *args: Extra positional arguments for `read`.
            **kwargs: Extra keyword arguments for `read`.

        Returns:
            The read's result, or `default` on failure.
        """
        try:
            return read(session_id, *args, **kwargs)
        except Exception:                                   # noqa: BLE001
            log.warning("could not read %s from Postgres for session %s",
                        label, session_id, exc_info=True)
            return default


session_store = SessionStore()      # module-level singleton
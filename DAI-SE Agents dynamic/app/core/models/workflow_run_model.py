"""Typed operational workflow-run records (``workflow_runs``)."""

from datetime import datetime

from pydantic import BaseModel

from app.config.constants import RunStatus
from app.utilities.llm_errors import is_retryable_code, message_for


class WorkflowRun(BaseModel):
    """One top-level workflow run: the authoritative state of a user operation."""

    run_id: str
    session_id: str
    user_id: str | None = None
    client_request_id: str | None = None
    status: str
    current_stage: str | None = None
    attempt: int = 1
    owner: str
    last_event_index: int = 0
    deck_version: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    cancel_requested_at: datetime | None = None
    cancel_requested_by: str | None = None
    pre_run_snapshot: str | None = None
    heartbeat_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        """True while the run holds the session's single active slot."""
        return self.status in RunStatus.ACTIVE

    def public(self) -> dict:
        """The client-facing view: no owner, raw error text or session snapshot.

        Returns:
            A JSON-serializable status dictionary.
        """
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "client_request_id": self.client_request_id,
            "status": self.status,
            "is_active": self.is_active,
            "current_stage": self.current_stage,
            "attempt": self.attempt,
            "last_event_index": self.last_event_index,
            "deck_version": self.deck_version,
            "artifact_ready": self.deck_version is not None,
            "error_code": self.error_code,
            "error": message_for(self.error_code) if self.error_code else None,
            "retryable": is_retryable_code(self.error_code),
            "cancel_requested": self.cancel_requested_at is not None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }

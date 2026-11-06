"""API/domain model for a stored deck (.pptx) version."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, StrictBool

from app.config.constants import DeckFeedbackConstants


class DeckVersion(BaseModel):
    session_id: str
    deck_version: int
    storage_path: str
    theme: Optional[str] = None
    plan_title: Optional[str] = None
    status: str = "generated"
    created_at: Optional[datetime] = None
    files: dict[str, str] = {}  # language -> storage path


class SubmitDeckVersionFeedbackRequest(BaseModel):
    """Payload for the single feedback submission allowed on a deck version."""

    thumbs_up: StrictBool
    comment: str | None = Field(
        default=None, max_length=DeckFeedbackConstants.COMMENT_MAX_LENGTH)


class DeckVersionFeedback(BaseModel):
    """Feedback stored for one generated deck version."""

    session_id: str
    deck_version: int
    thumbs_up: bool
    comment: str | None = None
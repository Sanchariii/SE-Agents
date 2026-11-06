"""Typed durable workflow events and discriminated chat timeline items."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class PersistedWorkflowEvent(BaseModel):
    """One user-visible SSE activity event stored for a workflow run."""

    id: int | None = None
    session_id: str
    run_id: str
    event_index: int
    type: str
    agent: str
    message: str
    data: dict = Field(default_factory=dict)
    ts: datetime
    created_at: datetime | None = None


class MessageTimelineItem(BaseModel):
    """A chat turn in the combined session timeline."""

    kind: Literal["message"] = "message"
    id: int | None = None
    run_id: str | None = None
    role: str
    content: str
    result: dict | None = None
    ts: datetime


class WorkflowEventTimelineItem(BaseModel):
    """A workflow activity event in the combined session timeline."""

    kind: Literal["workflow_event"] = "workflow_event"
    id: int | None = None
    run_id: str
    event_index: int
    type: str
    agent: str
    message: str
    data: dict = Field(default_factory=dict)
    ts: datetime


TimelineItem = MessageTimelineItem | WorkflowEventTimelineItem

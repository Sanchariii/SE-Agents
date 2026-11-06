"""Deck approval domain models."""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class Approval(BaseModel):
    id: int
    session_id: str
    project_title: Optional[str] = None
    deck_version: int
    status: str = "pending"             # pending | approved | rejected
    submitted_by: Optional[str] = None
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None
    note: Optional[str] = None
    created_at: Optional[datetime] = None


class SubmitApprovalRequest(BaseModel):
    session_id: str
    deck_version: int
    note: Optional[str] = None


class DecisionRequest(BaseModel):
    note: Optional[str] = None

"""Domain model for a durably-persisted chat turn.

The Redis `ChatMessage` (see document_model) is the fast working copy the agents read
for in-turn context; this is the Postgres system-of-record row, which additionally
carries the structured final-response metadata (`result`) of an assistant turn so the
deck outcome survives a Redis eviction/restart, not just the reply text.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class PersistedChatMessage(BaseModel):
    id: Optional[int] = None
    session_id: str
    run_id: Optional[str] = None
    role: str                              # user | assistant | system
    content: str
    result: Optional[dict] = None          # final-response metadata; assistant turns only
    created_at: Optional[datetime] = None

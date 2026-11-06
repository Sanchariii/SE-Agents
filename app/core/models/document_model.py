"""Shared domain models: documents, extraction output, CkM, session state."""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DocStatus(str, Enum):
    upload_in_progress = "upload_in_progress"   # registered; awaiting a direct-to-blob upload
    uploaded = "uploaded"
    extracting = "extracting"
    extracted = "extracted"
    indexed = "indexed"
    failed = "failed"


class DocumentRecord(BaseModel):
    session_id: str
    doc_id: str
    version: int
    filename: str
    mime_type: str
    sha256: str
    storage_path: str
    is_latest: bool = True
    status: DocStatus = DocStatus.uploaded
    error: Optional[str] = None
    uploaded_at: datetime = Field(default_factory=_now)


# --- direct-to-blob upload (presigned) ---
# The client uploads large files straight to Blob via a short-lived SAS URL, so the
# request never hits the API's ingress body-size limit (the cause of HTTP 413).
class UploadRegisterItem(BaseModel):
    filename: str
    size: Optional[int] = None         # bytes; enables an early size/limit check


class UploadRegisterRequest(BaseModel):
    files: list[UploadRegisterItem]


class UploadTicket(BaseModel):
    doc_id: str
    version: int
    filename: str
    storage_path: str
    upload_url: str                    # PUT bytes here with header 'x-ms-blob-type: BlockBlob'
    expires_in: int                    # seconds the upload_url stays valid


class UploadRegisterResponse(BaseModel):
    session_id: str
    uploads: list[UploadTicket]


class UploadCompleteItem(BaseModel):
    doc_id: str
    version: int


class UploadCompleteRequest(BaseModel):
    documents: list[UploadCompleteItem]



class ExtractedUnit(BaseModel):
    text: str
    section: str = ""
    page: Optional[int] = None
    kind: str = "text"                 # text | table | image_description | transcript | frame_caption
    meta: dict = {}


class CkMChunk(BaseModel):
    chunk_id: str
    doc_id: str
    version: int
    section: str = ""
    page: Optional[int] = None
    kind: str = "text"
    text: str
    meta: dict = {}


class CkM(BaseModel):
    doc_id: str
    version: int
    filename: str
    mime_type: str
    summary: Optional[str] = None
    chunks: list[CkMChunk] = []
    created_at: datetime = Field(default_factory=_now)


class ChatMessage(BaseModel):
    run_id: Optional[str] = None
    role: str                          # user | assistant | system
    content: str
    result: Optional[dict] = None      # final-response metadata for assistant turns
    ts: datetime = Field(default_factory=_now)


class DeckArtifact(BaseModel):
    deck_version: int
    storage_path: str
    plan_snapshot: dict = {}
    created_at: datetime = Field(default_factory=_now)


class SessionState(BaseModel):
    session_id: str
    docs: list[DocumentRecord] = []
    messages: list[ChatMessage] = []
    plan: Optional[dict] = None
    plan_status: str = "none"          # none | draft | awaiting_clarification | confirmed
    decks: list[DeckArtifact] = []
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class RetrievedChunk(BaseModel):
    chunk_id: str
    doc_id: str
    filename: str = ""
    section: str = ""
    kind: str = "text"
    text: str
    score: float = 0.0
    meta: dict = {}
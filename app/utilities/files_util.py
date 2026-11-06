"""MIME detection (extension-based, deterministic) and hashing."""

import hashlib
from pathlib import Path

from app.config.constants import StorageConstants


def detect_mime(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    mime = StorageConstants.EXTENSION_MIME.get(ext)
    if mime is None:
        raise ValueError(f"Unsupported file type: {ext or filename}")
    return mime


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
"""Shared bounds and style-only profile helpers for reference readers."""

import hashlib
import re
from collections import Counter

from app.config.constants import SkillReferenceConstants as Limits
from app.config.exceptions import AppError

_COLOR = re.compile(r"#[0-9a-fA-F]{6}\b")


def check_bytes(data):
    """Reject invalid or oversized uploads and return their stable digest."""
    if not isinstance(data, bytes):
        raise AppError("reference content must be bytes")
    if not data:
        raise AppError("reference content is empty")
    if len(data) > Limits.MAX_REFERENCE_BYTES:
        raise AppError(f"reference exceeds {Limits.MAX_REFERENCE_BYTES} bytes")
    return hashlib.sha256(data).hexdigest()


def color_values(values, limit=None):
    """Return frequent normalized colors, excluding malformed values."""
    counts = Counter(str(value).upper() for value in values
                     if isinstance(value, str) and _COLOR.fullmatch(value))
    return [color for color, _ in counts.most_common(limit or Limits.MAX_STYLE_ITEMS)]


def frequent(values, limit=None):
    """Return stable, case-insensitive frequent labels."""
    counts = Counter(str(value).strip() for value in values if str(value).strip())
    return [value for value, _ in counts.most_common(limit or Limits.MAX_STYLE_ITEMS)]


def profile(source_format, digest, summary, metrics, exemplars):
    """Build the common result shape without retaining source-document wording."""
    return {
        "source_format": source_format,
        "sha256": digest,
        "style_only": True,
        "summary": summary,
        "metrics": metrics,
        "exemplars": exemplars[:Limits.MAX_EXEMPLARS],
    }


def reference_error(message, cause=None):
    """Raise a client-reportable error with a safe, format-specific message."""
    error = AppError(message)
    if cause is not None:
        raise error from cause
    raise error

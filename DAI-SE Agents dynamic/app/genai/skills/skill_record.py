"""Validate the immutable skill version pinned to a deck session."""

import re

from app.config.constants import SkillReferenceConstants as Limits
from app.config.exceptions import AppError

_SKILL_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_BODY_CHARS = 20000
_MAX_ID_CHARS = 128


def _text(record, key, limit, required=True):
    """Validate and normalize one bounded string field.

    Args:
        record: The skill dictionary.
        key: Field name to read.
        limit: Maximum character count.
        required: Whether blank values are rejected and trimmed.

    Returns:
        The validated field value.
    """
    value = record.get(key)
    if not isinstance(value, str) or (required and not value.strip()):
        raise AppError(f"active skill {key} must be a non-empty string")
    if len(value) > limit:
        raise AppError(f"active skill {key} exceeds {limit} characters")
    return value.strip() if required else value


def _references(values):
    """Validate reference identity and digest pairs.

    Args:
        values: The reference records from a skill.

    Returns:
        A normalized list of unique reference records.
    """
    if not isinstance(values, list) or len(values) > Limits.MAX_REFERENCES:
        raise AppError(
            f"active skill references must be a list of at most {Limits.MAX_REFERENCES}")
    cleaned = []
    seen = set()
    for value in values:
        if not isinstance(value, dict) or set(value) != {"reference_id", "sha256"}:
            raise AppError("each skill reference needs only reference_id and sha256")
        ref_id = value.get("reference_id")
        digest = value.get("sha256")
        if not isinstance(ref_id, str) or not ref_id.strip() or len(ref_id) > _MAX_ID_CHARS:
            raise AppError("skill reference_id must be 1-128 characters")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise AppError("skill reference sha256 must be 64 lowercase hexadecimal characters")
        ref_id = ref_id.strip()
        if ref_id in seen:
            raise AppError(f"duplicate skill reference_id {ref_id!r}")
        seen.add(ref_id)
        cleaned.append({"reference_id": ref_id, "sha256": digest})
    return cleaned


def validate_active_skill(value):
    """Return a normalized, bounded skill record or raise a client-reportable error.

    Args:
        value: A dictionary with skill_id, display_name, description, version,
            body_md, and optional references. Each reference is pinned by an
            immutable ID and SHA-256 digest.

    Returns:
        A normalized skill record.
    """
    required = {"skill_id", "display_name", "description", "version", "body_md"}
    allowed = required | {"references"}
    if not isinstance(value, dict):
        raise AppError("active skill must be an object")
    missing = required - value.keys()
    unknown = value.keys() - allowed
    if missing:
        raise AppError(f"active skill is missing: {', '.join(sorted(missing))}")
    if unknown:
        raise AppError(f"active skill has unsupported fields: {', '.join(sorted(unknown))}")

    skill_id = _text(value, "skill_id", 64)
    if not _SKILL_ID.fullmatch(skill_id):
        raise AppError("active skill skill_id must be a lowercase hyphenated name")
    version = value.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise AppError("active skill version must be a positive integer")
    body = _text(value, "body_md", _MAX_BODY_CHARS)
    return {
        "skill_id": skill_id,
        "display_name": _text(value, "display_name", 128),
        "description": _text(value, "description", 1024),
        "version": version,
        "body_md": body,
        "references": _references(value.get("references", [])),
    }

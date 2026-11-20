"""Bind session-owned reference metadata to an active style skill."""

import json

from app.config.constants import SkillReferenceConstants as Limits
from app.config.exceptions import AppError, ConflictError, NotFoundError
from app.genai.skills.skill_record import validate_active_skill

_PROFILE_MARKER = (
    "\n\n---\nSERVER-INSPECTED STYLE-ONLY REFERENCE PROFILES:\n"
)


def _profile_guidance(reference, stored, index):
    profile = (stored or {}).get("profile")
    if not profile:
        raise NotFoundError("skill reference not found in this session")
    if profile.get("sha256") != reference["sha256"]:
        raise ConflictError("skill reference digest does not match the stored reference")
    data = {
        "summary": profile["summary"],
        "metrics": profile["metrics"],
    }
    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False,
                         separators=(",", ":"))
    return (f"Reference {index} ({profile['source_format']}):\n"
            f"{encoded}")


def bind_reference_profiles(skill, stored_references):
    """Validate session ownership and add extracted reference styles to the skill."""
    normalized = validate_active_skill(skill)
    body = normalized["body_md"].split(_PROFILE_MARKER, 1)[0].rstrip()
    guidance = [
        _profile_guidance(reference, stored_references.get(reference["reference_id"]), index)
        for index, reference in enumerate(normalized["references"], 1)
    ]
    if guidance:
        body = body + _PROFILE_MARKER + "\n".join(guidance)
    if len(body) > Limits.MAX_SKILL_PROMPT_CHARS:
        raise AppError(
            f"active skill and reference guidance exceed "
            f"{Limits.MAX_SKILL_PROMPT_CHARS} characters")
    normalized["body_md"] = body
    return normalized

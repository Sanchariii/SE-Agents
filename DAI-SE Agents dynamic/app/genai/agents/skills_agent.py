"""Prepare style-only skill enrichment for the existing deck designer."""

import json
import re

from app.config.exceptions import AppError
from app.genai.guardrails import TRUST_BOUNDARY
from app.genai.skills.skill_tools import build_skill_tools
from app.utilities.agent_utils import ToolCallingAgent

_PROFILE_FIELDS = {
    "source_format", "sha256", "style_only", "summary", "metrics", "exemplars",
}
_PROFILE_FORMATS = {"pptx", "pdf", "image", "png", "jpeg", "website"}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_PROFILE_CHARS = 30000
_MAX_INSTRUCTIONS_CHARS = 4000

SKILLS_AGENT_PROMPT = f"""You prepare style-only enrichment for the existing deck designer.
You do not create, edit, stitch, or publish a final deck, and you do not replace the
designer. The existing designer remains responsible for slide content, layout, code,
validation, and final output.

{TRUST_BOUNDARY}

The supplied JSON contains an inspected reference profile and optional style request.
Use only its bounded style metadata; do not infer source facts or pretend to have seen
visual details that the profile does not contain. Existing brand and readability rules
remain in force. Record conflicts as unresolved guidance; never silently override them.

Create a complete DeckBuilder probe only when it clarifies a reusable style recipe.
Use no type annotations and keep any function at most 50 lines. The generated code is
untrusted. Call execute_code only to learn whether the safety gate permits it; if it is
blocked, do not claim execution or evaluation. Call evaluate_result only after that
attempt. Do not put untested executable code into body_md.

Call run_skill with JSON containing skill_id, display_name, description, positive
integer version, body_md, and optional references. body_md contains declarative style
guidance for the existing designer, not a second deck-generation path, and must stay
within 4000 characters. Explain limitations and unresolved brand conflicts rather than
claiming visual verification."""


def _request_content(reference_profile, user_instructions):
    """Validate and bound the style-only input passed to the agent."""
    if not isinstance(reference_profile, dict):
        raise AppError("skill reference profile must be an object")
    if set(reference_profile) != _PROFILE_FIELDS:
        raise AppError("skill reference profile has unsupported or missing fields")
    if reference_profile.get("style_only") is not True:
        raise AppError("skills agent accepts style-only reference profiles")
    source_format = reference_profile.get("source_format")
    if not isinstance(source_format, str) or source_format not in _PROFILE_FORMATS:
        raise AppError("skill reference profile has an unsupported format")
    if not isinstance(reference_profile.get("sha256"), str) or not _SHA256.fullmatch(
            reference_profile["sha256"]):
        raise AppError("skill reference profile needs a valid SHA-256 digest")
    if (not isinstance(reference_profile.get("summary"), str)
            or not isinstance(reference_profile.get("metrics"), dict)
            or not isinstance(reference_profile.get("exemplars"), list)):
        raise AppError("skill reference profile has invalid style metadata")
    if not isinstance(user_instructions, str):
        raise AppError("skill instructions must be a string")
    if len(user_instructions) > _MAX_INSTRUCTIONS_CHARS:
        raise AppError("skill instructions exceed the 4000-character limit")
    payload = {"reference_profile": reference_profile,
               "user_style_request": user_instructions}
    try:
        content = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise AppError("skill reference profile must contain JSON values") from exc
    if len(content) > _MAX_PROFILE_CHARS:
        raise AppError("skill reference profile exceeds the 30000-character limit")
    return content


class SkillsAgent:
    """Run a bounded skill-preparation agent; never generate a final deck."""

    def __init__(self, session_id):
        self.session_id = session_id
        self.prepared_skill = None
        self.recipe_status = "not_requested"

    def run(self, reference_profile, user_instructions=""):
        """Create skill enrichment from a style-only profile and user request."""
        content = _request_content(reference_profile, user_instructions)
        state = {"code": None, "code_id": None, "execution_status": None, "skill": None}
        agent = ToolCallingAgent(
            name="skills",
            system_prompt=SKILLS_AGENT_PROMPT,
            tools=build_skill_tools(state),
            tool_intent={
                "create_code": "preparing_style_probe",
                "execute_code": "checking_execution_safety",
                "evaluate_result": "evaluating_style_probe",
                "run_skill": "validating_style_enrichment",
            },
            session_id=self.session_id,
            max_concurrency=1,
            redact_args=("code", "skill_record_json"),
        )
        result = agent.run(content)
        self.prepared_skill = state["skill"]
        self.recipe_status = (
            state["execution_status"]
            or ("not_evaluated" if state["code_id"] else "not_requested")
        )
        return result

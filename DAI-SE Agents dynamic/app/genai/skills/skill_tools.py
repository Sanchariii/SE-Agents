"""Tools for preparing and validating style-only skill enrichment."""

import hashlib
import json

from langchain_core.tools import StructuredTool
from pydantic import Field, create_model

from app.config.constants import PptCreatorConstants, SkillReferenceConstants
from app.config.exceptions import AppError
from app.genai.ppt import code_guard
from app.genai.skills.skill_record import validate_active_skill
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

_MAX_CODE_CHARS = PptCreatorConstants.MAX_EXEMPLAR_CODE_CHARS
_MAX_SKILL_JSON_CHARS = 30000
_EXECUTION_BLOCKED = (
    "Code execution is disabled: this service has no isolated worker enforcing "
    "filesystem, network, CPU/memory, timeout, and process-tree limits."
)


def _create_code_tool(state):
    def create_code(code):
        state.update(code=None, code_id=None, execution_status=None)
        source = code_guard.strip_code_fences(code)
        if not source:
            return {"status": "rejected", "reason": "Code must not be empty."}
        if len(source) > _MAX_CODE_CHARS:
            return {"status": "rejected",
                    "reason": f"Code exceeds the {_MAX_CODE_CHARS}-character limit."}
        rejection = code_guard.check_message(source)
        if rejection:
            return {"status": "rejected", "reason": rejection}

        code_id = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
        state.update(code=source, code_id=code_id)
        log.info("skills-agent code prepared: code_id=%s chars=%d", code_id,
                 len(source))
        return {"status": "created", "code_id": code_id, "characters": len(source)}

    return _make_tool(
        create_code, "create_code",
        "Validate and stage a bounded DeckBuilder probe. This does not execute or "
        "persist the code.",
        "code", "Complete DeckBuilder probe source code.",
        min_length=1, max_length=_MAX_CODE_CHARS,
    )


def _execute_code_tool(state):
    def execute_code(code_id):
        if not state["code"] or code_id != state["code_id"]:
            return {"status": "rejected", "executed": False,
                    "reason": "Code ID is missing, stale, or was not created in this run."}
        state["execution_status"] = "blocked"
        log.warning("skills-agent execution blocked: code_id=%s", code_id)
        return {"status": "blocked", "executed": False, "code_id": code_id,
                "reason": _EXECUTION_BLOCKED}

    return _make_tool(
        execute_code, "execute_code",
        "Request probe execution. Execution is blocked until a restricted worker is "
        "available.",
        "code_id", "ID returned by create_code.",
        min_length=1, max_length=64,
    )


def _evaluate_result_tool(state):
    def evaluate_result(code_id):
        if not state["code"] or code_id != state["code_id"]:
            return {"status": "rejected",
                    "reason": "Code ID is missing, stale, or was not created in this run."}
        state["execution_status"] = "not_evaluated"
        return {"status": "not_evaluated", "code_id": code_id,
                "reason": "No artifact was produced by an isolated execution."}

    return _make_tool(
        evaluate_result, "evaluate_result",
        "Evaluate the probe artifact. Reports not_evaluated when execution did not "
        "produce an artifact.",
        "code_id", "ID returned by create_code.",
        min_length=1, max_length=64,
    )


def _run_skill_tool(state):
    def run_skill(skill_record_json):
        state["skill"] = None
        if len(skill_record_json) > _MAX_SKILL_JSON_CHARS:
            return {"status": "rejected",
                    "reason": f"Skill record exceeds {_MAX_SKILL_JSON_CHARS} characters."}
        try:
            record = validate_active_skill(json.loads(skill_record_json))
        except json.JSONDecodeError:
            return {"status": "rejected", "reason": "Skill record must be valid JSON."}
        except AppError as exc:
            return {"status": "rejected", "reason": str(exc)}
        if len(record["body_md"]) > SkillReferenceConstants.MAX_SKILL_PROMPT_CHARS:
            return {
                "status": "rejected",
                "reason": "Skill body exceeds the designer prompt limit of "
                          f"{SkillReferenceConstants.MAX_SKILL_PROMPT_CHARS} characters.",
            }
        state["skill"] = record
        return {
            "status": "validated",
            "validation_scope": "skill-record schema only; not persisted",
            "recipe_status": (
                state["execution_status"]
                or ("not_evaluated" if state["code_id"] else "not_requested")
            ),
            "skill": record,
        }

    return _make_tool(
        run_skill, "run_skill",
        "Validate and return a style-only skill record. This does not save a session "
        "or publish a deck.",
        "skill_record_json",
        "JSON skill record with skill_id, display_name, description, version, "
        "body_md, and optional references.",
        min_length=1, max_length=_MAX_SKILL_JSON_CHARS,
    )


def _make_tool(function, name, description, field_name, field_description,
               min_length, max_length):
    schema_name = "".join(part.title() for part in name.split("_")) + "Input"
    schema = create_model(
        schema_name,
        **{field_name: (str, Field(
            ..., min_length=min_length, max_length=max_length,
            description=field_description,
        ))},
    )
    return StructuredTool.from_function(
        func=function, name=name, description=description, args_schema=schema,
    )


def build_skill_tools(state=None):
    """Create one isolated tool set for a single skills-agent run."""
    if state is None:
        state = {}
    state.setdefault("code", None)
    state.setdefault("code_id", None)
    state.setdefault("execution_status", None)
    state.setdefault("skill", None)
    return [
        _create_code_tool(state),
        _execute_code_tool(state),
        _evaluate_result_tool(state),
        _run_skill_tool(state),
    ]

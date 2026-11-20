"""Session-scoped API for style references and active designer skills."""

import asyncio
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.config.constants import Roles, RouteTags, SkillReferenceConstants as Limits
from app.config.exceptions import (
    AppError, ConflictError, ForbiddenError, NotFoundError,
)
from app.config.settings import get_settings
from app.core.dependencies.auth import require_session_owner_role
from app.core.handlers import document_handler
from app.core.handlers.session_store import session_store
from app.core.handlers.workflow_run_handler import workflow_run_handler
from app.genai.agents.skills_agent import SkillsAgent
from app.genai.skills.reference_profiles import bind_reference_profiles
from app.genai.skills.reference_readers.dispatch import inspect_file
from app.genai.skills.reference_readers.website import inspect_website

skill_router = APIRouter(
    prefix="/decks/{session_id}/skill", tags=[RouteTags.DECKS])
_OWNER = require_session_owner_role(Roles.MEMBER)


def _require_enabled():
    if not get_settings().dynamic_skills_enabled:
        raise ForbiddenError("dynamic skills are disabled")


async def _reject_active_run(session_id):
    active = await asyncio.to_thread(workflow_run_handler.get_active, session_id)
    if active:
        raise ConflictError(
            "finish or stop the active deck run before changing skill state")


def _check_keys(body, required, optional=()):
    if not isinstance(body, dict) or required - body.keys() or body.keys() - (
            required | set(optional)):
        raise AppError("request body has missing or unsupported fields")


def _store_profile(session_id, profile):
    reference_id = uuid.uuid4().hex
    session_store.save_skill_reference(session_id, reference_id, profile)
    return {
        "reference_id": reference_id,
        "sha256": profile["sha256"],
        "profile": profile,
    }


@skill_router.post("/references", status_code=201)
async def upload_skill_reference(
        session_id: str, file: UploadFile = File(...),
        _: dict = Depends(_OWNER)):
    """Inspect an isolated style reference without adding it to factual retrieval."""
    _require_enabled()
    await _reject_active_run(session_id)
    data = await file.read(Limits.MAX_REFERENCE_BYTES + 1)
    if len(data) > Limits.MAX_REFERENCE_BYTES:
        raise HTTPException(status_code=413, detail="skill reference exceeds the upload limit")
    mime = document_handler.validate_upload(file.filename or "", data)
    profile = await asyncio.to_thread(inspect_file, data, mime)
    saved = await asyncio.to_thread(_store_profile, session_id, profile)
    return {"session_id": session_id, **saved}


@skill_router.post("/references/website", status_code=201)
async def add_website_reference(
        session_id: str, body: dict,
        _: dict = Depends(_OWNER)):
    """Inspect a bounded website style profile from an operator-allowlisted host."""
    _require_enabled()
    await _reject_active_run(session_id)
    _check_keys(body, {"url"})
    hosts = get_settings().skill_reference_hosts
    if not hosts:
        raise AppError("website skill references require an operator host allowlist")
    if not isinstance(body["url"], str) or len(body["url"]) > 2048:
        raise AppError("website reference URL must be at most 2048 characters")
    profile = await asyncio.to_thread(inspect_website, body["url"], hosts)
    saved = await asyncio.to_thread(_store_profile, session_id, profile)
    return {"session_id": session_id, **saved}


@skill_router.post("/prepare")
async def prepare_skill(
        session_id: str, body: dict,
        _: dict = Depends(_OWNER)):
    """Use the SkillsAgent to prepare, but not activate, a style-only skill."""
    _require_enabled()
    _check_keys(body, {"reference_id"}, {"instructions"})
    reference_id = body["reference_id"]
    if not isinstance(reference_id, str) or not reference_id or len(reference_id) > 128:
        raise AppError("reference_id must be 1-128 characters")
    instructions = body.get("instructions", "")
    if not isinstance(instructions, str):
        raise AppError("instructions must be a string")
    stored = await asyncio.to_thread(
        session_store.get_skill_reference, session_id, reference_id)
    if stored is None:
        raise NotFoundError("skill reference not found in this session")
    profile = stored["profile"]
    agent = SkillsAgent(session_id)
    result = await asyncio.to_thread(
        agent.run, profile, instructions)
    skill = agent.prepared_skill
    if skill is not None:
        skill["references"] = [{
            "reference_id": reference_id, "sha256": profile["sha256"],
        }]
        skill = bind_reference_profiles(
            skill, {reference_id: stored})
    return {
        "session_id": session_id,
        "status": "prepared" if skill else "not_prepared",
        "skill": skill,
        "recipe_status": agent.recipe_status,
        "message": result.reply,
    }


@skill_router.get("")
async def get_skill(session_id: str, _: dict = Depends(_OWNER)):
    skill = await asyncio.to_thread(session_store.get_active_skill, session_id)
    return {"session_id": session_id, "skill": skill}


@skill_router.put("")
async def activate_skill(
        session_id: str, body: dict,
        _: dict = Depends(_OWNER)):
    """Validate and pin one skill version to this session's existing designer."""
    _require_enabled()
    await _reject_active_run(session_id)
    references = await asyncio.to_thread(
        session_store.get_skill_references, session_id)
    skill = bind_reference_profiles(body, references)
    saved = await asyncio.to_thread(session_store.set_active_skill, session_id, skill)
    return {"session_id": session_id, "skill": saved}


@skill_router.delete("")
async def clear_skill(session_id: str, _: dict = Depends(_OWNER)):
    """Clear even a disabled active skill so it cannot remain selected."""
    await _reject_active_run(session_id)
    await asyncio.to_thread(session_store.clear_active_skill, session_id)
    return {"session_id": session_id, "skill": None}

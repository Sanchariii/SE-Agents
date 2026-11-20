"""Designer-enrichment bounds, precedence, and cache identity."""

from types import SimpleNamespace

import pytest

from app.config.constants import SkillReferenceConstants
from app.config.exceptions import AppError
from app.core.models.deck_model import DeckDraft, DeckPlan, SlideDraft, SlidePlan
from app.genai.agents.brief import DeckBrief
from app.genai.skills.designer_enrichment import (
    add_skill_guidance, prepare_active_skill, skill_cache_suffix,
)
from app.genai.workflows import deck_workflow as workflow


def _skill(**changes):
    skill = {
        "skill_id": "reference-style",
        "display_name": "Reference style",
        "description": "Use the supplied style profile.",
        "version": 1,
        "body_md": "Use generous whitespace and a clear visual hierarchy.",
        "references": [{"reference_id": "ref-1", "sha256": "a" * 64}],
    }
    return {**skill, **changes}


def test_missing_skill_leaves_parameters_and_cache_key_unchanged():
    params = {"slide_type": "NORMAL", "planner_special_instructions": ""}
    assert add_skill_guidance(params, None) is params
    assert skill_cache_suffix(None) == ""


def test_active_skill_is_bounded_and_only_added_as_lower_priority_guidance():
    enrichment = prepare_active_skill(_skill())
    params = add_skill_guidance({"user_instructions": "Use a timeline"}, enrichment)
    assert "USER-SELECTED STYLE SKILL" in params["planner_special_instructions"]
    assert params["planner_special_instructions"].endswith(
        "Use generous whitespace and a clear visual hierarchy.")
    assert params["user_instructions"] == "Use a timeline"
    assert skill_cache_suffix(enrichment).startswith("|skill:")


def test_skill_body_cannot_exceed_the_prompt_budget():
    with pytest.raises(AppError, match="designer prompts"):
        prepare_active_skill(_skill(
            body_md="x" * (SkillReferenceConstants.MAX_SKILL_PROMPT_CHARS + 1)))


def test_skill_content_and_reference_hashes_invalidate_cache():
    first = prepare_active_skill(_skill())
    changed_body = prepare_active_skill(_skill(body_md="Use tighter spacing."))
    changed_reference = prepare_active_skill(_skill(
        references=[{"reference_id": "ref-1", "sha256": "b" * 64}]))
    assert skill_cache_suffix(first) != skill_cache_suffix(changed_body)
    assert skill_cache_suffix(first) != skill_cache_suffix(changed_reference)


def test_skill_key_is_deterministic_for_the_same_record():
    first = prepare_active_skill(_skill())
    second = prepare_active_skill(_skill())
    assert first == second


def test_workflow_design_key_invalidates_only_when_skill_is_active():
    planned = SlidePlan(slide_number=1, title="Overview")
    plan = DeckPlan(title="Training", slides=[planned])
    params = workflow._generation_params(planned, DeckBrief(), plan)
    original = workflow._design_key(params, planned)
    assert workflow._design_key(params, planned, None) == original
    active = workflow._design_key(params, planned, prepare_active_skill(_skill()))
    assert active != original


def _collect_with_skill(monkeypatch, tmp_path, skill):
    plan = DeckPlan(title="Training", slides=[
        SlidePlan(slide_number=1, title="Overview")])
    draft = DeckDraft(title="Training", slides=[
        SlideDraft(slide_number=1, title="Overview")])
    store = SimpleNamespace(
        get_plan=lambda _sid: (plan.model_dump(), ""),
        get_draft=lambda _sid: draft.model_dump(),
        get_content=lambda _sid: None,
        get_active_skill=lambda _sid: skill,
        save_draft=lambda *_args: None,
        save_content=lambda *_args: None,
    )
    captured = {}
    contract = SimpleNamespace(master_index=1, assignments={})

    def build_contract(*_args, **kwargs):
        captured["contract_guidance"] = kwargs["skill_guidance"]
        return contract

    def design(_sid, _draft, _plan, _workdir, _contract, enrichment=None):
        captured["enrichment"] = enrichment
        return [], {}, []

    class Stitcher:
        notes = []

        def __init__(self, _sid):
            pass

        def stitch(self, _files, path):
            return path

    monkeypatch.setattr(workflow, "session_store", store)
    monkeypatch.setattr(workflow.design_contract, "build", build_contract)
    monkeypatch.setattr(workflow, "_design_slides", design)
    monkeypatch.setattr(workflow, "DeckStitcher", Stitcher)
    monkeypatch.setattr(workflow.slide_cache, "save", lambda *_args: None)
    monkeypatch.setattr(workflow, "_step", lambda *_args: None)
    monkeypatch.setattr(workflow.tempfile, "mkdtemp",
                        lambda **_kwargs: str(tmp_path / "work"))
    return workflow._collect({"session_id": "session-1"}), captured


def test_collect_passes_active_skill_to_contract_and_existing_designer(
        monkeypatch, tmp_path):
    result, captured = _collect_with_skill(monkeypatch, tmp_path, _skill())
    assert "generous whitespace" in captured["contract_guidance"]
    assert captured["enrichment"]["key"]
    assert any("brand and readability rules remain fixed" in gap
               for gap in result["deck_gaps"])


def test_collect_keeps_the_no_skill_path_unchanged(monkeypatch, tmp_path):
    result, captured = _collect_with_skill(monkeypatch, tmp_path, None)
    assert captured["contract_guidance"] == ""
    assert captured["enrichment"] is None
    assert result["deck_gaps"] == []


def test_disabled_skill_is_rejected_without_changing_the_no_skill_path(monkeypatch):
    from app.config.exceptions import ForbiddenError

    monkeypatch.setenv("DYNAMIC_SKILLS_ENABLED", "false")
    assert prepare_active_skill(None) is None
    with pytest.raises(ForbiddenError, match="disabled"):
        prepare_active_skill(_skill())

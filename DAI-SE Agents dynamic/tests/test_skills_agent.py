"""Tests for the skills-agent tools and execution safety gate."""

import json

import pytest

from app.config.constants import PptCreatorConstants
from app.config.exceptions import AppError
from app.genai.agents import skills_agent
from app.genai.skills.skill_tools import build_skill_tools


def _tool_map():
    return {tool.name: tool for tool in build_skill_tools()}


def _valid_probe():
    return "from deck_builder import DeckBuilder\n"


def _valid_skill():
    return {
        "skill_id": "reference-style",
        "display_name": "Reference style",
        "description": "Use the supplied style profile.",
        "version": 1,
        "body_md": "Use clear hierarchy and preserve existing brand rules.",
    }


def test_tools_publish_explicit_bounded_schemas():
    tools = _tool_map()
    assert set(tools) == {"create_code", "execute_code", "evaluate_result", "run_skill"}
    schema = tools["create_code"].args_schema.model_json_schema()
    assert schema["properties"]["code"]["maxLength"] == (
        PptCreatorConstants.MAX_EXEMPLAR_CODE_CHARS)
    assert "skill_record_json" in tools["run_skill"].args


def test_create_code_checks_and_stages_without_returning_source():
    tool = _tool_map()["create_code"]
    rejected = tool.invoke({"code": "import os\n"})
    assert rejected["status"] == "rejected"

    created = tool.invoke({"code": f"```python\n{_valid_probe()}```"})
    assert created["status"] == "created"
    assert created["code_id"]
    assert "DeckBuilder" not in str(created)


def test_execute_code_is_blocked_and_reports_no_execution():
    tools = _tool_map()
    missing = tools["execute_code"].invoke({"code_id": "missing"})
    assert missing["status"] == "rejected"

    created = tools["create_code"].invoke({"code": _valid_probe()})
    result = tools["execute_code"].invoke({"code_id": created["code_id"]})
    assert result["status"] == "blocked"
    assert result["executed"] is False
    assert "isolated worker" in result["reason"]


def test_stale_code_ids_cannot_execute_or_evaluate():
    tools = _tool_map()
    first = tools["create_code"].invoke({"code": _valid_probe()})
    tools["create_code"].invoke({"code": "import os\n"})
    assert tools["execute_code"].invoke({"code_id": first["code_id"]})["status"] == (
        "rejected")

    tools["create_code"].invoke({"code": _valid_probe() + "value = 1\n"})
    assert tools["execute_code"].invoke({"code_id": first["code_id"]})["status"] == (
        "rejected")
    assert tools["evaluate_result"].invoke({"code_id": first["code_id"]})["status"] == (
        "rejected")


def test_evaluate_result_does_not_claim_success_without_an_artifact():
    tools = _tool_map()
    created = tools["create_code"].invoke({"code": _valid_probe()})
    tools["execute_code"].invoke({"code_id": created["code_id"]})
    result = tools["evaluate_result"].invoke({"code_id": created["code_id"]})
    assert result["status"] == "not_evaluated"
    assert "No artifact" in result["reason"]


def test_run_skill_validates_schema_and_marks_unexecuted_recipe():
    tools = _tool_map()
    created = tools["create_code"].invoke({"code": _valid_probe()})
    tools["execute_code"].invoke({"code_id": created["code_id"]})
    result = tools["run_skill"].invoke(
        {"skill_record_json": json.dumps(_valid_skill())})
    assert result["status"] == "validated"
    assert result["validation_scope"] == "skill-record schema only; not persisted"
    assert result["recipe_status"] == "blocked"

    invalid = tools["run_skill"].invoke({"skill_record_json": "{bad json"})
    assert invalid["status"] == "rejected"

    oversized = _valid_skill()
    oversized["body_md"] = "x" * 4001
    rejected = tools["run_skill"].invoke(
        {"skill_record_json": json.dumps(oversized)})
    assert rejected["status"] == "rejected"
    assert "designer prompt limit" in rejected["reason"]


def test_agent_accepts_only_style_profiles_and_uses_serial_tool_calls(monkeypatch):
    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run(self, content):
            captured["content"] = content
            return "prepared"

    monkeypatch.setattr(skills_agent, "ToolCallingAgent", FakeAgent)
    profile = {
        "source_format": "image",
        "sha256": "a" * 64,
        "style_only": True,
        "summary": "Blue palette.",
        "metrics": {},
        "exemplars": [],
    }
    assert skills_agent.SkillsAgent("session-1").run(profile) == "prepared"
    assert captured["max_concurrency"] == 1
    assert {tool.name for tool in captured["tools"]} == {
        "create_code", "execute_code", "evaluate_result", "run_skill",
    }
    assert json.loads(captured["content"])["reference_profile"] == profile
    for image_format in ("png", "jpeg"):
        skills_agent._request_content(
            {**profile, "source_format": image_format}, "")
    with pytest.raises(AppError, match="style-only"):
        skills_agent.SkillsAgent("session-1").run({**profile, "style_only": False})
    with pytest.raises(AppError, match="unsupported format"):
        skills_agent.SkillsAgent("session-1").run({**profile, "source_format": []})


def test_run_skill_tool_exposes_only_a_validated_skill_to_its_agent_state():
    state = {}
    tools = {tool.name: tool for tool in build_skill_tools(state)}
    response = tools["run_skill"].invoke(
        {"skill_record_json": json.dumps(_valid_skill())})

    assert response["status"] == "validated"
    assert state["skill"] == response["skill"]
    assert state["skill"]["references"] == []

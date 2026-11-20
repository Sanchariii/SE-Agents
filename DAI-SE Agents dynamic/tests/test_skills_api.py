"""Session authorization and API behavior for dynamic designer skills."""

from types import SimpleNamespace

import pytest

from app.config.exceptions import AppError, ConflictError, NotFoundError
from app.genai.skills.reference_profiles import bind_reference_profiles


def _profile(digest="a" * 64):
    return {
        "source_format": "image",
        "sha256": digest,
        "style_only": True,
        "summary": "Wide image with a dark palette.",
        "metrics": {"dominant_colors": ["#112233"]},
        "exemplars": [{"kind": "image", "aspect_ratio": 1.8}],
    }


def _skill(reference_id=None, digest="a" * 64):
    references = []
    if reference_id:
        references.append({"reference_id": reference_id, "sha256": digest})
    return {
        "skill_id": "clean-layout",
        "display_name": "Clean layout",
        "description": "A concise layout.",
        "version": 1,
        "body_md": "Use clear headings and ample whitespace.",
        "references": references,
    }


@pytest.fixture
def skill_client(app_instance, client, monkeypatch, session_store, claims_member):
    from app.core.dependencies.auth import require_auth, session_registry_handler
    from app.core.services import skill_service

    app_instance.dependency_overrides[require_auth] = lambda: claims_member
    monkeypatch.setattr(session_registry_handler, "owns", lambda *_args: True)
    monkeypatch.setattr(skill_service.workflow_run_handler, "get_active",
                        lambda *_args: None)
    monkeypatch.setattr(skill_service, "session_store", session_store)
    monkeypatch.setenv("DYNAMIC_SKILLS_ENABLED", "true")
    yield client
    app_instance.dependency_overrides.pop(require_auth, None)


def test_reference_upload_is_session_scoped_and_not_ingested(
        skill_client, session_store, monkeypatch):
    from app.core.services import skill_service

    monkeypatch.setattr(skill_service.document_handler, "validate_upload",
                        lambda *_args: "image/png")
    monkeypatch.setattr(skill_service, "inspect_file", lambda *_args: _profile())
    response = skill_client.post(
        "/decks/s1/skill/references",
        files={"file": ("look.png", b"bounded image bytes", "image/png")},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["profile"] == _profile()
    assert body["sha256"] == "a" * 64
    assert session_store.get_skill_reference("s1", body["reference_id"])["profile"] == _profile()
    assert session_store.get_skill_reference("another-session", body["reference_id"]) is None
    assert session_store.get_session("s1").docs == []


def test_activation_binds_server_inspected_profile_and_rejects_hash_mismatch(
        skill_client, session_store):
    session_store.save_skill_reference("s1", "ref-1", _profile())
    response = skill_client.put(
        "/decks/s1/skill", json=_skill("ref-1"))
    assert response.status_code == 200
    assert "#112233" in response.json()["skill"]["body_md"]
    assert session_store.get_active_skill("s1") == response.json()["skill"]
    assert skill_client.get("/decks/s1/skill").json()["skill"] == response.json()["skill"]
    assert skill_client.delete("/decks/s1/skill").json()["skill"] is None
    assert session_store.get_active_skill("s1") is None

    mismatch = skill_client.put(
        "/decks/s1/skill", json=_skill("ref-1", "b" * 64))
    assert mismatch.status_code == 409


def test_skill_prepare_uses_owned_reference_and_reports_recipe_status(
        skill_client, session_store, monkeypatch):
    from app.core.services import skill_service

    session_store.save_skill_reference("s1", "ref-1", _profile())

    class FakeSkillsAgent:
        def __init__(self, _session_id):
            self.prepared_skill = _skill()
            self.recipe_status = "blocked"

        def run(self, profile, instructions):
            assert profile == _profile()
            assert instructions == "Use a muted palette."
            return SimpleNamespace(reply="Prepared with execution disabled.")

    monkeypatch.setattr(skill_service, "SkillsAgent", FakeSkillsAgent)
    response = skill_client.post(
        "/decks/s1/skill/prepare",
        json={"reference_id": "ref-1", "instructions": "Use a muted palette."},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "prepared"
    assert result["recipe_status"] == "blocked"
    assert result["skill"]["references"] == [{
        "reference_id": "ref-1", "sha256": "a" * 64,
    }]
    assert "#112233" in result["skill"]["body_md"]


def test_prepare_rejects_reference_from_another_session(skill_client):
    response = skill_client.post(
        "/decks/s1/skill/prepare", json={"reference_id": "foreign-ref"})
    assert response.status_code == 404


def test_disabled_skills_reject_selection_but_allow_clear(
        skill_client, monkeypatch):
    monkeypatch.setenv("DYNAMIC_SKILLS_ENABLED", "false")
    response = skill_client.put("/decks/s1/skill", json=_skill())
    assert response.status_code == 403
    assert skill_client.delete("/decks/s1/skill").status_code == 200


def test_skill_mutation_is_rejected_during_an_active_deck_run(
        skill_client, monkeypatch):
    from app.core.services import skill_service

    monkeypatch.setattr(
        skill_service.workflow_run_handler, "get_active",
        lambda *_args: SimpleNamespace(run_id="run-1"),
    )
    response = skill_client.put("/decks/s1/skill", json=_skill())
    assert response.status_code == 409


def test_website_references_require_an_operator_allowlist(
        skill_client, monkeypatch):
    monkeypatch.setenv("SKILL_REFERENCE_ALLOWED_HOSTS", "")
    response = skill_client.post(
        "/decks/s1/skill/references/website",
        json={"url": "https://example.com"},
    )
    assert response.status_code == 400
    assert "allowlist" in response.json()["detail"]


def test_skill_routes_require_session_ownership(
        skill_client, monkeypatch):
    from app.core.dependencies.auth import session_registry_handler

    monkeypatch.setattr(session_registry_handler, "owns", lambda *_args: False)
    response = skill_client.get("/decks/s1/skill")
    assert response.status_code == 404


def test_reference_binding_rejects_missing_or_oversized_profiles():
    skill = _skill("ref-1")
    with pytest.raises(NotFoundError):
        bind_reference_profiles(skill, {})
    with pytest.raises(ConflictError):
        bind_reference_profiles(skill, {
            "ref-1": {"profile": _profile("b" * 64)},
        })
    with pytest.raises(AppError, match="4000 characters"):
        bind_reference_profiles(
            {**_skill("ref-1"), "body_md": "x" * 3900},
            {"ref-1": {"profile": _profile()}},
        )

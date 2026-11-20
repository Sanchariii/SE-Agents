"""Active skill validation and session-state persistence."""

import pytest

from app.config.exceptions import AppError


@pytest.fixture
def active_skill():
    return {
        "skill_id": "safety-onboarding",
        "display_name": "Safety onboarding",
        "description": "Training for new maintenance technicians.",
        "version": 4,
        "body_md": "## Visual style\nUse clear process diagrams.",
        "references": [{
            "reference_id": "reference-v3",
            "sha256": "a" * 64,
        }],
    }


def test_active_skill_is_validated_and_pinned_to_the_session(session_store, active_skill):
    saved = session_store.set_active_skill("s1", active_skill)

    assert saved == active_skill
    assert session_store.get_active_skill("s1") == active_skill
    assert session_store.get_active_skill("other") is None


def test_pinning_a_new_version_replaces_the_active_skill(session_store, active_skill):
    session_store.set_active_skill("s1", active_skill)
    updated = {**active_skill, "version": 5}

    session_store.set_active_skill("s1", updated)

    assert session_store.get_active_skill("s1") == updated


@pytest.mark.parametrize("change", [
    {"skill_id": "Bad Name"},
    {"version": True},
    {"version": 0},
    {"body_md": " "},
    {"extra": "not part of the record contract"},
    {"references": [{"reference_id": "r1", "sha256": "bad"}]},
    {"references": [{"reference_id": "r1", "sha256": "a" * 64},
                    {"reference_id": "r1", "sha256": "b" * 64}]},
])
def test_invalid_active_skill_is_rejected(session_store, active_skill, change):
    invalid = {**active_skill, **change}

    with pytest.raises(AppError):
        session_store.set_active_skill("s1", invalid)

    assert session_store.get_active_skill("s1") is None


def test_active_skill_is_in_snapshots_and_restored_after_a_stopped_run(
        session_store, active_skill):
    session_store.set_active_skill("s1", active_skill)
    before = session_store.run_snapshot("s1")
    session_store.clear_active_skill("s1")

    session_store.restore_snapshot("s1", before)

    assert before["active_skill"] == active_skill
    assert session_store.get_active_skill("s1") == active_skill


def test_old_snapshots_clear_skill_state_without_breaking_restore(session_store, active_skill):
    session_store.set_active_skill("s1", active_skill)
    old_snapshot = {"plan": None, "plan_status": "none"}

    session_store.restore_snapshot("s1", old_snapshot)

    assert session_store.get_active_skill("s1") is None


def test_invalid_snapshot_skill_is_rejected_before_rollback_changes_state(
        session_store, active_skill):
    session_store.set_active_skill("s1", active_skill)
    snapshot = {"plan": {"title": "before"}, "active_skill": {"version": 0}}

    with pytest.raises(AppError):
        session_store.restore_snapshot("s1", snapshot)

    assert session_store.get_active_skill("s1") == active_skill
    assert session_store.get_plan("s1")[0] is None


def test_active_skill_is_restored_after_a_redis_loss(
        session_store, monkeypatch, active_skill):
    from app.core.handlers import session_store as module

    memory = {"version": 1, "plan": {"title": "T"}, "active_skill": active_skill}
    monkeypatch.setattr(module.chat_message_handler, "latest_session_memory",
                        lambda *args, **kwargs: memory)

    assert session_store.restore_if_missing("s1") is True
    assert session_store.get_active_skill("s1") == active_skill


def test_durable_restore_does_not_overwrite_a_live_active_skill(
        session_store, monkeypatch, active_skill):
    from app.core.handlers import session_store as module

    existing = {**active_skill, "version": 2}
    session_store.set_active_skill("s1", existing)
    memory = {"version": 1, "active_skill": active_skill}
    monkeypatch.setattr(module.chat_message_handler, "latest_session_memory",
                        lambda *args, **kwargs: memory)
    session_store._r.delete(module.RedisKeys.SESSION.format(session_id="s1"))

    assert session_store.restore_if_missing("s1") is True
    assert session_store.get_active_skill("s1") == existing


def test_deleting_a_session_clears_its_active_skill(session_store, active_skill):
    session_store.set_active_skill("s1", active_skill)
    session_store.save_skill_reference("s1", "ref-1", {
        "source_format": "image", "sha256": "a" * 64, "style_only": True,
        "summary": "Palette.", "metrics": {}, "exemplars": [],
    })

    session_store.delete_session("s1")

    assert session_store.get_active_skill("s1") is None
    assert session_store.get_skill_references("s1") == {}


def test_skill_reference_profiles_are_snapshotted_and_restored(session_store):
    profile = {
        "source_format": "image",
        "sha256": "a" * 64,
        "style_only": True,
        "summary": "A muted palette.",
        "metrics": {"dominant_colors": ["#112233"]},
        "exemplars": [],
    }
    session_store.save_skill_reference("s1", "ref-1", profile)
    snapshot = session_store.run_snapshot("s1")
    session_store._r.delete(
        "skill_references:{session_id}".format(session_id="s1"))

    session_store.restore_snapshot("s1", snapshot)

    assert session_store.get_skill_reference("s1", "ref-1")["profile"] == profile

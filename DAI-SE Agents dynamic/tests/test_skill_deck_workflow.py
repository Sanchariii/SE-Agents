"""The deck workflow builds one deck design contract for either engine, hands it - with
the written deck, the creativity level, the session's style skill and that skill's
reference profiles - to the skill deck agent when DECK_ENGINE=skill_deck, and runs the
deck polish agent on the finished deck when DECK_POLISH is on."""

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

from app.core.models.deck_model import DeckDraft, DeckPlan, SlideDraft, SlidePlan
from app.genai.workflows import deck_workflow as workflow

ENGINE_NOTE = "layout: slide 1: example note"
POLISH_NOTE = "Deck polish (High): scorecard 90 -> 97, 4 change(s)."


PROFILE = {"source_format": "pptx", "sha256": "a" * 64, "exemplars": []}
CONTRACT = SimpleNamespace(master_index=1, assignments={
    1: SimpleNamespace(mode="layout", layout_key="M1-L11", free_region=None)})


def _skill():
    return {
        "skill_id": "reference-style",
        "display_name": "Reference style",
        "description": "Use the supplied style profile.",
        "version": 1,
        "body_md": "Use generous whitespace and a clear visual hierarchy.",
        "references": [{"reference_id": "ref-1", "sha256": "a" * 64}],
    }


def _collect_with_engine(monkeypatch, tmp_path, skill, polish=None):
    plan = DeckPlan(title="Training", slides=[
        SlidePlan(slide_number=1, title="Overview")])
    draft = DeckDraft(title="Training", slides=[
        SlideDraft(slide_number=1, title="Overview")])
    store = SimpleNamespace(
        get_plan=lambda _sid: (plan.model_dump(), ""),
        get_draft=lambda _sid: draft.model_dump(),
        get_content=lambda _sid: None,
        get_active_skill=lambda _sid: skill,
        get_skill_references=lambda _sid: {"ref-1": {"profile": PROFILE}},
        save_draft=lambda *_args: None,
        save_content=lambda *_args: None,
    )
    captured, steps = {}, []

    def build_deck(_sid, _draft, _plan, workdir, classification, contract, catalog=None,
                   creativity="MED", style_guidance="", references=None,
                   image_paths=None, writer=None, on_step=None):
        captured.update(style_guidance=style_guidance, classification=classification,
                        references=references, contract=contract, creativity=creativity,
                        image_paths=image_paths)
        return Path(workdir) / "deck.pptx", [ENGINE_NOTE]

    def classic_designer(*_args, **_kwargs):
        raise AssertionError("the classic designer must not run")

    monkeypatch.setattr(workflow, "session_store", store)
    monkeypatch.setattr(workflow.skill_deck, "enabled", lambda: True)
    monkeypatch.setattr(workflow.skill_deck, "build_deck", build_deck)
    monkeypatch.setattr(workflow.design_contract, "build", lambda *_a, **_k: CONTRACT)
    monkeypatch.setattr(workflow, "_design_slides", classic_designer)
    monkeypatch.setattr(workflow, "materialize_images", lambda *_a, **_k: [])
    monkeypatch.setattr(workflow.DeckBrief, "load",
                        lambda _sid: SimpleNamespace(effective_creativity=lambda: "HIGH"))
    monkeypatch.setattr(workflow.deck_polish, "enabled", lambda: polish is not None)
    if polish is not None:
        monkeypatch.setattr(workflow.deck_polish, "polish_deck", polish)
    monkeypatch.setattr(workflow, "agent_run", lambda _name: nullcontext())
    monkeypatch.setattr(workflow, "_step", lambda _sid, _stage, message: steps.append(message))
    monkeypatch.setattr(workflow.tempfile, "mkdtemp",
                        lambda **_kwargs: str(tmp_path / "work"))
    result = workflow._collect({"session_id": "session-1"})
    return result, captured, steps


def test_collect_builds_with_the_skill_deck_agent_and_the_active_style_skill(
        monkeypatch, tmp_path):
    result, captured, steps = _collect_with_engine(monkeypatch, tmp_path, _skill())
    assert result["pending_publish"] is True
    assert result["deck_path"] == str(tmp_path / "work" / "deck.pptx")
    assert "generous whitespace" in captured["style_guidance"]
    assert captured["references"] == [PROFILE]
    assert captured["contract"] is CONTRACT and captured["creativity"] == "HIGH"
    assert captured["image_paths"] == {}
    assert "Design contract: master M1, 0 slide(s) composed on canvas." in steps
    assert ENGINE_NOTE in result["deck_gaps"]
    assert any("brand and readability rules remain fixed" in gap
               for gap in result["deck_gaps"])


def test_collect_sends_no_style_skill_when_the_session_has_none(monkeypatch, tmp_path):
    result, captured, _ = _collect_with_engine(monkeypatch, tmp_path, None)
    assert result["pending_publish"] is True
    assert captured["style_guidance"] == ""
    assert captured["references"] == []
    assert result["deck_gaps"] == [ENGINE_NOTE]


def test_collect_polishes_the_finished_deck_at_the_creativity_level(monkeypatch, tmp_path):
    seen = {}

    def polish(deck, workdir, level, style_guidance="", writer=None, on_step=None):
        seen.update(deck=deck, level=level, style=style_guidance)
        return Path(workdir) / "polished.pptx", [POLISH_NOTE]

    monkeypatch.setattr(workflow.deck_polish, "level_for", lambda creativity: creativity)
    result, _, _ = _collect_with_engine(monkeypatch, tmp_path, None, polish=polish)
    assert seen["deck"] == tmp_path / "work" / "deck.pptx" and seen["level"] == "HIGH"
    assert result["deck_path"] == str(tmp_path / "work" / "polished.pptx")
    assert result["deck_gaps"] == [ENGINE_NOTE, POLISH_NOTE]


def test_a_polish_failure_publishes_the_deck_as_built(monkeypatch, tmp_path):
    def polish(*_args, **_kwargs):
        raise RuntimeError("polish broke")

    result, _, steps = _collect_with_engine(monkeypatch, tmp_path, None, polish=polish)
    assert result["pending_publish"] is True
    assert result["deck_path"] == str(tmp_path / "work" / "deck.pptx")
    assert "Deck polish failed; the deck is published as built." in steps

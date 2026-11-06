"""Max mode: the render-and-review tier for HIGH-creativity slides.

No gateway, no LibreOffice: the renderer and the judge are patched, and `_request_code`
answers with canned programs that really run in the sandbox and really hit the gate.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config.constants import CreativityLevels, PptCreatorConstants
from app.config.exceptions import SlideRenderError
from app.core.models.deck_model import (
    ContentBlock, DeckContext, SlideCodeBrief, SlideDraft,
)
from app.genai.agents import ppt_creator_agent as creator
from app.genai.agents import slide_reviewer
from app.genai.agents.slide_reviewer import ReviewIssue, VisualVerdict

GOOD_PROGRAM = """
from deck_builder import DeckBuilder
from context import TEMPLATE_PATH, CATALOG, FRAME, OUTPUT_PATH, LAYOUT_KEY

deck = DeckBuilder(TEMPLATE_PATH, CATALOG, frame=FRAME)
slide = deck.add_slide(LAYOUT_KEY)
slide.set_title("generated")
deck.save(OUTPUT_PATH)
"""

COLLIDING_PROGRAM = """
from deck_builder import DeckBuilder
from context import TEMPLATE_PATH, CATALOG, FRAME, OUTPUT_PATH, LAYOUT_KEY

deck = DeckBuilder(TEMPLATE_PATH, CATALOG, frame=FRAME)
slide = deck.add_slide(LAYOUT_KEY)
slide.set_title("collides")
slide.add_card("A", ["one"], 1.0, 6.9, 4.0, 0.5)
deck.save(OUTPUT_PATH)
"""

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 4000


def _brief(level: str, layout_key: str = "M1-L10", kind: str = "content") -> SlideCodeBrief:
    from app.genai.ppt import deck_builder as db
    from app.genai.ppt.template import registry as reg

    return SlideCodeBrief(
        slide_number=2, kind=kind,
        content=SlideDraft(slide_number=2, title="Reflow Profile Control",
                           blocks=[ContentBlock(slot="body1",
                                                bullets=["Preheat under 3 C/s"])]),
        context=DeckContext(title="SMT Quality", position=2, total=5),
        layout_key=layout_key, creativity_level=level,
        frame=db.frame_spec(13.333, 7.5, label="Internal",
                            symbol_path=str(reg.classification_symbol("internal")),
                            slide_number=2))


@pytest.fixture
def agent(tmp_path) -> creator.PptCreatorAgent:
    return creator.PptCreatorAgent("s1", tmp_path / "work")


def _queue(agent, monkeypatch, *programs):
    calls = {"n": 0, "messages": []}

    def fake_request(messages):
        calls["n"] += 1
        calls["messages"] = messages
        return programs[min(calls["n"], len(programs)) - 1]

    monkeypatch.setattr(agent, "_request_code", fake_request)
    return calls


def _max_mode(monkeypatch, verdicts=(), render_error=None, available=True):
    """Switch max mode on with a fake renderer and a queue of judge verdicts."""
    state = {"renders": 0, "reviews": 0, "calls": []}
    monkeypatch.setattr(creator, "get_settings",
                        lambda: SimpleNamespace(slide_max_mode_enabled=True))
    monkeypatch.setattr(creator.slide_render, "available", lambda: available)

    def render(pptx, out_dir):
        state["renders"] += 1
        if render_error is not None:
            raise render_error
        out = Path(out_dir) / f"r{state['renders']}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(_PNG)
        return out

    def review(png, brief, **kwargs):
        state["calls"].append(kwargs)
        state["reviews"] += 1
        return verdicts[min(state["reviews"], len(verdicts)) - 1]

    monkeypatch.setattr(creator.slide_render, "render_png", render)
    monkeypatch.setattr(creator.slide_reviewer, "review", review)
    return state


def _explode(monkeypatch):
    """Any max-mode touch fails the test."""
    def boom(*a, **k):
        raise AssertionError("max mode must not run")
    monkeypatch.setattr(creator, "get_settings", boom)
    monkeypatch.setattr(creator.slide_render, "available", boom)
    monkeypatch.setattr(creator.slide_render, "render_png", boom)
    monkeypatch.setattr(creator.slide_reviewer, "review", boom)


@pytest.mark.parametrize("level", [CreativityLevels.LOW, CreativityLevels.MED])
def test_low_and_med_never_touch_max_mode(agent, monkeypatch, level):
    _explode(monkeypatch)
    _queue(agent, monkeypatch, GOOD_PROGRAM)

    result = agent.build_slide(_brief(level))

    assert result.tier == 1 and not result.max_mode and result.visual_reviews == 0
    context = (agent.workdir / "slide_2" / "context.py").read_text(encoding="utf-8")
    assert "LAYOUT_CHOICES" not in context


def test_high_without_a_renderer_is_todays_ladder(agent, monkeypatch):
    state = _max_mode(monkeypatch, available=False)
    _queue(agent, monkeypatch, GOOD_PROGRAM)

    result = agent.build_slide(_brief(CreativityLevels.HIGH))

    assert not result.max_mode and state["renders"] == 0 and state["reviews"] == 0


def test_high_passes_on_the_first_review(agent, monkeypatch):
    state = _max_mode(monkeypatch, verdicts=[VisualVerdict(
        score=9, issues=[ReviewIssue(severity="minor", problem="nit")])])
    _queue(agent, monkeypatch, GOOD_PROGRAM)

    result = agent.build_slide(_brief(CreativityLevels.HIGH))

    assert result.max_mode and result.visual_passed and result.tier == 1
    assert result.visual_reviews == 1 and result.visual_score == 9
    assert Path(result.pptx_path).name.startswith("max_1_")
    assert state["renders"] == 1


def test_three_failed_reviews_ship_the_best_scored_candidate(agent, monkeypatch):
    state = _max_mode(monkeypatch, verdicts=[VisualVerdict(score=6), VisualVerdict(score=8),
                                             VisualVerdict(score=7)])
    calls = _queue(agent, monkeypatch, GOOD_PROGRAM)

    result = agent.build_slide(_brief(CreativityLevels.HIGH))

    assert state["reviews"] == PptCreatorConstants.MAX_VISUAL_REVIEWS
    assert result.visual_score == 8 and not result.visual_passed and result.gate_passed
    assert Path(result.pptx_path).name.startswith("max_2_")
    # The last redo carried exactly one image: the newest render.
    images = [b for m in calls["messages"][2:] if isinstance(m["content"], list)
              for b in m["content"] if b.get("type") in ("image", "image_url")]
    assert len(images) == 1


def test_never_gate_clean_falls_through_to_tier_one(agent, monkeypatch):
    state = _max_mode(monkeypatch, verdicts=[VisualVerdict(score=10)])
    programs = [COLLIDING_PROGRAM] * PptCreatorConstants.MAX_MODE_GENERATIONS + [GOOD_PROGRAM]
    _queue(agent, monkeypatch, *programs)

    result = agent.build_slide(_brief(CreativityLevels.HIGH))

    assert state["reviews"] == 0 and not result.max_mode and result.tier == 1


def test_a_render_failure_ships_the_gate_clean_candidate_unreviewed(agent, monkeypatch):
    state = _max_mode(monkeypatch, render_error=SlideRenderError("no soffice"))
    _queue(agent, monkeypatch, GOOD_PROGRAM)

    result = agent.build_slide(_brief(CreativityLevels.HIGH))

    assert result.max_mode and result.visual_reviews == 0 and result.gate_passed
    assert state["reviews"] == 0


def test_max_mode_moves_a_content_slide_onto_the_high_creativity_master(agent,
                                                                     monkeypatch):
    from app.genai.ppt.template import catalog as catalog_mod

    catalog = catalog_mod.load()
    stand_in = catalog.get("M1-L10")          # canvas-capable, like the master's title layout
    monkeypatch.setattr(catalog, "high_creativity_title", lambda: stand_in)
    monkeypatch.setattr(catalog, "high_creativity_blank", lambda: None)
    _max_mode(monkeypatch, verdicts=[VisualVerdict(score=10)])
    calls = _queue(agent, monkeypatch, GOOD_PROGRAM)

    result = agent.build_slide(_brief(CreativityLevels.HIGH, layout_key="M1-L11"))

    assert result.max_mode and result.layout_used == "M1-L10"
    first = calls["messages"][1]["content"][0]["text"]
    assert "HIGH CREATIVITY MASTER" in first and "MAX MODE" in first
    context = (agent.workdir / "slide_2" / "context.py").read_text(encoding="utf-8")
    assert "LAYOUT_CHOICES" in context


def test_a_content_slide_moves_onto_the_real_high_creativity_master(agent, monkeypatch):
    from app.genai.ppt.template import catalog as catalog_mod

    catalog = catalog_mod.load()
    title = catalog.high_creativity_title()
    if title is None:
        pytest.skip("this template has no usable High Creativity master")

    moved = agent._max_mode_brief(_brief(CreativityLevels.HIGH, layout_key="M1-L11"),
                                  catalog, None)

    assert moved.layout_key == title.key and moved.mode == "canvas"
    assert len(moved.layout_choices) == 2
    assert all(c.get("title_rect") for c in moved.layout_choices)


def test_cover_slides_keep_their_layout_and_fixed_artwork_is_skipped(agent):
    from app.genai.ppt.template import catalog as catalog_mod

    catalog = catalog_mod.load()
    cover = _brief(CreativityLevels.HIGH, layout_key="M0-L1", kind="cover")
    assert agent._max_mode_brief(cover, catalog, None).layout_key == "M0-L1"
    thanks = _brief(CreativityLevels.HIGH, layout_key="M1-L78", kind="thanks")
    assert agent._max_mode_brief(thanks, catalog, None) is None


def test_the_judges_words_never_reach_the_event_stream(agent, monkeypatch):
    secret = "PLANNER-SECRET-123"
    _max_mode(monkeypatch, verdicts=[VisualVerdict(score=10, summary=secret)])
    _queue(agent, monkeypatch, GOOD_PROGRAM)
    published = []
    monkeypatch.setattr(creator.events, "publish",
                        lambda sid, kind, **data: published.append(data))

    agent.build_slide(_brief(CreativityLevels.HIGH))

    assert published and all(secret not in str(event) for event in published)
    assert any(event.get("phase") == "review" for event in published)


@pytest.mark.parametrize("score,severity,expected", [
    (8, None, False), (9, "major", False), (9, "blocking", False),
    (9, "minor", True), (10, None, True), (9, "weird", False),
])
def test_the_pass_bar(score, severity, expected):
    issues = [ReviewIssue(severity=severity)] if severity else []
    assert slide_reviewer.passed(VisualVerdict(score=score, issues=issues)) is expected


def test_scores_are_clamped():
    assert VisualVerdict(score=0).score == 1 and VisualVerdict(score=11).score == 10


def test_without_images_keeps_the_brief_and_drops_older_renders():
    image = {"type": "image", "source": {}}
    messages = [{"role": "system", "content": "s"},
                {"role": "user", "content": [{"type": "text", "text": "brief"}, image]},
                {"role": "assistant", "content": "code"},
                {"role": "user", "content": [image, {"type": "text", "text": "fix"}]}]
    out = creator._without_images(messages)
    assert out[1] == messages[1]
    assert all(b.get("type") == "text" for b in out[3]["content"])

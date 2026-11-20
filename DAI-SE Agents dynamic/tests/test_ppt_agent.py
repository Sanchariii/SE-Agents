"""Per-slide content agent: research budgets, op gating and the fallback guarantee.

Runs entirely offline — retrieval and the model are stubbed. Layout and composition are
no longer this agent's job (see tests/test_ppt_creator_agent.py), so there is nothing
here about templates or slot budgets.
"""

import pytest

from app.config.constants import SlideAgentConstants
from app.core.models.deck_model import (
    ContentBlock, DeckContext, SlideDraft, SlidePlan, SlideTask,
)
from app.core.models.document_model import RetrievedChunk
from app.genai.agents import ppt_agent
from app.genai.tools import slide_tools


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

@pytest.fixture
def plan() -> SlidePlan:
    return SlidePlan(slide_number=3, title="Reflow Profile Control",
                     key_points=["Preheat ramp under 3 C/s", "Soak 60-120 s"],
                     content_needs=["reflow profile", "soak window"],
                     layout_hint="Title + Bullets")


@pytest.fixture
def context() -> DeckContext:
    return DeckContext(title="SMT Quality", audience="process engineers",
                       position=3, total=8, previous_title="AOI Basics")


@pytest.fixture
def workspace(plan, context) -> slide_tools.SlideWorkspace:
    return slide_tools.SlideWorkspace(
        session_id="s1", task=SlideTask(slide_number=3, op="create"),
        plan=plan, context=context)


def _tools(ws) -> dict:
    return {t.name: t for t in slide_tools.build_slide_tools(ws)}


def _chunk(chunk_id: str, text: str = "Preheat ramp must stay under 3 C/s.", **meta):
    return RetrievedChunk(chunk_id=chunk_id, doc_id="d1", filename="smt.pdf",
                          section="Reflow", kind=meta.pop("kind", "text"),
                          text=text, score=1.0, meta=meta)


# --------------------------------------------------------------------------- #
# research_slide                                                               #
# --------------------------------------------------------------------------- #

def test_research_collects_chunks_and_reports_coverage(workspace, monkeypatch):
    monkeypatch.setattr(slide_tools, "retrieve",
                        lambda *a, **k: [_chunk("c1"), _chunk("c2")])
    out = _tools(workspace)["research_slide"].invoke({"queries": ["reflow profile"]})
    assert len(workspace.chunks) == 2
    assert "2 new excerpt(s)" in out


def test_research_dedupes_within_the_slide(workspace, monkeypatch):
    """The same chunk returned by two queries is stored once."""
    monkeypatch.setattr(slide_tools, "retrieve", lambda *a, **k: [_chunk("c1")])
    tools = _tools(workspace)
    tools["research_slide"].invoke({"queries": ["a", "b"]})
    assert [c.chunk_id for c in workspace.chunks] == ["c1"]


def test_research_budget_is_enforced_by_the_tool(workspace, monkeypatch):
    """The model cannot retrieve forever: the tool refuses once the budget is spent."""
    monkeypatch.setattr(slide_tools, "retrieve", lambda *a, **k: [_chunk("c1")])
    tool = _tools(workspace)["research_slide"]
    for _ in range(SlideAgentConstants.MAX_RESEARCH_ROUNDS):
        tool.invoke({"queries": ["x"]})
    assert "budget spent" in tool.invoke({"queries": ["x"]}).lower()


def test_retrieval_failure_is_recorded_not_raised(workspace, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("search down")
    monkeypatch.setattr(slide_tools, "retrieve", boom)
    _tools(workspace)["research_slide"].invoke({"queries": ["x"]})
    assert workspace.chunks == [] and workspace.notes


def test_research_offers_the_image_assets_it_found(workspace, monkeypatch):
    monkeypatch.setattr(slide_tools, "retrieve",
                        lambda *a, **k: [_chunk("c9", kind="frame_caption",
                                                frame_path="s1/f/1.png")])
    out = _tools(workspace)["research_slide"].invoke({"queries": ["profile"]})
    assert "s1/f/1.png" in out
    assert workspace.keyframes() == ["s1/f/1.png"]


# --------------------------------------------------------------------------- #
# write_content                                                                #
# --------------------------------------------------------------------------- #

def test_write_content_stores_every_block_it_is_given(workspace):
    """No slot budget any more: nothing the agent wrote may be silently dropped."""
    blocks = [{"slot": f"body{i}", "bullets": [f"point {i}"]} for i in range(1, 7)]
    _tools(workspace)["write_content"].invoke({
        "title": "Reflow", "blocks": blocks, "notes": "why it matters"})
    assert len(workspace.candidate.blocks) == 6
    assert workspace.candidate.notes == "why it matters"


def test_write_content_keeps_long_copy_intact(workspace):
    long_text = "word " * 500
    _tools(workspace)["write_content"].invoke({
        "title": "Reflow", "blocks": [{"slot": "body1", "text": long_text}]})
    assert workspace.candidate.blocks[0].text == long_text


def test_write_content_falls_back_to_the_planned_title(workspace):
    _tools(workspace)["write_content"].invoke({
        "title": "  ", "blocks": [{"slot": "body1", "bullets": ["Soak 60-120 s"]}]})
    assert workspace.candidate.title == "Reflow Profile Control"


def test_write_content_carries_the_visual_steer_to_the_designer(workspace):
    _tools(workspace)["write_content"].invoke({
        "title": "Reflow", "blocks": [], "visual": "3-stage process flow"})
    assert workspace.candidate.layout == "3-stage process flow"


def test_write_content_keeps_an_image_ref_out_of_visible_copy(workspace):
    _tools(workspace)["write_content"].invoke({
        "title": "Reflow",
        "blocks": [{"slot": "picture1", "image_ref": "s1/f/1.png"}]})
    block = workspace.candidate.blocks[0]
    assert block.image_ref == "s1/f/1.png"
    assert not block.text and not block.bullets


# --------------------------------------------------------------------------- #
# run_ppt_agent: op gating                                                     #
# --------------------------------------------------------------------------- #

@pytest.fixture
def no_agent(monkeypatch):
    """Replace the tool-calling loop with a recorder, so ops are testable offline."""
    calls = {}

    class _Recorder:
        def __init__(self, name, system_prompt, tools, **kwargs):
            calls["tools"] = [t.name for t in tools]

        def run(self, user_content):
            calls["brief"] = user_content
            return None

    monkeypatch.setattr(ppt_agent, "ToolCallingAgent", _Recorder)
    return calls


def _run(op, plan, context, **kwargs):
    return ppt_agent.run_ppt_agent(
        "s1", SlideTask(slide_number=3, op=op, **kwargs.pop("task", {})),
        plan, context, **kwargs)


def test_keep_returns_the_stored_slide_without_touching_the_model(plan, context, no_agent):
    stored = SlideDraft(slide_number=3, title="Kept",
                        blocks=[ContentBlock(slot="body1", bullets=["unchanged"])])
    result = _run("keep", plan, context, previous=stored)
    assert result.slide_draft.title == "Kept"
    assert "tools" not in no_agent            # the agent was never constructed


def test_rewrite_is_denied_the_research_tool(plan, context, no_agent):
    _run("rewrite", plan, context)
    assert "research_slide" not in no_agent["tools"]
    assert "write_content" in no_agent["tools"]


def test_create_gets_every_tool(plan, context, no_agent):
    _run("create", plan, context)
    assert set(no_agent["tools"]) == {"research_slide", "write_content"}


def test_rewrite_reuses_the_stored_excerpts(plan, context, no_agent):
    result = _run("rewrite", plan, context, chunks=[_chunk("c1")])
    assert [c.chunk_id for c in result.chunks] == ["c1"]


def test_reuse_chunks_false_starts_from_nothing(plan, context, no_agent):
    result = _run("create", plan, context, chunks=[_chunk("c1")],
                  task={"reuse_chunks": False})
    assert result.chunks == []


def test_relayout_keeps_the_copy_and_only_restyles(plan, context, monkeypatch):
    """A design change must not cost a writer call — the copy is already approved."""
    def _boom(*a, **k):
        raise AssertionError("relayout must not construct the agent")
    monkeypatch.setattr(ppt_agent, "ToolCallingAgent", _boom)

    stored = SlideDraft(slide_number=3, title="Reflow",
                        blocks=[ContentBlock(slot="body1", bullets=["Preheat 3 C/s"])])
    result = ppt_agent.run_ppt_agent(
        "s1", SlideTask(slide_number=3, op="relayout",
                        layout_hint="3-stage process flow"),
        plan, context, previous=stored)
    assert result.slide_draft.layout == "3-stage process flow"
    assert result.slide_draft.blocks                     # copy survived the restyle


def test_a_silent_agent_still_produces_a_slide(plan, context, no_agent):
    """The agent writing nothing must never drop the slide from the deck."""
    result = _run("create", plan, context)
    assert result.slide_draft.title == "Reflow Profile Control"
    assert result.slide_draft.blocks                     # built from key_points
    assert "fallback" in result.notes


def test_an_agent_crash_is_contained(plan, context, monkeypatch):
    class _Crash:
        def __init__(self, *a, **k):
            pass

        def run(self, _):
            raise RuntimeError("gateway exploded")
    monkeypatch.setattr(ppt_agent, "ToolCallingAgent", _Crash)
    result = ppt_agent.run_ppt_agent("s1", SlideTask(slide_number=3, op="create"),
                                     plan, context)
    assert result.slide_draft is not None
    assert "agent failed" in result.notes


def test_the_brief_carries_neighbouring_context_not_the_whole_deck(plan, context, no_agent):
    _run("create", plan, context)
    brief = no_agent["brief"]
    assert "slide 3 of 8" in brief and "AOI Basics" in brief

"""The code-generating slide designer: the repair loop, the sandbox and the fallback.

No gateway and no real model anywhere — `_request_code` is stubbed with canned programs,
and the programs themselves really do run in a subprocess, which is what makes the
success / repair / gate / exhausted paths meaningful.
"""

from pathlib import Path

import pytest

from app.config.constants import PptCreatorConstants
from app.config.exceptions import UpstreamError
from app.core.models.deck_model import (
    ContentBlock, DeckContext, SlideCodeBrief, SlideDraft,
)
from app.genai.agents import ppt_creator_agent as creator


GOOD_PROGRAM = """
from deck_builder import DeckBuilder
from context import TEMPLATE_PATH, CATALOG, FRAME, OUTPUT_PATH

deck = DeckBuilder(TEMPLATE_PATH, CATALOG, frame=FRAME)
slide = deck.add_slide("M1-L10")
slide.set_title("generated")
deck.save(OUTPUT_PATH)
"""

# Lands a card straight across the footer band: runs fine, fails gate rule 5.
COLLIDING_PROGRAM = """
from deck_builder import DeckBuilder
from context import TEMPLATE_PATH, CATALOG, FRAME, OUTPUT_PATH

deck = DeckBuilder(TEMPLATE_PATH, CATALOG, frame=FRAME)
slide = deck.add_slide("M1-L10")
slide.set_title("collides")
slide.add_card("A", ["one"], 1.0, 6.9, 4.0, 0.5)
deck.save(OUTPUT_PATH)
"""

# Rejected by the AST guard before it ever runs.
FORBIDDEN_PROGRAM = """
from pptx import Presentation
prs = Presentation()
prs.save("out.pptx")
"""

BROKEN_PROGRAM = "from deck_builder import DeckBuilder\nraise RuntimeError('boom')\n"

SILENT_PROGRAM = "from deck_builder import DeckBuilder\nprint('I wrote nothing')\n"


@pytest.fixture
def brief() -> SlideCodeBrief:
    from app.genai.ppt import deck_builder as db
    from app.genai.ppt.template import registry as reg

    return SlideCodeBrief(
        slide_number=2,
        content=SlideDraft(slide_number=2, title="Reflow Profile Control",
                           blocks=[ContentBlock(slot="body1",
                                                bullets=["Preheat under 3 C/s"])],
                           notes="why it matters"),
        context=DeckContext(title="SMT Quality", position=2, total=5),
        layout_key="M1-L10",
        frame=db.frame_spec(13.333, 7.5, label="Internal",
                            symbol_path=str(reg.classification_symbol("internal")),
                            slide_number=2))


@pytest.fixture
def agent(tmp_path) -> creator.PptCreatorAgent:
    return creator.PptCreatorAgent("s1", tmp_path / "work")


def _queue(agent, monkeypatch, *programs):
    """Answer each code request with the next canned program."""
    calls = {"n": 0, "messages": []}

    def fake_request(messages):
        calls["n"] += 1
        calls["messages"] = messages
        return programs[min(calls["n"], len(programs)) - 1]

    monkeypatch.setattr(agent, "_request_code", fake_request)
    return calls


def _refuse_tier2(agent, monkeypatch):
    """Make tier 2 unavailable, so an exhausted tier 1 lands on tier 3.

    The tier-1 tests are about tier 1. Letting them reach `_request_layout_plan` would
    put a real gateway call in the suite.

    This stub raises `UpstreamError`, so it simulates a GATEWAY that never answers
    rather than a plan that will not fit. Tier 2 therefore gives up after
    MAX_UPSTREAM_RETRIES calls, not MAX_LAYOUT_PLAN_ATTEMPTS: a call that comes back
    with nothing teaches the model nothing, so it must not spend the design budget.
    """
    calls = {"n": 0}

    def fake_plan(messages):
        calls["n"] += 1
        raise UpstreamError("tier 2 is stubbed out in this test")

    monkeypatch.setattr(agent, "_request_layout_plan", fake_plan)
    return calls


def test_a_working_program_produces_the_slide_on_the_first_attempt(agent, brief,
                                                                   monkeypatch):
    calls = _queue(agent, monkeypatch, GOOD_PROGRAM)

    result = agent.build_slide(brief)

    assert calls["n"] == 1
    assert result.attempts == 1 and not result.fallback_used
    assert Path(result.pptx_path).exists()


def test_a_broken_program_is_repaired_and_then_succeeds(agent, brief, monkeypatch):
    calls = _queue(agent, monkeypatch, BROKEN_PROGRAM, GOOD_PROGRAM)

    result = agent.build_slide(brief)

    assert result.attempts == 2 and not result.fallback_used
    assert Path(result.pptx_path).exists()
    # The failing program and its traceback must both be in the repair prompt.
    repair = calls["messages"][-1]["content"]
    assert "boom" in repair and "OUTPUT_PATH" in repair


def test_a_program_that_writes_nothing_counts_as_a_failure(agent, brief, monkeypatch):
    calls = _queue(agent, monkeypatch, SILENT_PROGRAM)
    tier2 = _refuse_tier2(agent, monkeypatch)

    result = agent.build_slide(brief)

    assert calls["n"] == PptCreatorConstants.MAX_CODE_FIX_ATTEMPTS
    # The stub always fails upstream, which is capped separately from - and
    # never charged to - the design budget.
    assert tier2["n"] == PptCreatorConstants.MAX_UPSTREAM_RETRIES
    assert result.fallback_used and result.tier == 3
    # Tier 1 spends its whole design budget; tier 2's stub only ever fails upstream,
    # so it stops at the upstream cap. `attempts` on a tier-3 result is every gateway
    # call the slide cost, which is the sum of those two.
    assert result.attempts == (PptCreatorConstants.MAX_CODE_FIX_ATTEMPTS
                               + PptCreatorConstants.MAX_UPSTREAM_RETRIES)
    assert result.generations == result.attempts
    assert result.upstream_failures == PptCreatorConstants.MAX_UPSTREAM_RETRIES


def test_the_repair_budget_is_spent_then_the_slide_is_rendered_on_a_layout(
        agent, brief, monkeypatch, caplog):
    calls = _queue(agent, monkeypatch, BROKEN_PROGRAM)
    tier2 = _refuse_tier2(agent, monkeypatch)

    published = []
    monkeypatch.setattr(creator.events, "publish",
                        lambda sid, kind, **data: published.append(data))

    result = agent.build_slide(brief)

    assert calls["n"] == PptCreatorConstants.MAX_CODE_FIX_ATTEMPTS
    # The stub always fails upstream, which is capped separately from - and
    # never charged to - the design budget.
    assert tier2["n"] == PptCreatorConstants.MAX_UPSTREAM_RETRIES
    assert result.fallback_used and result.tier == 3
    # Tier 1 spends its whole design budget; tier 2's stub only ever fails upstream,
    # so it stops at the upstream cap. `attempts` on a tier-3 result is every gateway
    # call the slide cost, which is the sum of those two.
    assert result.attempts == (PptCreatorConstants.MAX_CODE_FIX_ATTEMPTS
                               + PptCreatorConstants.MAX_UPSTREAM_RETRIES)
    assert result.generations == result.attempts
    assert result.upstream_failures == PptCreatorConstants.MAX_UPSTREAM_RETRIES
    assert Path(result.pptx_path).exists()
    # The user is told, not just the log.
    assert any(event.get("phase") == "fallback" for event in published)
    # Both tier boundaries must be visible in the log: tier 1 gave up, tier 3 ran.
    messages = [record.getMessage() for record in caplog.records]
    assert any("leaving tier 1 unsatisfied" in message for message in messages)
    assert any("entering tier 3" in message for message in messages)


def test_a_gateway_failure_is_retried_like_any_other_failure(agent, brief, monkeypatch):
    calls = {"n": 0}

    def flaky(messages):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("gateway 500")
        return GOOD_PROGRAM

    monkeypatch.setattr(agent, "_request_code", flaky)
    result = agent.build_slide(brief)
    assert calls["n"] == 2 and not result.fallback_used


def test_the_deterministic_fallback_renders_on_a_real_template_layout(brief, agent,
                                                                      tmp_path):
    """plain_slide_pptx built on a blank layout, which inherited no theme at all. The
    fallback now renders the same content on a layout the template designed, so even a
    slide the model could not build is on-brand and passes the gate."""
    from pptx import Presentation

    path = agent._render_on_layout(brief, tmp_path / "fallback.pptx")
    deck = Presentation(str(path))
    slide = next(iter(deck.slides))

    assert slide.slide_layout.name != "Blank"
    text = " ".join(s.text_frame.text for s in slide.shapes if s.has_text_frame)
    assert "Reflow Profile Control" in text
    assert "Preheat under 3 C/s" in text
    assert slide.notes_slide.notes_text_frame.text == "why it matters"


def test_the_fallback_slide_itself_passes_the_geometry_gate(brief, agent, tmp_path):
    from pptx import Presentation

    from app.genai.ppt import geometry as geo
    from app.genai.ppt.template import catalog as catalog_mod

    path = agent._render_on_layout(brief, tmp_path / "fallback.pptx")
    catalog = catalog_mod.load()
    layout_name = next(iter(Presentation(str(path)).slides)).slide_layout.name
    layout = catalog.resolve_layout(layout_name)
    violations = geo.check_slide(path, layout, frame=brief.frame,
                                 theme_colors=catalog.theme.colors)
    assert geo.is_clean(violations), geo.format_violations(violations)


# --------------------------------------------------------------------------- #
# the repair loop now reacts to three kinds of failure, on one budget          #
# --------------------------------------------------------------------------- #

def test_a_guard_rejection_is_repaired_without_costing_an_execution(agent, brief,
                                                                    monkeypatch):
    executed = []
    original = agent._execute
    monkeypatch.setattr(agent, "_execute",
                        lambda *a, **k: (executed.append(1), original(*a, **k))[1])
    calls = _queue(agent, monkeypatch, FORBIDDEN_PROGRAM, GOOD_PROGRAM)

    result = agent.build_slide(brief)

    assert result.attempts == 2 and not result.fallback_used
    assert len(executed) == 1, "the rejected program must never have been run"
    feedback = calls["messages"][-1]["content"]
    assert "Presentation(" in feedback


def test_a_gate_violation_is_fed_back_and_repaired(agent, brief, monkeypatch):
    calls = _queue(agent, monkeypatch, COLLIDING_PROGRAM, GOOD_PROGRAM)

    result = agent.build_slide(brief)

    assert result.attempts == 2 and not result.fallback_used
    feedback = calls["messages"][-1]["content"]
    assert "rule 5" in feedback
    # The feedback must be actionable, not a bare assertion (spec's requirement).
    assert any(word in feedback for word in ("move", "shift", "shorten", "narrow"))


def test_a_slide_that_only_ever_collides_falls_back_to_an_on_layout_render(agent, brief,
                                                                          monkeypatch):
    calls = _queue(agent, monkeypatch, COLLIDING_PROGRAM)
    tier2 = _refuse_tier2(agent, monkeypatch)

    result = agent.build_slide(brief)

    assert calls["n"] == PptCreatorConstants.MAX_CODE_FIX_ATTEMPTS
    # The stub always fails upstream, which is capped separately from - and
    # never charged to - the design budget.
    assert tier2["n"] == PptCreatorConstants.MAX_UPSTREAM_RETRIES
    assert result.fallback_used and result.tier == 3
    # Tier 1 spends its whole design budget; tier 2's stub only ever fails upstream,
    # so it stops at the upstream cap. `attempts` on a tier-3 result is every gateway
    # call the slide cost, which is the sum of those two.
    assert result.attempts == (PptCreatorConstants.MAX_CODE_FIX_ATTEMPTS
                               + PptCreatorConstants.MAX_UPSTREAM_RETRIES)
    assert result.generations == result.attempts
    assert result.upstream_failures == PptCreatorConstants.MAX_UPSTREAM_RETRIES
    assert Path(result.pptx_path).exists()


def test_the_three_failure_kinds_share_one_budget(agent, brief, monkeypatch):
    """A slide that fails a guard check, then a traceback, then the gate, then runs out
    must not get three separate budgets."""
    calls = _queue(agent, monkeypatch, FORBIDDEN_PROGRAM, BROKEN_PROGRAM,
                   COLLIDING_PROGRAM, COLLIDING_PROGRAM, COLLIDING_PROGRAM,
                   GOOD_PROGRAM)
    tier2 = _refuse_tier2(agent, monkeypatch)

    result = agent.build_slide(brief)

    assert calls["n"] == PptCreatorConstants.MAX_CODE_FIX_ATTEMPTS
    # The stub always fails upstream, which is capped separately from - and
    # never charged to - the design budget.
    assert tier2["n"] == PptCreatorConstants.MAX_UPSTREAM_RETRIES
    assert result.fallback_used and result.tier == 3
    # Tier 1 spends its whole design budget; tier 2's stub only ever fails upstream,
    # so it stops at the upstream cap. `attempts` on a tier-3 result is every gateway
    # call the slide cost, which is the sum of those two.
    assert result.attempts == (PptCreatorConstants.MAX_CODE_FIX_ATTEMPTS
                               + PptCreatorConstants.MAX_UPSTREAM_RETRIES)
    assert result.generations == result.attempts
    assert result.upstream_failures == PptCreatorConstants.MAX_UPSTREAM_RETRIES


def test_a_clean_slide_is_accepted_without_a_repair_round(agent, brief, monkeypatch):
    calls = _queue(agent, monkeypatch, GOOD_PROGRAM)
    result = agent.build_slide(brief)
    assert calls["n"] == 1 and result.attempts == 1 and not result.fallback_used


# --------------------------------------------------------------------------- #
# the sealed sandbox                                                           #
# --------------------------------------------------------------------------- #

def test_the_builder_is_copied_into_the_slide_directory(tmp_path):
    from app.genai.ppt import sandbox
    from app.genai.ppt.template import catalog as catalog_mod

    catalog = catalog_mod.load()
    sandbox.stage(tmp_path, catalog, {"frame": {}}, [])
    assert (tmp_path / "deck_builder.py").is_file()
    assert (tmp_path / "catalog.json").is_file()
    assert (tmp_path / "context.py").is_file()


def test_the_copied_builder_is_importable_with_no_app_on_the_path(tmp_path):
    """The whole point of the sealed sandbox: the copy must stand alone."""
    import subprocess
    import sys

    from app.genai.ppt import sandbox
    from app.genai.ppt.template import catalog as catalog_mod

    sandbox.stage(tmp_path, catalog_mod.load(), {"frame": {}}, [])
    probe = tmp_path / "probe.py"
    probe.write_text(
        "from deck_builder import DeckBuilder\n"
        "from context import TEMPLATE_PATH, CATALOG\n"
        "deck = DeckBuilder(TEMPLATE_PATH, CATALOG)\n"
        "print('ok', len(deck.catalog.layouts))\n", encoding="utf-8")
    finished = subprocess.run(
        [sys.executable, str(probe)], cwd=str(tmp_path), capture_output=True, text=True,
        env=creator.PptCreatorAgent._sandbox_env(tmp_path))
    assert finished.returncode == 0, finished.stderr
    # The copy must see every layout the app's own catalog does.
    assert f"ok {len(catalog_mod.load().layouts)}" in finished.stdout


def test_the_sandbox_env_still_carries_no_pythonpath_and_no_secrets(tmp_path,
                                                                    monkeypatch):
    monkeypatch.setenv("APIM_CLIENT_SECRET", "super-secret")
    env = creator.PptCreatorAgent._sandbox_env(tmp_path)
    assert "PYTHONPATH" not in env
    assert "APIM_CLIENT_SECRET" not in env


def test_sandbox_paths_are_redirected_into_the_slide_directory(tmp_path):
    source = "open('/tmp/deck.pptx', 'wb')"
    rewritten = creator.PptCreatorAgent._normalize_code(source, tmp_path)
    assert "/tmp/" not in rewritten
    assert tmp_path.as_posix() in rewritten


def test_the_generated_program_never_inherits_service_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("APIM_CLIENT_SECRET", "super-secret")
    env = creator.PptCreatorAgent._sandbox_env(tmp_path)
    assert "APIM_CLIENT_SECRET" not in env
    assert env["TMP"] == str(tmp_path)


def test_a_hanging_program_is_killed_and_reported(agent, tmp_path, monkeypatch):
    monkeypatch.setattr(PptCreatorConstants, "EXEC_TIMEOUT_SECONDS", 1)
    code = tmp_path / "hang.py"
    code.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    succeeded, error = agent._execute(code, tmp_path)
    assert not succeeded and "did not finish" in error


# --------------------------------------------------------------------------- #
# the brand guide                                                              #
# --------------------------------------------------------------------------- #

def test_the_brand_guide_actually_loads():
    """This failed silently for the whole life of the feature: the constant said
    SKILL.md and the file on disk was rockwell-skill.md, so load_brand_skill() logged a
    warning and returned "" on every single slide."""
    creator.load_brand_skill.cache_clear()
    guide = creator.load_brand_skill()
    assert guide.strip(), "the brand guide is empty — the model gets no brand guidance"
    assert len(guide) > 2000


def test_the_guide_teaches_the_deckbuilder_api():
    guide = creator.load_brand_skill()
    for component in ("add_card", "add_stat", "add_icon_circle", "add_table",
                      "add_chart", "add_textbox", "set_title", "set_bullets"):
        assert component in guide, f"the guide never mentions {component}"


def test_the_guide_does_not_teach_the_code_the_guard_rejects():
    """The old guide taught hand-built textboxes and literal hex. Loading that would
    train the model to write programs code_guard is specified to refuse."""
    guide = creator.load_brand_skill()
    assert "prs.slide_layouts" not in guide
    assert "slide.shapes.add_textbox" not in guide
    assert "Presentation()" not in guide
    assert "## Available Layouts" not in guide      # the hardcoded layout list


def test_the_guide_says_layouts_are_discovered_at_runtime():
    guide = creator.load_brand_skill()
    assert "discovered at runtime" in guide
    assert "get_layout_details" in guide


def test_the_guide_covers_the_four_additions_this_design_needs():
    guide = creator.load_brand_skill()
    assert "canvas" in guide.lower()                  # composition modes
    assert "12" in guide and "grid" in guide.lower()  # the grid
    assert "classification" in guide.lower()          # the reserved zone
    for size in ("32", "28", "20", "15", "14", "12", "11", "10"):
        assert f"{size}" in guide                     # the type scale


# --------------------------------------------------------------------------- #
# response parsing                                                             #
# --------------------------------------------------------------------------- #

def test_code_is_read_from_the_code_execution_block():
    response = {"content": [
        {"type": "text", "text": "here you go"},
        {"type": PptCreatorConstants.CODE_BLOCK_TYPE,
         "name": "text_editor_code_execution",
         "input": {"file_text": "print('hi')"}},
    ]}
    assert creator._extract_code(response) == "print('hi')"


def test_code_is_read_from_a_fenced_reply_when_there_is_no_tool_block():
    response = {"choices": [{"message": {"content": "```python\nprint('hi')\n```"}}]}
    assert creator._extract_code(response).strip() == "print('hi')"


def test_a_reply_with_no_code_at_all_is_an_upstream_error():
    with pytest.raises(UpstreamError):
        creator._extract_code({"content": [{"type": "text", "text": "sorry"}]})


def test_images_that_cannot_be_read_are_skipped_not_fatal(tmp_path, monkeypatch):
    slide = SlideDraft(slide_number=1, title="T", blocks=[
        ContentBlock(slot="picture1", image_ref="s1/frames/1.png"),
        ContentBlock(slot="picture2", image_ref="s1/frames/2.png")])

    def half_broken(path):
        if path.endswith("1.png"):
            return b"binary-image"
        raise RuntimeError("blob gone")

    monkeypatch.setattr(creator.storage, "read_file", half_broken)
    paths = creator.materialize_images(slide, tmp_path)
    assert len(paths) == 1 and Path(paths[0]).read_bytes() == b"binary-image"


def test_the_prompt_carries_the_layout_contract_the_program_needs(agent, brief):
    """The design has the model fetch this with get_layout_details, but _request_code
    offers only the code-execution tool, so a prompt that told it to call one would
    leave it with a layout key and nothing else: no placeholder idx to address, no
    geometry, no reserved zones."""
    import json

    text = agent._user_prompt(brief)[0]["text"]
    assert "M1-L10" in text
    start = text.index("{", text.index("THE LAYOUT YOU WERE ASSIGNED"))
    details = json.loads(text[start:text.rindex("}", start, text.index("Save to")) + 1])
    assert details["key"] == "M1-L10"
    assert any(p["role"] == "title" for p in details["placeholders"])
    assert details["reserved_zones"] and details["safe_region"]


def test_the_prompt_matches_the_tool_that_describes_the_same_layout(agent, brief):
    """One payload, so the prompt and get_layout_details cannot drift."""
    import json

    from app.genai.tools.design_tools import layout_details
    from app.genai.ppt.template import catalog as catalog_mod

    catalog = catalog_mod.load()
    expected = layout_details(catalog, catalog.resolve_layout("M1-L10"), brief.frame)
    assert json.loads(agent._layout_details_block(brief)) == json.loads(
        json.dumps(expected, default=str))


def test_an_unresolvable_layout_does_not_break_the_prompt(agent, brief):
    brief.layout_key = "M9-L99"
    assert "could not be read" in agent._layout_details_block(brief)
    brief.layout_key = ""
    assert "no layout assigned" in agent._layout_details_block(brief)


def test_saving_twice_does_not_stamp_the_frame_twice(tmp_path):
    """Frame shapes are exempt from the gate's rules, so a doubled classification mark
    would ship unreported."""
    from pptx import Presentation

    from app.genai.ppt import deck_builder as db
    from app.genai.ppt.template import catalog as catalog_mod, registry as reg

    catalog = catalog_mod.load()
    frame = db.frame_spec(13.333, 7.5, label="Internal",
                          symbol_path=str(reg.classification_symbol("internal")),
                          slide_number=1)
    deck = db.DeckBuilder(reg.template_path(), catalog.to_dict(), frame=frame)
    deck.add_slide("M1-L10").set_title("T")
    deck.save(tmp_path / "once.pptx")
    out = deck.save(tmp_path / "twice.pptx")

    slide = next(iter(Presentation(str(out)).slides))
    stamped = sorted((s.name or "") for s in slide.shapes
                     if (s.name or "").startswith(db.FRAME_SHAPE_PREFIX))
    # M1-L10 carries its own mark, so the frame adds no logo: symbol, label, number.
    assert stamped == [f"{db.FRAME_SHAPE_PREFIX}classification-label",
                       f"{db.FRAME_SHAPE_PREFIX}classification-symbol",
                       f"{db.FRAME_SHAPE_PREFIX}slide-number"]

# --- tier 1 gateway request: the code-execution tool only for models that take it -- #

class _FakeGateway:
    """Records foundry_chat calls and answers with a fenced program."""

    def __init__(self):
        self.calls: list[dict] = []

    def foundry_chat(self, messages, model, **kwargs):
        self.calls.append({"model": model, **kwargs})
        return {"content": [{"type": "text", "text": f"```python\n{GOOD_PROGRAM}\n```"}]}

    @staticmethod
    def reply_text(response: dict) -> str:
        from app.utilities.apim_llm_utils import ApimLlmClient

        return ApimLlmClient.reply_text(response)


def _gateway(monkeypatch, model: str) -> _FakeGateway:
    gateway = _FakeGateway()
    monkeypatch.setattr(creator, "get_apim_llm_client", lambda: gateway)
    monkeypatch.setattr(creator.ModelConstants, "PPT_CODER", model)
    return gateway


def test_listed_model_is_asked_without_the_code_tool(agent, monkeypatch):
    model = next(iter(PptCreatorConstants.CODE_EXECUTION_UNSUPPORTED_MODELS))
    gateway = _gateway(monkeypatch, model)

    source = agent._request_code([{"role": "user", "content": "draw"}])

    assert "deck.save(OUTPUT_PATH)" in source
    assert len(gateway.calls) == 1
    assert "tools" not in gateway.calls[0] and "container" not in gateway.calls[0]


def test_unlisted_model_still_gets_the_code_tool(agent, monkeypatch):
    gateway = _gateway(monkeypatch, "claude-opus-5")

    agent._request_code([{"role": "user", "content": "draw"}])

    assert len(gateway.calls) == 1 and "tools" in gateway.calls[0]

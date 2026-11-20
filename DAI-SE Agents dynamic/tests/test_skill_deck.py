"""Tests for the skill deck agent (app/genai/skill_deck), v3: one DeckBuilder program
for the whole deck, on the official template.

No gateway calls: a fake program writer stands in for the model, and the programs
really run against SAMPLE.pptx in a staged run folder, so the staging, the kit, the code
guard, the sandbox and the graph are all real. The deck design contract is a small fake
with the same shape as ``design_contract.build``'s result.
"""
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config.constants import SkillDeckConstants as config
from app.genai.skill_deck import skills
from app.genai.skill_deck.llm import NoProgramError, extract_program

GOOD_PROGRAM = '''
from deck_builder import DeckBuilder
from deck_kit import WholeDeck
from context import OUTPUT_PATH, SLIDES

deck = WholeDeck()
for spec in SLIDES:
    s = deck.slide(spec["number"])
    s.set_title(f"Slide {spec['number']}")
    s.set_notes("Speaker notes.")
deck.save(OUTPUT_PATH)
'''
BROKEN_PROGRAM = ("from deck_builder import DeckBuilder\n"
                  "raise RuntimeError('boom on line 2')\n")
# Runs fine, but the last slide gets a text box far too small for its text.
CRAMMED_PROGRAM = GOOD_PROGRAM.replace(
    "deck.save(OUTPUT_PATH)",
    "s.add_textbox('word ' * 80, 1.0, 2.0, 3.0, 0.4, size=12)\ndeck.save(OUTPUT_PATH)")
QUIZ_PROGRAM = '''
from deck_builder import DeckBuilder
from deck_kit import WholeDeck
from context import OUTPUT_PATH, SLIDES

deck = WholeDeck()

def quiz(s):
    s.set_title("Knowledge check")
    for i, text in enumerate(["Release stored energy", "Start now"]):
        s.add_choice("AB"[i], text, 1.0 + 5.5 * i, 3.0, 5.0, 1.5, correct=(i == 0))

for spec in SLIDES:
    s = deck.slide(spec["number"])
    if spec["number"] == 1:
        s.set_title("Cover")
    else:
        quiz(s)
deck.save(OUTPUT_PATH)
'''


class FakeWriter:
    """Returns queued programs and records what it was sent."""

    def __init__(self, *programs):
        self.programs = list(programs)
        self.calls = []

    def write_program(self, system, messages):
        self.calls.append({"system": system, "messages": list(messages)})
        program = self.programs.pop(0)
        if program is None:
            raise NoProgramError("the model returned no DeckBuilder program")
        return program


@lru_cache(maxsize=1)
def _catalog():
    from app.genai.ppt.template import catalog as catalog_mod
    return catalog_mod.load()


def _key(fragment):
    """The first catalog layout whose name contains ``fragment``."""
    return next(l.key for l in _catalog().layouts if fragment.lower() in l.name.lower())


class FakeContract:
    """The shape of ``design_contract.build``'s result, with real frames."""
    master_index = 1
    grid = {"columns": 12, "rows": 6, "gutter": 0.15}
    tokens = {}

    def __init__(self, keys):
        self.assignments = {n: SimpleNamespace(layout_key=k, mode="layout", free_region=None)
                            for n, k in enumerate(keys, start=1)}

    def frame_for(self, number, layout):
        from app.genai.ppt import deck_builder
        from app.genai.ppt.template import registry
        catalog = _catalog()
        symbol = registry.classification_symbol("internal")
        return deck_builder.frame_spec(
            catalog.slide_width, catalog.slide_height, label=registry.label("internal"),
            symbol_path=str(symbol) if symbol else None, logo_path=None,
            logo_variant="color", slide_number=number)


def _contract(slides=3):
    return FakeContract([_key("Gradient Cover")] + [_key("Two-Line Title")] * (slides - 1))


def _slides(slides=3, quiz_answer_at=None):
    out = [SimpleNamespace(kind="cover" if n == 1 else "content", quiz=None, faq=None)
           for n in range(1, slides + 1)]
    if quiz_answer_at:
        question = SimpleNamespace(reveal=False)
        for n in range(2, slides + 1):
            out[n - 1].quiz = (SimpleNamespace(reveal=True) if n == quiz_answer_at
                               else question)
    return out


def _staged(tmp_path, slides=3, contract=None, draft_slides=None):
    from app.genai.skill_deck import staging
    contract = contract or _contract(slides)
    draft = SimpleNamespace(slides=draft_slides or _slides(slides))
    specs = staging.slide_specs(draft, contract, _catalog(), {})
    run = tmp_path / "run"
    staging.stage(run, _catalog(), specs, contract)
    return run, specs


def _invoke(writer, tmp_path, slides=3, **extra):
    from app.genai.skill_deck.graph import build_graph
    run, specs = _staged(tmp_path, slides)
    state = {"system": "SYSTEM", "messages": [{"role": "user", "content": "BRIEF"}],
             "run_dir": str(run), "output_path": str(tmp_path / "final.pptx"),
             "slide_count": len(specs), "attempts": 0, "layout_rounds": 0,
             "warnings": [], "good_deck": "", **extra}
    return build_graph(writer).invoke(state)


def _names(path):
    from pptx import Presentation
    return [[s.name for s in slide.shapes] for slide in Presentation(str(path)).slides]


# --- skills ------------------------------------------------------------------- #
def test_whole_deck_skill_is_discovered_with_its_frontmatter():
    skill = skills.select(["whole-deck"])[0]
    assert skill.name == "whole-deck" and "one DeckBuilder program" in skill.description
    assert "deck.slide(n)" in skill.body and "deck.save(OUTPUT_PATH)" in skill.body


def test_unknown_skill_names_the_available_ones():
    with pytest.raises(ValueError, match="whole-deck"):
        skills.select(["no-such-skill"])


def test_system_prompt_starts_with_the_apps_ppt_skill(monkeypatch):
    from app.genai.skill_deck import prompts
    monkeypatch.setattr(prompts, "creativity_guidance", lambda level: f"CREATIVITY {level}")
    system, names = prompts.system_prompt("HIGH")
    assert names == [config.PPT_SKILL_NAME, "whole-deck"]
    assert system.startswith("# Rockwell Automation: Presentation Guide")
    assert system.index("Presentation Guide") < system.index("=== SKILL: whole-deck")
    assert "CREATIVITY HIGH" in system
    assert "PRIORITY" in system


STYLE_GUIDANCE = "Skill ID: spacious-reference (version 1)\nUse generous whitespace."


def test_no_style_skill_without_guidance():
    assert skills.style_skill("") is None
    assert skills.style_skill("   ") is None


def test_style_skill_comes_after_the_ppt_skill_and_ranks_below_it(monkeypatch):
    from app.genai.skill_deck import prompts
    monkeypatch.setattr(prompts, "creativity_guidance", lambda level: "")
    system, names = prompts.system_prompt("MED", STYLE_GUIDANCE)
    assert names[-1] == config.STYLE_SKILL_NAME
    style_at = system.index(f"=== SKILL: {config.STYLE_SKILL_NAME}")
    assert system.index("=== SKILL: whole-deck") < style_at
    assert "Use generous whitespace." in system[style_at:]
    assert "follow the ppt skill" in system[style_at:]


# --- the model's answer ---------------------------------------------------------- #
def test_program_read_from_fenced_reply():
    reply = {"content": [{"type": "text", "text": f"Here:\n```python\n{GOOD_PROGRAM}```"}]}
    assert "deck = WholeDeck()" in extract_program(reply)


def test_program_read_from_code_execution_block():
    reply = {"content": [{"type": "server_tool_use", "name": "code_execution",
                          "input": {"file_text": GOOD_PROGRAM}}]}
    assert extract_program(reply) == GOOD_PROGRAM


def test_reply_without_a_deckbuilder_program_raises():
    plain = "```python\nfrom pptx import Presentation\nPresentation().save('x.pptx')\n```"
    with pytest.raises(NoProgramError):
        extract_program({"content": [{"type": "text", "text": plain}]})


def test_gateway_writer_sends_no_code_tool_or_container(monkeypatch):
    import sys
    import types
    from app.genai.skill_deck.llm import GatewayProgramWriter
    sent = {}

    class Client:
        def foundry_chat(self, messages, model, **kwargs):
            sent.update(kwargs, model=model, messages=messages)
            return {"content": [{"type": "text", "text": f"```python\n{GOOD_PROGRAM}```"}]}

    fake = types.ModuleType("app.utilities.apim_llm_utils")
    fake.get_apim_llm_client = Client
    monkeypatch.setitem(sys.modules, "app.utilities.apim_llm_utils", fake)
    program = GatewayProgramWriter().write_program("SYS", [{"role": "user", "content": "x"}])
    assert "WholeDeck" in program
    assert sent["model"] == config.MODEL and sent["max_tokens"] == config.MAX_TOKENS
    assert "tools" not in sent and "container" not in sent
    assert sent["messages"][0] == {"role": "system", "content": "SYS"}


# --- staging and the kit ------------------------------------------------------- #
def test_slide_specs_follow_the_contract_and_keep_quiz_families_together(tmp_path):
    from app.genai.skill_deck import staging
    cover, other, third = _key("Gradient Cover"), _key("Two-Line Title"), _key("Three Text")
    contract = FakeContract([cover, other, third, other])
    draft = SimpleNamespace(slides=_slides(4, quiz_answer_at=3))
    specs = staging.slide_specs(draft, contract, _catalog(), {2: ["/img/a.png"]})
    assert [s["layout_key"] for s in specs] == [cover, other, other, other]
    assert [s["reveal"] for s in specs] == [False, False, True, False]
    assert specs[1]["images"] == ["/img/a.png"] and specs[0]["images"] == []
    assert specs[2]["frame"]["slide_number"]["value"] == 3


def test_stage_writes_the_api_the_kit_the_catalog_and_the_context(tmp_path):
    run, specs = _staged(tmp_path)
    assert sorted(p.name for p in run.iterdir()) == [
        "catalog.json", "context.py", "deck_builder.py", config.KIT_FILENAME]
    context = (run / "context.py").read_text(encoding="utf-8")
    assert "SLIDES = [" in context and "TEMPLATE_PATH" in context
    assert config.RAW_DECK_FILENAME in context


def test_whole_deck_program_builds_on_the_template_with_a_frame_per_slide(tmp_path):
    from pptx import Presentation
    from app.genai.skill_deck import sandbox
    run, specs = _staged(tmp_path)
    ok, error, deck = sandbox.run_program(GOOD_PROGRAM, run)
    assert ok, error
    prs = Presentation(str(deck))
    assert len(prs.slides) == 3
    assert [s.slide_layout.name for s in prs.slides] == [
        _catalog().get(spec["layout_key"]).name for spec in specs]
    for number, slide in enumerate(prs.slides, start=1):
        numbers = [s.text_frame.text for s in slide.shapes if s.name == "frame:slide-number"]
        assert numbers == [str(number)]
        assert any(s.name == "frame:classification-label" for s in slide.shapes)


def test_answer_slide_alone_gets_the_correct_choice_highlighted(tmp_path):
    from pptx import Presentation
    from app.genai.skill_deck import sandbox
    contract = FakeContract([_key("Gradient Cover")] + [_key("Two-Line Title")] * 2)
    run, _ = _staged(tmp_path, contract=contract, draft_slides=_slides(3, quiz_answer_at=3))
    ok, error, deck = sandbox.run_program(QUIZ_PROGRAM, run)
    assert ok, error
    fills = []
    for slide in list(Presentation(str(deck)).slides)[1:]:
        card = next(s for s in slide.shapes if s.name == "choice-A-card")
        fills.append(str(card.fill.fore_color.rgb))
    question, answer = fills
    assert question != answer and answer == "ADDA94"


def test_slides_must_be_added_in_order_and_all_of_them(tmp_path):
    from app.genai.skill_deck import sandbox
    run, _ = _staged(tmp_path)
    skipping = GOOD_PROGRAM.replace("for spec in SLIDES:", "for spec in SLIDES[1:]:")
    ok, error, _ = sandbox.run_program(skipping, run)
    assert not ok and "next one is slide 1" in error
    missing = GOOD_PROGRAM.replace("for spec in SLIDES:", "for spec in SLIDES[:2]:")
    ok, error, _ = sandbox.run_program(missing, run)
    assert not ok and "were never added" in error


# --- the code guard (the classic designer's) ----------------------------------- #
def test_safe_program_passes_the_guard():
    from app.genai.skill_deck.sandbox import unsafe_reason
    assert unsafe_reason(GOOD_PROGRAM) == ""


def test_programs_outside_deckbuilder_are_rejected_before_they_run(tmp_path):
    from app.genai.skill_deck import sandbox
    run, _ = _staged(tmp_path)
    for extra, named in (("import subprocess\n", "subprocess"),
                         ("from pptx import Presentation\nPresentation()\n", "Presentation"),
                         ("x = s.shapes.add_textbox(0, 0, 1, 1)\n", "add_textbox"),
                         ("color = '#FF00FF'\n", "FF00FF")):
        ok, error, deck = sandbox.run_program(GOOD_PROGRAM + extra, run)
        assert (ok, deck) == (False, None), extra
        assert "rejected before running" in error and named in error, error
    assert not (run / config.CODE_FILENAME).exists()


def test_generated_program_gets_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("APIM_CLIENT_SECRET", "do-not-leak")
    program = GOOD_PROGRAM.replace(
        "deck = WholeDeck()",
        "import os\nassert 'APIM_CLIENT_SECRET' not in os.environ\ndeck = WholeDeck()")
    assert _invoke(FakeWriter(program), tmp_path)["status"] == "done"


# --- the graph -------------------------------------------------------------------- #
def test_graph_builds_the_deck(tmp_path):
    writer, steps = FakeWriter(GOOD_PROGRAM), []
    from app.genai.skill_deck.graph import build_graph
    run, specs = _staged(tmp_path)
    state = build_graph(writer, on_step=steps.append).invoke({
        "system": "SYSTEM", "messages": [{"role": "user", "content": "BRIEF"}],
        "run_dir": str(run), "output_path": str(tmp_path / "final.pptx"),
        "slide_count": 3, "attempts": 0, "layout_rounds": 0, "warnings": [],
        "good_deck": ""})
    assert state["status"] == "done", state.get("error")
    assert state["attempts"] == 1 and Path(state["final_deck"]).is_file()
    assert writer.calls[0]["system"] == "SYSTEM"
    assert "the program built the deck on the official template" in " ".join(steps)
    assert steps[-1].startswith("Deck scorecard: ")


def test_graph_repairs_a_failing_program(tmp_path):
    writer = FakeWriter(BROKEN_PROGRAM, GOOD_PROGRAM)
    state = _invoke(writer, tmp_path)
    assert state["status"] == "done" and state["attempts"] == 2
    assert "boom on line 2" in writer.calls[1]["messages"][-1]["content"]


def test_graph_recovers_from_a_reply_without_program(tmp_path):
    state = _invoke(FakeWriter(None, GOOD_PROGRAM), tmp_path)
    assert state["status"] == "done" and state["attempts"] == 2


def test_graph_sends_a_rejection_back_for_a_fix(tmp_path):
    writer = FakeWriter("import socket\n" + GOOD_PROGRAM, GOOD_PROGRAM)
    state = _invoke(writer, tmp_path)
    assert state["status"] == "done" and state["attempts"] == 2
    assert "import of 'socket' is not allowed" in writer.calls[1]["messages"][-1]["content"]


def test_graph_gives_up_after_max_attempts(tmp_path):
    state = _invoke(FakeWriter(*[BROKEN_PROGRAM] * config.MAX_FIX_ATTEMPTS), tmp_path)
    assert state["status"] == "failed" and state["attempts"] == config.MAX_FIX_ATTEMPTS
    assert not (tmp_path / "final.pptx").exists()


def test_graph_sends_deck_problems_back_once(tmp_path):
    writer = FakeWriter(CRAMMED_PROGRAM, GOOD_PROGRAM)
    state = _invoke(writer, tmp_path)
    assert state["status"] == "done" and state["layout_rounds"] == 1
    turn = writer.calls[1]["messages"][-1]["content"]
    assert "=== PROBLEMS ===" in turn and "needs about" in turn
    assert not any(w.startswith("layout:") for w in state["warnings"])


def test_graph_keeps_the_better_version_when_a_fix_round_makes_it_worse(tmp_path):
    from app.genai.skill_deck.graph import build_graph
    run, _ = _staged(tmp_path)
    writer, steps = FakeWriter(GOOD_PROGRAM, CRAMMED_PROGRAM), []
    state = build_graph(writer, on_step=steps.append).invoke({
        "system": "S", "messages": [{"role": "user", "content": "B"}],
        "run_dir": str(run), "output_path": str(tmp_path / "final.pptx"),
        "slide_count": 3, "attempts": 0, "layout_rounds": 0, "warnings": [],
        "good_deck": "", "style_target": STYLE_TARGET})
    assert state["status"] == "done" and len(writer.calls) == 2
    assert "Kept the earlier version of the deck: the fix round made it worse." in steps
    assert not any(w.startswith("layout:") for w in state["warnings"])


def test_graph_falls_back_to_the_last_good_deck(tmp_path):
    broken = [BROKEN_PROGRAM] * config.MAX_FIX_ATTEMPTS
    state = _invoke(FakeWriter(CRAMMED_PROGRAM, *broken), tmp_path)
    assert state["status"] == "done"
    assert any("fix round failed to run" in w for w in state["warnings"])


STYLE_TARGET = {"shapes": 3.0, "title_pt": 40.0, "card_share": 0.9}


def test_style_differences_go_back_in_the_fix_round(tmp_path):
    writer = FakeWriter(GOOD_PROGRAM, GOOD_PROGRAM)
    state = _invoke(writer, tmp_path, style_target=STYLE_TARGET)
    assert state["status"] == "done" and len(writer.calls) == 2
    turn = writer.calls[1]["messages"][-1]["content"]
    assert "STYLE DIFFERENCES" in turn and "Rockwell ppt skill always wins" in turn
    assert "style" in state["scorecard"]["areas"]


# --- the app adapter (deck workflow -> skill deck agent) --------------------- #
def _draft_and_plan():
    from app.core.models.deck_model import DeckDraft, DeckPlan
    draft = DeckDraft(title="LOTO Basics", subtitle="For technicians", slides=[
        {"slide_number": 1, "kind": "cover", "title": "LOTO Basics",
         "layout": "Gradient Cover", "notes": "Welcome everyone."},
        {"slide_number": 2, "title": "Six steps",
         "blocks": [{"slot": "body1", "bullets": ["Prepare", "Notify"],
                     "image_ref": "sess/img.png"}]}])
    plan = DeckPlan(title="LOTO Basics", audience="Technicians", classification="internal")
    return draft, plan


def test_deck_spec_keeps_content_and_drops_designer_fields():
    from app.genai.skill_deck import deck_engine
    draft, plan = _draft_and_plan()
    spec = deck_engine.deck_spec(draft, plan)
    assert spec["audience"] == "Technicians"
    assert [s["title"] for s in spec["slides"]] == ["LOTO Basics", "Six steps"]
    assert spec["slides"][0]["notes"] == "Welcome everyone."
    assert "layout" not in spec["slides"][0]
    assert spec["slides"][1]["blocks"] == [{"slot": "body1", "bullets": ["Prepare", "Notify"]}]


def _build(tmp_path, monkeypatch, writer, **kwargs):
    from app.genai.skill_deck import deck_engine, prompts
    monkeypatch.setattr(prompts, "creativity_guidance", lambda level: "")
    draft, plan = _draft_and_plan()
    return deck_engine.build_deck("sess", draft, plan, tmp_path, "internal", _contract(2),
                                  catalog=_catalog(), writer=writer, **kwargs)


def test_build_deck_returns_the_deck_and_sends_the_brief(tmp_path, monkeypatch):
    writer, steps = FakeWriter(GOOD_PROGRAM), []
    path, notes = _build(tmp_path, monkeypatch, writer, on_step=steps.append)
    assert path == tmp_path / "deck.pptx" and path.is_file()
    brief = writer.calls[0]["messages"][0]["content"]
    assert '"title": "Six steps"' in brief and "--- Slide 2" in brief
    assert _key("Two-Line Title") in brief and "THE LAYOUTS YOUR SLIDES WERE ASSIGNED" in brief
    assert steps[0] == f"Skill deck agent: passing skills {config.PPT_SKILL_NAME}, whole-deck."


def test_build_deck_raises_when_agent_gives_up(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError, match="gave up"):
        _build(tmp_path, monkeypatch, FakeWriter(*[BROKEN_PROGRAM] * config.MAX_FIX_ATTEMPTS))


def test_build_deck_passes_the_session_style_skill(tmp_path, monkeypatch):
    writer, steps = FakeWriter(GOOD_PROGRAM), []
    _build(tmp_path, monkeypatch, writer, style_guidance=STYLE_GUIDANCE,
           on_step=steps.append)
    assert "Use generous whitespace." in writer.calls[0]["system"]
    assert steps[0].endswith(f"whole-deck, {config.STYLE_SKILL_NAME}.")


def test_build_deck_checks_the_deck_against_the_references(tmp_path, monkeypatch):
    from app.genai.skill_deck import style_match
    profile = style_match.inspect_reference(_reference_deck(tmp_path, title_pt=40))
    writer, steps = FakeWriter(GOOD_PROGRAM, GOOD_PROGRAM), []
    _build(tmp_path, monkeypatch, writer, style_guidance=STYLE_GUIDANCE,
           references=[profile], on_step=steps.append)
    assert "Measured from the reference" in writer.calls[0]["system"]
    assert any(s.startswith("Style check: ") for s in steps)
    assert any(s.startswith("Deck scorecard: ") and "content" in s and "style" in s
               for s in steps)


def test_engine_switch_reads_the_setting(monkeypatch):
    from app.genai.skill_deck import deck_engine
    for value, expected in (("skill_deck", True), ("SKILL_DECK", True),
                            ("skills_agent", False), ("classic", False),
                            ("nonsense", False), ("", False)):
        monkeypatch.setattr(deck_engine, "get_settings",
                            lambda v=value: SimpleNamespace(deck_engine=v))
        assert deck_engine.enabled() is expected, value


# --- the layout check --------------------------------------------------------- #
def _layout_deck(tmp_path, *boxes):
    """One-slide-plus-cover deck; boxes = (slide, name, left, top, width, height, text)."""
    from pptx import Presentation
    from pptx.util import Inches, Pt
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slides = [prs.slides.add_slide(prs.slide_layouts[6]) for _ in range(2)]
    for number, name, left, top, width, height, text in boxes:
        box = slides[number - 1].shapes.add_textbox(Inches(left), Inches(top),
                                                    Inches(width), Inches(height))
        box.name = name
        box.text_frame.word_wrap = True
        if text:
            run = box.text_frame.paragraphs[0].add_run()
            run.text = text
            run.font.size = Pt(18)
    path = tmp_path / "layout.pptx"
    prs.save(str(path))
    return path


def test_layout_check_passes_a_clean_deck(tmp_path):
    from app.genai.skill_deck.layout_check import layout_problems
    deck = _layout_deck(tmp_path, (1, "Title", 0.6, 2.6, 6, 1, "Cover"),
                        (2, "Body", 0.6, 1.3, 11, 4, "Short text"),
                        (1, "Side Panel", 7.3, 0, 6.033, 7.5, ""))   # full-height bleed
    assert layout_problems(deck) == []


def test_layout_check_finds_each_kind_of_problem(tmp_path):
    from app.genai.skill_deck.layout_check import layout_problems
    deck = _layout_deck(
        tmp_path,
        (2, "Footer", 0.6, 6.9, 12, 0.4, "x"),              # bottom strip
        (2, "Wide Card", 11.0, 2.0, 2.2, 2.0, "x"),          # right margin
        (2, "Lost", 12.0, 7.0, 2.0, 1.0, "x"),               # off the slide
        (2, "Corner", 12.5, 0.1, 0.6, 0.5, ""),              # top-right logo zone
        (1, "Cover Tag", 0.3, 0.2, 1.0, 0.5, "x"),           # top-left logo zone
        (2, "Crammed", 0.6, 1.3, 3.0, 0.4, "word " * 60))    # text too long for box
    text = "\n".join(layout_problems(deck))
    assert "'Footer'" in text and "bottom strip" in text
    assert "'Wide Card'" in text and "right margin" in text
    assert "'Lost'" in text and "off the slide" in text
    assert "'Corner'" in text and "top-right logo zone" in text
    assert "'Cover Tag'" in text and "top-left logo zone" in text
    assert "'Crammed'" in text and "needs about" in text


def test_layout_check_groups_one_cause_per_slide(tmp_path):
    from app.genai.skill_deck.layout_check import layout_problems
    deck = _layout_deck(tmp_path, (2, "A", 0.6, 6.9, 3, 0.4, ""),
                        (2, "B", 4.0, 6.9, 3, 0.4, ""))
    problems = layout_problems(deck)
    assert len(problems) == 1 and "'A', 'B'" in problems[0]


def _shapes_deck(tmp_path, build):
    """A 2-slide deck; ``build(slide2)`` draws the shapes under test on slide 2."""
    from pptx import Presentation
    from pptx.util import Inches
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    prs.slides.add_slide(prs.slide_layouts[6])
    build(prs.slides.add_slide(prs.slide_layouts[6]))
    path = tmp_path / "shapes.pptx"
    prs.save(str(path))
    return path


def _card(slide, name, left, top, width, height):
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches
    card = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(left), Inches(top),
                                  Inches(width), Inches(height))
    card.name = name
    card.fill.solid()
    card.fill.fore_color.rgb = RGBColor(0xF6, 0xF7, 0xF8)
    return card


def _text(slide, name, left, top, width, height, text, size=18, on_paragraph=False):
    from pptx.util import Inches, Pt
    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    box.name = name
    box.text_frame.word_wrap = True
    paragraph = box.text_frame.paragraphs[0]
    run = paragraph.add_run()
    run.text = text
    (paragraph.font if on_paragraph else run.font).size = Pt(size)
    return box


def test_layout_check_finds_overlapping_cards(tmp_path):
    from app.genai.skill_deck.layout_check import layout_problems

    def build(slide):
        _card(slide, "Upper Card", 0.6, 1.3, 4, 2.0)          # ends at 3.3 in
        _card(slide, "Lower Card", 0.6, 3.2, 4, 2.0)          # starts at 3.2 in
        _card(slide, "Tidy Card", 5.0, 1.3, 4, 2.0)
        _card(slide, "Accent Bar", 5.0, 1.3, 4, 0.05)         # thin bar on top: fine
        _text(slide, "Card Label", 5.1, 1.4, 3.8, 0.5, "Inside its card")
    problems = "\n".join(layout_problems(_shapes_deck(tmp_path, build)))
    assert "'Upper Card' and 'Lower Card' run into each other" in problems
    assert "Tidy Card" not in problems and "Accent Bar" not in problems
    assert "Card Label" not in problems


def test_layout_check_finds_text_growing_into_the_next_box(tmp_path):
    from app.genai.skill_deck.layout_check import layout_problems

    def build(slide):
        _text(slide, "Long Title", 0.6, 1.3, 6.4, 1.0,
              "Lockout/Tagout: Stop the Machine, Protect the People", size=36)
        _text(slide, "Subtitle", 0.6, 2.4, 6.4, 0.5, "Audience: all plant staff")
    problems = "\n".join(layout_problems(_shapes_deck(tmp_path, build)))
    assert "'Long Title' and 'Subtitle' runs into each other" in problems


def test_layout_check_reads_paragraph_font_size(tmp_path):
    from app.genai.skill_deck.layout_check import layout_problems

    def build(slide):
        # 11 pt set on the paragraph fits; read as the 18 pt default it would not.
        _text(slide, "Small Print", 0.6, 1.3, 2.8, 0.45,
              "Trained to apply locks and tags", size=11, on_paragraph=True)
    assert layout_problems(_shapes_deck(tmp_path, build)) == []


# --- style match: the deck against the user's reference ---------------------- #
def _reference_deck(tmp_path, cards=2, title_pt=32, body_pt=18):
    """Cover plus two content slides: a title and ``cards`` cards with text."""
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches, Pt
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    prs.slides.add_slide(prs.slide_layouts[6])
    for _ in range(2):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        title = slide.shapes.add_textbox(Inches(0.6), Inches(0.4), Inches(9), Inches(0.8))
        title.text_frame.text = "A reference title"
        title.text_frame.paragraphs[0].runs[0].font.size = Pt(title_pt)
        for i in range(cards):
            card = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.6 + 4.2 * i),
                                          Inches(2), Inches(3.8), Inches(3))
            card.text_frame.text = "Card text"
            card.text_frame.paragraphs[0].runs[0].font.size = Pt(body_pt)
    path = tmp_path / "reference.pptx"
    prs.save(str(path))
    return path


def _busy_deck(tmp_path):
    """Cover plus a content slide crowded with twelve small plain text boxes."""
    from pptx import Presentation
    from pptx.util import Inches, Pt
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    prs.slides.add_slide(prs.slide_layouts[6])
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    for i in range(12):
        box = slide.shapes.add_textbox(Inches(0.6 + 3 * (i % 4)), Inches(1.3 + 1.6 * (i // 4)),
                                       Inches(2.9), Inches(1.5))
        box.text_frame.text = f"Point {i}"
        box.text_frame.paragraphs[0].runs[0].font.size = Pt(24 if i == 0 else 14)
    path = tmp_path / "busy.pptx"
    prs.save(str(path))
    return path


def test_reference_deck_becomes_a_style_target_through_the_skill_readers(tmp_path):
    from app.genai.skill_deck import style_match
    profile = style_match.inspect_reference(_reference_deck(tmp_path))
    assert profile["style_only"] is True and profile["source_format"] == "pptx"
    target = style_match.style_target([profile])
    assert target["shapes"] == 3 and target["title_pt"] == 32 and target["body_pt"] == 18
    assert round(target["card_share"], 2) == 0.67
    assert 0.3 < target["coverage"] < 0.5
    assert "about 3 shapes per content slide" in style_match.describe(target)


def test_style_targets_stay_inside_the_brand_ranges():
    from app.genai.skill_deck import style_match
    shape = {"kind": "TEXT_BOX (17)", "bounds": {"x": 0.1, "y": 0.1, "width": 0.5,
                                                 "height": 0.2}}
    profile = {"exemplars": [{"slide": 2, "shapes": [{**shape, "font_sizes_pt": [60]},
                                                     {**shape, "font_sizes_pt": [9]},
                                                     {**shape, "font_sizes_pt": [9]}]}]}
    target = style_match.style_target([profile])
    assert target["title_pt"] == config.STYLE_TITLE_PT_RANGE[1]
    assert target["body_pt"] == config.STYLE_BODY_PT_RANGE[0]


def test_image_reference_gives_no_style_target():
    from app.genai.skill_deck import style_match
    profile = {"source_format": "png", "exemplars": [{"kind": "image", "width": 10}]}
    assert style_match.style_target([profile]) == {}
    assert style_match.describe({}) == ""


def test_matching_deck_scores_high_and_busy_deck_gets_advice(tmp_path):
    from app.genai.skill_deck import style_match
    reference = _reference_deck(tmp_path)
    target = style_match.style_target([style_match.inspect_reference(reference)])
    match, differences = style_match.compare(target, style_match.deck_style(reference))
    assert match >= 95 and differences == []    # the reader rounds boxes to 0.001
    match, differences = style_match.compare(target, style_match.deck_style(
        _busy_deck(tmp_path)))
    assert match < 70
    joined = " ".join(differences)
    assert "use fewer, larger elements" in joined
    assert "put more of the content into cards" in joined


# --- deck scorecard ------------------------------------------------------------ #
def _messy_deck(tmp_path):
    """No logos, a non-brand font and colour, 9 pt text, a UK spelling, a repeat."""
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Inches, Pt
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    for title in ("LOTO Basics", "Six steps"):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        box = slide.shapes.add_textbox(Inches(0.6), Inches(1.4), Inches(8), Inches(1))
        box.text_frame.text = title
    slide = prs.slides[1]
    for top in (2.6, 4.2):
        box = slide.shapes.add_textbox(Inches(0.6), Inches(top), Inches(8), Inches(1))
        box.text_frame.text = "De-energise the machine first"
        run = box.text_frame.paragraphs[0].runs[0]
        run.font.name, run.font.size = "Comic Sans MS", Pt(9)
        run.font.color.rgb = RGBColor(0xFF, 0x00, 0xFF)
    path = tmp_path / "messy.pptx"
    prs.save(str(path))
    return path


def test_scorecard_finds_brand_and_readability_problems(tmp_path):
    from app.genai.skill_deck import scorecard
    card = scorecard.score_deck(_messy_deck(tmp_path))
    assert set(card["areas"]) == {"layout", "brand", "readability"}
    brand = " | ".join(card["areas"]["brand"]["details"])
    assert "no logo on slide(s) 1, 2" in brand
    assert "Comic Sans MS" in brand and "FF00FF" in brand
    reading = " | ".join(card["areas"]["readability"]["details"])
    assert "below 12 pt" in reading and "'energise' (slide 2)" in reading
    assert card["areas"]["brand"]["score"] < 50 and card["overall"] < 90


def test_scorecard_content_area_checks_the_written_deck(tmp_path):
    from app.genai.skill_deck import scorecard
    spec = {"slides": [{"title": "LOTO Basics", "notes": "Welcome."},
                       {"title": "Six steps"},
                       {"title": "Verify zero energy", "blocks": [{"bullets": ["Try start"]}]}]}
    content = scorecard.score_deck(_messy_deck(tmp_path), spec=spec)["areas"]["content"]
    details = " | ".join(content["details"])
    assert "2 slide(s) built for 3 written" in details
    assert "title missing on slide(s) 3" in details
    assert "speaker notes missing on slide(s) 1" in details
    assert "same text shown twice on slide(s) 2" in details
    assert content["score"] < 70


def _template_logo_deck(tmp_path):
    """Two slides on a layout that carries a logo picture, as the classic template does,
    plus template furniture ("frame:" label) in the bottom strip and a slide number past
    the agent's side margin."""
    from PIL import Image
    from pptx import Presentation
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT
    from pptx.util import Inches
    logo = tmp_path / "logo.png"
    Image.new("RGB", (40, 40), (205, 22, 63)).save(logo)
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    layout = prs.slide_layouts[6]
    slides = [prs.slides.add_slide(layout) for _ in range(2)]
    pic = slides[0].shapes.add_picture(str(logo), Inches(0), Inches(3.2), height=Inches(0.7))
    blip = pic._element.blipFill.blip
    blip.rEmbed = layout.part.relate_to(slides[0].part.related_part(blip.rEmbed), RT.IMAGE)
    layout.shapes._spTree.append(pic._element)        # moves the logo into the layout
    for slide in slides:
        label = slide.shapes.add_textbox(Inches(0.74), Inches(7.05), Inches(1.6), Inches(0.45))
        label.name, label.text_frame.text = "frame:classification-label", "INTERNAL"
        number = slide.shapes.add_textbox(Inches(12.31), Inches(7.05), Inches(0.6), Inches(0.45))
        number.name, number.text_frame.text = "frame:slide-number", "2"
        body = slide.shapes.add_textbox(Inches(1), Inches(1.5), Inches(11.9), Inches(1))
        body.text_frame.text = "Template content runs to its own margin"
    path = tmp_path / "template_logo.pptx"
    prs.save(str(path))
    return path


def _template_logo_deck(tmp_path):
    """Two slides on a layout that carries a logo picture, as the classic template does,
    plus template furniture ("frame:" label) in the bottom strip and a slide number past
    the agent's side margin."""
    from PIL import Image
    from pptx import Presentation
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT
    from pptx.util import Inches
    logo = tmp_path / "logo.png"
    Image.new("RGB", (40, 40), (205, 22, 63)).save(logo)
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    layout = prs.slide_layouts[6]
    slides = [prs.slides.add_slide(layout) for _ in range(2)]
    pic = slides[0].shapes.add_picture(str(logo), Inches(0), Inches(3.2), height=Inches(0.7))
    blip = pic._element.blipFill.blip
    blip.rEmbed = layout.part.relate_to(slides[0].part.related_part(blip.rEmbed), RT.IMAGE)
    layout.shapes._spTree.append(pic._element)        # moves the logo into the layout
    for slide in slides:
        label = slide.shapes.add_textbox(Inches(0.74), Inches(7.05), Inches(1.6), Inches(0.45))
        label.name, label.text_frame.text = "frame:classification-label", "INTERNAL"
        number = slide.shapes.add_textbox(Inches(12.31), Inches(7.05), Inches(0.6), Inches(0.45))
        number.name, number.text_frame.text = "frame:slide-number", "2"
        body = slide.shapes.add_textbox(Inches(1), Inches(1.5), Inches(11.9), Inches(1))
        body.text_frame.text = "Template content runs to its own margin"
    path = tmp_path / "template_logo.pptx"
    prs.save(str(path))
    return path




def test_scorecard_counts_the_template_and_tints_as_brand(tmp_path):
    from app.genai.deck_polish import checks
    from app.genai.skill_deck import scorecard
    assert "ADDA94" in checks.brand_palette()            # green at 40%: quiz answer card
    card = scorecard.score_deck(_messy_deck(tmp_path))
    brand = " | ".join(card["areas"]["brand"]["details"])
    assert "not on an official template layout: slide(s) 1, 2" in brand


def test_template_decks_are_scored_by_the_same_rules_as_agent_decks(tmp_path):
    from app.genai.skill_deck import layout_check, scorecard
    deck = _template_logo_deck(tmp_path)
    card = scorecard.score_deck(deck)
    assert not any("no logo" in d for d in card["areas"]["brand"]["details"])
    assert card["areas"]["layout"] == {"score": 100, "details": []}
    assert any("right margin" in p for p in layout_check.layout_problems(deck))


def test_universal_layout_finds_content_covering_a_logo_or_label(tmp_path):
    from app.genai.skill_deck import layout_check
    from pptx import Presentation
    from pptx.util import Inches
    deck = _template_logo_deck(tmp_path)
    prs = Presentation(str(deck))
    bar = prs.slides[1].shapes.add_textbox(Inches(0.3), Inches(6.9), Inches(5), Inches(0.5))
    bar.text_frame.text = "Safety first"
    prs.save(str(deck))
    problems = layout_check.layout_problems(deck, universal=True)
    assert problems == [f"slide 2: '{bar.name}' covers the brand mark or label "
                        "'frame:classification-label'"]


def test_scorecard_table_and_terminal_tool(tmp_path):
    import contextlib
    import io
    import json
    from scripts.developer_tools import deck_scorecard
    messy, reference = _messy_deck(tmp_path), _reference_deck(tmp_path)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = deck_scorecard.main([str(messy), str(reference), "--reference",
                                    str(reference), "--json", str(tmp_path / "s.json")])
    text = out.getvalue()
    assert code == 0
    assert "messy.pptx" in text and "reference.pptx" in text and "overall" in text
    assert "Measured from the reference" in text
    saved = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
    assert saved["reference.pptx"]["areas"]["style"]["score"] >= 95
    assert saved["messy.pptx"]["areas"]["style"]["score"] < 80


# --- the session's pinned references ------------------------------------------ #
def test_only_references_still_matching_their_pin_are_used():
    from app.genai.skill_deck import deck_engine
    good, changed = {"sha256": "a" * 64, "exemplars": []}, {"sha256": "c" * 64}
    skill = {"references": [{"reference_id": "r1", "sha256": "a" * 64},
                            {"reference_id": "r2", "sha256": "b" * 64},
                            {"reference_id": "gone", "sha256": "d" * 64}]}
    stored = {"r1": {"profile": good}, "r2": {"profile": changed}}
    assert deck_engine.active_reference_profiles(skill, stored) == [good]
    assert deck_engine.active_reference_profiles(None, stored) == []

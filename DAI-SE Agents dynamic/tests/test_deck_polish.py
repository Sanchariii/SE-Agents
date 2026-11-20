"""Tests for the deck polish agent (app/genai/deck_polish).

No gateway calls: a fake writer stands in for the model. The decks are real - built
with DeckBuilder on the official template (SAMPLE.pptx) with a stamped frame, exactly
as both engines build them - so the checks see the same template furniture they meet
in the app.
"""
from functools import lru_cache
from types import SimpleNamespace

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Inches, Pt

from app.config.constants import DeckPolishConstants as config
from app.genai.deck_polish import checks, engine, ops, reader, rules, spelling


@lru_cache(maxsize=1)
def _catalog():
    from app.genai.ppt.template import catalog as catalog_mod
    return catalog_mod.load()


def _key(fragment):
    return next(l.key for l in _catalog().layouts if fragment.lower() in l.name.lower())


def _frame():
    from app.genai.ppt import deck_builder
    from app.genai.ppt.template import registry
    symbol = registry.classification_symbol("internal")
    return deck_builder.frame_spec(_catalog().slide_width, _catalog().slide_height,
                                   label=registry.label("internal"),
                                   symbol_path=str(symbol) if symbol else None,
                                   logo_path=None, slide_number=2)


def _textbox(slide, text, left, top, width, height, size=18):
    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    box.text_frame.word_wrap = True
    for index, line in enumerate(text if isinstance(text, list) else [text]):
        paragraph = (box.text_frame.paragraphs[0] if index == 0
                     else box.text_frame.add_paragraph())
        run = paragraph.add_run()
        run.text = line
        run.font.size = Pt(size)
    return box


def _deck(tmp_path, name="built.pptx"):
    """Cover + two content slides on the template, framed like the engines frame them.

    Slide 2 carries the defects the rules fix; slide 3 a plain list the model can
    turn into cards or steps.
    """
    from app.genai.ppt import deck_builder
    from app.genai.ppt.template import registry
    deck = deck_builder.DeckBuilder(registry.template_path(), _catalog().to_dict(),
                                    frame=_frame())
    cover = deck.add_slide(_key("Gradient Cover with Logo"))
    cover.set_title("Lockout/Tagout Safety Basics")
    for title in ("Why it matters", "Six steps"):
        deck.add_slide(_key("Two-Line Title")).set_title(title)
    path = tmp_path / name
    deck.save(path)
    prs = Presentation(str(path))
    defects = prs.slides[1]
    note = _textbox(defects, "Recognise every energy source", 1.2, 2.2, 8, 0.6, size=8)
    note.text_frame.paragraphs[0].runs[0].font.color.rgb = RGBColor(0x4A, 0x8C, 0x2A)
    _textbox(prs.slides[2], ["Prepare: identify every source", "Notify: tell the team",
                             "Isolate: 6 devices to lock"], 1.2, 2.2, 11, 3.0)
    prs.save(str(path))
    return path


class FakeWriter:
    def __init__(self, operations=(), error=None):
        self.operations, self.error, self.calls = list(operations), error, []

    def write_ops(self, system, user):
        self.calls.append((system, user))
        if self.error:
            raise self.error
        return self.operations


def _run(tmp_path, deck, level, writer=None):
    from app.genai.deck_polish.graph import build_graph
    work = tmp_path / "work"
    steps = []
    state = build_graph(writer, steps.append).invoke({
        "source": str(deck), "level": level, "work_dir": str(work),
        "output_path": str(work / config.OUTPUT_FILENAME), "style_guidance": ""})
    return state, steps


def _shape(prs, slide, text):
    return next(s for s in prs.slides[slide - 1].shapes if reader.text_of(s).startswith(text))


# --- spelling ------------------------------------------------------------------- #
def test_us_spelling_keeps_case_and_leaves_ambiguous_words():
    assert spelling.to_us("Recognise the ORGANISATION's colour") == \
        "Recognize the ORGANIZATION's color"
    assert spelling.to_us("de-energisation; analyses stay; emphasis stays") == \
        "de-energization; analyses stay; emphasis stays"
    assert spelling.uk_words("Summarise the Centre") == ["Summarise", "Centre"]


# --- checks ----------------------------------------------------------------------- #
def test_checks_find_template_duplicates_and_text_defects(tmp_path):
    issues = checks.find_issues(Presentation(str(_deck(tmp_path))))
    found = {(i.slide, i.code) for i in issues}
    assert (1, "duplicate_label") in found          # the cover layout prints INTERNAL
    assert (2, "duplicate_number") in found         # the master prints the slide number
    assert {(2, "tiny_text"), (2, "off_palette"), (2, "uk_spelling")} <= found
    assert all(i.fix == "rule" for i in issues if i.code in (
        "duplicate_label", "duplicate_number", "tiny_text", "off_palette", "uk_spelling"))


def test_quiz_answer_green_is_on_brand():
    assert "ADDA94" in checks.brand_palette()
    assert checks.nearest_brand("4A8C2A") == "77C14C"


def test_shapes_on_template_graphics_are_marked_fixed(tmp_path):
    from app.genai.ppt.template import registry
    prs = Presentation(str(registry.template_path()))
    layout = next(l for m in prs.slide_masters for l in m.slide_layouts
                  if l.name == "Two Columns with Titles")
    slide = prs.slides.add_slide(layout)
    graphics = reader.template_graphics(slide)
    heads = [s for s in slide.placeholders if reader.inches(s.top) > 5.5]
    assert heads and all(reader.on_template_graphic(h, graphics) for h in heads)
    described = reader.describe_slide(slide, len(prs.slides))
    assert any(entry.get("fixed") for entry in described["shapes"])


def test_heading_under_its_column_is_found_and_lifted_from_medium_up(tmp_path):
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    for left, head in ((0.8, "Before restart"), (7.0, "Mistakes to avoid")):
        _textbox(slide, ["Tools removed", "Guards in place", "Everyone clear"],
                 left, 1.5, 5.5, 3.0)
        _textbox(slide, head, left, 4.7, 5.5, 0.5, size=20)
    path = tmp_path / "heads.pptx"
    prs.save(str(path))
    issues = checks.find_issues(Presentation(str(path)))
    assert [i.code for i in issues if i.code == "heading_below"] == ["heading_below"]
    low = Presentation(str(path))
    assert not any("heading" in c for c in rules.apply(low, checks.find_issues(low), "LOW"))
    med = Presentation(str(path))
    changes = rules.apply(med, checks.find_issues(med), "MED")
    assert "slide 1: moved 2 column heading(s) above their columns" in changes
    head = _shape(med, 1, "Before restart")
    assert abs(reader.inches(head.top) - 1.5) < 0.01


# --- the LOW level: rules only ------------------------------------------------------ #
def test_low_fixes_by_rules_and_never_calls_the_model(tmp_path):
    deck = _deck(tmp_path)
    writer = FakeWriter(error=AssertionError("LOW must not call the model"))
    state, steps = _run(tmp_path, deck, "LOW", writer)
    prs = Presentation(state["final_deck"])
    assert not any(s.name == config.FRAME_LABEL_NAME for s in prs.slides[0].shapes)
    assert not any(s.name == config.FRAME_NUMBER_NAME for s in prs.slides[1].shapes)
    note = _shape(prs, 2, "Recognize every energy source")
    run = note.text_frame.paragraphs[0].runs[0]
    assert run.font.size.pt == config.FLOOR_PT and str(run.font.color.rgb) == "77C14C"
    before = _shape(Presentation(str(deck)), 3, "Prepare")
    after = _shape(prs, 3, "Prepare")
    assert (before.left, before.top, before.width, before.height) == \
        (after.left, after.top, after.width, after.height)
    assert writer.calls == []
    assert steps[-1].startswith("Deck polish (Low): scorecard ")
    assert state["versions"][-1]["score"] >= state["versions"][0]["score"]


def test_polish_leaves_a_clean_deck_alone(tmp_path):
    clean = tmp_path / "clean.pptx"
    prs = Presentation()
    prs.slides.add_slide(prs.slide_layouts[6])
    prs.save(str(clean))
    path, notes = engine.polish_deck(clean, tmp_path, "LOW")
    assert path == clean and notes[0].endswith("0 change(s).")


# --- the model's operations ------------------------------------------------------- #
def test_medium_applies_the_allowed_operations_and_skips_the_rest(tmp_path):
    deck = _deck(tmp_path)
    prs = Presentation(str(deck))
    listing = _shape(prs, 3, "Prepare").shape_id
    frame = next(s for s in prs.slides[2].shapes if s.name.startswith("frame:")).shape_id
    writer = FakeWriter([
        {"op": "to_cards", "slide": 3, "shape": listing},
        {"op": "stat", "slide": 3, "value": "99%", "label": "made up", "box": [1, 5, 3, 1.2]},
        {"op": "move", "slide": 3, "shape": frame, "left": 1, "top": 1},
        {"op": "divider", "before": 3, "title": "Steps"},
    ])
    state, steps = _run(tmp_path, deck, "MED", writer)
    system, user = writer.calls[0]
    assert "Deck polish - Medium" in system and "## Brand Palette" in system
    assert '"to_cards"' in system and '"divider"' not in system
    assert '"slide":3' in user
    final = Presentation(state["final_deck"])
    names = [s.name for s in final.slides[2].shapes]
    assert sum(n.startswith(f"{config.SHAPE_PREFIX}card-3-") and n[-1].isdigit()
               for n in names) == 3
    assert len(final.slides) == 3
    rejected = " | ".join(state["rejected"])
    assert "never invent a figure" in rejected
    assert "stamped by the template" in rejected
    assert "'divider' is not allowed at this level" in rejected
    assert any("change(s) by the model kept" in s for s in steps)


def test_a_model_round_that_scores_worse_is_thrown_away(tmp_path):
    deck = _deck(tmp_path)
    prs = Presentation(str(deck))
    listing = _shape(prs, 3, "Prepare")
    title = next(s for s in prs.slides[2].shapes if reader.kind(s) == "title")
    writer = FakeWriter([{"op": "move", "slide": 3, "shape": listing.shape_id,
                          "left": reader.inches(title.left), "top": reader.inches(title.top)}])
    state, steps = _run(tmp_path, deck, "MED", writer)
    assert any("made the deck score worse" in s for s in steps)
    assert state["versions"][-1]["name"] == "rules"
    final = _shape(Presentation(state["final_deck"]), 3, "Prepare")
    assert final.top == listing.top


def test_the_rule_fixes_survive_a_model_failure(tmp_path):
    state, steps = _run(tmp_path, _deck(tmp_path), "HIGH",
                        FakeWriter(error=RuntimeError("gateway down")))
    assert "Deck polish: the model was not reachable; kept the rule fixes." in steps
    assert state["versions"][-1]["name"] == "rules"


def test_high_adds_steps_a_timeline_and_a_section_divider(tmp_path):
    prs = Presentation(str(_deck(tmp_path)))
    listing = _shape(prs, 3, "Prepare").shape_id
    applied, rejected = ops.apply(prs, [
        {"op": "to_timeline", "slide": 3, "shape": listing},
        {"op": "divider", "before": 3, "title": "The six steps"},
    ], "HIGH")
    assert rejected == [] and len(applied) == 2
    assert len(prs.slides) == 4
    assert "section" in prs.slides[2].slide_layout.name.lower() or \
        "divider" in prs.slides[2].slide_layout.name.lower()
    names = [s.name for s in prs.slides[3].shapes]
    assert f"{config.SHAPE_PREFIX}timeline-3-line" in names
    assert sum(n.startswith(f"{config.SHAPE_PREFIX}timeline-3-") and n[-1].isdigit()
               for n in names) == 3                  # one numbered circle per step
    numbers = [s.text_frame.text for slide in prs.slides for s in slide.shapes
               if s.name == config.FRAME_NUMBER_NAME]
    assert numbers == ["1", "2", "3", "4"]          # renumbered after the insert


def test_stat_must_use_a_figure_from_the_slide_and_free_space(tmp_path):
    prs = Presentation(str(_deck(tmp_path)))
    applied, rejected = ops.apply(prs, [
        {"op": "stat", "slide": 3, "value": "6", "label": "devices to lock",
         "box": [1.2, 2.4, 3, 1.2]},                        # covers the list: rejected
        {"op": "stat", "slide": 3, "value": "6", "label": "devices to lock",
         "box": [1.2, 5.4, 3, 1.2]},
    ], "MED")
    assert len(applied) == 1 and "covers the text" in rejected[0]


def test_shorten_moves_the_detail_into_the_notes(tmp_path):
    prs = Presentation(str(_deck(tmp_path)))
    listing = _shape(prs, 3, "Prepare").shape_id
    _, rejected = ops.apply(prs, [{"op": "shorten", "slide": 3, "shape": listing,
                                   "text": ["Prepare", "Notify"]}], "MED")
    assert "never drop a fact" in rejected[0]
    applied, rejected = ops.apply(prs, [{
        "op": "shorten", "slide": 3, "shape": listing, "text": ["Prepare", "Notify"],
        "notes": "Isolate: six devices to lock; identify every source; tell the team."}],
        "MED")
    assert rejected == [] and "detail moved to the speaker notes" in applied[0]
    assert reader.paragraphs(reader.find_shape(prs.slides[2], listing)) == ["Prepare", "Notify"]
    assert "six devices" in prs.slides[2].notes_slide.notes_text_frame.text


def test_cards_pick_one_readable_size_for_the_whole_group(tmp_path):
    prs = Presentation(str(_deck(tmp_path)))
    listing = _shape(prs, 3, "Prepare").shape_id
    ops.apply(prs, [{"op": "to_cards", "slide": 3, "shape": listing}], "MED")
    sizes = {run.font.size.pt for s in prs.slides[2].shapes
             if s.name.endswith("-text") for p in s.text_frame.paragraphs for run in p.runs
             if not run.font.bold}
    titles = [p.runs[0].text for s in prs.slides[2].shapes if s.name.endswith("-text")
              for p in s.text_frame.paragraphs if p.runs and p.runs[0].font.bold]
    assert len(sizes) == 1 and sizes.pop() >= 12
    assert titles == ["Prepare", "Notify", "Isolate"]


def test_two_card_groups_on_one_slide_share_one_text_size(tmp_path):
    prs = Presentation(str(_deck(tmp_path)))
    slide = prs.slides[2]
    listing = _shape(prs, 3, "Prepare")
    listing._element.getparent().remove(listing._element)
    _textbox(slide, ["Lock", "Tag"], 6.9, 2.2, 5.6, 3.6)
    _textbox(slide, ["Prepare: identify every energy source, every isolating device and "
                     "every stored hazard before anyone touches the machine",
                     "Notify: tell every operator and every affected employee who works "
                     "near the machine that it is about to be locked out"],
             0.8, 2.2, 5.6, 3.6)
    short, long = _shape(prs, 3, "Lock").shape_id, _shape(prs, 3, "Prepare").shape_id
    applied, rejected = ops.apply(prs, [{"op": "to_cards", "slide": 3, "shape": short},
                                        {"op": "to_cards", "slide": 3, "shape": long}],
                                  "MED")
    assert len(applied) == 2, rejected
    texts = [s for s in slide.shapes if s.name.endswith("-text")]
    body = {run.font.size.pt for s in texts for p in s.text_frame.paragraphs
            for run in p.runs if not run.font.bold}
    names = [s.name for s in slide.shapes if s.name.startswith(config.SHAPE_PREFIX)]
    assert len(texts) == 4 and len(body) == 1 and body.pop() < config.TEXT_LADDER[0]
    assert len(names) == len(set(names))


def test_text_estimate_reads_size_and_line_spacing_from_the_template(tmp_path):
    """A placeholder without its own size gets the template's, not a guess: the
    timeline's 12 pt step labels fit their thin bands, the title fits its box."""
    from app.genai.ppt.template import registry
    from app.genai.skill_deck import layout_check
    prs = Presentation(str(registry.template_path()))
    layout = next(l for m in prs.slide_masters for l in m.slide_layouts
                  if l.name == "Six-Step Gradient Timeline")
    slide = prs.slides.add_slide(layout)
    labels = sorted((p for p in slide.placeholders
                     if reader.box(p)[3] - reader.box(p)[1] < 0.5
                     and p.placeholder_format.type != 1), key=lambda p: reader.box(p)[0])
    assert len(labels) >= 6
    for placeholder, text in zip(labels, ["Prepare", "Notify", "Shut down",
                                          "Isolate, lock and tag", "Release stored energy",
                                          "Verify isolation"]):
        placeholder.text = text
    slide.shapes.title.text = "The Six Lockout Steps at a Glance"
    number = len(prs.slides)
    assert layout_check.inherited_pt(labels[4]) == 12
    found = [i for i in checks.find_issues(prs) if i.slide == number
             and i.code in ("overflow", "text_overlap")]
    assert not found, [i.line() for i in found]
    plain = layout_check._needed_height(slide.shapes.title)      # noqa: SLF001
    with layout_check.measuring():
        assert layout_check._needed_height(slide.shapes.title) == plain   # noqa: SLF001
    title = reader.box(slide.shapes.title)
    assert plain <= title[3] - title[1] + 0.02


# --- the setting, the scorecard and the terminal tool ---------------------------- #
def test_setting_switches_polish_and_picks_the_level(monkeypatch):
    for value, on, level in (("off", False, None), ("", False, None), ("on", True, "HIGH"),
                             ("medium", True, "MED"), ("LOW", True, "LOW"),
                             ("nonsense", False, None)):
        monkeypatch.setattr(engine, "get_settings",
                            lambda v=value: SimpleNamespace(deck_polish=v))
        assert engine.enabled() is on, value
        if on:
            assert engine.level_for("HIGH") == level
    monkeypatch.setattr(engine, "get_settings", lambda: SimpleNamespace(deck_polish="on"))
    assert engine.level_for("") == "MED" and engine.level_for("medium") == "MED"


def test_scorecard_counts_template_duplicates(tmp_path):
    from app.genai.skill_deck import scorecard
    deck = _deck(tmp_path)
    card = scorecard.score_deck(deck)
    assert "repeats it on top" in " | ".join(card["areas"]["layout"]["details"])
    assert "slide number printed twice" in " | ".join(card["areas"]["brand"]["details"])
    polished, _ = engine.polish_deck(deck, tmp_path, "LOW")
    after = scorecard.score_deck(polished)
    assert after["areas"]["layout"]["score"] > card["areas"]["layout"]["score"]
    assert after["areas"]["brand"]["score"] > card["areas"]["brand"]["score"]


def test_terminal_tool_polishes_and_prints_the_scorecards(tmp_path):
    import contextlib
    import io
    from scripts.developer_tools import deck_polish
    deck, out = _deck(tmp_path), tmp_path / "out.pptx"
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        assert deck_polish.main([str(deck), "--level", "low", "--out", str(out)]) == 0
    text = printed.getvalue()
    assert out.is_file() and "Saved" in text and "before" in text and "after" in text

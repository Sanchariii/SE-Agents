"""Slide-generation parameters and deck-level Quiz/FAQ layout consistency.

Covers the backward-compatible `SlideCodeBrief` parameters (slide_type,
creativity_level, user_instructions, planner_special_instructions, deck_design_context),
the direction block they add to the prompts, and the leader-first family coordination
that keeps every QUIZ (and every FAQ) slide of a deck on one layout.

As in `test_ppt_creator_agent.py`, there is no gateway: `_request_code` is stubbed with
canned programs that really run in the sandbox and really pass through the gate.
"""

import re
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.config.constants import (
    CreativityLevels, GeometryConstants, PptCreatorConstants,
    SlideTypes,
)
from app.config.exceptions import UpstreamError
from app.core.models.deck_model import (
    ContentBlock, DeckContext, DeckDesignContext, FamilyLayout, SlideCodeBrief,
    SlideDraft,
)
from app.genai.agents import ppt_creator_agent as creator
from app.genai.ppt import deck_design
from app.genai.ppt.template import catalog as catalog_mod
from app.utilities import run_control

#: Builds on whatever layout the slide was assigned, so it is valid for NORMAL slides,
#: family leaders and family followers alike.
FAMILY_PROGRAM = """
from deck_builder import DeckBuilder
from context import TEMPLATE_PATH, CATALOG, FRAME, OUTPUT_PATH, LAYOUT_KEY

deck = DeckBuilder(TEMPLATE_PATH, CATALOG, frame=FRAME)
slide = deck.add_slide(LAYOUT_KEY)
slide.set_title("family slide")
deck.save(OUTPUT_PATH)
"""

BROKEN_PROGRAM = "from deck_builder import DeckBuilder\nraise RuntimeError('boom')\n"

_SLIDE_RE = re.compile(r"Build slide (\d+) of")


# --------------------------------------------------------------------------------- #
# fixtures                                                                            #
# --------------------------------------------------------------------------------- #

@pytest.fixture
def store(session_store, monkeypatch):
    """Route deck design persistence to the fixture's in-memory store."""
    monkeypatch.setattr(deck_design, "session_store", session_store)
    return session_store


@pytest.fixture
def fast_polls(monkeypatch):
    """Followers re-check for their leader quickly, so the threaded tests stay fast."""
    monkeypatch.setattr(PptCreatorConstants, "FAMILY_LOCK_POLL_SECONDS", 0.02)


@pytest.fixture
def catalog():
    return catalog_mod.load()


def _brief(number: int, slide_type: str = SlideTypes.NORMAL, total: int = 9,
           **params) -> SlideCodeBrief:
    """A brief on the contract's usual content layout, with a real frame."""
    from app.genai.ppt import deck_builder as db
    from app.genai.ppt.template import registry as reg

    return SlideCodeBrief(
        slide_number=number,
        content=SlideDraft(slide_number=number, title=f"Slide {number}",
                           blocks=[ContentBlock(slot="body1",
                                                bullets=["Option one", "Option two"])],
                           notes="notes"),
        context=DeckContext(title="Field Service Basics", position=number,
                            total=total),
        layout_key="M1-L10",
        frame=db.frame_spec(13.333, 7.5, label="Internal",
                            symbol_path=str(reg.classification_symbol("internal")),
                            slide_number=number),
        slide_type=slide_type, **params)


def _record_requests(agent, monkeypatch, program: str = FAMILY_PROGRAM) -> dict:
    """Stub the code request; remember each slide's first prompt. Thread-safe."""
    prompts: dict[int, str] = {}
    lock = threading.Lock()

    def fake_request(messages):
        text = messages[1]["content"][0]["text"]
        number = int(_SLIDE_RE.search(text).group(1))
        with lock:
            prompts.setdefault(number, text)
        return program

    monkeypatch.setattr(agent, "_request_code", fake_request)
    return prompts


def _refuse_tier2(agent, monkeypatch) -> None:
    """Tier 2 behaves like an unreachable gateway, as in test_ppt_creator_agent."""
    def fake_plan(messages):
        raise UpstreamError("tier 2 is stubbed out in this test")

    monkeypatch.setattr(agent, "_request_layout_plan", fake_plan)


# --------------------------------------------------------------------------------- #
# the brief's parameters                                                              #
# --------------------------------------------------------------------------------- #

def test_a_brief_without_the_new_parameters_keeps_the_original_behaviour():
    brief = _brief(2)
    assert (brief.slide_type, brief.creativity_level) == (
        SlideTypes.NORMAL, CreativityLevels.MED)
    assert brief.planner_special_instructions == "" and brief.user_instructions == ""
    assert brief.deck_design_context is None
    assert brief.uses_defaults and not brief.is_family


@pytest.mark.parametrize("field,sent,expected", [
    ("slide_type", "quiz", "QUIZ"),
    ("slide_type", " Faq ", "FAQ"),
    ("slide_type", "poll", "NORMAL"),
    ("slide_type", None, "NORMAL"),
    ("creativity_level", "low", "LOW"),
    ("creativity_level", "HIGHEST", "HIGH"),       # not offered yet; maps onto HIGH
    ("creativity_level", "medium", "MED"),
    ("creativity_level", "wild", "MED"),
])
def test_parameters_are_normalised_and_never_fail_the_slide(field, sent, expected):
    assert getattr(_brief(1, **{field: sent}), field) == expected


def test_planner_instructions_default_to_empty_and_are_trimmed():
    assert _brief(1, planner_special_instructions=None).planner_special_instructions == ""
    assert _brief(1, planner_special_instructions="  hero image  ") \
        .planner_special_instructions == "hero image"


def test_the_deck_design_context_accepts_a_plain_description():
    brief = _brief(1, deck_design_context={"quiz_layout": "game-show style",
                                           "faq_layout": "", "unknown": 1})
    assert brief.deck_design_context.quiz_layout.description == "game-show style"
    assert not brief.deck_design_context.quiz_layout.established
    assert brief.deck_design_context.faq_layout is None


# --------------------------------------------------------------------------------- #
# the prompts                                                                         #
# --------------------------------------------------------------------------------- #

def test_a_default_brief_adds_nothing_to_either_prompt(tmp_path):
    agent = creator.PptCreatorAgent("s1", tmp_path / "work")
    brief = _brief(2)
    text = agent._user_prompt(brief)[0]["text"]
    assert creator.DIRECTION_HEADER.strip() not in text
    # The template's tail is exactly what it was before the parameters existed.
    assert text.endswith("=== LOCAL IMAGE FILES YOU MAY PLACE ===\n(none)\n")
    assert agent._direction_block(brief, None, tier=1) == ""
    fit = agent._layout_plan_prompt(brief, catalog_mod.load(), [], 1, "")
    assert fit.endswith("=== LOCAL IMAGE FILES YOU MAY PLACE ===\n(none)\n")


@pytest.mark.parametrize("field,level,marker", [
    ("creativity_level", "LOW", "CREATIVITY: LOW"),
    ("creativity_level", "HIGH", "CREATIVITY: HIGH"),
    ("user_instructions", "make it a timeline", "USER INSTRUCTIONS"),
    ("slide_type", "QUIZ", "SLIDE TYPE: QUIZ"),
    ("slide_type", "FAQ", "SLIDE TYPE: FAQ"),
])
def test_each_parameter_adds_its_own_direction(tmp_path, field, level, marker):
    agent = creator.PptCreatorAgent("s1", tmp_path / "work")
    block = agent._direction_block(_brief(2, **{field: level}), None, tier=1)
    assert block.startswith(creator.DIRECTION_HEADER) and marker in block


def test_med_levels_add_nothing_because_they_are_the_original_behaviour(tmp_path):
    agent = creator.PptCreatorAgent("s1", tmp_path / "work")
    assert agent._direction_block(
        _brief(2, creativity_level="MED"), None, tier=1) == ""


def test_high_creativity_stays_on_brand(tmp_path):
    agent = creator.PptCreatorAgent("s1", tmp_path / "work")
    block = " ".join(agent._direction_block(
        _brief(2, creativity_level="HIGH"), None, tier=1).split())
    assert "brand colour tokens only" in block and "stamped for you" in block


def test_tier_2_gets_no_creative_freedom(tmp_path):
    agent = creator.PptCreatorAgent("s1", tmp_path / "work")
    brief = _brief(2, creativity_level="HIGH", slide_type="QUIZ")
    block = agent._direction_block(brief, None, tier=2)
    assert "CREATIVITY" not in block and "SLIDE TYPE: QUIZ" in block


def test_planner_instructions_come_last_and_yield_to_everything_above(tmp_path):
    agent = creator.PptCreatorAgent("s1", tmp_path / "work")
    block = agent._direction_block(
        _brief(2, slide_type="QUIZ", user_instructions="Show the options as tiles",
               planner_special_instructions="Use a two-by-two grid"), None, tier=1)
    assert (block.index("SLIDE TYPE") < block.index("USER INSTRUCTIONS")
            < block.index("PLANNER"))
    assert block.rstrip().endswith("Use a two-by-two grid")
    assert "take precedence" in block


def test_the_principles_and_priority_live_in_both_system_prompts(tmp_path):
    agent = creator.PptCreatorAgent("s1", tmp_path / "work")
    for prompt in (agent._system_prompt(), agent._fit_system_prompt()):
        assert "DESIGN PRINCIPLES" in prompt and "FINAL DESIGN CHECK" in prompt
        assert prompt.index("DESIGN PRINCIPLES") < prompt.index("=== PRIORITY")
    priority = agent._system_prompt().split("=== PRIORITY")[1]
    assert priority.index("USER") < priority.index("PLANNER") < priority.index("principles")


def test_the_design_key_is_unchanged_by_the_detailing_removal():
    from app.core.models.deck_model import DeckPlan, SlidePlan
    from app.genai.agents.brief import DeckBrief
    from app.genai.workflows import deck_workflow as wf

    planned = SlidePlan(slide_number=3, title="Q", slide_type="QUIZ", quiz_role="question",
                        planner_special_instructions="QUIZ: test the torque")
    plan = DeckPlan(title="t", slides=[planned])
    params = wf._generation_params(planned, DeckBrief(), plan)
    key = wf._design_key(params, planned)
    # The retired detailing slot keeps its old default, so cached slides stay valid.
    assert key.split("|")[:4] == ["QUIZ", "MED", "MED", "question"]
    default = SlidePlan(slide_number=1, title="Plain")
    assert wf._design_key(wf._generation_params(default, DeckBrief(), plan), default) == ""
    asked = default.model_copy(update={"user_instructions": "make it a timeline"})
    assert "user:" in wf._design_key(wf._generation_params(asked, DeckBrief(), plan), asked)


def test_planner_instructions_reach_the_model_but_never_the_event_stream(
        tmp_path, monkeypatch):
    agent = creator.PptCreatorAgent("s1", tmp_path / "work")
    secret = "INTERNAL-PLANNER-NOTE-7731"
    prompts = _record_requests(agent, monkeypatch)
    published: list = []
    monkeypatch.setattr(creator.events, "publish",
                        lambda *a, **k: published.append((a, k)))

    result = agent.build_slide(_brief(2, planner_special_instructions=secret))

    assert result.gate_passed
    assert secret in prompts[2]
    assert published and all(secret not in repr(event) for event in published)


def test_active_skill_guidance_reaches_both_tiers_and_visual_review(tmp_path):
    from app.genai.skills.designer_enrichment import (
        add_skill_guidance, prepare_active_skill,
    )

    skill = {
        "skill_id": "spacious-reference",
        "display_name": "Spacious reference",
        "description": "Use whitespace.",
        "version": 1,
        "body_md": "Use generous whitespace.",
    }
    params = add_skill_guidance({}, prepare_active_skill(skill))
    guidance = params["planner_special_instructions"]
    brief = _brief(2, **params)
    agent = creator.PptCreatorAgent("s1", tmp_path / "work")

    assert guidance in agent._direction_block(brief, None, tier=1)
    assert guidance in agent._direction_block(brief, None, tier=2)

    from app.genai.agents import slide_reviewer
    assert guidance in slide_reviewer._intent(brief, "", 1, 0)


def test_high_creativity_composes_on_the_canvas_when_the_layout_has_one(tmp_path,
                                                                        catalog):
    agent = creator.PptCreatorAgent("s1", tmp_path / "work")
    canvas = next(l for l in catalog.layouts
                  if l.master_index == catalog.preferred_master and l.canvas_capable)
    brief = _brief(2, creativity_level="HIGH").model_copy(
        update={"layout_key": canvas.key})

    moved = agent._apply_creativity(brief, catalog)

    assert moved.mode == "canvas" and moved.free_region == canvas.free_region.to_list()
    kept = agent._apply_creativity(brief.model_copy(
        update={"creativity_level": "MED"}), catalog)
    assert kept.mode == "layout" and kept.free_region is None


# --------------------------------------------------------------------------------- #
# the template's own Quiz / FAQ layouts                                               #
# --------------------------------------------------------------------------------- #

def test_the_template_family_layouts_are_found_by_name(catalog):
    quiz = deck_design.template_family_layout(catalog, SlideTypes.QUIZ)
    faq = deck_design.template_family_layout(catalog, SlideTypes.FAQ)
    assert quiz is not None and quiz.name == "Quiz-1"
    assert faq is not None and faq.name == "FAQ-1"
    # Drawn as artwork with sample copy, so they can only be reproduced, not filled.
    assert not deck_design.directly_usable(quiz)
    assert not deck_design.can_host(quiz)


def test_a_reference_design_is_reproduced_on_a_clean_layout_of_its_own_master(catalog):
    quiz = deck_design.template_family_layout(catalog, SlideTypes.QUIZ)
    host = deck_design.host_layout_for(catalog, quiz)
    assert host.master_index == quiz.master_index
    assert host.canvas_capable and not host.fixed_texts
    assert deck_design.family_of(host) is None


def test_the_reference_artwork_is_described_in_brand_terms(catalog):
    from app.genai.ppt import deck_builder as db

    quiz = deck_design.template_family_layout(catalog, SlideTypes.QUIZ)
    host = deck_design.host_layout_for(catalog, quiz)
    shapes = deck_design.reference_design(catalog, quiz, host)
    palette = db.brand_palette(catalog.theme.colors)

    texts = [s for s in shapes if s.get("text")]
    assert any("QUESTION" in s["text"] for s in texts)
    # Invisible text holders read as text boxes, not as white rectangles.
    assert all(s["kind"] == "textbox" for s in texts if "fill" not in s)
    assert all(s["text_size_pt"] in GeometryConstants.TYPE_SCALE
               for s in texts if "text_size_pt" in s)
    assert all(s[key] in palette for s in shapes
               for key in ("fill", "outline", "text_color") if key in s)
    # The logo and title rule the host already draws are not listed again.
    assert not any(s["kind"] == "picture" for s in shapes)


@pytest.mark.parametrize("creativity,instructions,usage,source", [
    ("LOW", "", "reproduce", "template"),
    ("LOW", "make it a game show", "reproduce", "template"),
    ("MED", "", "reproduce", "template"),
    ("MED", "make it a game show", "inspire", "generated"),
    ("HIGH", "", "inspire", "generated"),
])
def test_creativity_decides_how_binding_the_template_layout_is(
        catalog, creativity, instructions, usage, source):
    brief = _brief(4, SlideTypes.QUIZ, creativity_level=creativity,
                   planner_special_instructions=instructions)
    plan = deck_design.plan_leader(catalog, brief, None)
    assert (plan.reference_usage, plan.source) == (usage, source)
    # Only the quiz design itself is described - never the master's logo or title rule.
    assert plan.reference_shapes
    assert all(shape["rect_in"][1] >= 1.0 for shape in plan.reference_shapes)
    if usage == "reproduce":
        host = catalog.get(plan.brief_updates["layout_key"])
        assert plan.brief_updates["mode"] == "canvas" and host.canvas_capable
        # The frame is re-derived for the layout the slide actually lands on.
        assert plan.brief_updates["frame"]["slide_number"]["value"] == 4
        assert plan.brief_updates["frame"]["classification"]["label_text"] == "Internal"
    else:
        assert plan.brief_updates == {}


def test_without_a_template_layout_the_leader_designs_its_own(catalog, monkeypatch):
    monkeypatch.setattr(deck_design, "template_family_layout", lambda *a: None)
    plan = deck_design.plan_leader(catalog, _brief(4, SlideTypes.FAQ,
                                                   creativity_level="LOW"), None)
    assert (plan.source, plan.reference_usage, plan.brief_updates) == (
        "generated", "", {})


def test_an_upstream_layout_key_is_honoured_as_the_base(catalog):
    directive = FamilyLayout(slide_type="QUIZ", layout_key="M1-L10",
                             description="two columns", upstream_fingerprint="x")
    plan = deck_design.plan_leader(catalog, _brief(4, SlideTypes.QUIZ), directive)
    assert plan.source == "upstream" and plan.brief_updates["layout_key"] == "M1-L10"


def test_tier_2_never_offers_a_quiz_or_faq_reference_layout(tmp_path, catalog):
    agent = creator.PptCreatorAgent("s1", tmp_path / "work")
    quiz = deck_design.template_family_layout(catalog, SlideTypes.QUIZ)
    host = deck_design.host_layout_for(catalog, quiz)
    brief = _brief(2).model_copy(update={"layout_key": host.key})
    candidates = agent._layout_candidates(brief, catalog)
    assert candidates and all(deck_design.family_of(c) is None for c in candidates)


def test_a_follower_in_tier_2_is_held_to_its_family_layout(tmp_path, catalog):
    agent = creator.PptCreatorAgent("s1", tmp_path / "work")
    follower = deck_design.FamilyAssignment(role="follower", slide_type="QUIZ")
    candidates = agent._layout_candidates(_brief(2, SlideTypes.QUIZ), catalog, follower)
    assert [c.key for c in candidates] == ["M1-L10"]


# --------------------------------------------------------------------------------- #
# deck-level consistency                                                              #
# --------------------------------------------------------------------------------- #

def test_parallel_quiz_slides_share_one_leader_and_one_layout(tmp_path, store,
                                                              fast_polls, monkeypatch):
    agent = creator.PptCreatorAgent("s1", tmp_path / "work")
    prompts = _record_requests(agent, monkeypatch)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(agent.build_slide,
                                [_brief(n, SlideTypes.QUIZ) for n in (3, 5, 6, 8)]))

    roles = sorted(r.family_role for r in results)
    assert roles == ["follower", "follower", "follower", "leader"]
    assert all(r.gate_passed and r.slide_type == "QUIZ" for r in results)
    assert len({r.family_layout_key for r in results}) == 1
    leader = next(r for r in results if r.family_role == "leader")
    assert "QUIZ LAYOUT FOR THIS DECK (you establish it)" in prompts[leader.slide_number]
    assert "Reproduce the brand template's reference design" in \
        prompts[leader.slide_number]
    for result in results:
        if result.family_role == "follower":
            text = prompts[result.slide_number]
            assert "ESTABLISHED QUIZ LAYOUT (mandatory)" in text
            assert f"established by slide {leader.slide_number}" in text
            assert "deck.add_slide(LAYOUT_KEY)" in text          # the exemplar
            assert "you establish it" not in text


def test_a_mixed_deck_keeps_quiz_and_faq_apart_and_leaves_normal_slides_alone(
        tmp_path, store, monkeypatch):
    agent = creator.PptCreatorAgent("s1", tmp_path / "work")
    prompts = _record_requests(agent, monkeypatch)
    plan = [(1, "NORMAL"), (2, "NORMAL"), (3, "QUIZ"), (4, "NORMAL"), (5, "FAQ"),
            (6, "QUIZ"), (7, "FAQ"), (8, "QUIZ"), (9, "NORMAL")]

    results = {n: agent.build_slide(_brief(n, kind)) for n, kind in plan}

    assert [n for n, r in results.items() if r.family_role == "leader"] == [3, 5]
    assert {n for n, r in results.items() if r.family_role == "follower"} == {6, 7, 8}
    for n, kind in plan:
        if kind == "NORMAL":
            assert results[n].family_role == "" and results[n].layout_used == "M1-L10"
            assert creator.DIRECTION_HEADER.strip() not in prompts[n]
    assert "established by slide 3" in prompts[8] and "SLIDE TYPE: QUIZ" in prompts[8]
    assert "established by slide 5" in prompts[7] and "SLIDE TYPE: FAQ" in prompts[7]
    stored = DeckDesignContext.model_validate(store.get_deck_design("s1"))
    assert stored.quiz_layout.established_at_slide == 3
    assert stored.faq_layout.established_at_slide == 5


def test_a_normal_deck_never_touches_the_deck_design(tmp_path, monkeypatch):
    agent = creator.PptCreatorAgent("s1", tmp_path / "work")
    _record_requests(agent, monkeypatch)

    def forbidden(*a, **k):
        raise AssertionError("a NORMAL slide read the deck design")

    monkeypatch.setattr(deck_design, "load", forbidden)
    assert agent.build_slide(_brief(2)).gate_passed
    assert agent._design is None


def test_a_later_turn_reuses_the_family_layout_it_persisted(tmp_path, store,
                                                            monkeypatch):
    first = creator.PptCreatorAgent("s1", tmp_path / "turn1")
    _record_requests(first, monkeypatch)
    established = first.build_slide(_brief(3, SlideTypes.QUIZ))
    assert established.family_role == "leader"

    second = creator.PptCreatorAgent("s1", tmp_path / "turn2")
    prompts = _record_requests(second, monkeypatch)
    added = second.build_slide(_brief(10, SlideTypes.QUIZ, total=10))

    assert added.family_role == "follower"
    assert added.family_layout_key == established.family_layout_key
    assert "established by slide 3" in prompts[10]
    # The exemplar never points the follower into the leader's sandbox.
    assert str(tmp_path / "turn1") not in prompts[10]
    assert (tmp_path / "turn1").as_posix() not in prompts[10]


def test_an_upstream_directive_redesigns_the_family_once(tmp_path, store, monkeypatch):
    agent = creator.PptCreatorAgent("s1", tmp_path / "turn1")
    _record_requests(agent, monkeypatch)
    agent.build_slide(_brief(3, SlideTypes.QUIZ))

    request = {"quiz_layout": "Game-show style: a big question band and four buzzers"}
    redesign = creator.PptCreatorAgent("s1", tmp_path / "turn2")
    prompts = _record_requests(redesign, monkeypatch)
    first = redesign.build_slide(_brief(4, SlideTypes.QUIZ,
                                        deck_design_context=request))
    again = redesign.build_slide(_brief(6, SlideTypes.QUIZ,
                                        deck_design_context=request))

    assert first.family_role == "leader"
    assert "Requested design for quiz slides: Game-show style" in prompts[4]
    # Custom request at MED: the template's Quiz layout is inspiration only.
    assert "Treat it as inspiration only" in prompts[4]
    # The same directive arriving again is not a new request.
    assert again.family_role == "follower" and "established by slide 4" in prompts[6]
    stored = DeckDesignContext.model_validate(store.get_deck_design("s1"))
    assert stored.quiz_layout.source == "upstream"
    assert stored.quiz_layout.established_at_slide == 4


def test_a_leader_that_crashes_hands_the_family_to_the_next_slide(tmp_path, store,
                                                                  monkeypatch):
    agent = creator.PptCreatorAgent("s1", tmp_path / "work")
    _record_requests(agent, monkeypatch)
    real_ladder = agent._run_ladder
    calls = {"n": 0}

    def crash_first(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("leader exploded")
        return real_ladder(*args, **kwargs)

    monkeypatch.setattr(agent, "_run_ladder", crash_first)
    with pytest.raises(RuntimeError):
        agent.build_slide(_brief(3, SlideTypes.FAQ))

    # The lock was released, so this neither hangs nor follows a layout nobody built.
    assert agent.build_slide(_brief(5, SlideTypes.FAQ)).family_role == "leader"


def test_a_leader_that_falls_back_establishes_nothing(tmp_path, store, monkeypatch):
    agent = creator.PptCreatorAgent("s1", tmp_path / "work")
    _record_requests(agent, monkeypatch, program=BROKEN_PROGRAM)
    _refuse_tier2(agent, monkeypatch)
    monkeypatch.setattr(creator.events, "publish", lambda *a, **k: None)

    fallback = agent.build_slide(_brief(3, SlideTypes.QUIZ))

    assert fallback.tier == 3 and fallback.family_role == "leader"
    entry = agent._design.family(SlideTypes.QUIZ)
    assert entry is None or not entry.established


def test_a_stopped_run_frees_a_slide_waiting_for_its_leader(store, fast_polls,
                                                           monkeypatch):
    registry = deck_design.DeckDesignRegistry("s1")
    assert registry.acquire(SlideTypes.QUIZ, 3) is None       # slide 3 leads
    # A different family is never held up by the QUIZ leader.
    assert registry.acquire(SlideTypes.FAQ, 4) is None
    registry.release(SlideTypes.FAQ)

    def stopped():
        raise run_control.RunCancelledError("run-1")

    monkeypatch.setattr(deck_design.run_control, "checkpoint", stopped)
    with pytest.raises(run_control.RunCancelledError):
        registry.acquire(SlideTypes.QUIZ, 5)
    registry.release(SlideTypes.QUIZ)


def test_an_established_layout_that_cannot_host_slides_is_re_established(
        tmp_path, store, monkeypatch, catalog):
    quiz = deck_design.template_family_layout(catalog, SlideTypes.QUIZ)
    stale = DeckDesignContext(quiz_layout=FamilyLayout(
        slide_type="QUIZ", layout_key=quiz.key, established_at_slide=2,
        source="upstream"))
    store.save_deck_design("s1", stale.model_dump(mode="json"))
    agent = creator.PptCreatorAgent("s1", tmp_path / "work")
    _record_requests(agent, monkeypatch)

    result = agent.build_slide(_brief(4, SlideTypes.QUIZ))

    assert result.family_role == "leader" and result.family_layout_key != quiz.key


# --------------------------------------------------------------------------------- #
# persistence                                                                         #
# --------------------------------------------------------------------------------- #

def test_upstream_merge_applies_each_directive_once():
    stored = DeckDesignContext(quiz_layout=FamilyLayout(
        slide_type="QUIZ", layout_key="M1-L10", established_at_slide=3))
    upstream = DeckDesignContext(quiz_layout="cards in a 2x2 grid")

    merged, changed = deck_design.merge_upstream(stored, upstream)
    assert changed == ["QUIZ"] and not merged.quiz_layout.established
    again, changed = deck_design.merge_upstream(merged, upstream)
    assert changed == [] and again == merged
    assert deck_design.merge_upstream(merged, None) == (merged, [])


def test_the_deck_design_is_part_of_the_session_snapshot(session_store):
    context = {"quiz_layout": {"slide_type": "QUIZ", "layout_key": "M3-L10",
                               "established_at_slide": 3}}
    session_store.save_deck_design("s1", context)
    before = session_store.run_snapshot("s1")
    assert before["deck_design"] == context

    session_store.save_deck_design("s1", {"quiz_layout": None})
    session_store.restore_snapshot("s1", before)
    assert session_store.get_deck_design("s1") == context

    session_store.restore_snapshot("s1", {**before, "deck_design": None})
    assert session_store.get_deck_design("s1") is None


def test_the_deck_design_is_restored_after_a_redis_loss(session_store, monkeypatch):
    from app.core.handlers import session_store as module

    context = {"faq_layout": {"slide_type": "FAQ", "layout_key": "M3-L10",
                              "established_at_slide": 5}}
    memory = {"version": 1, "plan": {"title": "T"}, "deck_design": context}
    monkeypatch.setattr(module.chat_message_handler, "latest_session_memory",
                        lambda *a, **k: memory)

    assert session_store.restore_if_missing("s1") is True
    assert session_store.get_deck_design("s1") == context


def test_deleting_a_session_drops_its_deck_design(session_store):
    session_store.save_deck_design("s1", {"quiz_layout": None})
    session_store.delete_session("s1")
    assert session_store.get_deck_design("s1") is None


def test_an_unreadable_store_degrades_to_an_empty_context(monkeypatch):
    class Broken:
        def get_deck_design(self, session_id):
            raise ConnectionError("redis is down")

        def save_deck_design(self, session_id, context):
            raise ConnectionError("redis is down")

    monkeypatch.setattr(deck_design, "session_store", Broken())
    assert deck_design.load("s1") == DeckDesignContext()
    assert deck_design.save("s1", DeckDesignContext()) is False


def test_the_contract_frame_is_unchanged_by_the_extraction(catalog):
    from app.genai.ppt.design_contract import DeckDesignContract, frame_for_layout

    contract = DeckDesignContract(
        master_index=1, grid={}, tokens={}, theme_font="Barlow",
        classification="internal", slide_width=catalog.slide_width,
        slide_height=catalog.slide_height)
    layout = catalog.get("M1-L10")
    assert contract.frame_for(4, layout) == frame_for_layout(
        "internal", catalog.slide_width, catalog.slide_height, 4, layout)

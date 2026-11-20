"""The once-per-deck contract. Every test here runs with a stub in place of the model:
the deterministic parts must be right on their own, and the one judgement call must be
validated against the catalog before it is trusted."""

import pytest

from app.core.models.deck_model import DeckDraft, DeckPlan, SlideDraft, SlidePlan
from app.genai.ppt import design_contract as dc
from app.genai.ppt.template import catalog as catalog_mod


@pytest.fixture(scope="module")
def cat():
    return catalog_mod.load()


@pytest.fixture
def plan():
    return DeckPlan(title="SMT Quality", classification="internal", slides=[
        SlidePlan(slide_number=1, kind="cover", title="SMT Quality"),
        SlidePlan(slide_number=2, kind="content", title="Reflow control",
                  visual="diagram"),
        SlidePlan(slide_number=3, kind="content", title="Yield by line", visual="table"),
        SlidePlan(slide_number=4, kind="thanks", title="Thank you"),
    ])


@pytest.fixture
def draft(plan):
    return DeckDraft(title=plan.title, slides=[
        SlideDraft(slide_number=s.slide_number, kind=s.kind, title=s.title)
        for s in plan.slides])


def stub(assignments):
    def ask(system, user):
        return assignments
    return ask


# --- the deterministic half ------------------------------------------------ #

def test_the_contract_pins_one_master_for_every_content_slide(plan, draft, cat):
    contract = dc.build(plan, draft, catalog=cat, ask=stub([]))
    assert contract.master_index == cat.preferred_master == 1
    for number in (2, 3):
        layout = cat.get(contract.assignments[number].layout_key)
        assert layout.master_index == 1


def test_cover_and_closing_slides_are_exempt_from_master_uniformity(plan, draft, cat):
    contract = dc.build(plan, draft, catalog=cat, ask=stub([]))
    cover = cat.get(contract.assignments[1].layout_key)
    assert "cover" in cover.tags


def test_the_grid_is_twelve_by_six_with_fixed_gutters(plan, draft, cat):
    grid = dc.build(plan, draft, catalog=cat, ask=stub([])).grid
    assert grid == {"columns": 12, "rows": 6, "gutter_in": 0.15}


def test_the_tokens_come_from_the_template_theme(plan, draft, cat):
    contract = dc.build(plan, draft, catalog=cat, ask=stub([]))
    assert contract.tokens["red"] == "CD163F"
    assert contract.tokens["blue"] == "003E7E"
    assert contract.theme_font == "Barlow"
    assert contract.tokens["_type_scale"] == list(dc.GC.TYPE_SCALE)


def test_the_frame_carries_the_classification_and_the_slide_number(plan, draft, cat):
    contract = dc.build(plan, draft, catalog=cat, ask=stub([]))
    frame = contract.frame_for(2, cat.get(contract.assignments[2].layout_key))
    assert frame["classification"]["label_text"] == "Internal"
    assert frame["classification"]["symbol_path"].endswith("Internal_Symbol.png")
    assert frame["slide_number"]["value"] == 2


def test_the_logo_variant_follows_the_layout_background(plan, draft, cat):
    contract = dc.build(plan, draft, catalog=cat, ask=stub([]))
    dark = next(l for l in cat.layouts if l.background_luminance < 0.5)
    frame = contract.frame_for(2, dark)
    assert frame["logo"] is None or frame["logo"]["variant"] == "white"


# --- the one judgement call, and its validation ---------------------------- #

def test_a_valid_model_assignment_is_used_as_given(plan, draft, cat):
    contract = dc.build(plan, draft, catalog=cat, ask=stub([
        {"slide_number": 2, "mode": "canvas", "layout_key": "M1-L10"},
        {"slide_number": 3, "mode": "layout", "layout_key": "M1-L10"},
    ]))
    assert contract.assignments[2].mode == "canvas"
    assert contract.assignments[2].layout_key == "M1-L10"
    assert contract.assignments[2].free_region is not None


def test_an_unknown_layout_key_is_rejected(plan, cat):
    parsed, problems = dc.validate_assignments(
        [{"slide_number": 2, "mode": "layout", "layout_key": "M9-L99"}], plan, cat)
    assert 2 not in parsed
    assert any("M9-L99" in p for p in problems)


def test_canvas_mode_on_a_layout_that_cannot_take_it_is_rejected(plan, cat):
    busy = next(l for l in cat.layouts if not l.canvas_capable and l.placeholders
                and l.master_index == cat.preferred_master)
    parsed, problems = dc.validate_assignments(
        [{"slide_number": 2, "mode": "canvas", "layout_key": busy.key}], plan, cat)
    assert 2 not in parsed
    assert any("canvas" in p.lower() for p in problems)


def test_a_content_slide_on_a_non_preferred_master_is_rejected(plan, cat):
    other = next(l for l in cat.layouts
                 if l.master_index != cat.preferred_master and "content" in l.tags)
    parsed, problems = dc.validate_assignments(
        [{"slide_number": 2, "mode": "layout", "layout_key": other.key}], plan, cat)
    assert 2 not in parsed
    assert any("master" in p.lower() for p in problems)


def test_a_malformed_entry_is_rejected_rather_than_raising(plan, cat):
    parsed, problems = dc.validate_assignments(
        ["not an object", {"mode": "layout"}, {"slide_number": "x"},
         {"slide_number": 99, "layout_key": "M1-L10"},
         {"slide_number": 2, "mode": "collage", "layout_key": "M1-L10"}], plan, cat)
    assert parsed == {}
    assert len(problems) == 5


def test_a_rejected_assignment_is_re_requested_once_then_falls_back(plan, draft, cat):
    calls = {"n": 0}

    def ask(system, user):
        calls["n"] += 1
        if calls["n"] == 1:
            return [{"slide_number": 2, "mode": "canvas", "layout_key": "M9-L99"}]
        return []                       # the second answer is no better

    contract = dc.build(plan, draft, catalog=cat, ask=ask)
    assert calls["n"] == 2, "the model is asked again exactly once"
    assigned = contract.assignments[2]
    assert assigned.mode == "layout"                        # the safe default
    assert cat.get(assigned.layout_key) is not None         # a real layout all the same


def test_the_second_request_names_what_was_wrong(plan, draft, cat):
    seen = []

    def ask(system, user):
        seen.append(user)
        return [{"slide_number": 2, "mode": "layout", "layout_key": "M9-L99"}]

    dc.build(plan, draft, catalog=cat, ask=ask)
    assert len(seen) == 2
    assert "M9-L99" in seen[1]


def test_style_guidance_reaches_art_direction_without_changing_default_prompt(
        plan, draft, cat):
    default = dc._describe_deck(plan, draft, cat)
    assert dc._describe_deck(plan, draft, cat, "") == default
    styled = dc._describe_deck(plan, draft, cat, "Use a spacious layout.")
    assert "USER-SELECTED STYLE GUIDANCE" in styled
    assert "Use a spacious layout." in styled
    assert "brand palette" in styled


def test_a_model_failure_never_fails_the_deck(plan, draft, cat):
    def ask(system, user):
        raise RuntimeError("gateway 500")

    contract = dc.build(plan, draft, catalog=cat, ask=ask)
    assert len(contract.assignments) == len(plan.slides)
    assert all(a.mode == "layout" for a in contract.assignments.values())


def test_every_slide_ends_up_assigned_whatever_the_model_said(plan, draft, cat):
    contract = dc.build(plan, draft, catalog=cat, ask=stub([
        {"slide_number": 2, "mode": "canvas", "layout_key": "M1-L10"}]))
    assert set(contract.assignments) == {1, 2, 3, 4}
    for assignment in contract.assignments.values():
        assert cat.get(assignment.layout_key) is not None


def test_brief_fields_are_exactly_what_the_code_brief_declares(plan, draft, cat):
    from app.core.models.deck_model import SlideCodeBrief

    contract = dc.build(plan, draft, catalog=cat, ask=stub([]))
    fields = contract.brief_fields(2, cat.get(contract.assignments[2].layout_key))
    assert set(fields) <= set(SlideCodeBrief.model_fields)
    assert set(fields) == {"mode", "layout_key", "grid", "frame", "tokens",
                           "free_region"}

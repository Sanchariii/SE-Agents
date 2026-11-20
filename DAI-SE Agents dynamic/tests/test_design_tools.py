"""The design agent's tools. Every one runs offline against the real template."""

import json

import pytest

from app.genai.tools import design_tools


@pytest.fixture(scope="module")
def tools():
    return {t.name: t for t in design_tools.build_design_tools("s1")}


def test_every_tool_the_design_declares_is_present(tools):
    assert set(tools) == {"get_template_summary", "search_layouts", "recommend_layouts",
                          "get_layout_details", "check_geometry", "run_slide_code",
                          "validate_slide"}


def test_the_summary_names_the_slide_size_fonts_and_preferred_master(tools):
    summary = tools["get_template_summary"].invoke({})
    assert "13.33" in summary and "Barlow" in summary
    assert "preferred" in summary.lower()
    assert "M1" in summary


def test_search_finds_layouts_by_keyword(tools):
    found = tools["search_layouts"].invoke({"query": "agenda", "limit": 5})
    assert "agenda" in found.lower()
    assert "M" in found                       # catalog keys are quoted back


def test_recommend_ranks_layouts_for_an_intent(tools):
    ranked = tools["recommend_layouts"].invoke({"intent": "cover", "limit": 3})
    assert "cover" in ranked.lower()


def test_layout_details_carry_idx_geometry_capacity_decor_and_the_safe_region(tools):
    details = json.loads(tools["get_layout_details"].invoke({"layout": "M1-L10"}))
    assert details["key"] == "M1-L10"
    title = next(p for p in details["placeholders"] if p["role"] == "title")
    assert title["idx"] == 0 and title["approx_chars"] > 0
    assert len(details["decor_rects"]) == 2
    assert details["safe_region"] and len(details["safe_region"]) == 4
    assert details["grid"]["columns"] == 12
    assert "reserved_zones" in details


def test_layout_details_on_an_unknown_layout_says_so_usefully(tools):
    answer = tools["get_layout_details"].invoke({"layout": "M9-L99"})
    assert "unknown" in answer.lower() or "did you mean" in answer.lower()


# --- check_geometry: the fast path ----------------------------------------- #

def test_check_geometry_clears_a_clean_arrangement(tools):
    boxes = [{"id": "a", "left": 1.0, "top": 2.0, "width": 3.0, "height": 1.0,
              "has_text": True, "filled": False, "text": "one"},
             {"id": "b", "left": 5.0, "top": 2.0, "width": 3.0, "height": 1.0,
              "has_text": True, "filled": False, "text": "two"}]
    answer = tools["check_geometry"].invoke({"boxes": boxes, "layout": "M1-L10"})
    assert "passed" in answer.lower()


def test_check_geometry_reports_an_overlap_before_any_code_runs(tools):
    boxes = [{"id": "card 2", "left": 6.80, "top": 2.40, "width": 3.60, "height": 1.80,
              "has_text": True},
             {"id": "card 3", "left": 8.00, "top": 2.40, "width": 3.60, "height": 1.80,
              "has_text": True}]
    answer = tools["check_geometry"].invoke({"boxes": boxes, "layout": "M1-L10"})
    assert "rule 2" in answer and "card 3" in answer


def test_check_geometry_protects_the_layouts_own_decor(tools):
    boxes = [{"id": "a", "left": 0.25, "top": 0.30, "width": 2.0, "height": 1.0}]
    answer = tools["check_geometry"].invoke({"boxes": boxes, "layout": "M1-L10"})
    assert "rule 5" in answer


def test_check_geometry_checks_overflow_only_when_given_text_and_a_size(tools):
    boxes = [{"id": "a", "left": 1.0, "top": 2.0, "width": 2.0, "height": 0.3,
              "has_text": True, "text": "word " * 120, "font_size": 15}]
    assert "rule 10" in tools["check_geometry"].invoke({"boxes": boxes,
                                                        "layout": "M1-L10"})
    sized_out = [{**boxes[0], "font_size": None}]
    assert "rule 10" not in tools["check_geometry"].invoke({"boxes": sized_out,
                                                            "layout": "M1-L10"})


def test_check_geometry_says_it_is_not_the_last_word(tools):
    """Rules 7-9 need the rendered XML; the tool must not imply otherwise."""
    boxes = [{"id": "a", "left": 1.0, "top": 2.0, "width": 3.0, "height": 1.0}]
    answer = tools["check_geometry"].invoke({"boxes": boxes, "layout": "M1-L10"})
    assert "validate_slide" in answer


def test_malformed_boxes_are_explained_not_raised(tools):
    answer = tools["check_geometry"].invoke({"boxes": [{"id": "a"}], "layout": "M1-L10"})
    assert "left" in answer and "required" in answer.lower()


def test_a_non_numeric_measurement_is_explained_not_raised(tools):
    boxes = [{"id": "a", "left": "far left", "top": 2.0, "width": 3.0, "height": 1.0}]
    answer = tools["check_geometry"].invoke({"boxes": boxes, "layout": "M1-L10"})
    assert "number" in answer.lower()


def test_the_run_and_validate_tools_say_so_when_nothing_is_bound(tools):
    """They are only usable inside a staged slide directory; unbound they must explain
    themselves rather than raise into the agent loop."""
    assert "no slide directory" in tools["run_slide_code"].invoke({"code": "x = 1"})
    assert "no slide directory" in tools["validate_slide"].invoke({})

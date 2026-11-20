"""The LangGraph agent.

    START -> write_program -> run_program --ok--> check_deck --clean--> finish -> score -> END
                 ^                |                    |
                 |             failed           problems (once)
                 +-- repair <-----+                    |
                 +-- fix_deck <------------------------+
                                  |
                 failed, no attempts left -> fail -> END
                 (or finish with the best deck so far, if there is one)

The prompt (``prompts.py``) and the run folder (``staging.py``) are prepared before
the graph starts, by ``deck_engine.build_deck``.

* write_program : the model writes ONE DeckBuilder program for the whole deck.
* run_program   : the classic code guard checks it, then it runs locally in the run
                  folder on the official template (no secrets inherited).
* repair        : the error goes back to the model for a fixed program.
* check_deck    : DeckBuilder's own warnings, the layout check every engine is scored
                  by, a slide count check and - when the user picked a reference - the
                  style match. Every version is kept and the best one is remembered.
* fix_deck      : once, those problems go back to the model for a fixed program.
* finish        : the BEST version becomes the deck (a fix round that made it worse is
                  thrown away). No logos are stamped: the template carries them and
                  DeckBuilder stamps each slide's frame.
* score         : the deck scorecard (layout, brand, readability, content, style).
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable, TypedDict

from app.config.constants import SkillDeckConstants as C
from app.genai.skill_deck import layout_check, sandbox, scorecard, style_match
from app.genai.skill_deck.llm import NoProgramError, ProgramWriter
from app.utilities import run_control
from app.utilities.logger_util import get_logger

log = get_logger(__name__)


class DeckState(TypedDict, total=False):
    """Everything the nodes read and write."""
    system: str                # system prompt with the skills
    messages: list[dict]       # conversation with the model
    run_dir: str               # staged run folder
    output_path: str           # where the final deck is saved
    slide_count: int           # slides the program must build
    program: str               # latest generated program
    attempts: int              # programs requested so far
    error: str                 # latest failure, fed back on repair
    raw_deck: str              # deck the latest program wrote
    good_deck: str             # best deck so far (fallback when a fix round fails)
    best: dict                 # {"deck", "problems", "style_score", "differences"}
    layout_problems: list[str] # what check_deck found in raw_deck
    style_target: dict         # measures from the reference ({} = no reference)
    style_score: int           # style match of raw_deck, 0-100
    style_differences: list[str]
    layout_rounds: int         # fix rounds done so far
    spec: dict                 # the written deck, for the content score
    scorecard: dict            # final deck's scorecard
    final_deck: str
    warnings: list[str]
    status: str                # "done" or "failed"


def _rank(version: dict) -> tuple[int, int]:
    """Lower is better: fewer problems first, then a closer style match."""
    return len(version.get("problems") or []), -(version.get("style_score") or 0)


def _problems(deck: Path, run_dir: Path, slide_count: int) -> list[str]:
    from pptx import Presentation
    problems = []
    built = len(Presentation(str(deck)).slides)
    if built != slide_count:
        problems.append(f"the deck has {built} slide(s) but SLIDES has {slide_count}")
    problems += [f"DeckBuilder: {w}" for w in sandbox.builder_warnings(run_dir)]
    problems += layout_check.layout_problems(deck, universal=True)
    return problems[:C.LAYOUT_MAX_PROBLEMS]


def build_graph(writer: ProgramWriter, on_step: Callable[[str], None] | None = None):
    """Compile the agent.

    Args:
        writer: Turns prompts into programs (the gateway model, or a fake in tests).
        on_step: Optional callback for short progress lines (the app shows them in
            the chat as the run's steps).

    Returns:
        A compiled LangGraph app; call ``.invoke(state)``.
    """
    from langgraph.graph import END, START, StateGraph

    def step(message: str) -> None:
        if on_step:
            on_step(message)

    def write_program(state: DeckState) -> DeckState:
        attempts = state.get("attempts", 0) + 1
        run_control.checkpoint()
        step(f"Skill deck agent: writing the deck program (attempt {attempts}).")
        try:
            program = writer.write_program(state["system"], state["messages"])
            return {"program": program, "attempts": attempts, "error": ""}
        except NoProgramError as exc:
            return {"program": "", "attempts": attempts, "error": str(exc)}

    def run_program(state: DeckState) -> DeckState:
        if not state.get("program"):
            return {"raw_deck": "", "error": state.get("error") or "no program"}
        run_control.checkpoint()
        ok, error, deck = sandbox.run_program(state["program"], Path(state["run_dir"]))
        step("Skill deck agent: the program built the deck on the official template."
             if ok else "Skill deck agent: the program failed; asking for a fix.")
        log.info("attempt %d: program %s", state["attempts"], "ran" if ok else "failed")
        return {"raw_deck": str(deck) if ok else "", "error": error}

    def after_run(state: DeckState) -> str:
        if state.get("raw_deck"):
            return "check_deck"
        if state.get("attempts", 0) < C.MAX_FIX_ATTEMPTS:
            return "repair"
        return "finish" if state.get("good_deck") else "fail"

    def check_deck(state: DeckState) -> DeckState:
        raw = Path(state["raw_deck"])
        rounds = state.get("layout_rounds", 0)
        kept = raw.with_name(f"{raw.stem}_v{rounds + 1}{raw.suffix}")
        shutil.copyfile(raw, kept)          # the next program saves over raw_deck
        try:
            problems = _problems(raw, Path(state["run_dir"]), state["slide_count"])
        except Exception as exc:            # a check bug must never cost the deck
            log.warning("deck check failed: %s", exc)
            problems = []
        step(f"Deck check: {len(problems)} problem(s) found." if problems
             else "Deck check: no problems found.")
        match, differences = None, []
        if state.get("style_target"):
            try:
                match, differences = style_match.compare(
                    state["style_target"], style_match.deck_style(raw))
                step(f"Style check: {match}% match with the reference"
                     + (f"; {len(differences)} difference(s)." if differences else "."))
            except Exception as exc:        # same rule as the deck check
                log.warning("style check failed: %s", exc)
        version = {"deck": str(kept), "problems": problems, "style_score": match,
                   "differences": differences}
        best = state.get("best")
        if best and _rank(best) < _rank(version):
            step("Kept the earlier version of the deck: the fix round made it worse.")
        else:
            best = version
        return {"good_deck": best["deck"], "best": best, "layout_problems": problems,
                "style_score": match, "style_differences": differences}

    def after_check(state: DeckState) -> str:
        wrong = state.get("layout_problems") or state.get("style_differences")
        if wrong and state.get("layout_rounds", 0) < C.MAX_LAYOUT_FIXES:
            return "fix_deck"
        return "finish"

    def fix_deck(state: DeckState) -> DeckState:
        problems = state.get("layout_problems") or []
        differences = state.get("style_differences") or []
        goals = (["fix the deck"] if problems else []) + (
            ["match the reference style"] if differences else [])
        step(f"Skill deck agent: asking the model to {' and '.join(goals)}.")
        sections = []
        if problems:
            sections.append("=== PROBLEMS ===\n" + "\n".join(f"- {p}" for p in problems))
        if differences:
            sections.append(
                "=== STYLE DIFFERENCES (from the reference the user picked) ===\n"
                + "\n".join(f"- {d}" for d in differences)
                + "\nMatch the reference where you can; the Rockwell ppt skill always wins.")
        messages = list(state["messages"])
        messages.append({"role": "assistant",
                         "content": f"```python\n{state['program']}\n```"})
        messages.append({"role": "user", "content": (
            "Your program ran, but the deck needs these fixes. Return the complete fixed "
            "program with the same content: move, resize or shorten only what is needed "
            "(detail goes into the speaker notes) and keep every slide on its assigned "
            "layout. It must still end with deck.save(OUTPUT_PATH).\n\n"
            + "\n\n".join(sections))})
        return {"messages": messages, "layout_rounds": state.get("layout_rounds", 0) + 1}

    def repair(state: DeckState) -> DeckState:
        messages = list(state["messages"])
        if state.get("program"):
            messages.append({"role": "assistant",
                             "content": f"```python\n{state['program']}\n```"})
        messages.append({"role": "user", "content": (
            "Running your program failed. Return the complete fixed program; it must "
            f"still end with deck.save(OUTPUT_PATH).\n\n=== ERROR ===\n{state['error']}")})
        return {"messages": messages}

    def finish(state: DeckState) -> DeckState:
        warnings = list(state.get("warnings", []))
        best = state.get("best") or {}
        if not state.get("raw_deck"):       # the fix round's program failed
            warnings.append("the fix round failed to run; the deck before it was used")
        warnings += [f"layout: {p}" for p in best.get("problems", [])]
        warnings += [f"style: {d}" for d in best.get("differences", [])]
        out = Path(state["output_path"])
        shutil.copyfile(best.get("deck") or state["good_deck"], out)
        return {"final_deck": str(out), "warnings": warnings, "status": "done"}

    def score(state: DeckState) -> DeckState:
        try:
            card = scorecard.score_deck(Path(state["final_deck"]), state.get("spec"),
                                        state.get("style_target"))
        except Exception as exc:            # a scoring bug must never cost the deck
            log.warning("scorecard failed: %s", exc)
            return {}
        log.info("%s", scorecard.summary_line(card))
        step(scorecard.summary_line(card))
        return {"scorecard": card}

    def fail(state: DeckState) -> DeckState:
        log.error("no deck after %d attempt(s): %s", state.get("attempts", 0),
                  state.get("error"))
        return {"status": "failed"}

    graph = StateGraph(DeckState)
    for name, node in (("write_program", write_program), ("run_program", run_program),
                       ("repair", repair), ("check_deck", check_deck),
                       ("fix_deck", fix_deck), ("finish", finish), ("score", score),
                       ("fail", fail)):
        graph.add_node(name, node)
    graph.add_edge(START, "write_program")
    graph.add_edge("write_program", "run_program")
    graph.add_conditional_edges("run_program", after_run,
                                {"check_deck": "check_deck", "repair": "repair",
                                 "finish": "finish", "fail": "fail"})
    graph.add_conditional_edges("check_deck", after_check,
                                {"fix_deck": "fix_deck", "finish": "finish"})
    graph.add_edge("repair", "write_program")
    graph.add_edge("fix_deck", "write_program")
    graph.add_edge("finish", "score")
    graph.add_edge("score", END)
    graph.add_edge("fail", END)
    return graph.compile()

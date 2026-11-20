"""The LangGraph agent.

    START -> inspect -> rule_fixes --LOW--------------------------------> finish -> END
                            |                                              ^
                            +--MEDIUM/HIGH--> plan_ops -> apply_ops -> judge

* inspect    : finds the issues (checks.py) and scores the deck as it came in.
* rule_fixes : the fixes code can make on its own (rules.py); this version is kept
               only if it scores at least as well as the deck that came in.
* plan_ops   : MEDIUM/HIGH only - the model gets the level's skill, the brand rules of
               the app's ppt skill, the user's style skill, the deck and what is still
               wrong, and answers with edit operations.
* apply_ops  : code checks each operation and applies the allowed ones (ops.py).
* judge      : scores the new version; it is kept only if it is not worse.
* finish     : saves the best version and writes the one-line summary.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable, TypedDict

from app.config.constants import DeckPolishConstants as C
from app.genai.deck_polish import checks, ops, prompts, rules
from app.genai.deck_polish.llm import OpsWriter
from app.genai.skill_deck import scorecard
from app.utilities import run_control
from app.utilities.logger_util import get_logger

log = get_logger(__name__)


class PolishState(TypedDict, total=False):
    source: str                # the deck the engine built
    level: str                 # LOW | MED | HIGH
    work_dir: str
    output_path: str
    style_guidance: str        # the session's active style skill ("" = none)
    versions: list[dict]       # {"name", "deck", "score", "issues"} in order
    issues: list               # issues of the latest version
    changes: list[str]
    rejected: list[str]
    reports: list[str]         # problems neither rules nor the model may fix
    operations: list[dict]
    pending: list[str]         # change sentences of the model round, until judged
    final_deck: str
    summary: str


def measure(deck: Path) -> tuple[int, list]:
    """(scorecard overall, issues) of a saved deck."""
    from pptx import Presentation
    issues = checks.find_issues(Presentation(str(deck)))
    try:
        score = scorecard.score_deck(deck)["overall"]
    except Exception as exc:            # a scoring bug must never cost the deck
        log.warning("scorecard failed on %s: %s", deck.name, exc)
        score = 0
    return score, issues


def rank(version: dict) -> tuple[int, int]:
    """Higher is better: the score first, then fewer open issues."""
    open_issues = [i for i in version["issues"] if i.fix != "report"]
    return version["score"], -len(open_issues)


def build_graph(writer: OpsWriter | None = None,
                on_step: Callable[[str], None] | None = None):
    """Compile the agent.

    Args:
        writer: Turns prompts into operations (the gateway model, or a fake in tests).
            Not needed at the LOW level.
        on_step: Optional callback for short progress lines shown in the chat.
    """
    from langgraph.graph import END, START, StateGraph
    from pptx import Presentation

    def step(message: str) -> None:
        if on_step:
            on_step(message)

    def save_version(state: PolishState, name: str, prs) -> dict:
        path = Path(state["work_dir"]) / f"polish_{len(state.get('versions') or [])}_{name}.pptx"
        prs.save(str(path))
        score, issues = measure(path)
        return {"name": name, "deck": str(path), "score": score, "issues": issues}

    def inspect(state: PolishState) -> PolishState:
        Path(state["work_dir"]).mkdir(parents=True, exist_ok=True)
        score, issues = measure(Path(state["source"]))
        counts = checks.count_by_fix(issues)
        label = C.LEVEL_LABELS[state["level"]]
        step(f"Deck polish ({label}): {len(issues)} issue(s) found; scorecard {score}/100.")
        log.info("polish %s: %s issue(s) %s, score %d", label, len(issues), counts, score)
        return {"versions": [{"name": "built", "deck": state["source"], "score": score,
                              "issues": issues}],
                "issues": issues, "changes": [], "rejected": [],
                "reports": [i.line() for i in issues if i.fix == "report"]}

    def rule_fixes(state: PolishState) -> PolishState:
        run_control.checkpoint()
        prs = Presentation(state["source"])
        changes = rules.apply(prs, state["issues"], state["level"])
        if not changes:
            return {}
        version = save_version(state, "rules", prs)
        versions = list(state["versions"])
        if rank(version) < rank(versions[-1]):
            log.warning("rule fixes scored worse (%s < %s); kept the deck as built",
                        rank(version), rank(versions[-1]))
            return {}
        step(f"Deck polish: {len(changes)} fix(es) by rules.")
        return {"versions": versions + [version], "issues": version["issues"],
                "changes": state["changes"] + changes}

    def after_rules(state: PolishState) -> str:
        return "plan_ops" if C.OPS_BY_LEVEL.get(state["level"]) and writer else "finish"

    def plan_ops(state: PolishState) -> PolishState:
        run_control.checkpoint()
        current = state["versions"][-1]
        prs = Presentation(current["deck"])
        step(f"Deck polish: asking the model to {('polish' if state['level'] == 'MED' else 'art-direct')} "
             "the deck.")
        try:
            operations = writer.write_ops(
                prompts.system_prompt(state["level"], state.get("style_guidance", "")),
                prompts.user_prompt(prs, state["issues"], state["level"]))
        except Exception as exc:        # the model is optional: keep the rule fixes
            log.warning("polish model call failed: %s", exc)
            step("Deck polish: the model was not reachable; kept the rule fixes.")
            return {"operations": []}
        log.info("model proposed %d operation(s)", len(operations))
        return {"operations": operations}

    def after_plan(state: PolishState) -> str:
        return "apply_ops" if state.get("operations") else "finish"

    def apply_ops(state: PolishState) -> PolishState:
        current = state["versions"][-1]
        prs = Presentation(current["deck"])
        applied, rejected = ops.apply(prs, state["operations"], state["level"])
        for reason in rejected:
            log.info("operation skipped: %s", reason)
        if not applied:
            step(f"Deck polish: none of the {len(state['operations'])} proposed change(s) "
                 "passed the checks.")
            return {"rejected": rejected}
        version = save_version(state, "model", prs)
        return {"versions": state["versions"] + [version], "rejected": rejected,
                "pending": applied}

    def judge(state: PolishState) -> PolishState:
        versions = list(state["versions"])
        if versions[-1]["name"] != "model":
            return {}
        applied = state.get("pending") or []
        if rank(versions[-1]) < rank(versions[-2]):
            step(f"Deck polish: the model's {len(applied)} change(s) made the deck score "
                 "worse, so they were thrown away.")
            return {"versions": versions[:-1]}
        step(f"Deck polish: {len(applied)} change(s) by the model kept"
             + (f"; {len(state['rejected'])} skipped by the checks." if state["rejected"]
                else "."))
        return {"issues": versions[-1]["issues"], "changes": state["changes"] + applied}

    def finish(state: PolishState) -> PolishState:
        versions = state["versions"]
        best = versions[-1]
        out = Path(state["output_path"])
        if best["deck"] != state["source"]:
            shutil.copyfile(best["deck"], out)
        else:
            out = Path(state["source"])
        first = versions[0]
        summary = (f"Deck polish ({C.LEVEL_LABELS[state['level']]}): scorecard "
                   f"{first['score']} -> {best['score']}, {len(state['changes'])} change(s)"
                   + (f", {len(state['reports'])} problem(s) need a person "
                      "(see notes)" if state["reports"] else "") + ".")
        step(summary)
        log.info("%s Kept: %s", summary, "; ".join(state["changes"]) or "nothing")
        return {"final_deck": str(out), "summary": summary}

    graph = StateGraph(PolishState)
    for name, node in (("inspect", inspect), ("rule_fixes", rule_fixes),
                       ("plan_ops", plan_ops), ("apply_ops", apply_ops), ("judge", judge),
                       ("finish", finish)):
        graph.add_node(name, node)
    graph.add_edge(START, "inspect")
    graph.add_edge("inspect", "rule_fixes")
    graph.add_conditional_edges("rule_fixes", after_rules,
                                {"plan_ops": "plan_ops", "finish": "finish"})
    graph.add_conditional_edges("plan_ops", after_plan,
                                {"apply_ops": "apply_ops", "finish": "finish"})
    graph.add_edge("apply_ops", "judge")
    graph.add_edge("judge", "finish")
    graph.add_edge("finish", END)
    return graph.compile()

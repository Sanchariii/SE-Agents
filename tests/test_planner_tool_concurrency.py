"""Regression: plan tools issued in one model turn must not overwrite each other's edits."""

import time

from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.prebuilt import ToolNode

from app.config.constants import AgentConstants


def _run(max_concurrency: int | None) -> list[str]:
    """Run three read-modify-write tools from one AI message; return the stored plan.

    Args:
        max_concurrency: Passed through the run config, as ToolCallingAgent does.

    Returns:
        The slide titles left in the shared store after all tools ran.
    """
    store = {"slides": ["a", "b", "c"]}

    def _mutate(fn) -> str:
        # read, slow "LLM call", write back — the same shape as the plan tools
        snapshot = list(store["slides"])
        time.sleep(0.05)
        store["slides"] = fn(snapshot)
        return "ok"

    @tool
    def revise(index: int) -> str:
        """Revise one slide."""
        return _mutate(lambda s: [x + "!" if i == index else x for i, x in enumerate(s)])

    @tool
    def add() -> str:
        """Append one slide."""
        return _mutate(lambda s: s + ["new"])

    calls = [{"name": "revise", "args": {"index": 0}, "id": "1"},
             {"name": "revise", "args": {"index": 2}, "id": "2"},
             {"name": "add", "args": {}, "id": "3"}]
    config = {"max_concurrency": max_concurrency} if max_concurrency else {}
    ToolNode([revise, add]).invoke([AIMessage(content="", tool_calls=calls)], config=config)
    return store["slides"]


def test_planner_tools_apply_every_edit_in_order() -> None:
    """Sequential execution keeps both revisions and the added slide."""
    assert _run(AgentConstants.PLANNER_TOOL_CONCURRENCY) == ["a!", "b", "c!", "new"]


def test_parallel_tools_lose_edits() -> None:
    """Documents the bug: parallel execution keeps only the last writer's view."""
    assert _run(None) != ["a!", "b", "c!", "new"]

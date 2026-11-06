"""Shared LangChain tool-calling agent scaffold used by every ReAct sub-agent (the
planner, and future researcher/writer/critic agents). Each sub-agent supplies its own
name, system prompt and tools; the model wiring, tool-calling loop and result parsing
live here once, so a new sub-agent is just tools + a prompt, not a reimplementation of
the loop."""

from dataclasses import dataclass, field
from typing import Any

from langchain.agents import create_agent

from app.config.constants import AgentConstants, ModelConstants, PlannerConstants
from app.utilities import events_util as events
from app.utilities.apim_llm_utils import get_apim_llm_client
from app.utilities.logger_util import get_logger
from langchain.agents.structured_output import ToolStrategy

log = get_logger(__name__)


@dataclass
class AgentResult:
    reply: str
    tool_calls: list[dict] = field(default_factory=list)   # [{"name": ..., "args": {...}}]
    actions: list[str] = field(default_factory=list)       # tool_calls mapped through tool_intent
    structured: Any | None = None

class ToolCallingAgent:
    """A LangGraph tool-calling loop: give it a name, a system prompt and tools, then call
    `.run(user_content)`. Tools are session-bound closures, so build a new instance per
    call rather than caching one across sessions."""

    def __init__(self, name: str, system_prompt: str, tools: list,
                model: str = ModelConstants.REACT_AGENT,
                recursion_limit: int = AgentConstants.REACT_RECURSION_LIMIT,
                tool_intent: dict[str, str] | None = None,
                session_id: str | None = None,
                response_format=None,
                max_concurrency: int | None = None,
                redact_args: tuple[str, ...] = ()):
        """Build the agent graph.

        Args:
            max_concurrency: Cap on tool calls executed at once from one model turn;
                1 runs them sequentially in the order the model issued them. None
                keeps LangGraph's default (all in parallel).
            redact_args: Tool argument names whose values are internal: they are
                replaced before the tool call is streamed to the client.
        """
        self.name = name
        self.recursion_limit = recursion_limit
        self.max_concurrency = max_concurrency
        self.tool_intent = tool_intent or {}
        self.session_id = session_id
        self.response_format = response_format
        self.redact_args = frozenset(redact_args)
        llm = get_apim_llm_client().get_langchain_llm(model=model)
        if response_format:
            self._graph = create_agent(model=llm, tools=tools, system_prompt=system_prompt, response_format=ToolStrategy(response_format),)
        else:
            self._graph = create_agent(model=llm, tools=tools, system_prompt=system_prompt)
        

    def run(self, user_content: str) -> AgentResult:
        # result = self._graph.invoke(
        #     {"messages": [{"role": "user", "content": user_content}]},
        #     config={"recursion_limit": self.recursion_limit})

        result ={}

        # for chunk in self._graph.stream( {"messages": [{"role": "user", "content": user_content}]},
        #             config={"recursion_limit": self.recursion_limit},
        #             ):
        #     if self.session_id:
        #         print(f"Publishing stream chunk for session {self.session_id}: {chunk}")
        #         print()
        #         print()
        #         events.publish(self.session_id, "agent_step", agent=self.name,
        #                        message=f"stream chunk: {chunk}")
        #         result = chunk  # Update result with the latest chunk


        # print(f"Final result from STREAM {self.name}: {result}")
        # messages = result.get("messages", [])

        config: dict = {"recursion_limit": self.recursion_limit}
        if self.max_concurrency:
            config["max_concurrency"] = self.max_concurrency
        result = self._graph.invoke(
            {"messages": [{"role": "user", "content": user_content}]}, config=config)

        messages = result.get("messages", [])

        # Ensure all messages have content attribute
        tool_calls = [{"name": call.get("name"), "args": call.get("args") or {}}
                      for msg in messages for call in (getattr(msg, "tool_calls", None) or [])]
        actions = [self.tool_intent[c["name"]] for c in tool_calls if c["name"] in self.tool_intent]

        reply = next((m.content if isinstance(m.content, str) else str(m.content)
                     for m in reversed(messages)
                     if getattr(m, "type", None) == "ai" and m.content), "")

        if self.session_id:
            for call in tool_calls:
                events.publish(self.session_id, "agent_step", agent=self.name,
                               message=f"{call['name']}({self._streamable(call['args'])})")

        log.info("%s agent run complete", self.name, extra={
            "session_id": self.session_id, "actions": actions})
        return AgentResult(reply=reply, tool_calls=tool_calls, actions=actions,structured=result.get("structured_response"),)

    def _streamable(self, args: dict) -> dict:
        """A tool call's arguments with every internal value replaced.

        Args:
            args: The arguments the model passed.

        Returns:
            A copy safe to stream to the client.
        """
        if not self.redact_args:
            return args
        return {key: (PlannerConstants.REDACTED_PLACEHOLDER if key in self.redact_args
                      else value) for key, value in args.items()}
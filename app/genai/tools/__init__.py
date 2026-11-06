"""LangChain tools shared by the ReAct sub-agents (planner, and future researcher/writer/
critic agents). Lives alongside `agents/`, not inside it, since these tools aren't owned
by any one agent. Each ``build_*_tools(session_id)`` factory returns tool instances bound
to one session, so the LLM never has to pass — or risk hallucinating — a session_id."""
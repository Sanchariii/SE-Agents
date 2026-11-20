"""Tools sub-agents use to ground their work in the session's uploaded source documents."""

from langchain_core.tools import tool

from app.core.handlers.session_store import session_store
from app.genai.retrieval.retriever import retrieve


def build_document_tools(session_id: str) -> list:
    """Doc-analysis tool instances bound to one session."""

    @tool
    def analyze_source_documents() -> str:
        """List every uploaded source document for this session with a short summary of
        what it covers, plus any source material the user pasted into the chat. Call this
        before planning or revising a deck so the plan is grounded in what the sources
        actually contain, not just their filenames."""
        # Lazy: planner imports the brief, which this tool module must not pull in early.
        from app.genai.agents.planner import document_summaries

        summary = document_summaries(session_id)
        if not session_store.latest_docs(session_id) and summary.startswith("(no "):
            return "No source documents have been uploaded for this session yet."
        return summary


    @tool
    def search_source_documents(query: str, top_k: int = 5) -> str:
        """Search the session's indexed source documents for passages relevant to `query`.
        Use this to check whether the sources actually cover a specific topic the user
        wants a slide about, before committing that slide to the plan."""
        chunks = retrieve(session_id, query, top_k=top_k)
        if not chunks:
            return f"No source content found for: {query!r}"
        return "\n".join(f"- [{c.filename} / {c.section}] {c.text}" for c in chunks)

    return [analyze_source_documents, search_source_documents]
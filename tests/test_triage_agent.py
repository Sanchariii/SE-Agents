"""Tests for triage context assembled from session state."""

from types import SimpleNamespace

from app.genai.agents import triage_agent
from app.genai.agents.triage_agent import TriageDecision, TriageInstruction


def test_document_inventory_reports_count_type_status_and_summary(monkeypatch):
    """Triage gets a short summary (not the document text) so it can judge topic fit."""
    documents = [
        SimpleNamespace(doc_id="d1", version=1, filename="source.pdf",
                        mime_type="application/pdf", status=SimpleNamespace(value="indexed")),
        SimpleNamespace(doc_id="d2", version=1, filename="notes.docx",
                        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        status="extracting"),
    ]
    monkeypatch.setattr(triage_agent.session_store, "latest_docs",
                        lambda session_id: documents)
    monkeypatch.setattr(triage_agent.session_store, "get_ckm", lambda doc_id, version:
                        SimpleNamespace(summary="AI gateway guide" if doc_id == "d1" else None))

    inventory = triage_agent._document_inventory("session-1")

    assert "Document count: 2" in inventory
    assert "source.pdf | type=application/pdf | status=indexed | summary: AI gateway guide" in inventory
    assert "notes.docx | type=application/vnd.openxmlformats-officedocument.wordprocessingml.document | status=extracting" in inventory


def test_document_inventory_reports_no_documents(monkeypatch):
    monkeypatch.setattr(triage_agent.session_store, "latest_docs", lambda session_id: [])

    assert triage_agent._document_inventory("session-1") == (
        "Document count: 0\n(no source documents uploaded)"
    )


def test_triage_sanitizes_emoji_from_reply_and_instruction_text():
    decision = TriageDecision(
        reply="Hello 👋",
        instructions=[TriageInstruction(intent="plan", text="Build it 🚀")],
    )

    sanitized = triage_agent._sanitize_decision(decision)

    assert sanitized.reply == "Hello "
    assert sanitized.instructions[0].text == "Build it "


def test_off_topic_turn_is_declined_and_never_reaches_planner(monkeypatch):
    class _Client:
        def chat_json(self, *a, **k):
            return TriageDecision(turn_type="off_topic", needs_planner=True, reply="",
                                  instructions=[TriageInstruction(intent="plan", text="python")],
                                  suggestions=["x"])
    monkeypatch.setattr(triage_agent, "get_apim_llm_client", lambda: _Client())
    monkeypatch.setattr(triage_agent.session_store, "get_plan", lambda sid: (None, ""))
    monkeypatch.setattr(triage_agent.session_store, "get_messages", lambda sid, limit=12: [])
    monkeypatch.setattr(triage_agent.session_store, "list_decks", lambda sid: [])
    monkeypatch.setattr(triage_agent.session_store, "get_draft", lambda sid: None)
    monkeypatch.setattr(triage_agent.session_store, "latest_docs", lambda sid: [])
    monkeypatch.setattr(triage_agent.DeckBrief, "load", classmethod(lambda cls, sid: cls()))
    decision = triage_agent.classify("s1", "what is python")
    assert decision.needs_planner is False
    assert decision.instructions == []
    assert decision.reply == triage_agent.OFF_TOPIC_REPLY


def test_triage_receives_extracted_facts_beyond_document_summary(monkeypatch):
    """Fine-grained source facts remain available to triage, not just the gate."""
    captured = []

    def chat_json(system, messages, schema, **kwargs):
        captured.append(messages[0]["content"])
        return TriageDecision(turn_type="chat", reply="Ready.")

    monkeypatch.setattr(triage_agent, "get_apim_llm_client",
                        lambda: SimpleNamespace(chat_json=chat_json))
    monkeypatch.setattr(triage_agent.session_store, "get_plan", lambda sid: (None, ""))
    monkeypatch.setattr(triage_agent.session_store, "get_messages", lambda sid, limit=12: [])
    monkeypatch.setattr(triage_agent.session_store, "list_decks", lambda sid: [])
    monkeypatch.setattr(triage_agent.session_store, "get_draft", lambda sid: None)
    monkeypatch.setattr(triage_agent.session_store, "latest_docs", lambda sid: [])
    monkeypatch.setattr(triage_agent.DeckBrief, "load", classmethod(lambda cls, sid: cls()))
    monkeypatch.setattr(triage_agent, "source_evidence",
                        lambda sid: "Handle 429s and transient failures.")
    triage_agent.classify("s1", "Add the guide's retry advice.")
    assert "Handle 429s and transient failures." in captured[0]

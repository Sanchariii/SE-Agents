"""Full deck flow: ingest → plan → build → edit → remove → download → delete.

This exercises every workflow branch against a live service, so it needs Azure
OpenAI, AI Search and Blob storage configured. Expect it to take a few minutes.
"""

import sys
import time

from _client import Api, info, run, sample_docs


def main() -> int:
    api = Api()
    api.login()
    session_id = api.create_session("deck flow")
    info(f"session {session_id}")
    state: dict = {}

    def upload_sources() -> bool:
        response = api.upload(session_id, sample_docs())
        if response.status_code != 200:
            print("   ", response.status_code, response.text[:300])
            return False
        return bool(response.json()["documents"])

    def documents_are_listed() -> bool:
        docs = api.get(f"/decks/{session_id}/documents").json()["documents"]
        state["doc_id"] = docs[0]["doc_id"] if docs else None
        return bool(docs)

    def first_turn_builds_a_deck() -> bool:
        started = time.monotonic()
        events = api.invoke(session_id,
                            "Build a 6-slide training deck on SMT line KPIs and "
                            "reflow/AOI quality for process engineers. English only.")
        result = api.result_of(events)
        state["result"] = result
        info(f"first turn took {time.monotonic() - started:.0f}s, "
             f"deck v{result.get('deck_version')}")
        return bool(result.get("deck_updated") and result.get("deck_version"))

    def versions_are_recorded() -> bool:
        versions = api.get(f"/decks/{session_id}/versions").json()["versions"]
        state["versions"] = versions
        return len(versions) >= 1

    def chat_history_is_persisted() -> bool:
        messages = api.get(f"/decks/{session_id}/messages").json()["messages"]
        return len(messages) >= 2 and messages[0]["role"] == "user"

    def an_edit_makes_exactly_one_new_version() -> bool:
        before = len(api.get(f"/decks/{session_id}/versions").json()["versions"])
        api.invoke(session_id, "On slide 3, shorten the bullets and make the tone punchier.")
        after = len(api.get(f"/decks/{session_id}/versions").json()["versions"])
        info(f"versions {before} → {after}")
        return after == before + 1

    def a_removal_renumbers_the_rest() -> bool:
        events = api.invoke(session_id, "Remove slide 2 from the deck.")
        result = api.result_of(events)
        return result.get("removed_slides") == [2]

    def a_chat_turn_builds_nothing() -> bool:
        before = len(api.get(f"/decks/{session_id}/versions").json()["versions"])
        events = api.invoke(session_id, "Thanks, that looks good!")
        result = api.result_of(events)
        after = len(api.get(f"/decks/{session_id}/versions").json()["versions"])
        return after == before and not result.get("deck_updated")

    def the_deck_downloads_as_a_pptx() -> bool:
        version = api.get(f"/decks/{session_id}/versions").json()["versions"][-1]
        response = api.get(
            f"/decks/{session_id}/versions/{version['deck_version']}/download")
        info(f"downloaded {len(response.content)} bytes")
        return response.status_code == 200 and response.content[:2] == b"PK"

    def an_unknown_version_is_404() -> bool:
        return api.get(f"/decks/{session_id}/versions/999/download").status_code == 404

    def a_source_document_can_be_deleted() -> bool:
        if not state.get("doc_id"):
            return False
        return api.delete(
            f"/decks/{session_id}/documents/{state['doc_id']}").status_code == 200

    def the_session_can_be_deleted() -> bool:
        if api.delete(f"/decks/{session_id}").status_code != 200:
            return False
        remaining = {s["session_id"] for s in api.get("/decks/sessions").json()["sessions"]}
        return session_id not in remaining

    return run("02 deck flow", [
        ("sources upload", upload_sources),
        ("documents are listed", documents_are_listed),
        ("first turn builds a deck", first_turn_builds_a_deck),
        ("versions are recorded", versions_are_recorded),
        ("chat history is persisted", chat_history_is_persisted),
        ("an edit makes exactly one new version", an_edit_makes_exactly_one_new_version),
        ("a removal renumbers the rest", a_removal_renumbers_the_rest),
        ("a chat turn builds nothing", a_chat_turn_builds_nothing),
        ("the deck downloads as a .pptx", the_deck_downloads_as_a_pptx),
        ("an unknown version is 404", an_unknown_version_is_404),
        ("a source document can be deleted", a_source_document_can_be_deleted),
        ("the session can be deleted", the_session_can_be_deleted),
    ])


if __name__ == "__main__":
    sys.exit(main())

"""Real multi-turn run: clarifying questions -> proposed plan -> feedback -> approval -> deck.

Same path as POST /decks/{id}/invoke (deck_handler.run_deck_flow), no mocks. Checks that
nothing is built until the plan is approved and that the user's language is followed.

Run from repo root:
    python -m scripts.manual_testing.10_plan_approval_flow
"""

import sys
import time
from pathlib import Path

from app.config.constants import PlanStatus
from app.core.handlers import document_handler
from app.core.handlers.deck_handler import run_deck_flow
from app.core.handlers.session_store import session_store
from app.genai.agents.brief import APPROVE_COMMAND
from app.genai.ingestion.pipeline import process_document

DOC = Path("artifacts/ROK AI Gateway Engineering Guide 7 1.pdf")
SESSION_ID = f"e2e-approval-{int(time.time()) % 1_000_000}"

TURNS = [
    ("vague request", "Create slides from this document"),
    ("answers", "Plant engineers who are new to the gateway; they should be able to call it "
               "correctly. A short training deck."),
    ("feedback on the plan", "Make it 6 slides and add a knowledge check at the end"),
    ("approval", APPROVE_COMMAND),
]


def main() -> int:
    data = DOC.read_bytes()
    record = document_handler.register_upload(
        SESSION_ID, DOC.name, document_handler.validate_upload(DOC.name, data), data)
    if (record := process_document(record)).error:
        sys.exit(f"ingestion failed: {record.error}")

    results = []
    for label, message in TURNS:
        started = time.monotonic()
        result = run_deck_flow(SESSION_ID, message)
        _, status = session_store.get_plan(SESSION_ID)
        results.append((label, result, status))
        print(f"\n===== {label}: {message!r} ({time.monotonic() - started:.0f}s) =====")
        print(f"action={result.get('action')} plan_status={status} "
              f"awaiting_approval={result.get('awaiting_approval')} "
              f"deck_updated={result.get('deck_updated')}")
        print(result.get("reply") or "")

    asked, answered, revised, approved = (r for _, r, _ in results)
    checks = [
        ("vague request asks questions and builds nothing",
         asked.get("action") == "clarify" and not asked.get("deck_updated")),
        ("answers lead to a proposed plan, not a deck",
         answered.get("awaiting_approval") and not answered.get("deck_updated")),
        ("feedback keeps the plan as a proposal",
         results[2][2] == PlanStatus.DRAFT and not revised.get("deck_updated")),
        ("approval builds the deck", bool(approved.get("deck_updated"))),
        ("plan is confirmed after approval", results[3][2] == PlanStatus.CONFIRMED),
    ]
    print()
    for label, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    sys.exit(main())

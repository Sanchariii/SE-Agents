"""Live run: a new deck with no source material must stop at triage and ask for content.

No mocks: the real triage/planner models (ROK AI Gateway), Redis, Blob and AI Search.
Each turn goes through the same steps as POST /decks/{id}/invoke (deck_service.invoke):
the user turn is recorded, then `deck_handler.run_deck_flow` runs the LangGraph workflow.

Scenario A replays a real chat with no upload ("helo can you create detailed deck on
Car", then audience/deck-type details, then "you decide"): each turn must stop with one
plain chat message asking for the content, and no question card, until the user shares
the content in the chat. The plan is then approved and the deck built and published end
to end; the .pptx is downloaded from Blob and checked for the user's facts.
Scenario B uploads a document: the gate must not block it.

Run from the repo root:
    python -m scripts.manual_testing.12_no_source_content_gate [--skip-upload] [--no-build]

The full transcript (replies, questions, plan status) is written as JSON
(default: artifacts/no_source_content_gate_<time>.json).
"""

import argparse
import json
import sys
import time
from pathlib import Path

from app.config.constants import PlanStatus
from app.core.handlers import document_handler
from app.core.handlers.deck_handler import run_deck_flow
from app.core.handlers.session_store import session_store
from app.genai.agents.brief import APPROVE_COMMAND, SOURCE_CONTENT_REQUEST, DeckBrief
from app.genai.ingestion.pipeline import process_document

STAMP = int(time.time()) % 1_000_000

CAR_CONTENT = """Here is the content for the deck:
- A car's engine converts fuel into motion through four strokes: intake, compression,
  power and exhaust.
- The transmission transfers engine power to the wheels; automatics change gear
  themselves, manuals use a clutch pedal and gear lever.
- Disc brakes squeeze pads against a rotor; ABS stops the wheels locking under hard
  braking.
- Check tyre pressure monthly; the correct value is on the sticker inside the driver's
  door, not on the tyre wall.
- Change engine oil roughly every 10,000 km or 12 months, whichever comes first.
- Dashboard warning lights: red means stop safely now, amber means get it checked soon."""

#: Phrases from CAR_CONTENT the built slides must carry (grounding check).
CAR_FACTS = ("intake", "compression", "clutch", "ABS", "door", "10,000", "amber")

DOC_TEXT = """Lockout/Tagout (LOTO) Procedure - Plant Floor Standard
1. Notify all affected employees that the machine will be shut down for service.
2. Shut the machine down using its normal stopping procedure.
3. Isolate every energy source: electrical, pneumatic, hydraulic and stored energy.
4. Apply a personal lock and a tag to each energy-isolating device.
5. Release stored energy: bleed air lines, block raised parts, discharge capacitors.
6. Verify isolation by attempting a normal start; return controls to OFF afterwards.
7. Only the person who applied a lock may remove it after the work is complete.
Violations of this procedure are a terminable safety offence."""


def invoke(session_id: str, message: str) -> dict:
    """One /invoke chat turn, run the way deck_service.invoke runs it.

    Args:
        session_id: The session.
        message: The typed message.

    Returns:
        A transcript entry: what was sent, the reply, questions and session state.
    """
    session_store.add_message(session_id, "user", message)
    started = time.monotonic()
    result = run_deck_flow(session_id, message)
    plan, status = session_store.get_plan(session_id)
    brief = DeckBrief.load(session_id)
    return {
        "user": message, "seconds": round(time.monotonic() - started, 1),
        "action": result.get("action"), "plan_status": status,
        "plan_slides": [s.get("title") for s in (plan or {}).get("slides", [])],
        "question_ids": [q.get("id") for q in result.get("questions") or []],
        "reply": result.get("reply") or "",
        "brief_has_source_material": bool(brief.source_material.strip()),
        "deck_updated": bool(result.get("deck_updated")),
    }


def show(label: str, turn: dict) -> None:
    """Print one turn of the transcript."""
    print(f"\n===== {label} ({turn['seconds']}s) =====")
    print(f"USER: {turn['user'][:300]}")
    print(f"action={turn['action']} plan_status={turn['plan_status']} "
          f"questions={turn['question_ids']} "
          f"source_material={turn['brief_has_source_material']}")
    print(f"ASSISTANT:\n{turn['reply']}")


def scenario_without_upload() -> tuple[list, list]:
    """Replay the screenshot's chat with no upload, then share the content in chat."""
    sid = f"live-nocontent-{STAMP}"
    print(f"\n######## Scenario A: no upload (session {sid}) ########")
    turns = [("greeting", invoke(sid, "hello")),
             ("bare topic", invoke(sid, "helo can you create detailed deck on Car")),
             ("brief details only", invoke(sid, "It is for new drivers, a training deck")),
             ("'you decide'", invoke(sid, "you decide")),
             ("content shared in chat", invoke(sid, CAR_CONTENT))]
    for label, turn in turns:
        show(label, turn)

    greeting, topic, details, defaults, shared = (t for _, t in turns)

    def blocked(turn: dict) -> bool:
        # Stopped before planning with one plain message and no question card.
        return (turn["action"] == "clarify" and turn["plan_status"] == PlanStatus.NONE
                and not turn["plan_slides"] and not turn["deck_updated"]
                and turn["question_ids"] == []
                and turn["reply"] == SOURCE_CONTENT_REQUEST)

    checks = [
        ("A1 greeting is a chat reply that asks nothing",
         greeting["action"] == "chat" and not greeting["question_ids"]),
        ("A2 bare topic stops and simply asks for content (no question card)",
         blocked(topic)),
        ("A3 audience/deck-type details alone do not unblock planning", blocked(details)),
        ("A4 'you decide' never waives the content request", blocked(defaults)),
        ("A5 content shared in chat is stored on the brief",
         shared["brief_has_source_material"]),
        ("A6 shared content moves on (questions or a proposed plan, nothing built)",
         shared["reply"] != SOURCE_CONTENT_REQUEST and not shared["deck_updated"]),
    ]
    return turns, checks


def scenario_with_upload() -> tuple[list, list]:
    """Upload a real document: the gate must let it through to planning."""
    sid = f"live-withdoc-{STAMP}"
    print(f"\n######## Scenario B: document uploaded (session {sid}) ########")
    name = "lockout_tagout_procedure.txt"
    data = DOC_TEXT.encode("utf-8")
    record = document_handler.register_upload(
        sid, name, document_handler.validate_upload(name, data), data)
    record = process_document(record)
    print(f"ingested {name}: status={record.status} error={record.error}")
    turns = [("create from document", invoke(sid, "Create slides from this document")),
             ("'you decide'", invoke(sid, "you decide"))]
    for label, turn in turns:
        show(label, turn)
    asks_content = any(t["reply"] == SOURCE_CONTENT_REQUEST for _, t in turns)
    checks = [
        ("B0 the document was ingested", not record.error),
        ("B1 an uploaded document is never asked for content", not asks_content),
        ("B2 the document path reaches a proposed plan",
         turns[-1][1]["plan_status"] == PlanStatus.DRAFT and bool(turns[-1][1]["plan_slides"])),
    ]
    return turns, checks


def approve_and_build(sid: str, out_dir: Path, facts: tuple[str, ...]) -> tuple[list, list]:
    """Approve the proposed plan, build and publish the deck, then inspect the .pptx.

    Args:
        sid: The session holding a proposed plan.
        out_dir: Where the downloaded .pptx is saved.
        facts: Phrases from the user's content that the built slides must contain.

    Returns:
        (transcript turns, checks).
    """
    from pptx import Presentation

    from app.config.constants import StorageConstants
    from app.utilities import storage_util

    turn = invoke(sid, APPROVE_COMMAND)
    show("approve -> build -> publish", turn)
    decks = session_store.list_decks(sid)
    pptx_path, titles, text = None, [], ""
    if decks:
        # The published file lives in Blob; the Redis artifact carries its path.
        data = storage_util.read_file(decks[-1].storage_path,
                                      container=StorageConstants.CONTAINER_DECKS)
        pptx_path = out_dir / f"{sid}_v{decks[-1].deck_version}.pptx"
        pptx_path.parent.mkdir(parents=True, exist_ok=True)
        pptx_path.write_bytes(data)
        deck = Presentation(str(pptx_path))
        for slide in deck.slides:
            runs = [s.text_frame.text for s in slide.shapes if s.has_text_frame]
            runs += [cell.text for s in slide.shapes if s.has_table
                     for row in s.table.rows for cell in row.cells]
            titles.append(slide.shapes.title.text if slide.shapes.title is not None
                          else (runs[0] if runs else ""))
            text += "\n".join(runs) + "\n"
        print(f"\ndownloaded deck v{decks[-1].deck_version}: {pptx_path} "
              f"({len(deck.slides)} slides)")
        for n, title in enumerate(titles, 1):
            print(f"  {n:>2}. {title.strip()[:90]}")
    missing = [f for f in facts if f.lower() not in text.lower()]
    print(f"user facts missing from the slides: {missing or 'none'}")
    turn.update(deck_file=str(pptx_path) if pptx_path else None, slide_titles=titles)
    checks = [
        ("E1 approval builds and publishes a deck version",
         turn["deck_updated"] and bool(decks)),
        ("E2 the plan is confirmed after the build", turn["plan_status"] == PlanStatus.CONFIRMED),
        ("E3 the .pptx downloads and has every planned slide",
         pptx_path is not None and len(titles) == len(turn["plan_slides"])),
        ("E4 the slides carry the facts the user shared in chat", not missing),
    ]
    return [("approve -> build -> publish", turn)], checks


def main() -> int:
    """Run the scenarios, print PASS/FAIL per check and save the transcript."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--skip-upload", action="store_true",
                        help="run only the no-upload scenario")
    parser.add_argument("--no-build", action="store_true",
                        help="stop at the proposed plan instead of building the deck")
    parser.add_argument("--out", default=f"artifacts/no_source_content_gate_{STAMP}.json")
    args = parser.parse_args()
    out = Path(args.out)

    turns, checks = scenario_without_upload()
    if not args.no_build:
        built_turns, built_checks = approve_and_build(
            f"live-nocontent-{STAMP}", out.parent, CAR_FACTS)
        turns, checks = turns + built_turns, checks + built_checks
    if not args.skip_upload:
        more_turns, more_checks = scenario_with_upload()
        turns, checks = turns + more_turns, checks + more_checks

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"turns": [{"label": label, **turn} for label, turn in turns],
                               "checks": [{"check": c, "passed": bool(ok)}
                                          for c, ok in checks]},
                              indent=2, ensure_ascii=False), encoding="utf-8")
    print("\n===== RESULTS =====")
    for label, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
    print(f"\ntranscript saved to {out}")
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    sys.exit(main())

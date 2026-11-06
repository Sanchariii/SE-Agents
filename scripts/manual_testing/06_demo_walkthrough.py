"""Narrated end-to-end walkthrough of the deck workflow, for live demos.

    python scripts/manual_testing/06_demo_walkthrough.py
    python scripts/manual_testing/06_demo_walkthrough.py --image diagram.png --extra notes.pdf

Unlike 0*.py this asserts nothing. It drives one realistic session — upload, build,
several edits in one message, a new topic, then a late document — and narrates every
agent event so an audience can watch the system work.
"""

import argparse
import mimetypes
import sys
import time
from pathlib import Path

from _client import Api, Colors, info, sample_docs, step

# Line-buffer stdout: piping the demo to a log otherwise hides every event until exit.
sys.stdout.reconfigure(line_buffering=True)

DIM, INFO, OK, RESET = Colors.DIM, Colors.INFO, Colors.OK, Colors.RESET
DONE_STATES = ("indexed", "extracted", "failed")

# Event data worth showing on screen; everything else is noise for a demo.
SHOWN = ("filename", "status", "chunks", "slide_number", "total", "phase", "op",
         "layout", "coverage", "verdict", "rounds_used", "language", "deck_version")


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Narrated deck-workflow demo.")
    p.add_argument("--docs", nargs="*", default=[], help="Source files (default: samples).")
    p.add_argument("--image", help="An image to upload, so a picture slot can be filled.")
    p.add_argument("--extra", help="Document to add mid-conversation (step 4).")
    p.add_argument("--brief", default="Build a 6-slide training deck for SMT process "
                                      "engineers on line KPIs and reflow/AOI quality. "
                                      "English only.")
    p.add_argument("--topic", default="solder paste inspection",
                   help="Topic for the 'add a slide' turn (step 3.4).")
    return p.parse_args()


def show(event: dict) -> None:
    """Print one SSE event: who acted, what they did, and the fields that matter."""
    kind = event.get("type", "")
    agent = (event.get("agent") or "").upper()
    data = {k: v for k, v in (event.get("data") or {}).items()
            if k in SHOWN and v not in (None, "", [], {})}
    tail = f"   {DIM}{data}{RESET}" if data else ""
    colour = OK if kind in ("deck", "done") else INFO if kind.startswith("agent") else DIM
    print(f"  {colour}{agent:<11}{RESET}{str(event.get('message') or '').strip()}{tail}")


def upload(api: Api, session: str, paths: list[Path]) -> None:
    """Upload with real MIME types, then wait out background ingestion."""
    files = [("files", (p.name, p.read_bytes(),
                        mimetypes.guess_type(p.name)[0] or "text/plain")) for p in paths]
    api.request("POST", f"/decks/{session}/documents", files=files)

    seen: dict[str, str] = {}
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        docs = api.get(f"/decks/{session}/documents").json()["documents"]
        for d in docs:
            if seen.get(d["filename"]) != d["status"]:
                seen[d["filename"]] = d["status"]
                print(f"  {DIM}INGEST     {d['filename']:<30}{RESET} {d['status']}")
        if docs and all(d["status"] in DONE_STATES for d in docs):
            return
        time.sleep(2)
    info("ingestion still running; carrying on anyway")


def turn(api: Api, session: str, heading: str, message: str) -> dict:
    """One user message, streamed to the screen, then a one-line outcome."""
    step(heading)
    print(f"{INFO}user >{RESET} {message}\n")
    started = time.monotonic()
    result = api.result_of(api.invoke(session, message, on_event=show))

    print(f"\n  {OK}result{RESET}  intent={result.get('intent')}  "
          f"v{result.get('deck_version')}  updated={result.get('deck_updated')}  "
          f"slides_built={result.get('slides_built')}  "
          f"{time.monotonic() - started:.0f}s")
    for gap in (result.get("gaps") or [])[:3]:
        print(f"  {DIM}gap     {gap}{RESET}")
    print(f"\n{DIM}assistant >{RESET} {str(result.get('reply') or '')[:700]}")
    return result


def main() -> int:
    args = _args()
    api = Api()
    api.login()
    session = api.create_session("demo walkthrough")
    info(f"session {session}")

    sources = [Path(p) for p in args.docs] or sample_docs()
    if args.image:
        sources.append(Path(args.image))

    step("1  Upload the source documents")
    upload(api, session, sources)

    turn(api, session, "2  Ask for a deck", args.brief)

    # One message, three instructions: triage splits them, the planner acts on each,
    # and the work order gives every slide its own op.
    turn(api, session, "3.1-3.3  Several changes in one message",
         "Put slide 3 on a layout with a picture area, change slide 4 to a "
         "three-column layout, and delete slide 2.")

    turn(api, session, "3.4  Add a new topic",
         f"Add a slide about {args.topic} after slide 3.")

    step("4  Add another document mid-conversation")
    # Without --extra, re-upload the last source: same filename means a new version,
    # which also shows retrieval filtering to the latest one.
    upload(api, session, [Path(args.extra)] if args.extra else sources[-1:])
    turn(api, session, "4  Ask the deck to use it",
         "I've just added another document — update the deck with what it covers.")

    step("Deck versions")
    versions = api.get(f"/decks/{session}/versions").json()["versions"]
    for v in versions:
        print(f"  v{v['deck_version']:<3} {v.get('theme', ''):<14} "
              f"{v.get('plan_title', '')}  ({len(v.get('files') or {})} language(s))")

    if versions:
        latest = versions[-1]["deck_version"]
        data = api.get(f"/decks/{session}/versions/{latest}/download").content
        out = Path(f"demo_deck_v{latest}.pptx")
        out.write_bytes(data)
        print(f"\n  {OK}saved{RESET}   {out}  ({len(data):,} bytes)")

    info(f"session {session} — replay any time with GET /decks/{session}/stream")
    return 0


if __name__ == "__main__":
    sys.exit(main())

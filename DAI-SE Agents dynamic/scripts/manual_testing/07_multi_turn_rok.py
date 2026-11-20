"""Real multi-turn end-to-end run on the ROK AI Gateway guide — no mocks.

Runs the same path as POST /decks/{id}/invoke (deck_handler.run_deck_flow) in-process:
ingest the PDF, build a 5-slide deck, then a follow-up turn that enriches slide 2,
adds image-backed content on the three API patterns and appends a thank-you slide.
After each turn it opens the published .pptx and checks what actually landed.

Run from repo root:
    python -m scripts.manual_testing.07_multi_turn_rok
"""

import io
import sys
import time
from pathlib import Path

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from app.config.constants import StorageConstants
from app.core.handlers import document_handler
from app.core.handlers.deck_handler import run_deck_flow
from app.core.handlers.session_store import session_store
from app.genai.ingestion.pipeline import process_document
from app.utilities import events_util as events
from app.utilities import storage_util as storage

DOC = Path("artifacts/ROK AI Gateway Engineering Guide 7 1.pdf")
SESSION_ID = f"e2e-rok-{int(time.time()) % 1_000_000}"
OUT_DIR = Path("artifacts/e2e_runs")

TURN_1 = """Turn the provided technical documentation into a professional PowerPoint deck for both technical and non-technical users.

Simplify the content while preserving accuracy and essential details. Use a clear learning sequence, minimal text, one main idea per slide, and visual explanations such as diagrams, workflows, comparisons, and practical examples.

For each slide, include a title, key content, takeaway, visual or layout recommendation, and speaker notes when useful.

Use plain language, define technical terms, avoid unnecessary jargon, and place supporting details in the notes or appendix. Ensure the final deck is accurate, accessible, consistent, and presentation-ready.
for english , in 5 slide"""

TURN_2 = ("enrich the slide 2 and add thank you slide at last , also add more content "
          "on three ways to interact  with image")


def _print_event(event: dict) -> None:
    """Echo every workflow event live, as the UI's activity feed would show it."""
    print(f"  [{event.get('agent')}] {str(event.get('message'))[:200]}", flush=True)


def ingest() -> None:
    """Upload and fully process the source PDF, exactly as the upload route does."""
    data = DOC.read_bytes()
    mime = document_handler.validate_upload(DOC.name, data)
    record = document_handler.register_upload(SESSION_ID, DOC.name, mime, data)
    record = process_document(record)
    print(f"ingested {DOC.name}: status={record.status.value} error={record.error!r}")
    if record.error:
        sys.exit("ingestion failed")


def plan_outline() -> list[dict]:
    """The stored plan's slides, printed and returned."""
    plan, status = session_store.get_plan(SESSION_ID)
    slides = (plan or {}).get("slides", [])
    print(f"\nPLAN ({status}, {len(slides)} slides)")
    for s in slides:
        print(f"  {s['slide_number']}. [{s['kind']}] {s['title']} "
              f"| visual={s.get('visual')!r} layout_hint={s.get('layout_hint')!r}")
        for kp in s.get("key_points", []):
            print(f"       - {kp}")
    return slides


def deck_slides(turn: int) -> list[dict]:
    """Download the latest published deck, save a copy, and dump each slide's content."""
    decks = session_store.list_decks(SESSION_ID)
    if not decks:
        print("NO DECK PUBLISHED")
        return []
    artifact = decks[-1]
    data = storage.read_file(artifact.storage_path, container=StorageConstants.CONTAINER_DECKS)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    local = OUT_DIR / f"{SESSION_ID}_turn{turn}_v{artifact.deck_version}.pptx"
    local.write_bytes(data)
    print(f"\nDECK v{artifact.deck_version} -> {local}")

    slides = []
    for number, slide in enumerate(Presentation(io.BytesIO(data)).slides, start=1):
        texts = [sh.text_frame.text.strip() for sh in slide.shapes
                 if sh.has_text_frame and sh.text_frame.text.strip()]
        texts += [" | ".join(c.text for c in row.cells)
                  for sh in slide.shapes if sh.has_table for row in sh.table.rows]
        pictures = sum(1 for sh in slide.shapes if sh.shape_type == MSO_SHAPE_TYPE.PICTURE)
        words = sum(len(t.split()) for t in texts)
        slides.append({"number": number, "text": "\n".join(texts),
                       "words": words, "pictures": pictures})
        print(f"  slide {number}: {words} words, {pictures} picture(s), "
              f"{len(slide.shapes)} shape(s)")
        for t in texts[:8]:
            print(f"      | {t[:160].replace(chr(10), ' / ')}")
    return slides


def run_turn(turn: int, message: str) -> dict:
    """Run one chat turn through the production handler and print the outcome."""
    print(f"\n{'=' * 78}\nTURN {turn}: {message[:90]}...\n{'=' * 78}")
    started = time.monotonic()
    result = run_deck_flow(SESSION_ID, message)
    print(f"\nturn {turn} took {time.monotonic() - started:.0f}s | "
          f"actions={result.get('actions_taken')} operations={result.get('operations')} "
          f"deck_updated={result.get('deck_updated')} v{result.get('deck_version')}")
    for gap in result.get("gaps") or []:
        print(f"  gap: {gap}")
    return result


def check(label: str, passed: bool) -> bool:
    print(f"{'PASS' if passed else 'FAIL'}  {label}")
    return passed


def main() -> int:
    print(f"session {SESSION_ID}")
    # Wrap dispatch so every event prints live, like the SSE feed.
    original_dispatch = events.dispatch
    events.dispatch = lambda sid, ev: (_print_event(ev), original_dispatch(sid, ev))[1]

    ingest()

    # ---- turn 1: build the deck ----
    run_turn(1, TURN_1)
    plan_1 = plan_outline()
    deck_1 = deck_slides(1)

    # ---- turn 2: enrich 2, enrich the "three ways" slide with an image, add thanks ----
    result_2 = run_turn(2, TURN_2)
    plan_2 = plan_outline()
    deck_2 = deck_slides(2)

    print(f"\n{'=' * 78}\nCHECKS\n{'=' * 78}")
    three_ways = next((s for s in plan_1 if "three" in s["title"].lower()
                       or "interact" in s["title"].lower()), None)
    ops = result_2.get("operations") or {}
    results = [
        check("turn 1 produced 5 slides", len(deck_1) == 5),
        check("turn 2 published a new version", bool(result_2.get("deck_updated"))),
        check("turn 2 plan has one more slide than turn 1", len(plan_2) == len(plan_1) + 1),
        check("last slide is a thank-you slide",
              bool(plan_2) and ("thank" in plan_2[-1]["title"].lower()
                                or plan_2[-1]["kind"] == "thanks")),
        check("slide 2 was rebuilt (not kept)", ops.get("2") not in (None, "keep")),
        check("slide 2 text changed",
              len(deck_1) > 1 and len(deck_2) > 1 and deck_1[1]["text"] != deck_2[1]["text"]),
    ]
    if three_ways:
        n = three_ways["slide_number"]
        results.append(check(f"'three ways' slide {n} was rebuilt",
                             ops.get(str(n)) not in (None, "keep")))
        if len(deck_2) >= n and len(deck_1) >= n:
            results.append(check(f"'three ways' slide {n} content changed "
                                 f"({deck_1[n-1]['words']} -> {deck_2[n-1]['words']} words)",
                                 deck_2[n - 1]["text"] != deck_1[n - 1]["text"]))
            results.append(check(f"'three ways' slide {n} carries a picture",
                                 deck_2[n - 1]["pictures"] > 0))
    else:
        results.append(check("found the 'three ways' slide in turn 1", False))

    failed = results.count(False)
    print(f"\n{'ALL PASSED' if not failed else f'{failed} FAILED'}  (session {SESSION_ID})")
    return 1 if failed else 0


TURN_3 = "add slide on quiz and remove slide 3"


def add_and_remove(session_id: str) -> int:
    """Run one add+remove turn on an existing session and check both landed.

    Args:
        session_id: A session that already has a published deck of 4+ slides.

    Returns:
        Process exit code: 0 when every check passes.
    """
    global SESSION_ID
    SESSION_ID = session_id
    original_dispatch = events.dispatch
    events.dispatch = lambda sid, ev: (_print_event(ev), original_dispatch(sid, ev))[1]

    before = plan_outline()
    deck_before = deck_slides(0)
    removed_title = before[2]["title"]
    result = run_turn(3, TURN_3)
    after = plan_outline()
    deck_after = deck_slides(3)

    titles = [s["title"] for s in after]
    new = [s for s in after if s["title"] not in {b["title"] for b in before}]
    ops = result.get("operations") or {}
    print(f"\n{'=' * 78}\nCHECKS\n{'=' * 78}")
    results = [
        check("a new version was published", bool(result.get("deck_updated"))),
        check(f"removed slide 3 ('{removed_title}') is gone", removed_title not in titles),
        check("exactly one new slide was added", len(new) == 1),
        check("the new slide is about a quiz",
              bool(new) and any(word in " ".join([new[0]["title"], new[0].get("visual", ""),
                                                  *new[0]["key_points"]]).lower()
                                for word in ("quiz", "knowledge check", "question"))),
        check("slide count unchanged (one removed, one added)", len(after) == len(before)),
        check("new slide sits before the closing slides",
              bool(new) and all(s["kind"] in ("takeaways", "thanks")
                                for s in after[new[0]["slide_number"]:])),
        check("new slide was created, not rewritten from the removed one",
              bool(new) and ops.get(str(new[0]["slide_number"])) == "create"),
        check("published deck matches the plan length", len(deck_after) == len(after)),
        check("closing slides still last", after[-1]["kind"] == before[-1]["kind"]),
    ]
    unchanged = [b["title"] for b in before if b["title"] != removed_title]
    results.append(check("every other slide survived", all(t in titles for t in unchanged)))
    print(f"deck slides before={len(deck_before)} after={len(deck_after)}")

    failed = results.count(False)
    print(f"\n{'ALL PASSED' if not failed else f'{failed} FAILED'}  (session {SESSION_ID})")
    return 1 if failed else 0


if __name__ == "__main__":
    # `--session <id>` runs only the add+remove follow-up turn on an existing session.
    if len(sys.argv) == 3 and sys.argv[1] == "--session":
        sys.exit(add_and_remove(sys.argv[2]))
    sys.exit(main())

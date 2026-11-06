"""Real end-to-end bilingual run (English + French) on the ROK AI Gateway guide — no mocks.

Runs the same path as POST /decks/{id}/invoke (deck_handler.run_deck_flow) in-process,
asks for the deck "only in english and french", then opens every published language
file and checks it is written in ONE language: the English deck must carry no French,
and the French deck no leftover English. Every text-bearing XML part is scanned
(slides, notes, charts, SmartArt), not just what python-pptx exposes as runs.

Run from repo root:
    python -m scripts.manual_testing.08_bilingual_rok
"""

import re
import sys
import time
import zipfile
from pathlib import Path

from app.core.handlers import document_handler
from app.core.handlers.deck_handler import run_deck_flow
from app.core.handlers.session_store import session_store
from app.genai.ingestion.pipeline import process_document
from app.utilities import events_util as events
from app.utilities import storage_util as storage

DOC = Path("artifacts/ROK AI Gateway Engineering Guide 7 1.pdf")
SESSION_ID = f"e2e-bilingual-{int(time.time()) % 1_000_000}"
OUT_DIR = Path("artifacts/e2e_runs")

PROMPT = """Turn the provided technical documentation into a professional PowerPoint deck for both technical and non-technical users.

Simplify the content while preserving accuracy and essential details. Use a clear learning sequence, minimal text, one main idea per slide, and visual explanations such as diagrams, workflows, comparisons, and practical examples.

For each slide, include a title, key content, takeaway, visual or layout recommendation, and speaker notes when useful.

Use plain language, define technical terms, avoid unnecessary jargon, and place supporting details in the notes or appendix. Ensure the final deck is accurate, accessible, consistent, and presentation-ready.
create only in english and french"""

# Function words that are unambiguous for one language; content words are not used
# because product names and acronyms legitimately stay untranslated.
_EN = frozenset("the and of to with for is are this that from by your you how what "
                "when which into each use using will can should".split())
_FR = frozenset("le la les des du et est sont une un pour avec dans sur par vous votre "
                "qui que ce cette ces aux au lorsque chaque utiliser peut doit".split())
_TEXT_PARTS = re.compile(r"ppt/(slides|notesSlides|charts|diagrams)/[^/]+\.xml$")
_TEXT_NODE = re.compile(r"<(?:a:t|c:v)>([^<]+)</(?:a:t|c:v)>")
_WORD = re.compile(r"[A-Za-zÀ-ÿ']+")

saved: dict[str, Path] = {}


def _capture_uploads() -> None:
    """Keep a local copy of every deck file the publish node uploads."""
    original = storage.save_bytes

    def save_and_copy(path: str, data: bytes, *args, **kwargs):
        if "/decks/" in path and path.endswith(".pptx"):
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            local = OUT_DIR / f"{SESSION_ID}_{Path(path).name}"
            local.write_bytes(data)
            saved[path] = local
        return original(path, data, *args, **kwargs)

    storage.save_bytes = save_and_copy


def _strings(deck: Path) -> list[tuple[str, str]]:
    """Every visible/notes string in the deck as (part name, text)."""
    found = []
    with zipfile.ZipFile(deck) as package:
        for name in package.namelist():
            if _TEXT_PARTS.search(name):
                xml = package.read(name).decode("utf-8", errors="ignore")
                found += [(name, t.strip()) for t in _TEXT_NODE.findall(xml) if t.strip()]
    return found


def _language_of(text: str) -> str | None:
    """'en', 'fr' or None (too short / ambiguous) from function-word counts."""
    words = [w.lower() for w in _WORD.findall(text)]
    en, fr = sum(w in _EN for w in words), sum(w in _FR for w in words)
    if en + fr < 2 or en == fr:
        return None
    return "en" if en > fr else "fr"


def audit(deck: Path, expected: str) -> list[tuple[str, str]]:
    """Strings in `deck` detected as a language other than `expected`."""
    strings = _strings(deck)
    wrong = [(part, text) for part, text in strings
             if (lang := _language_of(text)) and lang != expected]
    print(f"\n{deck.name}: {len(strings)} strings, {len(wrong)} not in '{expected}'")
    for part, text in wrong:
        print(f"   [{part}] {text[:150]}")
    return wrong


def main() -> int:
    print(f"session {SESSION_ID}")
    original_dispatch = events.dispatch
    events.dispatch = lambda sid, ev: (
        print(f"  [{ev.get('agent')}] {str(ev.get('message'))[:200]}", flush=True),
        original_dispatch(sid, ev))[1]
    _capture_uploads()

    data = DOC.read_bytes()
    record = document_handler.register_upload(
        SESSION_ID, DOC.name, document_handler.validate_upload(DOC.name, data), data)
    record = process_document(record)
    print(f"ingested {DOC.name}: status={record.status.value} error={record.error!r}")
    if record.error:
        sys.exit("ingestion failed")

    started = time.monotonic()
    result = run_deck_flow(SESSION_ID, PROMPT)
    print(f"\nturn took {time.monotonic() - started:.0f}s | "
          f"deck_updated={result.get('deck_updated')} v{result.get('deck_version')}")
    plan, _ = session_store.get_plan(SESSION_ID)
    print(f"plan languages: {(plan or {}).get('languages')}")

    english = [p for p in saved.values() if p.stem.endswith("_english")]
    french = [p for p in saved.values() if p.stem.endswith("_french")]
    others = [p for p in saved.values() if p not in english + french]
    checks = [
        ("plan languages are exactly English + French",
         (plan or {}).get("languages") == ["English", "French"]),
        ("one English deck published", len(english) == 1),
        ("one French deck published", len(french) == 1),
        ("no other language published", not others),
    ]
    if english:
        checks.append(("English deck has no French text", not audit(english[0], "en")))
    if french:
        checks.append(("French deck has no English text", not audit(french[0], "fr")))

    print(f"\n{'=' * 78}")
    for label, passed in checks:
        print(f"{'PASS' if passed else 'FAIL'}  {label}")
    for path in saved.values():
        print(f"saved {path}")
    return 0 if all(passed for _, passed in checks) else 1


if __name__ == "__main__":
    sys.exit(main())

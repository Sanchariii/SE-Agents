"""Render a slide to a PNG with headless LibreOffice, the way max mode does.

Builds one DeckBuilder slide on a regular content layout (and one on the High
Creativity master's title layout, when the template has a usable one), renders both,
and reports the LibreOffice binary, the PNG size, cold and warm timings, concurrent
renders on separate profiles, and missing fonts. `--review` also asks the real judge.

Run from the repository root:
    python scripts/manual_testing/13_render_slide.py
    python scripts/manual_testing/13_render_slide.py --review --out ./render_out
"""

import argparse
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.genai.ppt import slide_render
from app.genai.ppt.deck_builder import DeckBuilder
from app.genai.ppt.template import catalog as catalog_mod, registry


def _parse_args() -> argparse.Namespace:
    """Read the output folder and the review switch."""
    parser = argparse.ArgumentParser(description="Render slides with LibreOffice.")
    parser.add_argument("--out", type=Path, default=None,
                        help="Keep the files here (default: a temp folder).")
    parser.add_argument("--review", action="store_true",
                        help="Also send the first render to the visual judge.")
    return parser.parse_args()


def _build(catalog, layout_key: str, out: Path, title: str) -> Path:
    """One single-slide deck on `layout_key`."""
    deck = DeckBuilder(registry.template_path(),
                       catalog.to_dict(include_high_creativity=True), frame=None)
    slide = deck.add_slide(layout_key)
    try:
        slide.set_title(title)
    except Exception:                                         # noqa: BLE001
        pass
    return Path(deck.save(out / f"{layout_key}.pptx"))


def main() -> int:
    """Run the checks; exit 0 when every render succeeded."""
    args = _parse_args()
    out = args.out or Path(tempfile.mkdtemp(prefix="render_slide_"))
    out.mkdir(parents=True, exist_ok=True)
    binary = slide_render.find_soffice()
    print(f"LibreOffice: {binary or 'NOT FOUND'}")
    if binary is None:
        return 1
    catalog = catalog_mod.load()
    keys = [catalog.content_layouts[0].key]
    title = catalog.high_creativity_title()
    if title is not None:
        keys.append(title.key)
    else:
        print("High Creativity master unusable:",
              "; ".join(catalog.high_creativity_problems()))
    files = [_build(catalog, key, out, f"Render check on {key}") for key in keys]

    pngs = []
    for label in ("cold", "warm"):
        started = time.monotonic()
        png = slide_render.render_png(files[0], out / "review")
        pngs.append(png)
        print(f"{label} render of {files[0].name}: {time.monotonic() - started:.1f}s "
              f"-> {png}")
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=len(files) + 1) as pool:
        rendered = list(pool.map(lambda f: slide_render.render_png(f, out / "review"),
                                 files + files[:1]))
    print(f"{len(rendered)} concurrent render(s): {time.monotonic() - started:.1f}s")
    print("missing fonts:", slide_render.missing_fonts() or "none (or fc-match absent)")

    if args.review:
        from app.core.models.deck_model import DeckContext, SlideCodeBrief, SlideDraft
        from app.genai.agents import slide_reviewer

        brief = SlideCodeBrief(slide_number=1, content=SlideDraft(slide_number=1,
                                                                  title="Render check"),
                               context=DeckContext(title="Render check"))
        verdict = slide_reviewer.review(pngs[0].read_bytes(), brief)
        print("verdict:", verdict.model_dump_json(indent=2),
              "passed:", slide_reviewer.passed(verdict))
    print(f"files kept in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

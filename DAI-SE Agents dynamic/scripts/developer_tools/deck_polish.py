"""Polish a deck from a terminal - any .pptx, from either engine.

    python -m scripts.developer_tools.deck_polish "classic.pptx" --level low
    python -m scripts.developer_tools.deck_polish "classic.pptx" --level medium --out polished.pptx
    python -m scripts.developer_tools.deck_polish deck.pptx --level high --save-ops ops.json
    python -m scripts.developer_tools.deck_polish deck.pptx --level medium --ops ops.json

LOW needs no model. MEDIUM and HIGH call the model through the ROK AI Gateway with the
credentials in app/config/dev.env, unless --ops applies operations saved earlier.
Prints what was found, what changed, and the scorecard before and after.

In the app itself, set DECK_POLISH=on in app/config/dev.env instead.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

LEVELS = {"low": "LOW", "medium": "MED", "high": "HIGH"}


class SavedOps:
    """Operations read from a file instead of the model."""

    def __init__(self, path: Path):
        self.operations = json.loads(path.read_text(encoding="utf-8"))

    def write_ops(self, system: str, user: str) -> list[dict]:
        return self.operations


class RecordingWriter:
    """The model, with its answer saved to a file."""

    def __init__(self, path: Path):
        from app.genai.deck_polish.llm import GatewayOpsWriter
        self.inner, self.path = GatewayOpsWriter(), path

    def write_ops(self, system: str, user: str) -> list[dict]:
        operations = self.inner.write_ops(system, user)
        self.path.write_text(json.dumps(operations, indent=1), encoding="utf-8")
        print(f"Saved the model's {len(operations)} operation(s) to {self.path}")
        return operations


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.developer_tools.deck_polish")
    parser.add_argument("deck", type=Path, help="The .pptx to polish")
    parser.add_argument("--level", choices=sorted(LEVELS), default="low")
    parser.add_argument("--out", type=Path, help="Where to save (default: <deck>_polished_<level>.pptx)")
    parser.add_argument("--ops", type=Path, help="Apply operations from this JSON file (no model call)")
    parser.add_argument("--save-ops", type=Path, help="Save the model's operations to this JSON file")
    parser.add_argument("--style", type=Path, help="A style skill (.md) to pass to the model")
    args = parser.parse_args(argv)
    if not args.deck.is_file():
        parser.error(f"not found: {args.deck}")

    from app.genai.deck_polish.engine import polish_deck
    from app.genai.skill_deck import scorecard

    level = LEVELS[args.level]
    out = args.out or args.deck.with_name(f"{args.deck.stem}_polished_{args.level}.pptx")
    writer = None
    if args.ops:
        writer = SavedOps(args.ops)
    elif args.save_ops:
        writer = RecordingWriter(args.save_ops)
    style = args.style.read_text(encoding="utf-8") if args.style else ""

    with tempfile.TemporaryDirectory() as work:
        final, notes = polish_deck(args.deck, Path(work), level, style_guidance=style,
                                   writer=writer, on_step=lambda m: print("  " + m))
        if Path(final) == args.deck:
            print("\nNothing to change; the deck was not copied.")
        else:
            out.write_bytes(Path(final).read_bytes())
            print(f"\nSaved {out}")
    for note in notes[1:]:
        print(f"  needs a person: {note}")
    if Path(final) != args.deck:
        print()
        print(scorecard.format_table([("before", scorecard.score_deck(args.deck)),
                                      ("after", scorecard.score_deck(out))]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

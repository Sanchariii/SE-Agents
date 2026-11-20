"""Score decks side by side: layout, brand, readability, content and style (0-100).

    python -m scripts.developer_tools.deck_scorecard classic.pptx skill_deck.pptx
    python -m scripts.developer_tools.deck_scorecard deck.pptx --reference look.pptx
    python -m scripts.developer_tools.deck_scorecard a.pptx b.pptx --json scores.json

Works on decks from either engine, so the same document can be built both ways and
compared. --reference (.pptx, .pdf, .png, .jpg) adds the style area; it is inspected by
the same readers the dynamic skills API uses.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    from app.genai.skill_deck import scorecard, style_match
    parser = argparse.ArgumentParser(prog="python -m scripts.developer_tools.deck_scorecard")
    parser.add_argument("decks", nargs="+", type=Path, help="One or more .pptx files")
    parser.add_argument("--reference", type=Path,
                        help="Reference whose style the decks should match")
    parser.add_argument("--json", type=Path, help="Also save the scorecards as JSON")
    args = parser.parse_args(argv)

    missing = [str(d) for d in args.decks if not d.is_file()]
    if missing:
        parser.error(f"not found: {', '.join(missing)}")
    target = {}
    if args.reference:
        target = style_match.style_target([style_match.inspect_reference(args.reference)])
        print(style_match.describe(target) or "The reference has no slide geometry; "
              "the style area is skipped.")
        print()
    named = [(deck.name, scorecard.score_deck(deck, style_target=target))
             for deck in args.decks]
    print(scorecard.format_table(named))
    if args.json:
        args.json.write_text(json.dumps(dict(named), indent=2), encoding="utf-8")
        print(f"\nSaved {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Regenerate app/genai/ppt/measure.py's CHAR_EM table from real Barlow metrics.

Developer tool, run by hand, never imported by app/. It needs `fonttools` and a copy of
Barlow-Regular.ttf, neither of which is a project dependency — install them in a scratch
venv, run this, and paste the printed table into measure.py.

    pip install fonttools
    python scripts/calibrate_barlow.py path/to/Barlow-Regular.ttf

The printed widths are rounded UP to two decimals, which is what keeps the estimate
conservative; do not round to nearest.
"""

import math
import sys

PRINTABLE = (
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    " .,;:'\"`!?()[]{}/\\-_+=<>@#$%&*|"
)


def main(font_path: str) -> int:
    from fontTools.ttLib import TTFont            # noqa: PLC0415  (dev-only import)

    font = TTFont(font_path)
    upem = font["head"].unitsPerEm
    cmap = font.getBestCmap()
    hmtx = font["hmtx"]
    widths: dict[str, float] = {}
    for char in PRINTABLE:
        glyph = cmap.get(ord(char))
        if glyph is None:
            continue
        advance = hmtx[glyph][0] / upem
        widths[char] = math.ceil(advance * 100) / 100      # round UP, always
    print("CHAR_EM = {")
    for char, width in sorted(widths.items(), key=lambda kv: kv[1]):
        print(f"    {char!r}: {width:.2f},")
    print("}")
    print(f"# unitsPerEm={upem}, {len(widths)} glyphs measured")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))

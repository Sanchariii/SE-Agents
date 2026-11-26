"""Import the icons of one "icon set" slide into the icon library.

Each icon picture on the slide is named after the text label printed under it
("PERSON + ROBOT" -> person-robot), rendered from its SVG to a sharp PNG (the PNG
fallback stored in the deck is only ~100 px) and added to
``company-ppt-templet/icons/index.json`` with keywords from ``SYNONYMS``. Existing
entries keep their own keywords, so hand edits to the index survive a re-import.

Usage (from the backend root; needs ``pip install cairosvg``, falls back to the deck's
PNG without it):

    python scripts/developer_tools/import_icons.py \
        "company-ppt-templet/RA-Brand-Template-2026/RA-Brand-Template-2026/2026-RA-InternalTemplate.potx" 88
"""

import argparse
import io
import json
import re
from pathlib import Path

EMU = 914400
OUTPUT_PX = 512
R_EMBED = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"

SYNONYMS = {
    "person": ["people", "employee", "worker", "operator", "user", "technician", "staff"],
    "person-alt": ["people", "employee", "worker", "operator", "user", "staff"],
    "group": ["team", "people", "employees", "workers", "crew", "audience",
              "stakeholders"],
    "person-robot": ["robot", "robotics", "automation", "cobot", "collaboration",
                     "operator"],
    "clock": ["time", "schedule", "duration", "hours", "deadline", "timing", "minutes"],
    "calendar": ["date", "schedule", "plan", "deadline", "month", "week", "timeline"],
    "bar-chart": ["chart", "growth", "metrics", "kpi", "performance", "results",
                  "analytics"],
    "arrow": ["growth", "trend", "increase", "improvement", "progress", "next"],
    "machine": ["equipment", "machinery", "press", "motor", "tank", "mixer", "asset"],
    "line": ["production", "assembly", "conveyor", "manufacturing", "process",
             "workflow"],
    "plant": ["factory", "site", "facility", "manufacturing", "building"],
    "enterprise": ["company", "corporate", "organization", "headquarters", "business",
                   "office"],
    "data": ["signal", "information", "analytics", "stream", "digital"],
    "cloud": ["saas", "online", "internet", "hosting", "storage"],
    "hardware": ["controller", "plc", "rack", "device", "module", "electrical"],
    "software": ["application", "computer", "screen", "monitor", "dashboard", "hmi"],
}


class IconSlideImporter:
    """Reads one slide of icons and writes them into a library folder."""

    def __init__(self, deck_path, slide_number, out_dir):
        self.deck_path, self.slide_number = deck_path, slide_number
        self.out_dir = Path(out_dir)

    def run(self):
        slide = self._slide()
        labels = [s for s in slide.shapes if s.has_text_frame and s.text_frame.text.strip()
                  and not s.is_placeholder]
        index_path = self.out_dir / "index.json"
        index = json.loads(index_path.read_text()) if index_path.is_file() else []
        known = {entry["name"]: entry for entry in index}
        imported = []
        for shape in slide.shapes:
            if shape.shape_type != 13:
                continue
            name = self._unique(self._label_for(shape, labels), known, imported)
            if not name:
                continue
            (self.out_dir / f"{name}.png").write_bytes(self._png(slide, shape))
            entry = known.get(name) or {"name": name, "file": f"{name}.png",
                                        "keywords": SYNONYMS.get(name, []),
                                        "category": "brand-2026"}
            known[name] = entry
            imported.append(name)
        index_path.write_text(json.dumps(sorted(known.values(), key=lambda e: e["name"]),
                                         indent=1) + "\n")
        print(f"imported {len(imported)} icon(s): {', '.join(imported)}")

    def _slide(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from app.genai.ppt.template import loader

        return loader.open_presentation(str(self.deck_path)).slides[self.slide_number - 1]

    @staticmethod
    def _label_for(icon, labels):
        """The label printed just under the icon, as a slug ("" when none)."""
        centre = icon.left + icon.width / 2
        below = [l for l in labels if 0 <= l.top - icon.top <= 1.5 * EMU
                 and abs(l.left + l.width / 2 - centre) <= 0.7 * EMU]
        if not below:
            return ""
        label = min(below, key=lambda l: abs(l.left + l.width / 2 - centre))
        return re.sub(r"[^a-z0-9]+", "-", label.text_frame.text.lower()).strip("-")

    @staticmethod
    def _unique(name, known, imported):
        """A second "person" becomes "person-alt" (then "-3", "-4"...)."""
        if not name or name not in imported:
            return name
        candidate, n = f"{name}-alt", 3
        while candidate in imported:
            candidate, n = f"{name}-{n}", n + 1
        return candidate

    @staticmethod
    def _png(slide, shape):
        svg = next((e for e in shape._element.iter() if e.tag.endswith("svgBlip")), None)
        if svg is not None:
            try:
                import cairosvg

                blob = slide.part.related_part(svg.get(R_EMBED)).blob
                return cairosvg.svg2png(bytestring=blob, output_width=OUTPUT_PX)
            except ImportError:
                print("cairosvg not installed: using the deck's small PNG fallback")
        return shape.image.blob


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("deck")
    parser.add_argument("slide", type=int, help="1-based slide number")
    parser.add_argument("--out", default="company-ppt-templet/icons")
    args = parser.parse_args()
    IconSlideImporter(args.deck, args.slide, args.out).run()


if __name__ == "__main__":
    main()

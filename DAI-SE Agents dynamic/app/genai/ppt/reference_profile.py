"""The structure of a reference deck - never its words.

``ReferenceProfiler.profile(path)`` reads the reference once, before its slides are
stripped, and returns a small JSON-able profile: for every slide its layout, its role
in the deck (cover, agenda, section, content, quiz, takeaways, closing), its visual
pattern (columns, image side, cards, table, chart, screenshot, diagram, text) and its
text density; plus deck-level layout usage and cadence. Titles are read only to tell
roles apart and are never stored, so the profile can go into prompts without any of
the reference's content travelling with it.
"""

import re
from collections import Counter

from app.utilities.logger_util import get_logger

log = get_logger(__name__)

_ROLE_WORDS = (                      # checked in order against layout name, then title
    ("cover", r"\bcover\b|title slide"),
    ("agenda", r"agenda|contents|\bindex\b"),
    ("closing", r"thank|closing|\bq ?& ?a\b|questions\?"),
    ("quiz", r"quiz|knowledge check|assessment|\btest your"),
    ("takeaways", r"takeaway|summary|recap|key points"),
    ("section", r"section|chapter|divider"),
)
_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
_PATTERN_WORDS = (
    ("screenshot", r"screenshot|laptop|device"),
    ("grid", r"quadrant|\bgrid\b|2x2"),
    ("cards", r"boxes|cards|dashboard|tiles"),
    ("quote", r"quote"),
)
_TABLE, _CHART, _PICTURE, _GROUP, _PLACEHOLDER = 19, 3, 13, 6, 14     # MSO_SHAPE_TYPE
_EMU = 914400


class ReferenceProfiler:
    """Builds the structure profile of one reference deck."""

    def profile(self, path):
        """The profile of the deck at ``path`` (.pptx or .potx)."""
        from app.genai.ppt.template import loader

        prs = loader.open_presentation(str(path))
        width, height = prs.slide_width / _EMU, prs.slide_height / _EMU
        keys = {}
        for mi, master in enumerate(prs.slide_masters):
            for li, layout in enumerate(master.slide_layouts):
                keys[id(layout.element)] = f"M{mi}-L{li}"
        slides = []
        total = len(prs.slides)
        for number, slide in enumerate(prs.slides, start=1):
            layout = slide.slide_layout
            slides.append({
                "slide": number,
                "layout": keys.get(id(layout.element), ""),
                "role": self._role(layout.name, self._title(slide), number, total),
                "pattern": self._pattern(slide, layout.name, width, height),
                **self._density(slide),
            })
        profile = {"slide_count": total, "slides": slides,
                   "sequence": [s["role"] for s in slides],
                   "layout_usage": dict(Counter(s["layout"] for s in slides if s["layout"])),
                   "cadence": self._cadence(slides)}
        log.info("reference profiled: %d slide(s), roles %s", total,
                 dict(Counter(profile["sequence"])))
        return profile

    @staticmethod
    def _title(slide):
        title = slide.shapes.title
        return title.text_frame.text if title is not None and title.has_text_frame else ""

    @staticmethod
    def _role(layout_name, title, number, total):
        for text in (layout_name, title):
            for role, pattern in _ROLE_WORDS:
                if re.search(pattern, text or "", re.I):
                    return role
        if number == 1:
            return "cover"
        if number == total and total > 2:
            return "closing"
        return "content"

    def _pattern(self, slide, layout_name, width, height):
        shapes = list(self._walk(slide.shapes))
        kinds = Counter(getattr(s, "shape_type", None) for s in shapes)
        if kinds[_TABLE] or any(getattr(s, "has_table", False) for s in shapes):
            return "table"
        if kinds[_CHART] or any(getattr(s, "has_chart", False) for s in shapes):
            return "chart"
        for pattern, words in _PATTERN_WORDS:
            if re.search(words, layout_name, re.I):
                return pattern
        columns = self._columns(layout_name)
        if columns:
            return f"columns-{columns}"
        side = self._image_side(shapes, width, height)
        if side:
            return side
        if kinds[_GROUP] + sum(1 for s in shapes if s.shape_type == 1) >= 4:
            return "diagram"
        return "text"

    @staticmethod
    def _columns(layout_name):
        match = re.search(r"\b(\d|one|two|three|four|five|six)\b[\w\s]{0,20}?colum",
                          layout_name, re.I)
        if not match:
            return 0
        value = match.group(1).lower()
        count = int(value) if value.isdigit() else _NUMBERS[value]
        return count if count >= 2 else 0

    @staticmethod
    def _image_side(shapes, width, height):
        pictures = [s for s in shapes if s.shape_type == _PICTURE
                    or (s.shape_type == _PLACEHOLDER and "PICTURE" in
                        str(getattr(s.placeholder_format, "type", "")))]
        pictures = [s for s in pictures if s.width and s.height]
        if not pictures:
            return ""
        biggest = max(pictures, key=lambda s: s.width * s.height)
        area = biggest.width * biggest.height / (_EMU * _EMU)
        if area < 0.12 * width * height:
            return ""
        if area >= 0.6 * width * height:
            return "full-image"
        centre = (biggest.left + biggest.width / 2) / _EMU
        return "image-left" if centre < width * 0.45 else (
            "image-right" if centre > width * 0.55 else "image-centre")

    def _density(self, slide):
        """Paragraph and character counts outside the title, plus a bucket."""
        title = slide.shapes.title
        bullets = chars = 0
        for shape in self._walk(slide.shapes):
            if title is not None and shape.shape_id == title.shape_id:
                continue
            if getattr(shape, "has_text_frame", False):
                for paragraph in shape.text_frame.paragraphs:
                    text = "".join(run.text for run in paragraph.runs).strip()
                    if text:
                        bullets += 1
                        chars += len(text)
        bucket = "light" if chars < 150 else "medium" if chars < 500 else "heavy"
        return {"paragraphs": bullets, "chars": chars, "density": bucket}

    @classmethod
    def _walk(cls, shapes):
        for shape in shapes:
            yield shape
            if getattr(shape, "shape_type", None) == _GROUP:
                yield from cls._walk(shape.shapes)

    @staticmethod
    def _cadence(slides):
        """How many content slides typically sit between quizzes and between sections."""
        result = {}
        for role in ("quiz", "section"):
            gaps, run = [], 0
            for slide in slides:
                if slide["role"] == role:
                    if run:
                        gaps.append(run)
                    run = 0
                elif slide["role"] == "content":
                    run += 1
            result[f"{role}_every"] = round(sum(gaps) / len(gaps)) if gaps else 0
        return result

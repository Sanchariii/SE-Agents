"""The reference's section-divider artwork (its big "01", "02"...), reused on our deck.

A reference's section slides carry artwork that is drawn on the slide, not in the
layout - the D2R ILT deck draws each section number as a large translucent freeform.
``SectionArt.harvest`` keeps that artwork from the reference's section slides, in
order, before the slides are stripped; only shapes with no text and no picture or
link are kept, so none of the reference's words or images can travel. After the deck
is stitched, ``SectionArt.stamp`` puts artwork k behind the title of our k-th section
slide.
"""

import copy
import json
from pathlib import Path

from lxml import etree

from app.utilities.logger_util import get_logger

log = get_logger(__name__)

_EMU = 914400
_P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_MIN_AREA_SHARE = 0.02            # smaller shapes are details, not divider artwork


class SectionArt:
    """Harvests and stamps section-divider artwork."""

    @staticmethod
    def sidecar(template_path):
        path = Path(str(template_path))
        return path.with_name(path.stem + ".section_art.json")

    def harvest(self, path, profile):
        """One list of shape XML strings per section slide of the deck at ``path``."""
        from app.genai.ppt.template import loader

        roles = [s.get("role") for s in (profile or {}).get("slides") or []]
        prs = loader.open_presentation(str(path))
        area = prs.slide_width * prs.slide_height
        art = []
        for slide, role in zip(prs.slides, roles):
            if role != "section":
                continue
            kept = [etree.tostring(shape._element).decode() for shape in slide.shapes
                    if self._is_artwork(shape, area)]
            art.append(kept)
        log.info("section artwork harvested from %d section slide(s): %s shape(s)",
                 len(art), [len(a) for a in art])
        return art

    @staticmethod
    def _is_artwork(shape, area):
        """Decoration only: not a placeholder, no text, nothing linked, big enough."""
        if getattr(shape, "is_placeholder", False) or shape.width is None:
            return False
        xml = etree.tostring(shape._element).decode()
        if any(f" r:{name}=" in xml for name in ("embed", "id", "link", "pict")):
            return False
        if "<a:t>" in xml or "<a:t " in xml:
            return False
        return shape.width * shape.height >= _MIN_AREA_SHARE * area

    def stamp(self, deck_path, section_titles, template_path):
        """Put the reference's k-th section artwork on our k-th section slide, found by
        its title. Returns how many slides were stamped; the deck is saved in place."""
        sidecar = self.sidecar(template_path) if template_path else None
        if not section_titles or sidecar is None or not sidecar.is_file():
            return 0
        art = json.loads(sidecar.read_text())
        if not art:
            return 0
        from pptx import Presentation

        prs = Presentation(str(deck_path))
        wanted = [t.strip() for t in section_titles]
        stamped = 0
        for slide in prs.slides:
            title = slide.shapes.title
            text = title.text_frame.text.strip() if title is not None else ""
            if text not in wanted:
                continue
            k = wanted.index(text)
            wanted[k] = None                         # each title is used once
            if k < len(art) and art[k]:
                self._insert(slide, art[k])
                stamped += 1
        if stamped:
            prs.save(str(deck_path))
        log.info("section artwork stamped on %d of %d section slide(s)", stamped,
                 len(section_titles))
        return stamped

    @staticmethod
    def _insert(slide, shapes_xml):
        """Insert behind everything else on the slide, with fresh shape ids."""
        tree = slide.shapes._spTree
        next_id = max([int(e.get("id")) for e in tree.iter(f"{{{_P_NS}}}cNvPr")
                       if str(e.get("id", "")).isdigit()] + [1]) + 1
        anchor = 2                                   # after nvGrpSpPr and grpSpPr
        for xml in shapes_xml:
            element = copy.deepcopy(etree.fromstring(xml))
            for c_nv_pr in element.iter(f"{{{_P_NS}}}cNvPr"):
                c_nv_pr.set("id", str(next_id))
                next_id += 1
            tree.insert(anchor, element)
            anchor += 1

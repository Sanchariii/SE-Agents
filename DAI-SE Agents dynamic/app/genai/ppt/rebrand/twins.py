"""D2R twins: a hidden copy of each reference layout that shows the D2R mark.

The rebrand takes the D2R mark off a reference's layouts, because most slides are not
about D2R. Before it does, ``LayoutTwin.make`` copies each such layout as it was, as
"<name> (D2R)" on the same master. The catalog never offers a twin to the designers
(``is_twin``); a slide about D2R is moved onto its layout's twin after it is built
(``program_mark.ProgramMark``), so it looks exactly like the reference did.
"""

import copy

from lxml import etree

from app.config.constants import ReferenceDesignConstants as C
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_FIRST_ID = 2147483648               # layout and master ids start here (ECMA-376)


class LayoutTwin:
    """Makes, recognises and finds D2R twins of layouts."""

    @staticmethod
    def name_of(name):
        return f"{(name or '').strip()} {C.PROGRAM_LAYOUT_SUFFIX}"

    @staticmethod
    def is_twin(layout_or_name):
        name = getattr(layout_or_name, "name", layout_or_name) or ""
        return name.strip().endswith(C.PROGRAM_LAYOUT_SUFFIX)

    @classmethod
    def find(cls, layout):
        """The twin of ``layout`` on its master, or None."""
        wanted = cls.name_of(layout.name)
        for candidate in layout.slide_master.slide_layouts:
            if (candidate.name or "").strip() == wanted:
                return candidate
        return None

    @classmethod
    def make(cls, prs, layout):
        """Copy ``layout`` (shapes, pictures, background) onto its master as its twin.

        Returns:
            The new layout's name.
        """
        from pptx.opc.constants import RELATIONSHIP_TYPE as RT
        from pptx.parts.slide import SlideLayoutPart

        source = layout.part
        package = source.package
        partname = package.next_partname("/ppt/slideLayouts/slideLayout%d.xml")
        element = copy.deepcopy(source._element)
        part = SlideLayoutPart(partname, source.content_type, package, element)
        rel_map = {}
        for r_id, rel in source.rels.items():
            if rel.is_external:
                rel_map[r_id] = part.relate_to(rel.target_ref, rel.reltype, True)
            else:
                rel_map[r_id] = part.relate_to(rel.target_part, rel.reltype)
        cls._remap(element, rel_map)
        name = cls.name_of(layout.name)
        element.cSld.set("name", name)

        master = layout.slide_master
        r_id = master.part.relate_to(part, RT.SLIDE_LAYOUT)
        id_list = master._element.find(f"{{{_P_NS}}}sldLayoutIdLst")
        entry = etree.SubElement(id_list, f"{{{_P_NS}}}sldLayoutId")
        entry.set("id", str(cls._next_id(prs)))
        entry.set(f"{{{_R_NS}}}id", r_id)
        log.debug("layout %r twinned as %r", layout.name, name)
        return name

    @staticmethod
    def _remap(element, rel_map):
        """Point the copy's r:embed / r:id / r:link at the twin's own relationships."""
        for node in element.iter():
            for key, value in list(node.attrib.items()):
                if key.startswith(f"{{{_R_NS}}}") and value in rel_map:
                    node.set(key, rel_map[value])

    @staticmethod
    def _next_id(prs):
        """A layout id not used by any master or layout in the deck."""
        used = [_FIRST_ID]
        for master in prs.slide_masters:
            for entry in master._element.iter(f"{{{_P_NS}}}sldLayoutId"):
                used.append(int(entry.get("id", 0)))
        for entry in prs.part._element.iter(f"{{{_P_NS}}}sldMasterId"):
            used.append(int(entry.get("id", 0)))
        return max(used) + 1

    @classmethod
    def use_on(cls, slide):
        """Move ``slide`` onto its layout's twin. True when it was moved."""
        from pptx.opc.constants import RELATIONSHIP_TYPE as RT

        twin = cls.find(slide.slide_layout)
        if twin is None:
            return False
        for rel in slide.part.rels.values():
            if rel.reltype == RT.SLIDE_LAYOUT:
                rel._target = twin.part
                return True
        return False

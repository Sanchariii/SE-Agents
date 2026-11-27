"""Put Rockwell's logo on a reference deck's design before anything is built on it.

Reference design mode copies an uploaded deck's layouts, artwork, colours and fonts.
Its logos are artwork too, so ``Rebrander.run`` (once, on the prepared copy) leaves
every layout showing Rockwell's logo and no other company's:

1. Logo-sized pictures on masters and layouts (groups included) that look like a known
   Rockwell mark are kept; Rockwell's classification symbols are left alone
   (``LogoFinder``, ``MarkLibrary``).
2. Another company's logo - unrecognised and in a corner, on a master or repeated - is
   swapped in place for the Rockwell mark that fits its box (``MarkPlacer.replace``).
3. A layout still showing no logo gets one in the first free spot (``MarkPlacer.add``).

The frame then adds no mark of its own on a reference deck
(``design_contract.frame_for_layout``), so a slide never shows two logos.
"""

from app.config.constants import ReferenceDesignConstants as C
from app.genai.ppt.rebrand.finder import LogoFinder
from app.genai.ppt.rebrand.geometry import EMU_PER_IN, Geometry
from app.genai.ppt.rebrand.marks import MarkLibrary
from app.genai.ppt.rebrand.placer import MarkPlacer
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

__all__ = ["Geometry", "LogoFinder", "MarkLibrary", "MarkPlacer", "RebrandReport",
           "Rebrander"]


class RebrandReport:
    """What the rebrand changed, for the log and the user's progress note."""

    def __init__(self, kept=0, replaced=0, added=0, no_room=None, removed=0):
        self.kept = kept                    # Rockwell marks already in the design
        self.replaced = replaced            # other logos swapped for a Rockwell mark
        self.added = added                  # layouts that had no logo and got one
        self.no_room = list(no_room or [])  # layouts left without a mark
        self.removed = removed              # programme marks dropped beside a Rockwell one

    def to_dict(self):
        return {"kept": self.kept, "replaced": self.replaced, "added": self.added,
                "no_room": list(self.no_room), "removed": self.removed}

    @classmethod
    def from_dict(cls, data):
        data = data or {}
        return cls(int(data.get("kept", 0)), int(data.get("replaced", 0)),
                   int(data.get("added", 0)), data.get("no_room", ()),
                   int(data.get("removed", 0)))

    def note(self):
        """One line for the user, or "" when nothing about the logos changed."""
        parts = []
        if self.replaced:
            parts.append(f"replaced {self.replaced} non-Rockwell logo(s) with the "
                         "Rockwell logo")
        if self.added:
            parts.append(f"added the Rockwell logo to {self.added} layout(s) that had "
                         "none")
        if self.no_room:
            parts.append(f"{len(self.no_room)} layout(s) had no free spot for it")
        if not parts:
            return ""
        text = "; ".join(parts)
        return text[0].upper() + text[1:] + "."


class Rebrander:
    """Leaves every layout of a prepared reference showing Rockwell's logo.

    Args:
        marks: Rockwell fingerprints (tests); ``MarkLibrary.known_marks()`` otherwise.
        icons: Classification-symbol fingerprints (tests); ``policy_icons()`` otherwise.
    """

    def __init__(self, marks=None, icons=None, program=None):
        self.marks, self.icons, self.program = marks, icons, program

    def run(self, prs, luminance_of=None, shade_of=None):
        """Rebrand ``prs`` in place and return a ``RebrandReport``.

        Args:
            prs: The prepared reference (python-pptx Presentation).
            luminance_of: ``(master_index, layout_index or None) -> 0..1`` background
                luminance, for the white lockup on dark backgrounds; light when None.
            shade_of: ``(master_index, shape) -> luminance or None``, so an added mark
                over a dark band or photo is the white lockup; light when None.
        """
        slide_w, slide_h = prs.slide_width / EMU_PER_IN, prs.slide_height / EMU_PER_IN
        report, placer = RebrandReport(), MarkPlacer(slide_w, slide_h)
        found = LogoFinder(self.marks, self.icons, self.program).find(prs, slide_w,
                                                                      slide_h)
        owners = {}
        for candidate in found:
            owners.setdefault(candidate.digest, set()).add(id(candidate.owner))
        uses = {digest: len(ids) for digest, ids in owners.items()}
        masters = {id(m) for m in prs.slide_masters}
        index_of = {}
        for mi, master in enumerate(prs.slide_masters):
            index_of[id(master)] = (mi, None)
            for li, layout in enumerate(master.slide_layouts):
                index_of[id(layout)] = (mi, li)

        def dark(owner):
            return luminance_of is not None and luminance_of(*index_of[id(owner)]) < 0.5

        icon_rows = LogoFinder.in_icon_rows(found)
        with_rockwell = {id(c.owner) for c in found if c.rockwell}
        has_mark = set(with_rockwell)
        for candidate in found:
            if id(candidate) in icon_rows and not candidate.rockwell:
                continue
            if candidate.program and id(candidate.owner) in with_rockwell:
                # A Rockwell mark is already there (the RA lockup under a D2R ribbon).
                candidate.shape._element.getparent().remove(candidate.shape._element)
                report.removed += 1
                continue
            if candidate.rockwell:
                report.kept += 1
                has_mark.add(id(candidate.owner))
                continue
            if not (candidate.program
                    or LogoFinder.is_foreign(candidate, uses, masters, slide_w, slide_h)):
                continue
            _, _, width, height = candidate.box
            mark = MarkLibrary.mark_file(width / height >= C.LOCKUP_MIN_ASPECT,
                                         dark(candidate.owner))
            where = index_of[id(candidate.owner)]
            if mark is None:
                log.warning("no Rockwell mark on disk to replace %r on %s with; it is "
                            "removed", candidate.shape.name, where)
                candidate.shape._element.getparent().remove(candidate.shape._element)
                continue
            placer.replace(candidate, mark)
            report.replaced += 1
            has_mark.add(id(candidate.owner))
            log.info("logo %r on %s replaced with %s", candidate.shape.name, where,
                     mark.name)

        for mi, master in enumerate(prs.slide_masters):
            for li, layout in enumerate(master.slide_layouts):
                if id(layout) in has_mark or (id(master) in has_mark
                                              and Geometry.shows_master_shapes(layout)):
                    continue
                shade = (lambda shape, mi=mi: shade_of(mi, shape)) if shade_of else None
                placed = placer.add(layout, dark(layout), shade)
                if placed:
                    report.added += 1
                    log.debug("layout M%d-L%d %r had no logo; %s added", mi, li,
                              layout.name, placed)
                else:
                    report.no_room.append(f"M{mi}-L{li}")
                    log.warning("layout M%d-L%d %r has no logo and no free spot for one",
                                mi, li, layout.name)
        log.info("reference rebrand: %d Rockwell mark(s) kept, %d other logo(s) replaced, "
                 "%d added, %d layout(s) without room", report.kept, report.replaced,
                 report.added, len(report.no_room))
        return report

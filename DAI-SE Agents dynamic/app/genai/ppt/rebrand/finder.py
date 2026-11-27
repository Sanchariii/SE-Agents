"""Finding the logos in a reference deck and telling Rockwell's from others."""

from app.config.constants import ReferenceDesignConstants as C
from app.genai.ppt.rebrand.geometry import Geometry
from app.genai.ppt.rebrand.marks import MarkLibrary
from app.utilities.logger_util import get_logger

log = get_logger(__name__)


class LogoCandidate:
    """A logo-sized picture on a master or layout."""

    def __init__(self, owner, shape, box, rockwell, digest, program=False):
        self.owner = owner              # the master or layout it is drawn on
        self.shape = shape
        self.box = box                  # on the slide, inches
        self.rockwell = rockwell        # a Rockwell mark, kept
        self.program = program          # a programme mark (D2R), removed or swapped
        self.digest = digest


class LogoFinder:
    """Every logo-sized picture on the masters and layouts, with its verdict.

    Args:
        marks: Rockwell fingerprints; ``MarkLibrary.known_marks()`` by default.
        icons: Classification-symbol fingerprints; ``MarkLibrary.policy_icons()``.
    """

    def __init__(self, marks=None, icons=None, program=None):
        self.marks = MarkLibrary.known_marks() if marks is None else marks
        self.icons = MarkLibrary.policy_icons() if icons is None else icons
        self.program = MarkLibrary.program_marks() if program is None else program

    def find(self, prs, slide_w, slide_h):
        """The candidates, groups included. Classification symbols are left out."""
        found, verdicts = [], {}
        for master in prs.slide_masters:
            for owner in (master, *master.slide_layouts):
                for shape, box in Geometry.pictures(owner.shapes):
                    if not Geometry.is_logo_sized(box, slide_w, slide_h):
                        continue
                    try:
                        digest = shape.image.sha1
                        if digest not in verdicts:
                            verdicts[digest] = self._verdict(shape.image.blob)
                    except Exception as exc:                 # noqa: BLE001
                        log.debug("picture %r cannot be read (%s); not a logo "
                                  "candidate", shape.name, exc)
                        continue
                    if verdicts[digest] is None:
                        log.debug("picture %r is a classification symbol; left as it "
                                  "is", shape.name)
                        continue
                    verdict = verdicts[digest]
                    found.append(LogoCandidate(owner, shape, box, verdict is True, digest,
                                               program=verdict == "program"))
        return found

    def _verdict(self, blob):
        """None for a classification symbol, "program" for a programme mark, else
        whether it is a Rockwell mark."""
        probe = MarkLibrary.fingerprint(blob)
        if MarkLibrary.matches(probe, self.icons):
            return None
        if MarkLibrary.matches(probe, self.program):
            return "program"
        return MarkLibrary.matches(probe, self.marks)

    @staticmethod
    def in_icon_rows(found):
        """ids of candidates sitting in a row of 3+ same-size pictures on one master or
        layout - social-media or feature icons, which a logo never is."""
        rows = set()
        for candidate in found:
            _, top, width, height = candidate.box
            row = [other for other in found if other.owner is candidate.owner
                   and abs(other.box[1] - top) <= 0.1 and abs(other.box[2] - width) <= 0.05
                   and abs(other.box[3] - height) <= 0.05]
            if len(row) >= 3:
                rows.add(id(candidate))
        return rows

    @staticmethod
    def is_foreign(candidate, uses, masters, slide_w, slide_h):
        """Another company's logo: unrecognised and furniture - in a corner, on a
        master, or repeated across layouts (``uses`` counts distinct masters/layouts,
        so a row of icons on one slide is not "repeated"). A one-off picture mid-slide
        is the design's artwork and is left alone."""
        if candidate.rockwell or candidate.program:
            return False
        return (Geometry.in_corner(candidate.box, slide_w, slide_h)
                or id(candidate.owner) in masters
                or uses[candidate.digest] >= C.LOGO_MIN_REPEATS)

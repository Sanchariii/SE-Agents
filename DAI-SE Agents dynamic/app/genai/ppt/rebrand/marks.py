"""Rockwell's own marks: recognising them in a reference, and the files to place."""

import io

from app.config.constants import PptConstants, ReferenceDesignConstants as C
from app.genai.ppt.rebrand.geometry import EMU_PER_IN, Geometry
from app.utilities.logger_util import get_logger

log = get_logger(__name__)


class MarkLibrary:
    """Fingerprints of Rockwell's logos and classification symbols, built once."""

    _known = None
    _icons = None
    _aspects = {}

    @staticmethod
    def fingerprint(blob):
        """A perceptual hash of an image and its aspect ratio, or None if unreadable.

        The image is trimmed to its visible pixels and laid on mid-grey, so a white mark
        reads like a coloured one, then reduced to a difference hash: small rendering
        differences move a few bits, a different logo about half of them.
        """
        from PIL import Image

        size = C.FINGERPRINT_SIZE
        try:
            image = Image.open(io.BytesIO(blob)).convert("RGBA")
        except Exception as exc:                             # noqa: BLE001
            log.debug("image cannot be read for fingerprinting (%s)", exc)
            return None
        visible = image.getchannel("A").point(lambda a: 255 if a > 16 else 0).getbbox()
        if visible:
            image = image.crop(visible)
        if not image.width or not image.height:
            return None
        canvas = Image.new("RGBA", image.size, (128, 128, 128, 255))
        canvas.alpha_composite(image)
        pixels = canvas.convert("L").resize((size + 1, size), Image.LANCZOS).load()
        bits = 0
        for y in range(size):
            for x in range(size):
                bits = (bits << 1) | (pixels[x, y] > pixels[x + 1, y])
        return bits, image.width / image.height

    @staticmethod
    def matches(probe, marks):
        """True when ``probe`` is close to any of ``marks``."""
        if probe is None:
            return False
        bits, aspect = probe
        for known_bits, known_aspect in marks:
            if max(aspect, known_aspect) / min(aspect, known_aspect) > C.MAX_ASPECT_DRIFT:
                continue
            if bin(bits ^ known_bits).count("1") <= C.MATCH_MAX_BITS:
                return True
        return False

    @classmethod
    def known_marks(cls):
        """Rockwell marks: the logo files (``logos/recognised`` too) and the logo-sized
        pictures in the brand template itself."""
        if cls._known is None:
            from app.genai.ppt.template import registry

            root = registry.template_root() / PptConstants.LOGO_DIR
            found = [cls.fingerprint(p.read_bytes()) for p in sorted(root.rglob("*.png"))]
            found += cls._template_marks()
            cls._known = tuple(mark for mark in found if mark)
            log.info("%d Rockwell mark fingerprint(s) loaded", len(cls._known))
        return cls._known

    @classmethod
    def _template_marks(cls):
        from app.genai.ppt.template import loader, registry

        found, seen = [], set()
        try:
            prs = loader.open_presentation(str(registry.brand_template_path()))
            width, height = prs.slide_width / EMU_PER_IN, prs.slide_height / EMU_PER_IN
            for master in prs.slide_masters:
                for owner in (master, *master.slide_layouts):
                    for shape, box in Geometry.pictures(owner.shapes):
                        if not Geometry.is_logo_sized(box, width, height):
                            continue
                        try:
                            digest, blob = shape.image.sha1, shape.image.blob
                        except ValueError:          # a linked, not embedded, picture
                            continue
                        if digest not in seen:
                            seen.add(digest)
                            found.append(cls.fingerprint(blob))
        except Exception as exc:                             # noqa: BLE001
            log.warning("the brand template's own logos could not be read (%s); only "
                        "the logo files are recognised", exc, exc_info=True)
        return found

    @classmethod
    def policy_icons(cls):
        """Rockwell's classification symbols: left alone, never counted as a logo."""
        if cls._icons is None:
            from app.genai.ppt.template import registry

            folder = registry.template_root() / PptConstants.CLASSIFICATION_ICON_DIR
            found = [cls.fingerprint(p.read_bytes()) for p in sorted(folder.glob("*.png"))]
            cls._icons = tuple(mark for mark in found if mark)
            log.info("%d classification symbol fingerprint(s) loaded", len(cls._icons))
        return cls._icons

    @classmethod
    def is_rockwell(cls, blob, marks=None):
        """True when the image looks like one of Rockwell's own marks."""
        return cls.matches(cls.fingerprint(blob),
                           cls.known_marks() if marks is None else marks)

    @staticmethod
    def mark_file(lockup, white):
        """The file to place: the RA hexagon, or the colour / white lockup."""
        from app.genai.ppt.template import registry

        if not lockup:
            return registry.logo_path(C.HEXAGON)
        return registry.logo_path(C.LOCKUP_WHITE if white else C.LOCKUP_COLOR)

    @classmethod
    def aspect(cls, path):
        """width / height of a mark file."""
        if path not in cls._aspects:
            from PIL import Image

            with Image.open(path) as image:
                cls._aspects[path] = image.width / image.height
        return cls._aspects[path]

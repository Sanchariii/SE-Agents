"""Put Rockwell's logo on a reference deck's design before anything is built on it.

Reference design mode copies an uploaded deck's layouts, artwork, colours and fonts.
Its logos are artwork too, so without this step a deck built on another company's
template would carry that company's logo, and a template with no logo would carry
none (the stamped frame skips its own mark on any layout that draws artwork, so it
cannot be relied on to add one).

``rebrand`` runs once on the prepared copy (slides already stripped) and leaves every
layout showing Rockwell's logo and no other company's:

1. Every small picture on a master or layout, grouped ones included, is a logo
   candidate. One that looks like a known Rockwell mark (perceptual match against
   ``company-ppt-templet/logos`` and the brand template's own logos) is kept where it
   is. Rockwell's data-classification symbols are recognised too and left alone.
2. A candidate that does not match is treated as another company's logo when it is
   furniture - in a corner, on a master, or repeated across layouts - and is swapped
   in place for the Rockwell mark that fits its box: the full lockup in a wide box,
   the RA hexagon in a compact one, the white lockup on a dark background.
3. A layout that still shows no logo - neither its own nor its master's - gets one in
   the first free spot: the RA hexagon in a top corner, else the lockup in the footer
   band beside the slide number. Over a dark background, band or photo the white
   lockup is used, because the red hexagon disappears on red.

The frame then adds no mark of its own on a reference deck (see
``design_contract.frame_for_layout``), so a slide never shows two logos.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from app.config.constants import PptConstants, ReferenceDesignConstants as C
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

EMU_PER_IN = 914400
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R_EMBED = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
_PICTURE = 13                                 # MSO_SHAPE_TYPE.PICTURE
_GROUP = 6                                    # MSO_SHAPE_TYPE.GROUP
_FURNITURE = {"DATE", "FOOTER", "SLIDE_NUMBER", "HEADER"}
_NON_TEXT_PLACEHOLDERS = {"PICTURE", "BITMAP", "MEDIA_CLIP", "CHART", "TABLE",
                          "ORG_CHART", "OBJECT"}


@dataclass
class RebrandReport:
    """What ``rebrand`` changed, for the log and the user's progress note."""
    kept: int = 0                       # Rockwell marks already in the design
    replaced: int = 0                   # other logos swapped for a Rockwell mark
    added: int = 0                      # layouts that had no logo and got one
    no_room: list[str] = field(default_factory=list)   # layouts left without a mark

    def to_dict(self) -> dict:
        return {"kept": self.kept, "replaced": self.replaced, "added": self.added,
                "no_room": list(self.no_room)}

    @classmethod
    def from_dict(cls, data: dict | None) -> "RebrandReport":
        data = data or {}
        return cls(kept=int(data.get("kept", 0)), replaced=int(data.get("replaced", 0)),
                   added=int(data.get("added", 0)),
                   no_room=list(data.get("no_room", ())))

    def note(self) -> str:
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


# --------------------------------------------------------------------------------- #
# recognising a mark                                                                 #
# --------------------------------------------------------------------------------- #

def fingerprint(blob: bytes) -> tuple[int, float] | None:
    """A perceptual hash of a logo image and its aspect ratio.

    The image is trimmed to its visible pixels and laid on mid-grey, so a white mark
    made for dark backgrounds is as recognisable as a coloured one, then reduced to a
    difference hash. Small rendering differences (size, anti-aliasing, a gradient)
    move a few bits; a different logo moves about half of them.

    Args:
        blob: The image bytes (PNG, JPEG, ...).

    Returns:
        (hash bits, width / height), or None when the image cannot be read.
    """
    from PIL import Image

    size = C.FINGERPRINT_SIZE
    try:
        image = Image.open(io.BytesIO(blob)).convert("RGBA")
    except Exception as exc:                                 # noqa: BLE001
        log.debug("image cannot be read for fingerprinting (%s)", exc)
        return None
    visible = image.getchannel("A").point(lambda a: 255 if a > 16 else 0).getbbox()
    if visible:
        image = image.crop(visible)
    if not image.width or not image.height:
        return None
    canvas = Image.new("RGBA", image.size, (128, 128, 128, 255))
    canvas.alpha_composite(image)
    grey = canvas.convert("L").resize((size + 1, size), Image.LANCZOS)
    pixels = grey.load()
    bits = 0
    for y in range(size):
        for x in range(size):
            bits = (bits << 1) | (pixels[x, y] > pixels[x + 1, y])
    return bits, image.width / image.height


def _matches(probe: tuple[int, float] | None, marks) -> bool:
    if probe is None:
        return False
    bits, aspect = probe
    for known_bits, known_aspect in marks:
        if max(aspect, known_aspect) / min(aspect, known_aspect) > C.MAX_ASPECT_DRIFT:
            continue
        if bin(bits ^ known_bits).count("1") <= C.MATCH_MAX_BITS:
            return True
    return False


@lru_cache
def known_marks() -> tuple[tuple[int, float], ...]:
    """Fingerprints of every Rockwell mark: the approved logo files (recursively, so
    ``logos/recognised`` can hold marks that are only ever recognised, never placed)
    and the logo-sized pictures in the brand template itself."""
    from app.genai.ppt.template import loader, registry

    found: list[tuple[int, float]] = []
    for path in sorted((registry.template_root() / PptConstants.LOGO_DIR).rglob("*.png")):
        mark = fingerprint(path.read_bytes())
        if mark:
            found.append(mark)
    try:
        prs = loader.open_presentation(str(registry.brand_template_path()))
        width, height = prs.slide_width / EMU_PER_IN, prs.slide_height / EMU_PER_IN
        seen: set[str] = set()
        for master in prs.slide_masters:
            for owner in (master, *master.slide_layouts):
                for shape, box in _pictures(owner.shapes):
                    if not _is_logo_sized(box, width, height):
                        continue
                    try:
                        digest, blob = shape.image.sha1, shape.image.blob
                    except ValueError:              # a linked, not embedded, picture
                        continue
                    if digest in seen:
                        continue
                    seen.add(digest)
                    mark = fingerprint(blob)
                    if mark:
                        found.append(mark)
    except Exception as exc:                                 # noqa: BLE001
        log.warning("the brand template's own logos could not be read (%s); only the "
                    "logo files are recognised as Rockwell marks", exc, exc_info=True)
    log.info("%d Rockwell mark fingerprint(s) loaded", len(found))
    return tuple(found)


@lru_cache
def policy_icons() -> tuple[tuple[int, float], ...]:
    """Fingerprints of Rockwell's data-classification symbols. A reference that already
    draws one is showing its classification, not a logo: it is left exactly as it is
    and does not count as the layout's logo."""
    from app.genai.ppt.template import registry

    folder = registry.template_root() / PptConstants.CLASSIFICATION_ICON_DIR
    found = tuple(mark for mark in (fingerprint(path.read_bytes())
                                    for path in sorted(folder.glob("*.png"))) if mark)
    log.info("%d classification symbol fingerprint(s) loaded", len(found))
    return found


def is_rockwell_mark(blob: bytes, marks=None) -> bool:
    """True when the image looks like one of Rockwell's own marks.

    Args:
        blob: The image bytes.
        marks: Fingerprints to compare against; ``known_marks()`` by default.
    """
    return _matches(fingerprint(blob), known_marks() if marks is None else marks)


# --------------------------------------------------------------------------------- #
# geometry, groups included                                                          #
# --------------------------------------------------------------------------------- #

#: Maps a shape's own EMU coordinates to the slide's: slide = scale * own + offset,
#: per axis, as (scale_x, offset_x, scale_y, offset_y). Identity at the top level.
_IDENTITY = (1.0, 0.0, 1.0, 0.0)


def _group_transform(group, outer) -> tuple[float, float, float, float]:
    """The transform of a group's children, composed with the group's own."""
    xfrm = group._element.grpSpPr.find(f"{{{_A}}}xfrm")
    if xfrm is None:
        return outer
    off, ext = xfrm.find(f"{{{_A}}}off"), xfrm.find(f"{{{_A}}}ext")
    ch_off, ch_ext = xfrm.find(f"{{{_A}}}chOff"), xfrm.find(f"{{{_A}}}chExt")
    if None in (off, ext, ch_off, ch_ext):
        return outer
    sx = int(ext.get("cx")) / int(ch_ext.get("cx")) if int(ch_ext.get("cx")) else 1.0
    sy = int(ext.get("cy")) / int(ch_ext.get("cy")) if int(ch_ext.get("cy")) else 1.0
    tx = int(off.get("x")) - int(ch_off.get("x")) * sx
    ty = int(off.get("y")) - int(ch_off.get("y")) * sy
    osx, otx, osy, oty = outer
    return osx * sx, osx * tx + otx, osy * sy, osy * ty + oty


def _box(shape, transform=_IDENTITY) -> tuple[float, float, float, float] | None:
    """A shape's [left, top, width, height] on the slide, in inches."""
    if None in (shape.left, shape.top, shape.width, shape.height):
        return None
    sx, tx, sy, ty = transform
    return ((shape.left * sx + tx) / EMU_PER_IN, (shape.top * sy + ty) / EMU_PER_IN,
            shape.width * sx / EMU_PER_IN, shape.height * sy / EMU_PER_IN)


def _pictures(shapes, transform=_IDENTITY):
    """Every picture in a shape tree, with its slide box and its transform."""
    for shape in shapes:
        if shape.shape_type == _GROUP:
            yield from _pictures(shape.shapes, _group_transform(shape, transform))
        elif shape.shape_type == _PICTURE and not getattr(shape, "is_placeholder", False):
            box = _box(shape, transform)
            if box is not None:
                yield shape, box


def _transform_of(shapes, target, transform=_IDENTITY):
    for shape in shapes:
        if shape is target or shape._element is target._element:
            return transform
        if shape.shape_type == _GROUP:
            found = _transform_of(shape.shapes, target, _group_transform(shape, transform))
            if found is not None:
                return found
    return None


def _is_logo_sized(box, slide_w: float, slide_h: float) -> bool:
    """A picture small enough to be a logo rather than a photo or a background."""
    _, _, width, height = box
    return (C.LOGO_MIN_SIDE_IN <= min(width, height)
            and max(width, height) <= C.LOGO_MAX_SIDE_IN
            and width * height <= C.LOGO_MAX_AREA_SHARE * slide_w * slide_h)


def _in_corner(box, slide_w: float, slide_h: float) -> bool:
    left, top, width, height = box
    cx, cy = left + width / 2, top + height / 2
    return (min(cx, slide_w - cx) <= C.LOGO_CORNER_X_IN
            and min(cy, slide_h - cy) <= C.LOGO_CORNER_Y_IN)


def _overlaps(a, b, tolerance: float = 0.02) -> bool:
    return (min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]) > tolerance
            and min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]) > tolerance)


def _shows_master_shapes(layout) -> bool:
    return layout.element.get("showMasterSp") not in ("0", "false")


# --------------------------------------------------------------------------------- #
# finding logos                                                                      #
# --------------------------------------------------------------------------------- #

@dataclass
class _Candidate:
    owner: object              # the master or layout the picture is drawn on
    shape: object
    box: tuple                 # on the slide, inches
    rockwell: bool
    digest: str


def _candidates(prs, slide_w: float, slide_h: float, marks,
                icons=None) -> list[_Candidate]:
    """Every logo-sized picture on the masters and layouts, with its verdict.
    Classification symbols are left out: they are not logos."""
    marks = known_marks() if marks is None else marks
    icons = policy_icons() if icons is None else icons
    found: list[_Candidate] = []
    verdicts: dict[str, bool | None] = {}
    for master in prs.slide_masters:
        for owner in (master, *master.slide_layouts):
            for shape, box in _pictures(owner.shapes):
                if not _is_logo_sized(box, slide_w, slide_h):
                    continue
                try:
                    image = shape.image
                    digest = image.sha1
                    if digest not in verdicts:
                        probe = fingerprint(image.blob)
                        verdicts[digest] = (None if _matches(probe, icons)
                                            else _matches(probe, marks))
                except Exception as exc:                     # noqa: BLE001
                    log.debug("picture %r cannot be read (%s); not a logo candidate",
                              shape.name, exc)
                    continue
                if verdicts[digest] is None:
                    log.debug("picture %r is a classification symbol; left as it is",
                              shape.name)
                    continue
                found.append(_Candidate(owner, shape, box, verdicts[digest], digest))
    return found


def _is_foreign_logo(candidate: _Candidate, uses: dict[str, int], masters: set,
                     slide_w: float, slide_h: float) -> bool:
    """An unrecognised picture counts as another company's logo when it is furniture:
    in a corner, on a master, or repeated across layouts (``uses`` counts the distinct
    masters/layouts drawing it, so a row of icons on one slide is not "repeated"). A
    one-off picture mid-slide is the design's artwork and is left alone."""
    if candidate.rockwell:
        return False
    return (_in_corner(candidate.box, slide_w, slide_h)
            or id(candidate.owner) in masters
            or uses[candidate.digest] >= C.LOGO_MIN_REPEATS)


# --------------------------------------------------------------------------------- #
# placing Rockwell's mark                                                            #
# --------------------------------------------------------------------------------- #

def _mark_file(lockup: bool, white: bool) -> Path | None:
    from app.genai.ppt.template import registry

    if not lockup:
        return registry.logo_path(C.HEXAGON)
    return registry.logo_path(C.LOCKUP_WHITE if white else C.LOCKUP_COLOR)


@lru_cache
def _image_aspect(path: Path) -> float:
    from PIL import Image

    with Image.open(path) as image:
        return image.width / image.height


def _fit(box, aspect: float, slide_w: float) -> tuple[float, float, float, float]:
    """The mark's rectangle inside a logo's box: as large as fits, aspect kept, held
    against the slide edge the box is nearer to and centred vertically."""
    left, top, width, height = box
    mark_w = min(width, height * aspect)
    mark_h = mark_w / aspect
    near_right = (left + width / 2) > slide_w / 2
    x = left + width - mark_w if near_right else left
    y = top + (height - mark_h) / 2
    return x, y, mark_w, mark_h


def _strip_svg(blip) -> None:
    """Drop an `asvg:svgBlip` extension, which PowerPoint would draw instead of the new
    raster, along with any crop of the old picture."""
    ext_list = blip.find(f"{{{_A}}}extLst")
    if ext_list is not None:
        blip.remove(ext_list)
    crop = blip.getparent().find(f"{{{_A}}}srcRect")
    if crop is not None:
        blip.getparent().remove(crop)


def _replace(candidate: _Candidate, mark: Path, slide_w: float) -> None:
    """Swap the picture's image for ``mark`` and fit its frame to the mark's shape.
    Positions are written back in the picture's own (group) coordinates."""
    shape = candidate.shape
    _, rel_id = candidate.owner.part.get_or_add_image_part(str(mark))
    blip = shape._element.blipFill.find(f"{{{_A}}}blip")
    blip.set(_R_EMBED, rel_id)
    _strip_svg(blip)
    sx, tx, sy, ty = _transform_of(candidate.owner.shapes, shape) or _IDENTITY
    x, y, w, h = _fit(candidate.box, _image_aspect(mark), slide_w)
    shape.left = int((x * EMU_PER_IN - tx) / sx)
    shape.top = int((y * EMU_PER_IN - ty) / sy)
    shape.width, shape.height = int(w * EMU_PER_IN / sx), int(h * EMU_PER_IN / sy)
    shape.name = C.MARK_SHAPE_NAME


def _holds_text(shape) -> bool:
    """A box that will hold words: a text placeholder, or a shape with text in it."""
    if getattr(shape, "is_placeholder", False):
        kind = shape.placeholder_format.type
        return kind is None or kind.name not in _NON_TEXT_PLACEHOLDERS
    return bool(getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip())


def _surroundings(layout, slide_w: float, slide_h: float,
                  shade_of=None) -> tuple[list[tuple], list[tuple]]:
    """What a new mark must avoid on this layout, and what it may sit on.

    Args:
        layout: The python-pptx slide layout.
        slide_w: Slide width, inches.
        slide_h: Slide height, inches.
        shade_of: ``shape -> luminance or None``; backdrops count as light without it.

    Returns:
        (obstacles, backdrops). Obstacles are the text boxes and smaller artwork of the
        layout and, when it shows them, its master. Backdrops are the large picture or
        text-free areas a logo may be placed over (a photo half, a colour band), each
        as (box, luminance or None); a full-slide background is neither, it is judged
        by the layout's luminance.
    """
    owners = [layout]
    if _shows_master_shapes(layout):
        owners.append(layout.slide_master)
    obstacles, backdrops = [], []
    for owner in owners:
        for shape in owner.shapes:
            # Date / footer / slide-number placeholders never reach a generated slide:
            # the frame draws its own classification and number instead.
            if (getattr(shape, "is_placeholder", False)
                    and shape.placeholder_format.type is not None
                    and shape.placeholder_format.type.name in _FURNITURE):
                continue
            box = _box(shape)
            if box is None:
                continue
            share = box[2] * box[3] / (slide_w * slide_h)
            if share >= C.BACKGROUND_AREA_SHARE:
                continue
            if share >= C.BACKDROP_AREA_SHARE and not _holds_text(shape):
                backdrops.append((box, shade_of(shape) if shade_of else None))
            else:
                obstacles.append(box)
    return obstacles, backdrops


def _add_mark(layout, slide_w: float, slide_h: float, dark: bool,
              shade_of=None) -> str | None:
    """Put a Rockwell mark on a layout that has none.

    Spots, best first: the top-left and top-right corners, then the footer band just
    left of the slide number (the frame keeps the band's left end for the
    classification). Each spot takes the mark that reads there: the colour mark (the
    hexagon in a corner, the lockup in the footer) on a light area, the white lockup
    on a dark layout or over a dark band or photo. The first spot whose mark is clear
    of text and artwork wins.

    Args:
        layout: The python-pptx slide layout, changed in place.
        slide_w: Slide width, inches.
        slide_h: Slide height, inches.
        dark: True when the layout's own background is dark.
        shade_of: ``shape -> luminance or None``, to judge the areas a spot sits on.

    Returns:
        The placed mark's file name, or None when every spot is taken.
    """
    from pptx.oxml.shapes.picture import CT_Picture

    from app.genai.ppt import deck_builder

    hexagon_file = _mark_file(lockup=False, white=False)
    color_file = _mark_file(lockup=True, white=False)
    white_file = _mark_file(lockup=True, white=True)
    if None in (hexagon_file, color_file, white_file):
        return None
    hex_w = C.ADDED_MARK_WIDTH_IN
    hexagon = (hex_w, hex_w / _image_aspect(hexagon_file))
    lock_h = C.ADDED_LOCKUP_HEIGHT_IN
    lockup = (lock_h * _image_aspect(color_file), lock_h)
    margin = C.ADDED_MARK_MARGIN_IN
    band_h = deck_builder.FOOTER_BAND_HEIGHT_IN
    number_left = (slide_w - deck_builder.FRAME_SIDE_MARGIN_IN
                   - deck_builder._SLIDE_NUMBER_WIDTH_IN)
    footer_top = slide_h - band_h + (band_h - lockup[1]) / 2

    def at(spot: str, size) -> tuple[float, float, float, float]:
        if spot == "footer":
            return (number_left - C.FOOTER_MARK_GAP_IN - size[0], footer_top, *size)
        left = slide_w - margin - size[0] if spot == "right" else margin
        return (left, margin, *size)

    obstacles, backdrops = _surroundings(layout, slide_w, slide_h, shade_of)

    def is_dark(rect) -> bool:
        shades = [shade for box, shade in backdrops
                  if shade is not None and _overlaps(rect, box)]
        return min(shades) < 0.5 if shades else dark

    choice = None
    for spot in ("left", "right", "footer"):
        colour = (color_file, lockup) if spot == "footer" else (hexagon_file, hexagon)
        mark, size = (white_file, lockup) if is_dark(at(spot, colour[1])) else colour
        rect = at(spot, size)
        if not any(_overlaps(rect, other) for other in obstacles):
            choice = (mark, rect)
            break
    if choice is None:
        return None
    mark, (left, top, width, height) = choice
    # Layout shape collections have no add_picture, so the <p:pic> is built the way
    # python-pptx builds one on a slide.
    _, rel_id = layout.part.get_or_add_image_part(str(mark))
    shapes = layout.shapes
    picture = CT_Picture.new_pic(
        shapes._next_shape_id, C.MARK_SHAPE_NAME, "Rockwell logo", rel_id,
        int(left * EMU_PER_IN), int(top * EMU_PER_IN), int(width * EMU_PER_IN),
        int(height * EMU_PER_IN))
    shapes._spTree.insert_element_before(picture, "p:extLst")
    return mark.name


# --------------------------------------------------------------------------------- #
# the pass                                                                           #
# --------------------------------------------------------------------------------- #

def rebrand(prs, luminance_of=None, marks=None, icons=None,
            shade_of=None) -> RebrandReport:
    """Leave every layout of ``prs`` showing Rockwell's logo and no other company's.

    Args:
        prs: The prepared reference (python-pptx Presentation), changed in place.
        luminance_of: ``(master_index, layout_index or None) -> 0.0..1.0`` background
            luminance, to pick the white lockup on dark backgrounds; light when None.
        marks: Rockwell fingerprints, injected in tests; ``known_marks()`` otherwise.
        icons: Classification-symbol fingerprints, injected in tests;
            ``policy_icons()`` otherwise.
        shade_of: ``(master_index, shape) -> luminance or None``, so an added mark
            over a dark band or photo is the white lockup; light when None.

    Returns:
        What was kept, replaced and added.
    """
    slide_w, slide_h = prs.slide_width / EMU_PER_IN, prs.slide_height / EMU_PER_IN
    report = RebrandReport()
    found = _candidates(prs, slide_w, slide_h, marks, icons)
    owners: dict[str, set[int]] = {}
    for candidate in found:
        owners.setdefault(candidate.digest, set()).add(id(candidate.owner))
    uses = {digest: len(ids) for digest, ids in owners.items()}
    masters = {id(m) for m in prs.slide_masters}
    index_of = {}
    for mi, master in enumerate(prs.slide_masters):
        index_of[id(master)] = (mi, None)
        for li, layout in enumerate(master.slide_layouts):
            index_of[id(layout)] = (mi, li)

    def dark(owner) -> bool:
        if luminance_of is None:
            return False
        return luminance_of(*index_of[id(owner)]) < 0.5

    has_mark: set[int] = set()
    for candidate in found:
        if candidate.rockwell:
            report.kept += 1
            has_mark.add(id(candidate.owner))
            continue
        if not _is_foreign_logo(candidate, uses, masters, slide_w, slide_h):
            continue
        _, _, width, height = candidate.box
        mark = _mark_file(lockup=width / height >= C.LOCKUP_MIN_ASPECT,
                          white=dark(candidate.owner))
        where = index_of[id(candidate.owner)]
        if mark is None:
            log.warning("no Rockwell mark on disk to replace %r on %s with; it is "
                        "removed", candidate.shape.name, where)
            candidate.shape._element.getparent().remove(candidate.shape._element)
            continue
        _replace(candidate, mark, slide_w)
        report.replaced += 1
        has_mark.add(id(candidate.owner))
        log.info("logo %r on %s replaced with %s", candidate.shape.name, where,
                 mark.name)

    for mi, master in enumerate(prs.slide_masters):
        for li, layout in enumerate(master.slide_layouts):
            if id(layout) in has_mark or (id(master) in has_mark
                                          and _shows_master_shapes(layout)):
                continue
            placed = _add_mark(layout, slide_w, slide_h, dark(layout),
                               (lambda shape, mi=mi: shade_of(mi, shape))
                               if shade_of else None)
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

"""Putting Rockwell's mark on a reference: swapping a logo, or adding one."""

from app.config.constants import ReferenceDesignConstants as C
from app.genai.ppt.rebrand.geometry import A_NS, EMU_PER_IN, Geometry
from app.genai.ppt.rebrand.marks import MarkLibrary

R_EMBED = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
_FURNITURE = {"DATE", "FOOTER", "SLIDE_NUMBER", "HEADER"}
_NON_TEXT = {"PICTURE", "BITMAP", "MEDIA_CLIP", "CHART", "TABLE", "ORG_CHART", "OBJECT"}


class MarkPlacer:
    """Places marks on one deck's masters and layouts (sizes in inches)."""

    def __init__(self, slide_w, slide_h):
        self.slide_w, self.slide_h = slide_w, slide_h

    def replace(self, candidate, mark):
        """Swap the picture's image for ``mark``, fitted to its box: as large as fits,
        held against the nearer slide edge. Written back in the picture's own (group)
        coordinates; a stale SVG and crop of the old picture are dropped."""
        shape = candidate.shape
        _, rel_id = candidate.owner.part.get_or_add_image_part(str(mark))
        blip = shape._element.blipFill.find(f"{{{A_NS}}}blip")
        blip.set(R_EMBED, rel_id)
        for parent, tag in ((blip, "extLst"), (blip.getparent(), "srcRect")):
            old = parent.find(f"{{{A_NS}}}{tag}")
            if old is not None:
                parent.remove(old)
        sx, tx, sy, ty = (Geometry.transform_of(candidate.owner.shapes, shape)
                          or Geometry.IDENTITY)
        x, y, w, h = self._fit(candidate.box, MarkLibrary.aspect(mark))
        shape.left, shape.top = int((x * EMU_PER_IN - tx) / sx), int((y * EMU_PER_IN - ty) / sy)
        shape.width, shape.height = int(w * EMU_PER_IN / sx), int(h * EMU_PER_IN / sy)
        shape.name = C.MARK_SHAPE_NAME

    def _fit(self, box, aspect):
        left, top, width, height = box
        mark_w = min(width, height * aspect)
        mark_h = mark_w / aspect
        near_right = (left + width / 2) > self.slide_w / 2
        return (left + width - mark_w if near_right else left,
                top + (height - mark_h) / 2, mark_w, mark_h)

    def add(self, layout, dark, shade_of=None):
        """Put a mark on a layout that has none; returns its file name, or None when
        every spot is taken.

        Spots, best first: top-left, top-right, then the footer band just left of the
        slide number. A light spot takes the colour mark (hexagon in a corner, lockup in
        the footer); a dark layout, band or photo takes the white lockup.
        """
        files = (MarkLibrary.mark_file(False, False), MarkLibrary.mark_file(True, False),
                 MarkLibrary.mark_file(True, True))
        if None in files:
            return None
        hexagon_file, color_file, white_file = files
        hexagon = (C.ADDED_MARK_WIDTH_IN,
                   C.ADDED_MARK_WIDTH_IN / MarkLibrary.aspect(hexagon_file))
        lockup = (C.ADDED_LOCKUP_HEIGHT_IN * MarkLibrary.aspect(color_file),
                  C.ADDED_LOCKUP_HEIGHT_IN)
        obstacles, backdrops = self._surroundings(layout, shade_of)
        for spot in ("left", "right", "footer"):
            mark, size = (color_file, lockup) if spot == "footer" else (hexagon_file, hexagon)
            if self._is_dark(self._at(spot, size), backdrops, dark):
                mark, size = white_file, lockup
            rect = self._at(spot, size)
            if not any(Geometry.overlaps(rect, other) for other in obstacles):
                self._insert(layout, mark, rect)
                return mark.name
        return None

    def _at(self, spot, size):
        from app.genai.ppt import deck_builder

        if spot == "footer":
            band = deck_builder.FOOTER_BAND_HEIGHT_IN
            number_left = (self.slide_w - deck_builder.FRAME_SIDE_MARGIN_IN
                           - deck_builder._SLIDE_NUMBER_WIDTH_IN)
            return (number_left - C.FOOTER_MARK_GAP_IN - size[0],
                    self.slide_h - band + (band - size[1]) / 2, *size)
        margin = C.ADDED_MARK_MARGIN_IN
        left = self.slide_w - margin - size[0] if spot == "right" else margin
        return (left, margin, *size)

    @staticmethod
    def _is_dark(rect, backdrops, dark):
        shades = [shade for box, shade in backdrops
                  if shade is not None and Geometry.overlaps(rect, box)]
        return min(shades) < 0.5 if shades else dark

    def _surroundings(self, layout, shade_of):
        """(obstacles, backdrops). Obstacles: text boxes and smaller artwork of the
        layout and, when shown, its master. Backdrops: large picture or text-free areas
        a mark may sit on, as (box, luminance or None). Date/footer/number placeholders
        never reach a slide, and a full-slide background is judged by the layout."""
        owners = [layout] + ([layout.slide_master]
                             if Geometry.shows_master_shapes(layout) else [])
        obstacles, backdrops = [], []
        for owner in owners:
            for shape in owner.shapes:
                kind = self._placeholder_kind(shape)
                box = Geometry.box(shape)
                if kind in _FURNITURE or box is None:
                    continue
                share = box[2] * box[3] / (self.slide_w * self.slide_h)
                if share >= C.BACKGROUND_AREA_SHARE:
                    continue
                if share >= C.BACKDROP_AREA_SHARE and not self._holds_text(shape, kind):
                    backdrops.append((box, shade_of(shape) if shade_of else None))
                else:
                    obstacles.append(box)
        return obstacles, backdrops

    @staticmethod
    def _placeholder_kind(shape):
        if not getattr(shape, "is_placeholder", False):
            return ""
        kind = shape.placeholder_format.type
        return kind.name if kind is not None else None

    @staticmethod
    def _holds_text(shape, kind):
        if kind != "":
            return kind is None or kind not in _NON_TEXT
        return bool(getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip())

    @staticmethod
    def _insert(layout, mark, rect):
        # Layout shape collections have no add_picture, so the <p:pic> is built the
        # way python-pptx builds one on a slide.
        from pptx.oxml.shapes.picture import CT_Picture

        _, rel_id = layout.part.get_or_add_image_part(str(mark))
        shapes = layout.shapes
        left, top, width, height = (int(v * EMU_PER_IN) for v in rect)
        picture = CT_Picture.new_pic(shapes._next_shape_id, C.MARK_SHAPE_NAME,
                                     "Rockwell logo", rel_id, left, top, width, height)
        shapes._spTree.insert_element_before(picture, "p:extLst")

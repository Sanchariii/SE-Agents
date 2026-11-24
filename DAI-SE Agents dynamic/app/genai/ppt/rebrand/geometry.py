"""Where shapes sit on a slide, groups included, for the rebrand pass."""

from app.config.constants import ReferenceDesignConstants as C

A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
EMU_PER_IN = 914400
PICTURE = 13                                  # MSO_SHAPE_TYPE.PICTURE
GROUP = 6                                     # MSO_SHAPE_TYPE.GROUP


class Geometry:
    """Boxes are (left, top, width, height) on the slide, in inches.

    A transform maps a shape's own EMU coordinates to the slide's:
    slide = scale * own + offset, per axis, as (scale_x, offset_x, scale_y, offset_y).
    """

    IDENTITY = (1.0, 0.0, 1.0, 0.0)

    @staticmethod
    def group_transform(group, outer):
        """The transform of a group's children, composed with the group's own."""
        xfrm = group._element.grpSpPr.find(f"{{{A_NS}}}xfrm")
        if xfrm is None:
            return outer
        off, ext = xfrm.find(f"{{{A_NS}}}off"), xfrm.find(f"{{{A_NS}}}ext")
        ch_off, ch_ext = xfrm.find(f"{{{A_NS}}}chOff"), xfrm.find(f"{{{A_NS}}}chExt")
        if None in (off, ext, ch_off, ch_ext):
            return outer
        sx = int(ext.get("cx")) / int(ch_ext.get("cx")) if int(ch_ext.get("cx")) else 1.0
        sy = int(ext.get("cy")) / int(ch_ext.get("cy")) if int(ch_ext.get("cy")) else 1.0
        tx = int(off.get("x")) - int(ch_off.get("x")) * sx
        ty = int(off.get("y")) - int(ch_off.get("y")) * sy
        osx, otx, osy, oty = outer
        return osx * sx, osx * tx + otx, osy * sy, osy * ty + oty

    @staticmethod
    def box(shape, transform=IDENTITY):
        """A shape's box on the slide, or None when it has no position."""
        if None in (shape.left, shape.top, shape.width, shape.height):
            return None
        sx, tx, sy, ty = transform
        return ((shape.left * sx + tx) / EMU_PER_IN, (shape.top * sy + ty) / EMU_PER_IN,
                shape.width * sx / EMU_PER_IN, shape.height * sy / EMU_PER_IN)

    @classmethod
    def pictures(cls, shapes, transform=IDENTITY):
        """Every picture in a shape tree, with its box on the slide."""
        for shape in shapes:
            if shape.shape_type == GROUP:
                yield from cls.pictures(shape.shapes, cls.group_transform(shape, transform))
            elif shape.shape_type == PICTURE and not getattr(shape, "is_placeholder", False):
                box = cls.box(shape, transform)
                if box is not None:
                    yield shape, box

    @classmethod
    def transform_of(cls, shapes, target, transform=IDENTITY):
        """The transform that applies to ``target`` inside ``shapes``, or None."""
        for shape in shapes:
            if shape is target or shape._element is target._element:
                return transform
            if shape.shape_type == GROUP:
                found = cls.transform_of(shape.shapes, target,
                                         cls.group_transform(shape, transform))
                if found is not None:
                    return found
        return None

    @staticmethod
    def is_logo_sized(box, slide_w, slide_h):
        """Small enough to be a logo rather than a photo or a background."""
        _, _, width, height = box
        return (C.LOGO_MIN_SIDE_IN <= min(width, height)
                and max(width, height) <= C.LOGO_MAX_SIDE_IN
                and width * height <= C.LOGO_MAX_AREA_SHARE * slide_w * slide_h)

    @staticmethod
    def in_corner(box, slide_w, slide_h):
        left, top, width, height = box
        cx, cy = left + width / 2, top + height / 2
        return (min(cx, slide_w - cx) <= C.LOGO_CORNER_X_IN
                and min(cy, slide_h - cy) <= C.LOGO_CORNER_Y_IN)

    @staticmethod
    def overlaps(a, b, tolerance=0.02):
        return (min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]) > tolerance
                and min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]) > tolerance)

    @staticmethod
    def shows_master_shapes(layout):
        return layout.element.get("showMasterSp") not in ("0", "false")

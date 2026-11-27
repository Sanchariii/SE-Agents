"""The D2R programme logo, only on slides that are about D2R.

Every deck carries Rockwell's logo. The D2R mark belongs to one programme, so it is
shown only on a slide whose own content mentions D2R (``ProgramMark.is_about``):

* On the Rockwell template, the frame stamps the Rockwell lockup
  (``design_contract.frame_for_layout``); on a D2R slide ``brand_file`` swaps that
  frame logo for the D2R mark, in the same spot.
* On a reference deck, the rebrand takes the D2R marks off its layouts, keeping a hidden
  "(D2R)" twin of each such layout (``rebrand.twins``); ``brand_file`` moves a D2R
  slide onto its layout's twin, so it shows the reference's own D2R mark.

It runs on each slide's file before stitching, so the slide's number - and so its
content - is known exactly. A slide file that changes is written as a copy, so the
per-slide cache keeps its original.
"""

import io
import json
import re
from pathlib import Path

from app.config.constants import ReferenceDesignConstants as C
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

_FRAME_LOGO = "frame:logo"          # deck_builder.FRAME_SHAPE_PREFIX + "logo"


class ProgramMark:
    """Decides and applies the D2R mark per slide."""

    _pattern = re.compile(C.PROGRAM_TOPIC_PATTERN, re.IGNORECASE)

    @classmethod
    def is_about(cls, slide):
        """True when the slide's visible content (title, text, tables, quiz, FAQ)
        mentions D2R. Speaker notes and image references do not count."""
        if slide is None:
            return False
        content = slide.model_dump(mode="json",
                                   exclude={"notes", "image_candidates", "layout"})
        return bool(cls._pattern.search(json.dumps(content, ensure_ascii=False)))

    def brand_file(self, path, slide, out_dir):
        """The slide file to stitch: ``path`` itself, or a copy in ``out_dir`` with the
        logo set for this slide's content.

        Args:
            path: One slide's .pptx.
            slide: Its ``SlideDraft`` (None for a slide with no draft of its own).
            out_dir: Where a changed copy is written.
        """
        from pptx import Presentation

        from app.genai.ppt.rebrand.twins import LayoutTwin

        about = self.is_about(slide)
        prs = Presentation(str(path))
        changed = 0
        for page in prs.slides:
            changed += self._set_frame_logo(page, about)
            if about and LayoutTwin.use_on(page):
                changed += 1
        if not changed:
            return Path(path)
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / Path(path).name
        prs.save(str(target))
        log.info("slide %s: logo set for %s content (%d change(s))",
                 getattr(slide, "slide_number", "?"), "D2R" if about else "general",
                 changed)
        return target

    @staticmethod
    def _set_frame_logo(page, about):
        """Swap the frame's logo picture for the D2R mark or the Rockwell lockup."""
        from app.genai.ppt.template import registry

        changed = 0
        for shape in list(page.shapes):
            if shape.name != _FRAME_LOGO or shape.shape_type != 13:
                continue
            blob = shape.image.blob
            white = blob in (registry.logo_path(C.LOCKUP_WHITE).read_bytes(),
                             registry.logo_path(C.PROGRAM_MARK_WHITE).read_bytes())
            if about:
                wanted = C.PROGRAM_MARK_WHITE if white else C.PROGRAM_MARK
            else:
                wanted = C.LOCKUP_WHITE if white else C.LOCKUP_COLOR
            data = registry.logo_path(wanted).read_bytes()
            if data == blob:
                continue
            ProgramMark._swap_image(page, shape, data)
            changed += 1
        return changed

    @staticmethod
    def _swap_image(page, shape, data):
        """Point the picture at ``data``, keeping its left, top and width."""
        from PIL import Image
        from pptx.oxml.ns import qn

        _, rel_id = page.part.get_or_add_image_part(io.BytesIO(data))
        shape._element.blipFill.find(qn("a:blip")).set(qn("r:embed"), rel_id)
        with Image.open(io.BytesIO(data)) as image:
            shape.height = int(shape.width * image.height / image.width)

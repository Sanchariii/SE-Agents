"""The collector: many single-slide .pptx files -> one deck, in plan order.

Every slide and this target deck are built from the same brand template, so a slide's
layout exists under the same name in both. That makes stitching simpler than it was:
each target slide is created on its OWN layout rather than on a blank one, and theme,
fonts, master decor, footer and logo inherit properly instead of having to be re-drawn
into every slide.

A source that cannot be read is skipped with a note rather than failing the deck:
losing one slide's design is always better than losing the whole deck.
"""

import copy
import io
from pathlib import Path

from app.utilities import events_util as events
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

# Relationship types that point at a binary part the copied shapes need with them.
_COPYABLE_REL_TYPES = ("image", "chart", "oleObject", "package", "media", "video", "audio")

# PowerPoint reopens a file in whatever view it was last saved in (viewProps.xml
# `lastView`). The target deck inherits that from the brand template, so a template
# saved while in Slide Master view makes every downloaded deck open there too.
_VIEW_PROPS_RELTYPE = ("http://schemas.openxmlformats.org/officeDocument/2006/"
                       "relationships/viewProps")
_NORMAL_VIEW = "sldView"


class DeckStitcher:
    """Merges per-slide .pptx files into one deck.

    Stateless apart from the notes it collects, so one instance can stitch one deck.
    """

    def __init__(self, session_id: str = "") -> None:
        """
        Args:
            session_id: When given, a dropped slide is announced to the client as it
                happens. Losing a slide is content loss, and it otherwise only
                surfaces in the deck's gap list once the whole turn is finished.
                Omit it and the stitcher publishes nothing, which is what the tests
                and the diagnostic scripts want.
        """
        self.notes: list[str] = []
        self.session_id = session_id

    def stitch(self, slide_files: list[tuple[int, Path]], out_path: Path) -> Path:
        """Build the final deck from the per-slide files.

        Args:
            slide_files: (slide_number, path) pairs; stitched in slide-number order.
            out_path: Where the merged deck is written.

        Returns:
            The path of the written deck.

        Raises:
            ValueError: When no slide could be copied, so there is no deck to publish.
        """
        # Imported lazily: python-pptx is heavy and only needed when a deck is built.
        from pptx import Presentation

        from app.genai.ppt.template import loader, registry

        log.info("stitching %d source file(s) into %s", len(slide_files), out_path)
        target = Presentation(str(registry.template_path()))
        loader.clear_slides(target)          # the template ships 31 sample slides
        masters = list(target.slide_masters)
        log.debug("target deck opened from the template: %d master(s), %d layout(s)",
                  len(masters), sum(len(m.slide_layouts) for m in masters))

        copied = 0
        used_masters: set[int] = set()
        for slide_number, path in sorted(slide_files, key=lambda item: item[0]):
            try:
                source = Presentation(str(path))
            except Exception as exc:                          # noqa: BLE001
                log.error("slide %d could not be read from %s: %s", slide_number, path, exc,
                          exc_info=True)
                self.notes.append(f"Slide {slide_number} was dropped: its file could not "
                                  f"be read ({exc}).")
                if self.session_id:
                    events.publish(
                        self.session_id, "agent_step", agent="deck",
                        message=(f"Slide {slide_number} could not be read and is "
                                 f"missing from the deck."),
                        slide_number=slide_number, phase="dropped")
                continue

            found = list(source.slides)
            if len(found) != 1:
                log.warning("slide %d's file holds %d slides, not 1; all are kept",
                            slide_number, len(found))
            for source_slide in found:
                layout = self._matching_layout(masters, source_slide, target)
                log.debug("slide %d: %r -> target layout %r", slide_number,
                          source_slide.slide_layout.name, layout.name)
                self._copy_slide(source_slide, target.slides.add_slide(layout))
                used_masters.add(next((i for i, m in enumerate(masters)
                                       if m is layout.slide_master
                                       or m.part is layout.slide_master.part), -1))
                copied += 1
                # One generated file is expected to hold exactly one slide; if the model
                # produced more, they are all kept rather than silently truncated.

        if not copied:
            log.error("no slide could be copied into the deck; %d source file(s) were "
                      "all unreadable", len(slide_files))
            raise ValueError("no slide could be copied into the deck")

        self._apply_high_creativity(target, used_masters)
        self._open_in_normal_view(target)
        target.save(str(out_path))
        log.info("stitched %d of %d slide(s) into %s (%d bytes)%s",
                 copied, len(slide_files), out_path, out_path.stat().st_size,
                 f", {len(self.notes)} dropped" if self.notes else "")
        return out_path

    # --- internals ---------------------------------------------------------- #
    @staticmethod
    def _apply_high_creativity(target, used_masters: set[int]) -> None:
        """Put the High Creativity master on brand when a slide sits on it.

        The target deck is a fresh copy of the template, so the per-slide overrides do
        not carry over; they are applied again here, and only when the deck actually
        uses that master. Every other deck is saved exactly as before.

        Args:
            target: The deck being built.
            used_masters: Indices of the masters the copied slides sit on.
        """
        from app.config.constants import PptConstants
        from app.genai.ppt.deck_builder import apply_high_creativity_overrides
        from app.genai.ppt.template import catalog as catalog_mod

        catalog = catalog_mod.load()
        hc = catalog.high_creativity_master
        if hc is None or hc not in used_masters:
            return
        prompts = {l.layout_index: list(l.prompt_shapes)
                   for l in catalog.high_creativity_layouts if l.prompt_shapes}
        log.info("deck uses the High Creativity master M%d; applying the brand palette, "
                 "%s and the sample-title removal to it", hc, PptConstants.FONT_NAME)
        apply_high_creativity_overrides(target, hc, catalog.preferred_master,
                                        PptConstants.FONT_NAME, prompts)

    @staticmethod
    def _open_in_normal_view(target) -> None:
        """Make the deck open in Normal (slide) view, whatever the template was saved in.

        Best-effort: a deck that opens in the wrong view is still a correct deck, so a
        failure here is logged and the stitch carries on.

        Args:
            target: The deck being built; its viewProps part is rewritten in place.

        Returns:
            None.
        """
        from lxml import etree

        try:
            part = next((rel.target_part for rel in target.part.rels.values()
                         if rel.reltype == _VIEW_PROPS_RELTYPE and not rel.is_external),
                        None)
            if part is None:
                # No viewProps part at all: PowerPoint already defaults to Normal view.
                log.debug("deck has no viewProps part; it opens in Normal view")
                return
            # python-pptx loads viewProps as an opaque Part, so edit its raw XML.
            root = etree.fromstring(part.blob)
            previous = root.get("lastView")
            if previous == _NORMAL_VIEW:
                return
            root.set("lastView", _NORMAL_VIEW)
            part._blob = etree.tostring(root, xml_declaration=True,
                                        encoding="UTF-8", standalone=True)
            log.info("deck view reset from %r to Normal view so it does not open in "
                     "the template's saved view", previous or "default")
        except Exception as exc:                              # noqa: BLE001
            log.warning("could not reset the deck's opening view; it may open in the "
                        "template's saved view (e.g. Slide Master): %s", exc,
                        exc_info=True)

    @staticmethod
    def _matching_layout(masters, source_slide, target):
        """The target layout matching the one this slide was built on.

        Because both decks come from the same template, matching by layout name is
        exact. A source that did not come from our template falls back to a blank
        layout, which keeps the slide rather than dropping it.

        Args:
            masters: The target deck's slide masters.
            source_slide: The slide being copied.
            target: The deck being built, for the blank-layout fallback.

        Returns:
            A slide layout belonging to `target`.
        """
        wanted = (source_slide.slide_layout.name or "").strip().lower()
        for master in masters:
            for layout in master.slide_layouts:
                if (layout.name or "").strip().lower() == wanted:
                    return layout
        log.warning("no layout named %r in the template, so this slide did not come "
                    "from it; falling back to the emptiest layout, which means it "
                    "inherits none of the master's theme or furniture", wanted)
        return DeckStitcher._blank_layout(target)

    @staticmethod
    def _blank_layout(target):
        """The emptiest layout in the target deck, for a slide from another template."""
        layout = min((layout for master in target.slide_masters
                      for layout in master.slide_layouts),
                     key=lambda layout: len(layout.placeholders))
        log.debug("blank-layout fallback resolved to %r with %d placeholder(s)",
                  layout.name, len(layout.placeholders))
        return layout

    def _copy_slide(self, source_slide, target_slide) -> None:
        """Copy one slide's shapes, related parts and notes onto a fresh target slide.

        The target slide inherits its layout's placeholders, and the source already
        filled and finalised its own copies of them, so those inherited ones are
        removed before the source tree is appended. Leaving them would ship an empty
        "Click to edit" box under every generated slide.

        Args:
            source_slide: The slide read from a generated single-slide deck.
            target_slide: The slide just added to the deck being built.

        Returns:
            None. `target_slide` is mutated in place.
        """
        inherited = list(target_slide.placeholders)
        for shape in inherited:
            shape._element.getparent().remove(shape._element)

        rel_map = self._copy_related_parts(source_slide, target_slide)

        copied = 0
        for shape in source_slide.shapes:
            element = copy.deepcopy(shape._element)
            self._remap_relationship_ids(element, rel_map)
            target_slide.shapes._spTree.append(element)
            copied += 1
        log.debug("copied %d shape(s) and %d related part(s); dropped %d inherited "
                  "placeholder(s)", copied, len(rel_map), len(inherited))

        if source_slide.has_notes_slide:
            text = source_slide.notes_slide.notes_text_frame.text
            if text:
                target_slide.notes_slide.notes_text_frame.text = text
                log.debug("carried %d chars of speaker notes across", len(text))

    @staticmethod
    def _copy_related_parts(source_slide, target_slide) -> dict[str, str]:
        """Re-attach the parts the copied shapes reference to the target slide.

        Args:
            source_slide: Slide whose relationships are being copied.
            target_slide: Slide the parts are attached to.

        Returns:
            Mapping of old relationship id -> new relationship id, for shape rewriting.
        """
        rel_map: dict[str, str] = {}
        for rel_id, rel in source_slide.part.rels.items():
            if not any(kind in rel.reltype for kind in _COPYABLE_REL_TYPES):
                continue
            try:
                if rel.is_external:
                    new_id = target_slide.part.rels.get_or_add_ext_rel(
                        rel.reltype, rel.target_ref)
                elif rel.reltype.endswith("/image"):
                    # Re-import into the target package so python-pptx assigns a
                    # unique part name. Attaching an ImagePart from each source deck
                    # directly can write duplicate ppt/media/imageN.* ZIP entries,
                    # which PowerPoint treats as a damaged OPC package.
                    _, new_id = target_slide.part.get_or_add_image_part(
                        io.BytesIO(rel.target_part.blob))
                else:
                    new_id = target_slide.part.rels.get_or_add(rel.reltype, rel.target_part)
            except Exception as exc:                          # noqa: BLE001
                # Drop just this part: the shape still renders, minus its media.
                log.warning("related part %s could not be copied, so the shape "
                            "pointing at it will render empty: %s", rel.reltype, exc,
                            exc_info=True)
                continue
            rel_map[rel_id] = new_id
        return rel_map

    @staticmethod
    def _remap_relationship_ids(element, rel_map: dict[str, str]) -> None:
        """Point a copied shape's r:embed / r:link / r:id at the new relationship ids.

        Args:
            element: The deep-copied shape XML element.
            rel_map: Old -> new relationship id mapping from `_copy_related_parts`.

        Returns:
            None. `element` is mutated in place.
        """
        if not rel_map:
            return
        namespace = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
        remapped = 0
        for node in element.iter():
            for attribute in ("embed", "link", "id", "pict", "dm", "lo", "qs", "cs"):
                key = f"{namespace}{attribute}"
                old = node.get(key)
                if old and old in rel_map:
                    node.set(key, rel_map[old])
                    remapped += 1
        if remapped:
            log.debug("re-pointed %d relationship reference(s) on a copied shape",
                      remapped)


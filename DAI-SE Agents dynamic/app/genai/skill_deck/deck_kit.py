"""Copied into the run folder beside the model's program - like ``deck_builder.py``,
it must never import ``app.*`` (the sandbox has no access to it).

``WholeDeck`` is DeckBuilder for a WHOLE deck: ``deck.slide(n)`` puts slide n on the
layout the deck design contract assigned it, and ``deck.save()`` stamps every slide's
own frame (classification, slide number, logo variant) and highlights the correct quiz
choice on answer slides only. DeckBuilder itself has one frame and one reveal switch
per deck, which is right for the per-slide designer and wrong for a whole deck.
"""
from deck_builder import DeckBuilder

import context


class WholeDeck(DeckBuilder):
    """The whole deck on the official template, one assigned layout per slide."""

    def __init__(self):
        super().__init__(context.TEMPLATE_PATH, context.CATALOG, frame=None,
                         reveal_answers=False)
        self._specs = {spec["number"]: spec for spec in context.SLIDES}
        self._added = []

    def slide(self, number):
        """Add slide ``number`` on its assigned layout and return its SlideHandle.

        Slides must be added in order (1, 2, 3 ...), each exactly once.
        """
        spec = self._specs.get(number)
        if spec is None:
            raise KeyError(f"there is no slide {number}; SLIDES has 1 to {len(self._specs)}")
        expected = len(self._added) + 1
        if number != expected:
            raise ValueError(f"add the slides in order: the next one is slide {expected}, "
                             f"not {number}")
        handle = self.add_slide(spec["layout_key"])
        self._added.append((handle, spec))
        return handle

    def images(self, number):
        """The image files slide ``number`` may place (absolute paths)."""
        return list(self._specs[number].get("images") or [])

    def save(self, path=None):
        """Stamp each slide's own frame, reveal answers where asked, then write."""
        missing = sorted(set(self._specs) - {spec["number"] for _, spec in self._added})
        if missing:
            raise RuntimeError(f"slide(s) {missing} were never added; call deck.slide(n) "
                               "for every slide in SLIDES")
        for handle, spec in self._added:
            if spec.get("reveal"):
                handle._reveal_choices()
        for handle, spec in self._added:
            self.frame = spec.get("frame") or {}
            handle.finalize()               # idempotent: DeckBuilder.save will not re-stamp
        self.frame = {}
        return super().save(path or context.OUTPUT_PATH)

---
name: whole-deck
description: Build the WHOLE deck in one DeckBuilder program on the official template, so the slides read as one deck.
---
# Whole deck in one program

The Rockwell ppt skill above says how a slide is built. This skill says how the whole
deck is put together: you design every slide in ONE Python program, so you can keep one
visual language from the first slide to the last.

## The program

Begin it with exactly these lines:

    from deck_builder import DeckBuilder
    from deck_kit import WholeDeck
    from context import OUTPUT_PATH, SLIDES

    deck = WholeDeck()

- `s = deck.slide(n)` adds slide n on the layout the deck design contract assigned it
  and returns its slide handle. Use the placeholder API and the components of the ppt
  skill on `s` (`s.set_title`, `s.set_bullets`, `s.add_card`, `s.grid`, ...).
- Add the slides in order - 1, 2, 3 ... - each slide in SLIDES exactly once.
- `deck.images(n)` lists the image files slide n may place. Use no other path.
- Finish with `deck.save(OUTPUT_PATH)` as the LAST line. It stamps every slide's own
  frame (classification label, slide number, logo) and highlights the correct option
  on quiz answer slides. Never draw any of those yourself.
- `SLIDES[n - 1]` is slide n's assignment: `layout_key`, `mode` ("layout" or "canvas"),
  `free_region` (canvas mode), `kind`. The brief repeats it for every slide.

## One deck, not N separate slides

- Write one small helper function per recurring pattern (a card row, a two-column
  comparison, a quiz slide, a step sequence) and call it for every slide of that kind.
  Same pattern -> same sizes, positions, colours and type sizes on every slide.
- Use the same accent colour for the same meaning everywhere (e.g. `red` for the key
  element, `navy` for headings) and the same card style throughout.
- Keep the content density even: no slide crammed while the next is nearly empty -
  move detail into the speaker notes instead.
- Keep the program compact: helpers first, then one short block per slide.

## Quiz and FAQ slides

- Every QUIZ slide is built by the same helper: the counter ("Question 2 of 5") as a
  small label, the question as the focal text, then EVERY option with
  `s.add_choice(letter, text, left, top, width, height, correct=<True only for the
  option at quiz.correct_index>)`, in order, letters A, B, C ... All options look
  identical; never single out the correct one yourself.
- An answer slide (`"reveal": true`) is built by calling the SAME helper with the SAME
  content as its question; `deck.save` highlights the correct option on it.
- FAQ slides: one helper; number the questions from `faq_start`, and pair each
  question visibly with its answer (one card, row or band per pair).

## Content

- The content is final: keep the slide order, the titles and the wording; tighten
  on-slide wording only where it cannot fit readably, and move the detail into the
  speaker notes. Never invent or drop a fact.
- Put each slide's `notes` on it with `s.set_notes(...)`.

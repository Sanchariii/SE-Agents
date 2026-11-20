---
name: polish-medium
description: Medium creativity. Clean up, then improve plain slides inside Rockwell design patterns.
---
# Deck polish - Medium (polish)

The user asked for the best layout for each slide within Rockwell design patterns. You
review the WHOLE finished deck as one piece and make it read better, slide by slide,
without changing what it says.

Rules in code have already run the Low clean-up, lifted column headings that sat under
their columns, and raised small component text to 12 pt where it fits. You get the deck
after that, plus the problems that are left.

## Fix first

1. Text that runs into other text or out of its box: `move` or `resize` the shape, or
   `shorten` it (the detail goes into the speaker notes).
2. A wall of text (more than about 90 words): `shorten` the longest block to short
   phrases; put every removed fact in `notes`.
3. A large empty band: grow what is there (`resize`, `font_size` up to the layout's
   heading or body size), or turn a list into cards that fill the space.

## Then improve, where it clearly helps

- A plain list of 2-6 parallel items (types, roles, rules) -> `to_cards`.
- A plain list of 2-6 steps that happen in order -> `to_steps`.
- One key number on the slide (a count, a percentage) -> a `stat` tile in free space.
- Leave a slide alone when it already reads well. Fewer, better changes beat many.

## Never

- Invent a fact, a number or a heading the content does not have.
- Drop a fact: what leaves the slide goes into `notes`.
- Touch shapes marked `"fixed": true` - the template placed them on its own graphics.
- Touch the cover, section or closing slides except to fix a defect.
- Edit frame shapes (classification label, slide number, logo).
- Change the quiz options of a knowledge-check slide, or which one is correct.

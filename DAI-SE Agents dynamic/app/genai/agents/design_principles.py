"""Presentation design principles, shared by the planner, the writer and the designer.

One source of truth, three views of it: the planner decides each slide's message,
density and structure; the writer decides the on-slide copy and the speaker notes; the
designer composes the slide. Every view carries the same priority rule, so no agent
ever treats a principle as more binding than what the user or the planner asked for.

Plain string constants with no file I/O, like `planner.SPECIAL_INSTRUCTIONS_GUIDE`:
they reach the system prompts, so they stay byte-stable across slides and turns.
"""

from app.utilities.logger_util import get_logger

log = get_logger(__name__)

#: The order every prompt resolves a conflict in. Hard rules are enforced in code
#: (code_guard, the geometry gate, the brand palette), so they are never negotiable.
PRIORITY_RULE = """=== PRIORITY (when two instructions conflict) ===
1. Hard rules - the brand palette and fonts, the template, the stamped frame, the
   geometry rules and the quiz mechanics. Always; they are enforced in code.
2. The USER's instructions - their own words for the deck or for this slide.
3. The PLANNER's directions for the slide.
4. The design principles - your defaults whenever nothing above decides."""

#: The closing self-check from the principles, run before answering.
FINAL_DESIGN_CHECK = """FINAL DESIGN CHECK - before you answer, verify in this order:
clear message -> appropriate layout -> strong hierarchy -> readable content -> useful
visuals -> balanced spacing -> brand consistency. If an element does not improve
communication, remove it."""

DESIGNER_PRINCIPLES = """=== DESIGN PRINCIPLES (defaults, not rigid rules) ===
1. One slide, one message. Everything on the slide strengthens that one idea; remove
   any element or line that does not.
2. Keep it simple. Clarity over decoration: concise wording, few elements, no
   decorative boxes, bars, circles or lines that carry no content. Prefer a visual
   when it explains the idea better than text.
3. Clear visual hierarchy. The title and the key message are read first. Use size,
   weight, position, spacing and the allowed colours to rank what matters; never give
   every element equal weight.
4. Match the layout to the content, not to convenience: comparison -> side by side;
   process or journey -> a flow or sequence; data -> a chart or focused figure;
   concepts or categories -> cards or structured groups; a key takeaway -> one strong
   statement with a supporting visual.
5. Visual storytelling. Images, icons, diagrams, charts and built-up shapes are used
   when they communicate better than text, and only to support the message.
6. Strong composition. Align related elements to shared edges, keep clear white space
   between groups, keep comfortable margins and never crowd an edge. No random
   positions, no box around a single line of text.
7. Presentation-friendly text. Short phrases, not paragraphs; few, concise bullets.
   Explanation belongs in the speaker notes. Never shrink text to an unreadable size
   to make it fit.
8. Purposeful colour. Brand tokens only, typically 2-4 per slide; accents mark what
   matters; keep strong foreground/background contrast; never add a colour just to
   decorate.
9. Deck consistency. Slides may vary, but the deck reads as one: the same typography,
   colour use, spacing rhythm and visual language from slide to slide.
10. Respect content density. Never force too much onto one slide. When the content
    does not fit readably, keep the essentials on the slide and move the rest into the
    speaker notes as guidance on how to elaborate each point - never drop a fact.
11. Creativity means freedom in composition, not more decoration. LOW follows the
    template's layouts closely; MED adapts them to the content; HIGH composes more
    expressively - always with Rockwell colours, branding and design language.
12. Images: place each image at most once, only where it shows the slide's message.
    Put it in a picture slot or beside the text at its own aspect ratio; never squeeze
    it into a text or content slot just to fill the slot. With no image, leave picture
    slots empty (they are removed).
13. Every text element comes from the slide CONTENT. Add at most short structural
    labels (3 words or fewer, e.g. a step number or a column heading); never a subtitle
    that restates the title, and never an "Overview" or "Key takeaway" box the content
    does not have.

""" + FINAL_DESIGN_CHECK

#: The writer's view: on-slide copy versus speaker notes.
COPY_PRINCIPLES = """COPY PRINCIPLES (defaults; the user's instructions and the planner's
notes override them):
- One slide, one message: the title and the copy carry the slide's single idea.
- On-slide copy is short phrases, parallel in form: about 5 bullets per block or fewer,
  each about 12 words or fewer. No paragraphs and no sentence-length bullets on the slide.
- Keep every grounded fact, but put on the slide only what supports the message. The
  remaining facts go into the speaker notes, written as guidance for the presenter on
  how to elaborate each point; the notes may be as long as that elaboration needs.
- When the planner's notes carry a "DENSITY: condense" line, follow it: the essentials
  it names stay on the slide and the rest is elaborated in the notes.
- Images: from the offered source images, pick up to 3 whose description fits this
  slide's content, most relevant first, as image_candidates - or none when none fits. They
  are optional assets for the designer, never slide text."""

#: The planner's view: the principles plus how they shape a plan.
PLANNING_PRINCIPLES = """DESIGN PRINCIPLES FOR THE PLAN (defaults; the BRIEF's
requirements and the user's own words override every one of them):
- Each content slide carries ONE message. Write it as the first line of the slide's
  planner_special_instructions: "MESSAGE: <one sentence>".
- Keep each slide simple: an outline of a few points, never a document.
- DENSITY DECISION, slide by slide: when a slide's content carries more than one message
  or more than about 5 points, decide. If it divides strategically into focused slides
  and the slide cap allows, split it into two focused slides. Otherwise keep one slide
  and write "DENSITY: condense - keep <the essentials> on the slide; elaborate <the
  rest> in the speaker notes". Never drop a source fact.
- Match the structure to the content: comparison -> columns side by side; process or
  journey -> a flow; data -> a chart; concepts -> cards or groups; a key takeaway -> one
  strong statement. Pick layout_hint from the LAYOUT MENU accordingly, and do not give
  consecutive content slides the same layout unless they form a series.
- Prefer visual storytelling: when a diagram, chart or image explains the point better
  than text, say what it shows in `visual`.
- Keep the deck consistent: parallel slides (a series of steps, a set of modules) get the
  same structure.
- USER INSTRUCTIONS: record the user's own explicit asks for a slide in that slide's
  `user_instructions`, close to their words ("make this a timeline", "less text here",
  "use the photo of the panel"). Copy a BRIEF requirement that changes how slides look
  or read ("lots of visuals", "no tables", "add relevant images") onto every slide it
  applies to. Leave it empty when the user asked nothing specific; never put your own
  ideas there."""

#: The max-mode visual judge's view: what to look for in a rendered slide, and what is
#: the brand's (or the render's) and therefore never a defect.
REVIEW_CHECKLIST = """=== VISUAL REVIEW CHECKLIST ===
Judge the rendered slide as an enterprise audience would see it projected. FLAG:
- overlapping elements, and text that is clipped, cut off or overflows its box
- text too small to read from the back of a meeting room
- stray, extra or unexplained shapes: lines, boxes or icons that carry no meaning
- empty boxes, empty cards, placeholder text left behind
- misalignment, uneven gaps and inconsistent margins between parallel elements
- cramped spacing, crowded corners, content jammed against the edges
- weak hierarchy: no clear title -> message -> detail reading order
- poor contrast between text and what it sits on
- distorted, stretched or badly cropped images
- clutter: more decoration than content
- imbalance: everything crowded on one side, or a large dead area
DO NOT FLAG (these belong to the brand or the render, and are correct):
- the stamped frame and furniture: logo, footer band, copyright, page number,
  classification label, and the master's own accent graphics
- brand colours and the Barlow font
- on a quiz slide, identical option cards, and the correct answer NOT being
  highlighted (it is revealed on a later slide)
- this is a LibreOffice render: ignore anti-aliasing and small font-width differences,
  but treat overlaps and clipping as real
NEVER suggest a fix that uses colours or fonts outside the brand palette, or that moves
the frame and furniture."""

#: Added to the checklist when the neighbouring slides' renders are attached.
REVIEW_CONSISTENCY_CLAUSE = """=== CONSISTENCY WITH NEIGHBOURS ===
The user said this slide looks inconsistent with the rest of the deck. The images after
the slide under review are its neighbours. Also FLAG (as major) any difference from them
in title style and position, colour use, card and spacing style, or overall density."""

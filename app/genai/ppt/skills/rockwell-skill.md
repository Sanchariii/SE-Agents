# Rockwell Automation: Presentation Guide (PowerPoint)

Use this reference when creating or reviewing a PowerPoint deck branded as Rockwell Automation.
Decks are generated **from the approved template (`SAMPLE.pptx`)**: every slide must use one of the
template's own layouts so the theme, fonts, colours, logo, footer and slide dimensions are inherited
automatically. Never build slides from scratch on a blank layout when a template layout fits.

---

## Slide Dimensions

- **Widescreen 16:9**: 13.33" x 7.50" (read from the template at runtime)
- All positions and sizes below are in inches on this canvas

---

## Layouts Come From the Template Catalog

The layouts are **not** listed here on purpose: they are discovered at runtime by inspecting `SAMPLE.pptx`
(several masters, hundreds of layouts, many with identical names).

**Your assigned layout is described in full in your brief** — placeholders with their
`idx`, slot name, role, geometry and capacity; the graphics the layout already draws;
the reserved zones; and the region you may compose in. Read it there. The catalog tools
below are the same information for a layout you were *not* assigned, and are only
available when your brief says you may call tools:

| Tool | Use it to |
|---|---|
| `get_template_summary` | Slide size, theme fonts/colours, masters, the preferred content master, every unique layout with its slot summary |
| `search_layouts` | Find layouts by keywords / tags (`cover`, `agenda`, `section`, `columns`, `3-column`, `process`, `dashboard`, `photo`, `title-only`, `closing`, ...) |
| `recommend_layouts` | Rank layouts for a slide intent + content shape (number of text blocks, picture, table, chart) |
| `get_layout_details` | Placeholders in reading order with `idx`, `slot` name, role, geometry, font size and capacity - **required before using a layout** |
| `check_geometry` | Check an arrangement's rectangles **before** writing code — free, and the fastest way to find a collision |
| `validate_slide` | The authoritative geometry, brand and fidelity check on the file you produced |

Rules for choosing layouts:

- Reference layouts by their catalog key (`M1-L36`) or exact name; never invent names.
- Keep all content slides on the **preferred content master** reported by the summary for a consistent look.
  Use a cover layout only for an opening slide and a closing ("Thank you") layout only at the end.
- Match the layout to the content shape: N text blocks -> an N-column layout; a photo -> a layout with a picture slot;
  a table or chart -> a layout with a large `content` slot or a title-only layout with free canvas.
- Placeholders are addressed by **idx** or **slot name** (`title`, `subtitle`, `heading1`, `body1`, `picture1`,
  `content1`, `number1`, `caption1`). Slot names follow the visual reading order (left to right, top to bottom).
- Picture slots marked "(text ok)" may hold a short label (number or keyword) when no image file exists.

---

## Placeholders First, Extras Second

1. Fill the template placeholders: title, subtitle, headings, bodies, picture slots.
2. Only then add extra shapes (cards, stat tiles, icon circles, tables, charts) - in free space, inside the slide,
   never covering filled placeholders.
3. Unused placeholders are removed automatically so no "Click to edit" prompts remain; a missing title or subtitle
   placeholder falls back to a brand-styled text box.
4. Do **not** draw your own title, subtitle or section-label text boxes when the layout provides placeholders -
   the layouts already position the title, subtitle, logo and footer.

### Content density

- Respect the `approx_chars` / `approx_lines` capacity hints of the layout in your brief.
- Bullets: 3-5 per block, <= ~90 characters each, sentence case, no trailing periods.
- Headings: 2-5 words. Titles: one line (<= ~60 characters). Subtitles: <= ~90 characters.
- Text that does not fit is shrunk automatically - placeholders never below 10 pt, component
  text (text boxes, cards, shape labels) along the type scale and never below 11 pt. Shorten it
  instead of relying on shrinking; what cannot fit readably goes into the speaker notes.
- Use `**bold**` inside a bullet to emphasise the lead phrase.

---

## Brand Palette

Theme accent colours are read from the template; the names below are available everywhere a colour is accepted.

| Name | Hex | Use |
|---|---|---|
| `red` | `#CD163F` | Rockwell Red - accents, icon circles, card accent bars (never large fills) |
| `red_dark` | `#A8112F` | Tags, chips |
| `navy` | `#1B2A4A` | Headings, table headers, dark fills |
| `blue` | `#003E7E` | Theme accent 2 - secondary series, shapes |
| `orange` | `#F58024` | Theme accent 3 |
| `light_blue` | `#00AEEF` | Theme accent 4 |
| `yellow` | `#F9C20A` | Theme accent 5 |
| `green` | `#77C14C` | Theme accent 6 |
| `body` | `#5A6470` | Body text |
| `caption` | `#8A929E` | Captions, footnotes |
| `border` | `#D8DBDF` | Card borders, table rules, divider lines |
| `surface` | `#F6F7F8` | Light tile backgrounds |
| `white` / `black` | `#FFFFFF` / `#000000` | |
| `red_light` | `#FEF2F2` | Callout backgrounds |

**Pass the NAME, never the hex.** The hex column is there so you know what a token looks like; a hex
literal anywhere in your program is rejected before it runs, and the theme tokens (`accent1`..`accent6`,
`dk1`, `lt1`, `dk2`, `lt2`) are accepted as names too.

### Font

The template theme font is **Barlow** (layout placeholders use *Barlow Light*). Text placed in placeholders inherits
the template formatting - do not override fonts. Extra text boxes use the theme body font automatically.

---

## Components (DeckBuilder helpers)

Your program is written against `DeckBuilder` and nothing else. Start it with
`from deck_builder import DeckBuilder`; the sandbox puts that module beside your
program. Never construct `Presentation(...)` yourself, never call `.shapes.add_*`
directly, and never write a hex colour — all three are rejected before your program
runs.

```
s.add_card(title, body_or_bullets, left, top, width, height, accent="red")
   Rounded rectangle, white fill, thin border, short accent bar, bold navy title (14 pt), body 11 pt.
   The title box grows to fit a wrapped title and the body starts below it, so size the card
   for both (s.text_height helps).
s.add_stat(value, label, left, top, width, height, color="red", fill=None)
   Big number (32 pt bold) with a caption - KPIs and key figures
s.add_icon_circle("01", left, top, diameter=0.8, fill="red", color="white")
   Filled circle with a number/keyword - solid icon substitute (Rockwell style prefers filled, not outlined)
s.add_shape("chevron|rounded_rectangle|oval|right_arrow|pentagon|diamond|hexagon", left, top, width, height,
            fill="red", text="Step", color="white")
   Process steps, callouts, tags
s.add_table(columns, rows, left, top, width, height)
   Navy header with white uppercase 11 pt bold text, white body cells, thin bottom rules, no zebra striping
s.add_chart("column|bar|line|pie|doughnut|stacked_column|area", categories, series, left, top, width, height)
   Native chart in brand colours (red, blue, orange, light blue, green ...), light gridlines, legend at the bottom
s.add_textbox(text, left, top, width, height, size=12, color="body", align="left")
s.add_line(left, top, width, height, color="border")
s.add_image(path, left, top, width, height)
s.add_choice("A", option_text, left, top, width, height, correct=False, accent=None)
   One quiz answer option: rounded card, big bold letter, option text. Use it for EVERY option of a
   QUIZ slide and pass correct=True on the right one. All choices look identical on the question
   slide; the answer slide re-runs your program and only then is the correct card highlighted.
```

Placeholder API: `s.set_title`, `s.set_subtitle`, `s.set_text`, `s.set_bullets`, `s.set_picture`,
`s.set_table`, `s.set_chart`, `s.set_notes`. `set_picture` crops an image to its slot only when
little is lost; otherwise it shows the whole image at its own aspect ratio inside the slot.

Composition helpers (pure arithmetic, no shapes drawn):

```
s.text_height(text, width, size=12, bullets=False, bold=False) -> inches the text needs
s.fits(text, width, height, size=12, bullets=False, bold=False) -> True when it fits
s.split(rect, 3, axis="row", gap=0.2)        -> 3 side-by-side rects that never overlap
s.split(rect, [2, 1], axis="column", gap=0.2) -> a 2:1 vertical stack
```

Size every text-bearing box with `s.text_height` or check it with `s.fits` before placing it,
and divide space with `s.split` (or the grid) instead of hand-picked coordinates.

Guidelines: same size for cards in a row; 0.15-0.25" gaps; leave the footer band (bottom ~0.45") free;
left-align paragraphs; centre text only inside shapes, circles and stat tiles.

---

## Slide Structure Patterns

### Cover
Use a template cover layout (`cover` tag). Title in the title placeholder; presenter, team and date in the
caption/subtitle placeholder; a photo only when a real image file is available (otherwise pick the no-photo cover).

### Content slide
Title + subtitle placeholders, then the layout's headings/bodies/pictures. Add a takeaway element (a card or a
stat tile) only when the content carries a takeaway - never as filler for free space.

### Comparison / grouped points
2-, 3-, 4- or 5-column layouts: one heading + one body per column, parallel wording, equal density.

### Process / timeline
Process or timeline layouts (numbered circles, arrows, stages); one short phrase per step, optional chevrons.

### Data
Table or chart in a large `content` slot or on a title-only layout; one message per chart, data labels for few points.

### Section divider / closing
Section layouts (short title, optional subtitle, dark or photo backgrounds) and the template "Thank you" layout
(fixed text and logo already on the layout - add nothing).

---

## Speaker Notes

Put explanatory text, sources and caveats from the brief into `notes`, not on the slide.

---

## Composition Modes

Every slide is built in one of two modes, and the mode is assigned to you — do not
change it.

**`layout` mode** — fill a real template layout's placeholders. This is correct
whenever the content maps onto a layout that already exists: three text columns, a five
item agenda, a photo with text, headshots. Use `set_title`, `set_subtitle`, `set_text`,
`set_bullets`, `set_picture`, `set_table`, `set_chart`. Add components only in space the
layout leaves genuinely free.

**`canvas` mode** — you were given a `canvas_capable` layout and the free region below
its title. Compose that region yourself: charts, tables, cards, composed diagrams,
brand shapes. You still sit on a real master, so the theme, Barlow, the background
treatment and the master's decorative graphics are inherited — you are composing inside
a branded frame, not on a blank page.

### The grid

The content region carries a **12 column x 6 row** grid with fixed 0.15in gutters. Use
`slide.grid(col, row, cols, rows)` to get `(left, top, width, height)` in inches for a
block. Two adjacent grid cells never overlap, which is the cheapest way to satisfy the
geometry rules:

    left, top, width, height = slide.grid(0, 1, cols=4, rows=2)
    slide.add_card("Preheat", ["Ramp under 3 C/s"], left, top, width, height)

Place things on the grid unless you have a reason not to. Hand-picked coordinates are
allowed, and are where collisions come from.

---

## Geometry Rules

Your slide is reopened and measured after it is built. These are checked exactly, by
rectangle arithmetic, and a failure sends the program back to you:

1. Every element sits inside the canvas, 0.30in from every edge.
2. **No text overlaps other text. There is no exception to this one.**
3. Text over a shape must sit *wholly inside* that shape. A label inside a card is
   correct; a label hanging off its card's edge is not.
4. Two filled shapes may nest or stand apart, never partly overlap.
5. Nothing may touch a reserved zone: the footer band, the classification mark, or a
   graphic the layout already draws. `get_layout_details` lists them.
6. Nothing may be drawn over a filled placeholder. Fill it through the placeholder API
   instead.
7. No "Click to edit" or "Second level" prompt text may remain anywhere.
8. Every colour is a brand token. Never a hex literal.
9. Every font is the theme font, at a size on the type scale below.
10. Text fits its box at the size you set (rule 10 measures it), and a table fits its frame
    once its rows grow to fit their text.
11. No empty text boxes.
12. No line of text appears twice on the slide (no title restated in a text box).
13. Each image appears once.
14. No image is stretched or squeezed off its own aspect ratio.

Aim for these while you arrange, rather than discovering them afterwards: size boxes with
`s.text_height` / `s.fits` and divide space with `s.split` or the grid before you place
anything. Decorative elements that carry no content are counted; a slide with more of them
than content is sent back to be simplified.

---

## The Classification Mark Is Not Yours To Draw

Every slide carries a classification label, its policy symbol, the slide number and —
where the layout does not already have one — the brand mark. All of it is stamped by
`finalize()`, from one code path, in a reserved band along the bottom of the slide.

Do not draw a classification label, a footer, a slide number or a logo. Do not place
anything in the bottom 0.45in of the slide. Rule 5 rejects it.

---

## Type Scale

Placeholder text inherits the template's own sizes and is correct by definition — do
not override it. Text you add through a component must use one of these sizes, and
nothing else:

| Size | Use |
|---|---|
| 32 pt | stat value |
| 28 pt | title (only where a layout has no title placeholder) |
| 20 pt | heading |
| 15 pt | body |
| 14 pt | card title |
| 12 pt | shape label |
| 11 pt | table cell, caption |
| 10 pt | floor — never smaller |

---

## Common Mistakes to Avoid

- Building slides on a blank layout with hand-drawn titles when a template layout exists
- Using Rockwell Red (`red`) for large background fills (use Navy or the template's dark layouts instead)
- Underlines or decorative lines below titles
- Body text in pure black (use the `body` token)
- Leaving template prompt text ("Click to edit ...") or empty placeholders behind
- Overflowing placeholders instead of shortening text
- Inconsistent card sizing across a row; elements outside the 13.33" x 7.5" canvas or over the footer
- Mixing layouts from different masters in one deck
- Mixing Metis colours with Rockwell colours
- Using outlined icons (Rockwell style prefers filled/solid shapes)
- Centring body text (left-align paragraphs)
- Referencing image files that do not exist on disk
- Drawing your own footer, slide number, logo or classification label (all are stamped for you)
- Hand-picking coordinates where `slide.grid(...)` or `slide.split(...)` would do
- Writing a hex colour instead of a brand token name
- Decorative boxes, bars, circles or lines that carry no content; a box around a single line of text
- Invented labels or headings ("Overview", "Key takeaway") the content does not have
- The same image on two slides, or twice on one; an image squeezed into a text slot

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
(several masters, hundreds of layouts, many with identical names). Always work from the catalog tools:

| Tool | Use it to |
|---|---|
| `get_template_summary` | Slide size, theme fonts/colours, masters, the preferred content master, every unique layout with its slot summary |
| `search_layouts` | Find layouts by keywords / tags (`cover`, `agenda`, `section`, `columns`, `3-column`, `process`, `dashboard`, `photo`, `title-only`, `closing`, ...) |
| `recommend_layouts` | Rank layouts for a slide intent + content shape (number of text blocks, picture, table, chart) |
| `get_layout_details` | Placeholders in reading order with `idx`, `slot` name, role, geometry, font size and capacity - **required before using a layout** |
| `submit_presentation_plan` | Validate the structured plan against the catalog (layout keys, placeholder targets, text fit, geometry) |

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

- Respect the `approx_chars` / `approx_lines` capacity hints from `get_layout_details`.
- Bullets: 3-5 per block, <= ~90 characters each, sentence case, no trailing periods.
- Headings: 2-5 words. Titles: one line (<= ~60 characters). Subtitles: <= ~90 characters.
- Text that does not fit is shrunk automatically (never below 10 pt) - shorten it instead of relying on shrinking.
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

Hex values (`#RRGGBB`) are accepted too.

### Font

The template theme font is **Barlow** (layout placeholders use *Barlow Light*). Text placed in placeholders inherits
the template formatting - do not override fonts. Extra text boxes use the theme body font automatically.

---

## Components (DeckBuilder helpers)

```
s.add_card(title, body_or_bullets, left, top, width, height, accent="red")
   Rounded rectangle, white fill, thin border, short accent bar, bold navy title (14 pt), body 11 pt
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
```

Guidelines: same size for cards in a row; 0.15-0.25" gaps; leave the footer band (bottom ~0.4") free;
left-align paragraphs; centre text only inside shapes, circles and stat tiles.

---

## Slide Structure Patterns

### Cover
Use a template cover layout (`cover` tag). Title in the title placeholder; presenter, team and date in the
caption/subtitle placeholder; a photo only when a real image file is available (otherwise pick the no-photo cover).

### Content slide
Title + subtitle placeholders, then the layout's headings/bodies/pictures. Optional key-takeaway card or stat tile
in the free band above the footer when the layout leaves room.

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

## Common Mistakes to Avoid

- Building slides on a blank layout with hand-drawn titles when a template layout exists
- Using Rockwell Red (`#CD163F`) for large background fills (use Navy or the template's dark layouts instead)
- Underlines or decorative lines below titles
- Body text in pure black (use `#5A6470`)
- Leaving template prompt text ("Click to edit ...") or empty placeholders behind
- Overflowing placeholders instead of shortening text
- Inconsistent card sizing across a row; elements outside the 13.33" x 7.5" canvas or over the footer
- Mixing layouts from different masters in one deck
- Mixing Metis colours with Rockwell colours
- Using outlined icons (Rockwell style prefers filled/solid shapes)
- Centring body text (left-align paragraphs)
- Referencing image files that do not exist on disk

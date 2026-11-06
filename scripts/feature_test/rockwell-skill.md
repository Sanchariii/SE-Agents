# Rockwell Automation: Presentation Guide (PowerPoint)

Use this reference when creating or reviewing a PowerPoint deck, branded as Rockwell Automation.

---

## Slide Dimensions

- **Widescreen 16:9**: 13.33" x 7.50" (default)
- All measurements below assume this size

---

## Available Layouts (from Rockwell PPTX template)

| Layout Index | Name | Use For |
|---|---|---|
| 0 | Cover 1 | Title slide with right-side photo |
| 1 | Cover 2 | Alternate cover layout |
| 2 | Cover 3 | Alternate cover layout |
| 3 | Cover 4 | Alternate cover layout |
| 4 | 1_Title Only 1 | Content slides (most common) |
| 5 | 2_1 Column A | Single-column content |
| 6 | Title Only | Simple title slide |
| 7 | Title and Content | Standard content with placeholder |
| 9 | Gray Dots Blank | Decorative blank |
| 11 | 2 Column D | Two-column layout |
| 12 | Blank | Empty slide |
| 13 | 3 Column A | Three-column layout |

---

## Standard Slide Furniture

### Section Label (top-left, every content slide)
- Font: Barlow, 12pt, Bold
- Color: Rockwell Red `#CD163F`
- Transform: UPPERCASE
- Position: top-left of content area
- Examples: "INTRO", "SCENARIOS", "DEMO PREP", "ARCHITECTURE"

### Slide Title
- Font: Barlow, 32pt, Bold
- Color: Navy `#1B2A4A`
- Position: below section label
- Left-aligned, no underline, no decorative line below

### Slide Subtitle
- Font: Barlow, 14.5pt, Regular
- Color: Body `#5A6470`
- Position: below title
- Max width ~65 characters

---

## Slide Structure Patterns

### Cover Slide
```
┌──────────────────┬──────────────────┐
│                  │                  │
│  [Logo]          │                  │
│                  │   [Full-bleed    │
│  TITLE           │    cover photo]  │
│  32pt+ Bold      │                  │
│                  │                  │
│  Location | Date │                  │
│                  │                  │
└──────────────────┴──────────────────┘
```
- Left half: white or Navy background
- Right half: placeholder for image (photo bleeds to edges)
- Title: large, bold
- Subtitle: location, date, event

### Content Slide (most common)
```
┌─────────────────────────────────────┐
│  SECTION LABEL          (red, 12pt) │
│  Slide Title            (navy, 32pt)│
│  Subtitle               (gray, 14pt)│
│                                     │
│  ┌─────────┐ ┌─────────┐ ┌───────┐ │
│  │  Card 1 │ │  Card 2 │ │Card 3 │ │
│  │         │ │         │ │       │ │
│  └─────────┘ └─────────┘ └───────┘ │
│                                     │
└─────────────────────────────────────┘
```

### Card Component
```
┌─────────────────────────┐
│  CARD TITLE              │  ← Barlow 14-17pt Bold, Red #CD163F or Navy
│  Subtitle/heading        │  ← Barlow 12.5pt Bold, Navy #1B2A4A
│  Body text here          │  ← Barlow 11pt Regular, Body #5A6470
│                          │
│  Connects:  Label        │  ← 10.5pt Bold Red + Regular Caption gray
└─────────────────────────┘
```
- Rounded Rectangle shape (corner radius ~8-12px)
- White or very light fill
- Optional: thin border `#D8DBDF`

---

## Programmatic Implementation (python-pptx)

### Color Constants
```python
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor

ROK = {
    "red":        RGBColor(0xCD, 0x16, 0x3F),  # Rockwell Red
    "red_dark":   RGBColor(0xA8, 0x11, 0x2F),  # Tags, chips
    "navy":       RGBColor(0x1B, 0x2A, 0x4A),  # Headings, dark bg
    "body":       RGBColor(0x5A, 0x64, 0x70),  # Body text
    "caption":    RGBColor(0x8A, 0x92, 0x9E),  # Captions
    "border":     RGBColor(0xD8, 0xDB, 0xDF),  # Borders
    "surface":    RGBColor(0xF6, 0xF7, 0xF8),  # Light bg
    "white":      RGBColor(0xFF, 0xFF, 0xFF),
    "red_light":  RGBColor(0xFE, 0xF2, 0xF2),  # Callout bg
}

# Hex strings (for fill operations that need string format)
ROK_HEX = {
    "red":       "CD163F",
    "red_dark":  "A8112F",
    "navy":      "1B2A4A",
    "body":      "5A6470",
    "caption":   "8A929E",
    "border":    "D8DBDF",
    "surface":   "F6F7F8",
    "white":     "FFFFFF",
    "red_light": "FEF2F2",
}
```

### Font
Use `"Barlow"` as the font face. Fallback: `"Segoe UI"` if Barlow unavailable.

### Adding a Section Label
```python
from pptx.util import Inches, Pt

def add_section_label(slide, text, left=0.7, top=0.5):
    txBox = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(3), Inches(0.3))
    tf = txBox.text_frame
    p = tf.paragraphs[0]
    run = p.add_run()
    run.text = text.upper()
    run.font.name = "Barlow"
    run.font.size = Pt(12)
    run.font.bold = True
    run.font.color.rgb = ROK["red"]
```

### Adding a Slide Title
```python
def add_slide_title(slide, text, left=0.7, top=0.85):
    txBox = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(11), Inches(0.6))
    tf = txBox.text_frame
    p = tf.paragraphs[0]
    run = p.add_run()
    run.text = text
    run.font.name = "Barlow"
    run.font.size = Pt(32)
    run.font.bold = True
    run.font.color.rgb = ROK["navy"]
```

### Adding a Subtitle
```python
def add_subtitle(slide, text, left=0.7, top=1.5):
    txBox = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(10), Inches(0.4))
    tf = txBox.text_frame
    p = tf.paragraphs[0]
    run = p.add_run()
    run.text = text
    run.font.name = "Barlow"
    run.font.size = Pt(14)
    run.font.color.rgb = ROK["body"]
```

### Adding a Card
```python
from pptx.enum.shapes import MSO_SHAPE

def add_card(slide, title, body, x, y, w=3.5, h=2.5):
    # Card background
    shape = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h)
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = ROK["white"]
    shape.line.color.rgb = ROK["border"]
    shape.line.width = Pt(1)

    # Title
    txBox = slide.shapes.add_textbox(
        Inches(x + 0.2), Inches(y + 0.2), Inches(w - 0.4), Inches(0.4)
    )
    tf = txBox.text_frame
    p = tf.paragraphs[0]
    run = p.add_run()
    run.text = title
    run.font.name = "Barlow"
    run.font.size = Pt(14)
    run.font.bold = True
    run.font.color.rgb = ROK["navy"]

    # Body
    txBox2 = slide.shapes.add_textbox(
        Inches(x + 0.2), Inches(y + 0.65), Inches(w - 0.4), Inches(h - 0.85)
    )
    tf2 = txBox2.text_frame
    tf2.word_wrap = True
    p2 = tf2.paragraphs[0]
    run2 = p2.add_run()
    run2.text = body
    run2.font.name = "Barlow"
    run2.font.size = Pt(11)
    run2.font.color.rgb = ROK["body"]
```

---

## Table Style

```python
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor

# Table header: Navy bg, white text, 11pt Bold uppercase
# Body cells: white bg, border-bottom #EBEDF0
# Cell padding: ~0.08" horizontal
# No zebra striping
```

---

## Logo Placement

### Cover Slide
- `assets/logo_horizontal.png` on light backgrounds
- `assets/logo_horizontal_white.png` on dark/Navy backgrounds
- Position: top-left, approx x=0.5", y=0.4", height ~0.45"

### Closing Slide
- White logo variant on Navy background
- Tagline (`assets/tagline_white.png`) centered below logo
- Or just logo + "Thank you" in white

---

## Common Mistakes to Avoid

- Using Rockwell Red (`#CD163F`) for large background fills (use Navy instead)
- Underlines or decorative lines below titles
- Body text in pure black (use `#5A6470`)
- Missing section labels on content slides
- Inconsistent card sizing across a row
- Mixing Metis colors with Rockwell colors
- Using outlined icons (Rockwell style prefers filled/solid)
- Centering body text (left-align paragraphs)

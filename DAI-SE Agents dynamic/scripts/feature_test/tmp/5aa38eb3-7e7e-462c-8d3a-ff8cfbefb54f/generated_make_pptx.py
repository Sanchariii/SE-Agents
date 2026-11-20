#!/usr/bin/env python3
"""
Create an enriched Rockwell Automation branded slide 5:
"Security Best Practices & Key Takeaways"
with 5 columns, circle icon headings, polished cards, and visual appeal.
"""

import os, math
from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE, MSO_CONNECTOR_TYPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.oxml.ns import qn
import lxml.etree as etree

# -- Rockwell colour palette --------------------------------------------------
ROK = {
    "red":        RGBColor(0xCD, 0x16, 0x3F),
    "red_dark":   RGBColor(0xA8, 0x11, 0x2F),
    "navy":       RGBColor(0x1B, 0x2A, 0x4A),
    "body":       RGBColor(0x5A, 0x64, 0x70),
    "caption":    RGBColor(0x8A, 0x92, 0x9E),
    "border":     RGBColor(0xD8, 0xDB, 0xDF),
    "surface":    RGBColor(0xF6, 0xF7, 0xF8),
    "white":      RGBColor(0xFF, 0xFF, 0xFF),
    "red_light":  RGBColor(0xFE, 0xF2, 0xF2),
    "navy_light": RGBColor(0xE8, 0xEB, 0xF0),
    "green":      RGBColor(0x00, 0x8A, 0x52),
    "amber":      RGBColor(0xE8, 0x8C, 0x00),
    "teal":       RGBColor(0x00, 0x7B, 0x8A),
    "purple":     RGBColor(0x6B, 0x3F, 0xA0),
    "blue":       RGBColor(0x1A, 0x73, 0xE8),
}

# Column accent colors for each of the 5 practice areas
COL_ACCENTS = [ROK["red"], ROK["blue"], ROK["teal"], ROK["purple"], ROK["amber"]]
COL_LIGHT_BG = [
    RGBColor(0xFE, 0xF2, 0xF2),   # light red
    RGBColor(0xEA, 0xF2, 0xFD),   # light blue
    RGBColor(0xE6, 0xF6, 0xF7),   # light teal
    RGBColor(0xF3, 0xED, 0xFA),   # light purple
    RGBColor(0xFE, 0xF7, 0xE6),   # light amber
]

FONT = "Barlow"
SLD_W = Inches(13.33)
SLD_H = Inches(7.50)

# -- Presentation setup -------------------------------------------------------
prs = Presentation()
prs.slide_width = SLD_W
prs.slide_height = SLD_H

# Use blank layout
blank_layout = prs.slide_layouts[6]  # Blank
slide = prs.slides.add_slide(blank_layout)


# -- Helper functions ---------------------------------------------------------
def set_shape_shadow(shape, blur=Pt(6), dist=Pt(2), alpha=25):
    """Add a soft drop shadow to a shape via XML manipulation."""
    spPr = shape._element.find(qn('a:spPr'))
    if spPr is None:
        spPr = shape._element.find('.//' + qn('p:spPr'))
    if spPr is None:
        return
    effectLst = spPr.find(qn('a:effectLst'))
    if effectLst is None:
        effectLst = etree.SubElement(spPr, qn('a:effectLst'))
    outerShdw = etree.SubElement(effectLst, qn('a:outerShdw'))
    outerShdw.set('blurRad', str(int(blur)))
    outerShdw.set('dist', str(int(dist)))
    outerShdw.set('dir', '5400000')
    outerShdw.set('rotWithShape', '0')
    srgbClr = etree.SubElement(outerShdw, qn('a:srgbClr'))
    srgbClr.set('val', '000000')
    alphaEl = etree.SubElement(srgbClr, qn('a:alpha'))
    alphaEl.set('val', str(alpha * 1000))


def add_text_box(slide, text, x, y, w, h, font_name=FONT, font_size=Pt(11),
                 bold=False, color=ROK["body"], align=PP_ALIGN.LEFT,
                 word_wrap=True, line_spacing=None):
    """Add a formatted text box and return (shape, text_frame)."""
    txBox = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = txBox.text_frame
    tf.word_wrap = word_wrap
    p = tf.paragraphs[0]
    p.alignment = align
    if line_spacing:
        p.line_spacing = line_spacing
    run = p.add_run()
    run.text = text
    run.font.name = font_name
    run.font.size = font_size
    run.font.bold = bold
    run.font.color.rgb = color
    return txBox, tf


def add_bullet_list(slide, bullets, x, y, w, h, font_size=Pt(10.5),
                    color=ROK["body"], bullet_color=None, spacing=Pt(6)):
    """Add a multi-paragraph bulleted text box."""
    txBox = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = txBox.text_frame
    tf.word_wrap = True
    for i, bullet_text in enumerate(bullets):
        if i == 0:
            p = tf.paragraphs[0]
        else:
            p = tf.add_paragraph()
        p.alignment = PP_ALIGN.LEFT
        p.space_after = spacing
        p.level = 0

        # Add bullet character (using a simple ASCII-safe bullet)
        run_bullet = p.add_run()
        run_bullet.text = "\u2022  "  # standard bullet character
        run_bullet.font.name = FONT
        run_bullet.font.size = Pt(7)
        run_bullet.font.color.rgb = bullet_color if bullet_color else color

        run = p.add_run()
        run.text = bullet_text
        run.font.name = FONT
        run.font.size = font_size
        run.font.color.rgb = color
    return txBox, tf


def set_vertical_center(shape):
    """Set vertical text anchoring to middle for a shape."""
    txBody = shape._element.find(qn('p:txBody'))
    if txBody is None:
        txBody = shape._element.find('.//' + qn('a:txBody'))
    if txBody is not None:
        bodyPr = txBody.find(qn('a:bodyPr'))
        if bodyPr is not None:
            bodyPr.set('anchor', 'ctr')


# -- 1. Background: white fill ------------------------------------------------
bg = slide.background
fill = bg.fill
fill.solid()
fill.fore_color.rgb = ROK["white"]

# -- 2. Top accent bar (thin red line at very top) ----------------------------
bar = slide.shapes.add_shape(
    MSO_SHAPE.RECTANGLE, Inches(0), Inches(0), SLD_W, Inches(0.06)
)
bar.fill.solid()
bar.fill.fore_color.rgb = ROK["red"]
bar.line.fill.background()

# -- 3. Section label ----------------------------------------------------------
add_text_box(slide, "SECURITY & GOVERNANCE", 0.7, 0.30, 4, 0.3,
             font_size=Pt(12), bold=True, color=ROK["red"])

# -- 4. Slide title ------------------------------------------------------------
add_text_box(slide, "Security Best Practices & Key Takeaways", 0.7, 0.68, 11, 0.65,
             font_size=Pt(32), bold=True, color=ROK["navy"])

# -- 5. Subtitle ---------------------------------------------------------------
add_text_box(slide, "Follow these practices on every integration with the ROK AI Gateway",
             0.7, 1.35, 10, 0.35, font_size=Pt(14.5), color=ROK["body"])

# -- 6. Decorative horizontal divider line below subtitle ----------------------
divider = slide.shapes.add_shape(
    MSO_SHAPE.RECTANGLE, Inches(0.7), Inches(1.82), Inches(11.93), Inches(0.015)
)
divider.fill.solid()
divider.fill.fore_color.rgb = ROK["border"]
divider.line.fill.background()


# -- 7. Five columns with circle headings + cards -----------------------------

# Column data
columns = [
    {
        "number": "01",
        "heading": "Secure Secrets",
        "bullets": [
            "Never hard-code client secrets or subscription keys",
            "Store all credentials in a secure vault (e.g., Azure Key Vault)",
            "Never commit secrets to source control or documentation"
        ]
    },
    {
        "number": "02",
        "heading": "Rotate Credentials",
        "bullets": [
            "Rotate client secrets before expiration date",
            "Prevents service disruption from expired tokens",
            "Re-issue APIM subscription keys on a regular schedule"
        ]
    },
    {
        "number": "03",
        "heading": "Handle Failures",
        "bullets": [
            "Implement retry logic for HTTP 429 responses",
            "Handle transient failures gracefully with back-off",
            "Respect gateway quota and rate-limit headers"
        ]
    },
    {
        "number": "04",
        "heading": "Audit & Trace",
        "bullets": [
            "Log request IDs and response IDs on every call",
            "Enables full traceability and debugging capability",
            "Required for incident investigation & compliance"
        ]
    },
    {
        "number": "05",
        "heading": "Target Correctly",
        "bullets": [
            "Always target the correct environment endpoint",
            "Non-prod and prod scopes are strictly separate",
            "Misconfigured endpoints risk data leakage"
        ]
    },
]

# Layout calculations
margin_left = 0.50
margin_right = 0.50
col_gap = 0.25
usable_w = 13.33 - margin_left - margin_right
col_count = 5
card_w = (usable_w - (col_count - 1) * col_gap) / col_count
card_top = 2.15
circle_size = 0.90
card_body_top = card_top + circle_size / 2 + 0.05
card_h = 4.55

for i, col in enumerate(columns):
    cx = margin_left + i * (card_w + col_gap)
    accent = COL_ACCENTS[i]
    light_bg = COL_LIGHT_BG[i]

    # -- Card rounded rectangle -----------------------------------------------
    card = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE,
        Inches(cx), Inches(card_body_top),
        Inches(card_w), Inches(card_h)
    )
    card.fill.solid()
    card.fill.fore_color.rgb = ROK["white"]
    card.line.color.rgb = ROK["border"]
    card.line.width = Pt(0.75)
    # Adjust corner radius
    card.adjustments[0] = 0.04
    set_shape_shadow(card, blur=Pt(8), dist=Pt(3), alpha=12)

    # -- Top accent stripe on card --------------------------------------------
    stripe = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        Inches(cx), Inches(card_body_top),
        Inches(card_w), Inches(0.06)
    )
    stripe.fill.solid()
    stripe.fill.fore_color.rgb = accent
    stripe.line.fill.background()

    # -- Circle with number ---------------------------------------------------
    circle_x = cx + (card_w - circle_size) / 2
    circle_y = card_top
    circle = slide.shapes.add_shape(
        MSO_SHAPE.OVAL,
        Inches(circle_x), Inches(circle_y),
        Inches(circle_size), Inches(circle_size)
    )
    circle.fill.solid()
    circle.fill.fore_color.rgb = accent
    circle.line.fill.background()
    set_shape_shadow(circle, blur=Pt(6), dist=Pt(2), alpha=20)

    # Number text inside circle
    tf = circle.text_frame
    tf.word_wrap = False
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    run = p.add_run()
    run.text = col["number"]
    run.font.name = FONT
    run.font.size = Pt(22)
    run.font.bold = True
    run.font.color.rgb = ROK["white"]
    p.space_before = Pt(0)
    p.space_after = Pt(0)
    set_vertical_center(circle)

    # -- Column heading -------------------------------------------------------
    heading_y = card_body_top + 0.50
    add_text_box(slide, col["heading"],
                 cx + 0.15, heading_y, card_w - 0.30, 0.4,
                 font_size=Pt(15), bold=True, color=ROK["navy"],
                 align=PP_ALIGN.CENTER)

    # -- Small accent underline below heading ---------------------------------
    underline_w = 0.6
    underline_x = cx + (card_w - underline_w) / 2
    underline = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        Inches(underline_x), Inches(heading_y + 0.42),
        Inches(underline_w), Inches(0.025)
    )
    underline.fill.solid()
    underline.fill.fore_color.rgb = accent
    underline.line.fill.background()

    # -- Bullet points --------------------------------------------------------
    bullet_y = heading_y + 0.62
    bullet_h = card_h - 1.2
    add_bullet_list(slide, col["bullets"],
                    cx + 0.20, bullet_y, card_w - 0.40, bullet_h,
                    font_size=Pt(10), color=ROK["body"],
                    bullet_color=accent, spacing=Pt(8))

    # -- Bottom accent bar inside card ----------------------------------------
    bottom_bar_y = card_body_top + card_h - 0.08
    bottom_bar = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        Inches(cx + 0.15), Inches(bottom_bar_y),
        Inches(card_w - 0.30), Inches(0.03)
    )
    bottom_bar.fill.solid()
    bottom_bar.fill.fore_color.rgb = light_bg
    bottom_bar.line.fill.background()


# -- 8. Bottom call-out banner ------------------------------------------------
banner_y = 7.00
banner_h = 0.38
banner = slide.shapes.add_shape(
    MSO_SHAPE.ROUNDED_RECTANGLE,
    Inches(0.70), Inches(banner_y),
    Inches(11.93), Inches(banner_h)
)
banner.fill.solid()
banner.fill.fore_color.rgb = RGBColor(0xFE, 0xF2, 0xF2)
banner.line.fill.background()
banner.adjustments[0] = 0.15

# Banner text (using ASCII-safe warning indicator)
add_text_box(
    slide,
    "/!\\  All concrete values in documentation are examples -- real secrets and keys must never appear in code, docs, or source control.",
    1.0, banner_y + 0.03, 11.3, banner_h - 0.06,
    font_size=Pt(11), bold=False, color=ROK["red_dark"],
    align=PP_ALIGN.CENTER
)


# -- 9. Footer / bottom-right page number badge ------------------------------
page_badge = slide.shapes.add_shape(
    MSO_SHAPE.ROUNDED_RECTANGLE,
    Inches(12.55), Inches(0.28), Inches(0.55), Inches(0.35)
)
page_badge.fill.solid()
page_badge.fill.fore_color.rgb = ROK["navy"]
page_badge.line.fill.background()
page_badge.adjustments[0] = 0.25
tf = page_badge.text_frame
tf.word_wrap = False
p = tf.paragraphs[0]
p.alignment = PP_ALIGN.CENTER
run = p.add_run()
run.text = "05"
run.font.name = FONT
run.font.size = Pt(13)
run.font.bold = True
run.font.color.rgb = ROK["white"]
set_vertical_center(page_badge)


# -- 10. Add speaker notes ---------------------------------------------------
notes_slide = slide.notes_slide
notes_tf = notes_slide.notes_text_frame
notes_tf.text = (
    "The ROK AI Gateway Engineering Guide explicitly states: never hard-code secrets or tokens, "
    "rotate secrets before expiration, handle 429s and transient failures, and log request and "
    "response IDs for traceability. All concrete values in the guide are examples or placeholders "
    "-- real client secrets and subscription keys must never appear in documentation or source "
    "control. Non-prod and prod environments use separate APIM scopes, so targeting the wrong "
    "endpoint is a governance and security risk. These five practices apply to every consumer of "
    "the gateway, regardless of application type."
)


# -- Save ---------------------------------------------------------------------
out_path = "C:/Users/AKhodke/OneDrive - Rockwell Automation, Inc/Desktop/SE-Agents/se-agent-backend/scripts/feature_test/tmp/5aa38eb3-7e7e-462c-8d3a-ff8cfbefb54f/slide5_security_best_practices.pptx"
prs.save(out_path)
print("Done - Saved to " + out_path)

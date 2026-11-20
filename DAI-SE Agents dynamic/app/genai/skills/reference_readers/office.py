"""Extract bounded layout and typography metadata from PPTX and PDF files."""

import io
import zipfile
from collections import Counter

from app.config.constants import SkillReferenceConstants as Limits
from app.config.exceptions import AppError
from app.genai.skills.reference_readers.common import (
    check_bytes, color_values, frequent, profile, reference_error,
)


def _archive_bounds(data):
    """Reject archives whose entry count or expanded size is unsafe."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
    except (OSError, zipfile.BadZipFile) as exc:
        reference_error("reference is not a valid PowerPoint file", exc)
    if len(entries) > Limits.MAX_ARCHIVE_ENTRIES:
        reference_error("PowerPoint reference has too many archive entries")
    expanded = sum(entry.file_size for entry in entries)
    if expanded > Limits.MAX_ARCHIVE_BYTES:
        reference_error("PowerPoint reference expands beyond the size limit")


def _rgb(value):
    """Read an explicit RGB value without failing on theme-based colors."""
    try:
        return f"#{value.rgb}" if value.type and value.rgb else ""
    except (AttributeError, TypeError, ValueError):
        return ""


def _shape_style(shape, slide_width, slide_height):
    """Describe one shape's geometry and visual tokens without its text."""
    fonts, sizes, colors = [], [], []
    if getattr(shape, "has_text_frame", False):
        for paragraph in shape.text_frame.paragraphs:
            for run in paragraph.runs:
                font = run.font
                if font.name:
                    fonts.append(font.name)
                if font.size:
                    sizes.append(round(font.size.pt, 1))
                color = _rgb(font.color)
                if color:
                    colors.append(color)
    try:
        fill = _rgb(shape.fill.fore_color)
        if fill:
            colors.append(fill)
    except (AttributeError, TypeError, ValueError):
        pass
    bounds = {
        "x": round(shape.left / slide_width, 3),
        "y": round(shape.top / slide_height, 3),
        "width": round(shape.width / slide_width, 3),
        "height": round(shape.height / slide_height, 3),
    }
    return {
        "kind": str(shape.shape_type),
        "placeholder": bool(getattr(shape, "is_placeholder", False)),
        "bounds": bounds,
        "fonts": fonts[:4],
        "font_sizes_pt": sizes[:6],
        "colors": color_values(colors, 4),
    }, fonts, sizes, colors


def inspect_pptx(data):
    """Return layout, typography, and palette metadata from a bounded PPTX."""
    digest = check_bytes(data)
    _archive_bounds(data)
    try:
        from pptx import Presentation
        deck = Presentation(io.BytesIO(data))
        if len(deck.slides) > Limits.MAX_PPTX_SLIDES:
            raise AppError(f"PowerPoint reference exceeds {Limits.MAX_PPTX_SLIDES} slides")
        if not deck.slide_width or not deck.slide_height:
            raise AppError("PowerPoint reference has invalid slide dimensions")
        fonts, sizes, colors, counts, exemplars = [], [], [], Counter(), []
        width, height = deck.slide_width, deck.slide_height
        for slide_number, slide in enumerate(deck.slides, 1):
            if len(slide.shapes) > Limits.MAX_PPTX_SHAPES_PER_SLIDE:
                raise AppError(f"PowerPoint slide {slide_number} has too many shapes")
            shapes = []
            for shape in slide.shapes:
                style, used_fonts, used_sizes, used_colors = _shape_style(
                    shape, width, height)
                shapes.append(style)
                counts[style["kind"]] += 1
                fonts.extend(used_fonts)
                sizes.extend(used_sizes)
                colors.extend(used_colors)
            if slide_number <= Limits.MAX_EXEMPLARS:
                exemplars.append({"slide": slide_number, "shapes": shapes[:30]})
        metrics = {
            "slide_count": len(deck.slides),
            "aspect_ratio": round(width / height, 3),
            "fonts": frequent(fonts),
            "font_sizes_pt": sorted(set(sizes))[:Limits.MAX_STYLE_ITEMS],
            "colors": color_values(colors),
            "shape_kinds": dict(counts.most_common(Limits.MAX_STYLE_ITEMS)),
        }
        summary = (f"{len(deck.slides)} slides, {width / height:.2f}:1 aspect ratio; "
                   f"common fonts: {', '.join(metrics['fonts'][:4]) or 'theme-defined'}.")
        return profile("pptx", digest, summary, metrics, exemplars)
    except AppError:
        raise
    except Exception as exc:
        reference_error("PowerPoint reference could not be inspected", exc)


def _pdf_page_style(page, page_number, fonts, sizes):
    """Collect a bounded page exemplar and update shared typography metrics."""
    width, height = float(page.mediabox.width), float(page.mediabox.height)
    if width <= 0 or height <= 0:
        raise AppError(f"PDF page {page_number} has invalid dimensions")
    spans = []
    block_count = [0]

    def collect(text, _cm, tm, font_dict, font_size):
        if not text.strip():
            return
        block_count[0] += 1
        font = str((font_dict or {}).get("/BaseFont", "")).lstrip("/")
        if font:
            fonts.append(font)
        if font_size:
            sizes.append(round(float(font_size), 1))
        if len(spans) < 30:
            spans.append({
                "x": round(float(tm[4]) / width, 3),
                "y": round(float(tm[5]) / height, 3),
                "font": font,
                "size_pt": round(float(font_size), 1) if font_size else None,
            })

    page.extract_text(visitor_text=collect)
    return {"aspect_ratio": round(width / height, 3),
            "text_blocks": block_count[0], "text_layout": spans}


def inspect_pdf(data):
    """Return page geometry and typography metadata without retaining PDF text."""
    digest = check_bytes(data)
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data), strict=False)
        if len(reader.pages) > Limits.MAX_PDF_PAGES:
            raise AppError(f"PDF reference exceeds {Limits.MAX_PDF_PAGES} pages")
        fonts, sizes, page_shapes, exemplars = [], [], [], []
        for page_number, page in enumerate(reader.pages, 1):
            page_style = _pdf_page_style(page, page_number, fonts, sizes)
            page_shapes.append({key: page_style[key]
                                for key in ("aspect_ratio", "text_blocks")})
            if page_number <= Limits.MAX_EXEMPLARS:
                exemplars.append({"page": page_number,
                                  "text_layout": page_style["text_layout"]})
        metrics = {
            "page_count": len(reader.pages),
            "aspect_ratios": frequent([str(p["aspect_ratio"]) for p in page_shapes]),
            "fonts": frequent(fonts),
            "font_sizes_pt": sorted(set(sizes))[:Limits.MAX_STYLE_ITEMS],
            "colors": [],
        }
        summary = (f"{len(reader.pages)} pages; common fonts: "
                   f"{', '.join(metrics['fonts'][:4]) or 'not embedded or unavailable'}.")
        return profile("pdf", digest, summary, metrics, exemplars)
    except AppError:
        raise
    except Exception as exc:
        reference_error("PDF reference could not be inspected", exc)

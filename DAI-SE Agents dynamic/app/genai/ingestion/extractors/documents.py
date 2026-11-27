"""Deterministic extractors for text/csv/pdf/docx/xlsx/pptx."""

import csv
import io

from app.core.models.document_model import ExtractedUnit


class TextExtractor:
    name = "text_extractor"
    mime_types = ("text/plain", "text/markdown")

    def extract(self, data, record):
        text = data.decode("utf-8", errors="replace").strip()
        return [ExtractedUnit(text=text, section=record.filename)] if text else []


class CsvExtractor:
    name = "csv_extractor"
    mime_types = ("text/csv",)

    def extract(self, data, record):
        text = data.decode("utf-8-sig", errors="replace")
        rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
        if not rows:
            return []
        table = "\n".join(" | ".join(c.strip() for c in r) for r in rows)
        return [ExtractedUnit(text=table, section=record.filename, kind="table")]


class PdfExtractor:
    name = "pdf_extractor"
    mime_types = ("application/pdf",)

    def extract(self, data, record):
        from pypdf import PdfReader
        units = []
        for i, page in enumerate(PdfReader(io.BytesIO(data)).pages, start=1):
            text = (page.extract_text() or "").strip()
            if text:
                units.append(ExtractedUnit(text=text, section=f"page {i}", page=i))
        return units


class DocxExtractor:
    name = "docx_extractor"
    mime_types = ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",)

    def extract(self, data, record):
        from docx import Document
        units, section, buf = [], "", []

        def flush():
            if buf:
                units.append(ExtractedUnit(text="\n".join(buf), section=section))
                buf.clear()

        doc = Document(io.BytesIO(data))
        for para in doc.paragraphs:
            t = para.text.strip()
            if not t:
                continue
            if para.style.name.startswith("Heading"):
                flush(); section = t
            else:
                buf.append(t)
        flush()
        for i, table in enumerate(doc.tables, start=1):
            rows = [" | ".join(c.text.strip() for c in row.cells) for row in table.rows]
            table_text = "\n".join(r for r in rows if r.strip(" |"))
            if table_text:
                units.append(ExtractedUnit(text=table_text, section=f"table {i}", kind="table"))
        return units


class XlsxExtractor:
    name = "xlsx_extractor"
    mime_types = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",)

    def extract(self, data, record):
        from openpyxl import load_workbook
        units = []
        wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        for sheet in wb.worksheets:
            rows = []
            for row in sheet.iter_rows(values_only=True):
                if any(c is not None and str(c).strip() for c in row):
                    rows.append(" | ".join("" if c is None else str(c).strip() for c in row))
            if rows:
                units.append(ExtractedUnit(text="\n".join(rows), section=sheet.title, kind="table"))
        return units


class PptxExtractor:
    name = "pptx_extractor"
    mime_types = ("application/vnd.openxmlformats-officedocument.presentationml.presentation",
                  "application/vnd.openxmlformats-officedocument.presentationml.template")

    def extract(self, data, record):
        from app.genai.ppt.template.loader import open_presentation_bytes
        units = []
        prs = open_presentation_bytes(data)          # .potx templates open too
        for i, slide in enumerate(prs.slides, start=1):
            texts, title = [], ""
            for shape in slide.shapes:
                if not getattr(shape, "has_text_frame", False):
                    continue
                st = shape.text_frame.text.strip()
                if not st:
                    continue
                if shape == slide.shapes.title:
                    title = st
                else:
                    texts.append(st)
            if slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame.text.strip()
                if notes:
                    texts.append(f"[speaker notes] {notes}")
            body = "\n".join(texts)
            if title or body:
                units.append(ExtractedUnit(text=body or title, section=title or f"slide {i}", page=i))
        return units
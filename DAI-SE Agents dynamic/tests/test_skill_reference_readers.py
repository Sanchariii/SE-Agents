"""Style-only reference inspection for bounded document, image, and web inputs."""

import io

import pytest
from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_AUTO_SHAPE_TYPE
from pptx.util import Inches, Pt
from pypdf import PdfWriter

from app.config.constants import SkillReferenceConstants as Limits
from app.config.exceptions import AppError
from app.genai.skills.reference_readers import inspect_file, inspect_website


def _pptx():
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    shape = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
    run = shape.text_frame.paragraphs[0].add_run()
    run.text = "Private reference wording"
    run.font.name = "Arial"
    run.font.size = Pt(24)
    run.font.color.rgb = RGBColor(20, 40, 60)
    panel = slide.shapes.add_shape(
        MSO_AUTO_SHAPE_TYPE.RECTANGLE, Inches(1), Inches(2), Inches(3), Inches(2))
    panel.fill.solid()
    panel.fill.fore_color.rgb = RGBColor(20, 40, 60)
    data = io.BytesIO()
    deck.save(data)
    return data.getvalue()


def _pdf():
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    data = io.BytesIO()
    writer.write(data)
    return data.getvalue()


def _image(width=32, height=16):
    data = io.BytesIO()
    Image.new("RGB", (width, height), "#14283C").save(data, format="PNG")
    return data.getvalue()


def test_pptx_extracts_visual_metadata_without_reference_text():
    result = inspect_file(_pptx(), "application/vnd.openxmlformats-officedocument.presentationml.presentation")

    assert result["style_only"] is True
    assert result["source_format"] == "pptx"
    assert result["metrics"]["slide_count"] == 1
    assert "Arial" in result["metrics"]["fonts"]
    assert "#14283C" in result["metrics"]["colors"]
    assert "Private reference wording" not in str(result)


def test_pdf_extracts_page_geometry_and_reports_style_only():
    result = inspect_file(_pdf(), "application/pdf")

    assert result["source_format"] == "pdf"
    assert result["metrics"]["page_count"] == 1
    assert result["metrics"]["aspect_ratios"] == ["0.773"]
    assert result["style_only"] is True


def test_png_extracts_dimensions_and_palette():
    result = inspect_file(_image(), "image/png")

    assert result["source_format"] == "png"
    assert result["metrics"]["width"] == 32
    assert result["metrics"]["height"] == 16
    assert result["metrics"]["dominant_colors"] == ["#14283C"]


@pytest.mark.parametrize("mime_type,data", [
    ("application/pdf", b"not a PDF"),
    ("application/vnd.openxmlformats-officedocument.presentationml.presentation",
     b"not a zip"),
    ("image/jpeg", b"not an image"),
])
def test_malformed_reference_bytes_are_reported(mime_type, data):
    with pytest.raises(AppError):
        inspect_file(data, mime_type)


def test_unsupported_mime_type_is_rejected():
    with pytest.raises(AppError, match="unsupported"):
        inspect_file(b"document", "text/plain")


def test_reference_upload_byte_limit_is_enforced(monkeypatch):
    monkeypatch.setattr(Limits, "MAX_REFERENCE_BYTES", 4)

    with pytest.raises(AppError, match="exceeds"):
        inspect_file(b"12345", "image/png")


def test_image_pixel_limit_is_checked_before_decode(monkeypatch):
    monkeypatch.setattr(Limits, "MAX_IMAGE_PIXELS", 100)

    with pytest.raises(AppError, match="pixel limit"):
        inspect_file(_image(11, 10), "image/png")


def test_pptx_slide_limit_is_enforced(monkeypatch):
    monkeypatch.setattr(Limits, "MAX_PPTX_SLIDES", 0)

    with pytest.raises(AppError, match="slides"):
        inspect_file(_pptx(), "application/vnd.openxmlformats-officedocument.presentationml.presentation")


def test_pdf_page_limit_is_enforced(monkeypatch):
    monkeypatch.setattr(Limits, "MAX_PDF_PAGES", 0)

    with pytest.raises(AppError, match="pages"):
        inspect_file(_pdf(), "application/pdf")


def test_website_extracts_css_metrics_without_page_copy(monkeypatch):
    html = (b'<h1>Private heading</h1><p style="font-family: Barlow; color: #123456; '
            b'font-size: 18px; padding: 12px">Private wording</p><script>hidden</script>')
    monkeypatch.setattr("app.genai.skills.reference_readers.website._public_address",
                        lambda _host: "93.184.216.34")
    monkeypatch.setattr("app.genai.skills.reference_readers.website._fetch_once",
                        lambda *_args: (200, None, html))

    result = inspect_website("https://example.com/style", ["example.com"])

    assert result["source_format"] == "website"
    assert result["metrics"]["fonts"] == ["Barlow"]
    assert result["metrics"]["colors"] == ["#123456"]
    assert result["metrics"]["elements"]["h1"] == 1
    assert "Private heading" not in str(result)
    assert "Private wording" not in str(result)


@pytest.mark.parametrize("url,hosts", [
    ("http://example.com", ["example.com"]),
    ("https://example.com", []),
    ("https://unlisted.example.com", ["example.com"]),
    ("https://user:pass@example.com", ["example.com"]),
    ("https://example.com:444", ["example.com"]),
])
def test_website_rejects_unsafe_or_unapproved_origins(url, hosts):
    with pytest.raises(AppError):
        inspect_website(url, hosts)


def test_website_rejects_a_host_that_resolves_to_private_space(monkeypatch):
    monkeypatch.setattr(
        "app.genai.skills.reference_readers.website.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("127.0.0.1", 443))])

    with pytest.raises(AppError, match="non-public"):
        inspect_website("https://example.com", ["example.com"])


def test_website_rejects_a_public_and_private_dns_result(monkeypatch):
    monkeypatch.setattr(
        "app.genai.skills.reference_readers.website.socket.getaddrinfo",
        lambda *_args, **_kwargs: [
            (None, None, None, None, ("93.184.216.34", 443)),
            (None, None, None, None, ("10.0.0.2", 443)),
        ])

    with pytest.raises(AppError, match="non-public"):
        inspect_website("https://example.com", ["example.com"])


def test_website_revalidates_redirect_destination_before_fetch(monkeypatch):
    calls = []

    def fake_fetch(host, _address, _path):
        calls.append(host)
        return 302, "https://internal.example/", b""

    monkeypatch.setattr("app.genai.skills.reference_readers.website._public_address",
                        lambda _host: "93.184.216.34")
    monkeypatch.setattr("app.genai.skills.reference_readers.website._fetch_once", fake_fetch)

    with pytest.raises(AppError, match="allowlisted"):
        inspect_website("https://example.com", ["example.com"])

    assert calls == ["example.com"]

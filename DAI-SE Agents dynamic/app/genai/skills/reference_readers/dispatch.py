"""Choose a supported style-only reference reader by verified MIME type."""

from app.config.exceptions import AppError
from app.genai.skills.reference_readers.image import inspect_image
from app.genai.skills.reference_readers.office import inspect_pdf, inspect_pptx
from app.genai.skills.reference_readers.website import inspect_website

_READERS = {
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": inspect_pptx,
    "application/pdf": inspect_pdf,
    "image/png": inspect_image,
    "image/jpeg": inspect_image,
}


def inspect_file(data, mime_type):
    """Inspect one uploaded reference using its allowlisted MIME type."""
    reader = _READERS.get(str(mime_type or "").lower())
    if reader is None:
        raise AppError(f"unsupported skill reference type: {mime_type or 'unknown'}")
    return reader(data)

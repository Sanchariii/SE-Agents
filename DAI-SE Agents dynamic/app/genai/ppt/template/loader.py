"""Open the company .potx files with python-pptx.

python-pptx rejects .potx outright: its main part content type is
'…presentationml.template.main+xml', which maps to no PresentationPart. Swapping
that one string in [Content_Types].xml in memory makes the package loadable
without touching the file on disk.
"""

import io
import zipfile
from pathlib import Path

from pptx import Presentation

TEMPLATE_CT = b"application/vnd.openxmlformats-officedocument.presentationml.template.main+xml"
PRESENTATION_CT = (b"application/vnd.openxmlformats-officedocument."
                   b"presentationml.presentation.main+xml")
R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def open_presentation(path: str):
    source_path = Path(path)
    if source_path.suffix.lower() != ".potx":
        return Presentation(str(source_path))
    return open_presentation_bytes(source_path.read_bytes())


def open_presentation_bytes(data: bytes):
    """A .pptx or .potx held in memory (an uploaded file), opened with python-pptx."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, \
            zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            blob = source.read(item.filename)
            if item.filename == "[Content_Types].xml":
                blob = blob.replace(TEMPLATE_CT, PRESENTATION_CT)
            target.writestr(item, blob)
    buffer.seek(0)
    return Presentation(buffer)


def clear_slides(prs) -> None:
    """The brand .potx files ship 82-91 sample slides — start from an empty deck."""
    slide_id_list = prs.slides._sldIdLst
    for slide_id in list(slide_id_list):
        prs.part.drop_rel(slide_id.get(R_NS))
        slide_id_list.remove(slide_id)
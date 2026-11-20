"""Media extractors: image captioning (AOAI vision) and video (local pipeline).

Video runs locally for now — see app/genai/ingestion/local/README.md.
"""

import base64

from app.config.constants import IngestionConstants
from app.config.settings import get_settings
from app.core.models.document_model import DocumentRecord, ExtractedUnit
from app.genai.ingestion.local.video.pipeline import extract_video
from app.utilities.apim_llm_utils import get_apim_llm_client
from app.config.constants import IngestionConstants, ModelConstants
from app.utilities.logger_util import get_logger

log = get_logger(__name__)


class ImageExtractor:
    name = "image_extractor"
    mime_types = ("image/png", "image/jpeg", "image/webp")

    def extract(self, data: bytes, record: DocumentRecord) -> list[ExtractedUnit]:
        b64 = base64.b64encode(data).decode()
        # encoded = base64.b64encode(image_data).decode("ascii")
        client = get_apim_llm_client()

        resp = client.chat_completion(
            deployment=ModelConstants.DEFAULT,
            max_tokens=IngestionConstants.IMAGE_DESCRIPTION_MAX_TOKENS,
            messages = [{"role": "user", "content": [
                {"type": "text", "text": "Describe this image for a training deck, "
                                         "including any visible text, labels, and diagrams."},
                {"type": "image_url", "image_url": {"url": f"data:{record.mime_type};base64,{b64}"}},
                ]}] )

        text = client.reply_text(resp).strip()
        log.info("image %s described (%d chars); placeable from %s", record.filename,
                 len(text), record.storage_path)
        # The upload itself is the image, so its storage path is what a slide places.
        return [ExtractedUnit(text=text, section=record.filename,
                              kind="image_description",
                              meta={"frame_path": record.storage_path})] if text else []


class VideoExtractor:
    name = "video_extractor"
    mime_types = ("video/mp4", "video/quicktime", "video/x-matroska")

    def extract(self, data: bytes, record: DocumentRecord) -> list[ExtractedUnit]:
        return extract_video(data, record)
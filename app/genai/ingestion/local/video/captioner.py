"""Caption keyframes with Azure OpenAI vision."""

import base64
import mimetypes
from pathlib import Path

from app.config.constants import IngestionConstants
from app.config.settings import get_settings
from app.utilities.clients_util import get_aoai_client
from app.utilities.apim_llm_utils import get_apim_llm_client
from app.config.constants import IngestionConstants, ModelConstants

from app.utilities.logger_util import get_logger

log = get_logger(__name__)

PROMPT = (
    "This is a keyframe from a training video. Describe what it shows for a "
    "training-content knowledge base: transcribe any on-screen text verbatim, "
    "and if it is a slide, UI screenshot, or diagram, explain what it presents. "
    "Be concise (under 120 words).")


def caption_frame(frame_path: Path) -> str:
    """Generate a text caption for a keyframe using Azure OpenAI vision.

    Args:
        frame_path: local path to the JPEG keyframe.

    Returns:
        Caption string, or empty string if the model returns nothing.
    """
    log.info("captioning frame: %s", frame_path.name)
    b64 = base64.b64encode(frame_path.read_bytes()).decode()

    client = get_apim_llm_client()
    mime_type, _ = mimetypes.guess_type(frame_path.name)

    try:
        resp = client.chat_completion(
            deployment=ModelConstants.DEFAULT,
            max_tokens=IngestionConstants.IMAGE_DESCRIPTION_MAX_TOKENS,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": "Describe this image for a training deck, "
                                         "including any visible text, labels, and diagrams."},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
            ]}])

        text = client.reply_text(resp).strip()
        log.info("caption generated for %s (%s chars)", frame_path.name, len(text))
        return text
    except Exception as exc:                             # noqa: BLE001
        log.error("captioning failed for %s: %s", frame_path.name, exc, exc_info=True)
        return ""
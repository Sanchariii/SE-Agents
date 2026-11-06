"""Send a local image to the APIM Azure OpenAI route and print its description.

Run from the repository root:
    python scripts/manual_testing/04_apim_image.py path/to/image.png
"""

import argparse
import base64
import mimetypes
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config.constants import IngestionConstants, ModelConstants
from app.utilities.apim_llm_utils import get_apim_llm_client
from app.utilities.logger_util import get_logger

log = get_logger(__name__)
_SUPPORTED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp",}
_PROMPT = ("Describe this image for a training deck. Include visible text, labels, "
           "tables, charts, diagrams, and their key relationships. Do not invent details."
           "explain and revire if the slide looks fine , no over flow text , not falt text slide should look good and clear and readable , if not then suggest how to improve the slide , ")


def _parse_args() -> argparse.Namespace:
    """Read the local image path and optional APIM Azure OpenAI deployment.

    Returns:
        Parsed command-line arguments.
    """
    parser = argparse.ArgumentParser(description="Test APIM vision processing with a local image.")
    parser.add_argument("image", type=Path, help="Path to a PNG, JPEG, or WebP image.")
    parser.add_argument("--deployment", default=ModelConstants.DEFAULT,
                        help="APIM Azure OpenAI vision deployment (default: %(default)s).")
    return parser.parse_args()


def _image_message(image_path: Path) -> list[dict]:
    """Build the Azure OpenAI multimodal message for one local image.

    Args:
        image_path: Existing PNG, JPEG, or WebP file.

    Returns:
        A chat-completions message containing the prompt and base64 data URL.

    Raises:
        ValueError: If the file is empty or not a supported image type.
    """
    mime_type, _ = mimetypes.guess_type(image_path.name)
    if mime_type not in _SUPPORTED_MIME_TYPES:
        raise ValueError("image must be a PNG, JPEG, or WebP file")

    image_data = image_path.read_bytes()
    if not image_data:
        raise ValueError("image file is empty")

    encoded = base64.b64encode(image_data).decode("ascii")
    return [{"role": "user", "content": [
        {"type": "text", "text": _PROMPT},
        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{encoded}"}},
    ]}]


def main() -> int:
    """Call the configured APIM vision deployment and print its result.

    Returns:
        Zero when APIM returns a description; otherwise one.
    """
    args = _parse_args()
    image_path = args.image.resolve()
    if not image_path.is_file():
        print(f"Image not found: {image_path}", file=sys.stderr)
        return 1

    try:
        log.info("sending image to APIM", extra={"filename": image_path.name,
                                                   "deployment": args.deployment})
        client = get_apim_llm_client()
        response = client.chat_completion(
            _image_message(image_path), deployment=args.deployment,
            max_tokens=IngestionConstants.IMAGE_DESCRIPTION_MAX_TOKENS)
        description = client.reply_text(response)
        print(description or "APIM returned an empty description.")
        print(f"\nUsage: {client.usage_of(response)}")
        return 0 if description else 1
    except Exception as exc: 
        log.exception("APIM image processing test failed", extra={"filename": image_path.name})
        print(f"APIM image processing failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
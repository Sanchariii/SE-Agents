"""Probe whether the ROK AI Gateway passes images through, per route and block shape.

Max mode sends a slide render to the judge (ModelConstants.SLIDE_REVIEWER) and, on a
redo, to the designer (ModelConstants.PPT_CODER). This draws a PNG carrying a random
code word and sends it as raw requests (no param shedding, no retries), so each case
shows exactly what the gateway does. A case only passes when the reply contains the
code word: a gateway that accepts the request but strips the image would otherwise
look like a success.

Results decide ApimConstants.FOUNDRY_IMAGE_FORMAT, ModelConstants.SLIDE_REVIEWER and
PptCreatorConstants.MAX_MODE_RENDER_TO_DESIGNER.

Run from the repository root:
    python scripts/manual_testing/12_apim_foundry_image.py
    python scripts/manual_testing/12_apim_foundry_image.py --models claude-sonnet-5-5
"""

import argparse
import base64
import io
import random
import string
import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config.constants import ApimConstants, ModelConstants
from app.utilities import tls_util as tls
from app.utilities.apim_llm_utils import get_apim_llm_client

_TIMEOUT_SECONDS = 120
_QUESTION = "What single word is written in this image? Reply with the word only."


def _parse_args() -> argparse.Namespace:
    """Read the foundry models to probe."""
    defaults = sorted({ModelConstants.SLIDE_REVIEWER, ModelConstants.PPT_CODER})
    parser = argparse.ArgumentParser(description="Probe APIM image support.")
    parser.add_argument("--models", nargs="+", default=defaults,
                        help="Foundry models to probe (default: %(default)s).")
    return parser.parse_args()


def _png(word: str) -> bytes:
    """A 600x200 PNG with `word` drawn large."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (600, 200), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("arial.ttf", 72)
    except OSError:
        font = ImageFont.load_default()
    draw.text((40, 60), word, fill="black", font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _post(url: str, body: dict) -> tuple[int, dict | str, int]:
    """POST a raw body; returns (status, payload, milliseconds)."""
    client = get_apim_llm_client()
    started = time.monotonic()
    resp = requests.post(url, headers=client._headers(), json=body,
                         verify=tls.verify_setting(), timeout=_TIMEOUT_SECONDS)
    try:
        payload: dict | str = resp.json()
    except ValueError:
        payload = resp.text
    return resp.status_code, payload, int((time.monotonic() - started) * 1000)


def _report(label: str, word: str, status: int, payload, elapsed: int) -> bool:
    """Print one verdict line; True when the reply echoed the code word."""
    client = get_apim_llm_client()
    text = client.reply_text(payload) if isinstance(payload, dict) else ""
    usage = client.usage_of(payload) if isinstance(payload, dict) else {}
    seen = status < 400 and word.lower() in text.lower()
    verdict = "OK  " if seen else "FAIL"
    detail = (f"reply={text.strip()[:40]!r} usage={usage}" if status < 400
              else f"-> {str(payload)[:2000]}")
    print(f"[{verdict}] {status} {elapsed:>6}ms  {label}  {detail}")
    return seen


def main() -> int:
    """Run every case; exit 0 when at least one foundry shape works per model."""
    args = _parse_args()
    client = get_apim_llm_client()
    word = "".join(random.choices(string.ascii_uppercase, k=6))
    b64 = base64.b64encode(_png(word)).decode("ascii")
    anthropic = {"type": "image",
                 "source": {"type": "base64", "media_type": "image/png", "data": b64}}
    openai = {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
    foundry = f"{client._base_url()}/foundry/models/chat/completions"
    print(f"code word: {word}; FOUNDRY_IMAGE_FORMAT is currently "
          f"{ApimConstants.FOUNDRY_IMAGE_FORMAT!r}\n")

    ok_all = True
    for model in args.models:
        results = {}
        for shape, block in (("anthropic", anthropic), ("image_url", openai)):
            body = {"model": model, "max_tokens": 50, "messages": [
                {"role": "user", "content": [block, {"type": "text", "text": _QUESTION}]}]}
            results[shape] = _report(f"{model} {shape}", word, *_post(foundry, body))
        # The designer's redo shape: system, an assistant turn, then image + text.
        designer = {"model": model, "max_tokens": 16000, "messages": [
            {"role": "system", "content": "You answer briefly."},
            {"role": "user", "content": [{"type": "text", "text": "Hello."}]},
            {"role": "assistant", "content": "Hello."},
            {"role": "user", "content": [anthropic, {"type": "text", "text": _QUESTION}]}]}
        _report(f"{model} designer-shape (anthropic)", word, *_post(foundry, designer))
        ok_all &= any(results.values())
        print()

    # Control: the azure-openai route that ingestion already uses.
    azure = (f"{client._base_url()}/openai/deployments/{ModelConstants.DEFAULT}"
             f"/chat/completions?api-version={ApimConstants.CHAT_API_VERSION}")
    _report(f"{ModelConstants.DEFAULT} image_url (control)", word, *_post(azure, {
        "max_tokens": 50, "messages": [
            {"role": "user", "content": [openai, {"type": "text", "text": _QUESTION}]}]}))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())

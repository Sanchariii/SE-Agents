"""Extract bounded palette and dimensions from PNG and JPEG references."""

import io
import warnings

from PIL import Image, UnidentifiedImageError

from app.config.constants import SkillReferenceConstants as Limits
from app.config.exceptions import AppError
from app.genai.skills.reference_readers.common import check_bytes, profile, reference_error


def inspect_image(data):
    """Return image dimensions, aspect ratio, and a small dominant-color palette."""
    digest = check_bytes(data)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as original:
                width, height = original.size
                image_format = original.format
                if width < 1 or height < 1 or width * height > Limits.MAX_IMAGE_PIXELS:
                    raise AppError("image reference dimensions exceed the pixel limit")
                if image_format not in ("PNG", "JPEG"):
                    raise AppError("image reference must be PNG or JPEG")
                original.verify()
            with Image.open(io.BytesIO(data)) as original:
                image = original.convert("RGB")
                image.thumbnail((128, 128))
                quantized = image.quantize(colors=6)
                palette = quantized.getpalette()
                colors = quantized.getcolors(128 * 128) or []
                colors.sort(reverse=True)
                dominant = [
                    "#{:02X}{:02X}{:02X}".format(*palette[index * 3:index * 3 + 3])
                    for _, index in colors[:6]
                ]
        metrics = {
            "width": width,
            "height": height,
            "aspect_ratio": round(width / height, 3),
            "dominant_colors": dominant,
        }
        summary = (f"{width}x{height} image, {width / height:.2f}:1 aspect ratio; "
                   f"dominant colors: {', '.join(dominant[:4]) or 'unavailable'}.")
        return profile(image_format.lower(), digest, summary, metrics,
                       [{"kind": "image", **metrics}])
    except AppError:
        raise
    except (OSError, ValueError, UnidentifiedImageError,
            Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        reference_error("PNG/JPEG reference could not be inspected", exc)

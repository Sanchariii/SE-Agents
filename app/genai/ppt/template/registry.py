"""The brand template and its marks, by name.

Decision 2: one template, resolved by a single constant. The classification no longer
selects a file - it is rendered onto the slide instead (Decision 3), so this module
resolves the template, the policy symbols and the logos, and normalizes the
classification for display. The classification is never inferred by an LLM: it is
either stated explicitly by the user or defaults to Internal.
"""

import re
from functools import lru_cache
from pathlib import Path

from app.config.constants import PptConstants
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

CLASSIFICATIONS = PptConstants.CLASSIFICATIONS
DEFAULT_CLASSIFICATION = PptConstants.DEFAULT_CLASSIFICATION

_MENTION = re.compile(
    r"\b(restricted|confidential|co-?branded|internal(?:\s+use)?|public)\b", re.I)


def normalize(value: str | None) -> str:
    key = (value or "").strip().lower().replace("-", "").replace(" ", "")
    if key.startswith("cobrand"):
        key = "cobranded"
    if key.startswith("internal"):
        key = "internal"
    if key not in CLASSIFICATIONS:
        log.debug("classification %r is not one of %s; defaulting to %r",
                  value, CLASSIFICATIONS, DEFAULT_CLASSIFICATION)
        return DEFAULT_CLASSIFICATION
    return key


def detect_classification(text: str) -> str | None:
    """Explicit user mention only — returns None when nothing was stated."""
    match = _MENTION.search(text or "")
    if match is None:
        log.debug("no classification stated in %d chars of text", len(text or ""))
        return None
    found = normalize(match.group(1))
    log.info("classification %r detected from the user's own words (%r)",
             found, match.group(1))
    return found


def label(classification: str | None) -> str:
    key = normalize(classification)
    rendered = "Co-branded" if key == "cobranded" else key.capitalize()
    log.debug("classification %r renders as %r", classification, rendered)
    return rendered


@lru_cache
def template_root() -> Path:
    root = Path(PptConstants.TEMPLATE_DIR)
    if not root.is_absolute():
        # app/genai/ppt/template/registry.py -> project root
        root = Path(__file__).resolve().parents[4] / root
    if not root.is_dir():
        log.error("brand asset root %s does not exist; the template, the policy "
                  "symbols and the logos will all be missing", root)
    else:
        log.debug("brand asset root resolved to %s", root)
    return root


@lru_cache
def template_path() -> Path:
    """The one brand template every slide and the stitched deck are built from.

    Returns:
        Absolute path of SAMPLE.pptx.

    Raises:
        FileNotFoundError: When it is missing. Deliberately fatal: there is no second
            template, and shipping an unbranded deck silently is worse than failing.
    """
    path = template_root() / PptConstants.TEMPLATE_FILENAME
    if not path.is_file():
        log.error("brand template not found at %s; every slide, the stitched deck and "
                  "the catalog all read it, so nothing can be built", path)
        raise FileNotFoundError(
            f"brand template not found at {path}. It is required: there is no fallback.")
    log.info("brand template resolved: %s (%d bytes)", path, path.stat().st_size)
    return path


@lru_cache
def classification_symbol(classification: str | None) -> Path | None:
    """The policy symbol stamped beside the classification label, when one exists.

    Args:
        classification: Any accepted spelling; normalized here.

    Returns:
        Absolute path of the symbol PNG, or None when the brand pack has none for this
        classification (co-branded has no symbol). A missing symbol is a warning, never
        an error — the slide renders without the mark.
    """
    name = PptConstants.CLASSIFICATION_ICON_FILENAME.format(
        label=label(classification).replace("-", ""))
    path = template_root() / PptConstants.CLASSIFICATION_ICON_DIR / name
    if path.is_file():
        log.debug("policy symbol for %r: %s", classification, path.name)
        return path
    log.warning("no policy symbol for classification %r at %s; the classification "
                "label will be stamped without its mark", classification, path)
    return None


@lru_cache
def logo_path(filename: str) -> Path | None:
    """An approved brand mark by filename, or None when it is not on disk."""
    path = template_root() / PptConstants.LOGO_DIR / filename
    if path.is_file():
        log.debug("brand logo %s resolved to %s", filename, path)
        return path
    log.warning("brand logo %s missing at %s; no mark will be placed", filename, path)
    return None
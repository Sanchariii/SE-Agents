"""The per-slide .pptx cache: what lets a follow-up turn regenerate only what changed.

Generating a slide costs a full code-generation run, so a slide whose content did not
change this turn is fetched from blob storage instead of being rebuilt. The cache key is
the slide's *content* hash, which means a slide the planner left alone is reused even if
its number moved, and a slide whose copy changed is always rebuilt.
"""

import hashlib
from pathlib import Path

from app.config.constants import PptCreatorConstants, StorageConstants
from app.core.handlers.session_store import session_store
from app.core.models.deck_model import SlideDraft
from app.utilities import storage_util as storage
from app.utilities.logger_util import get_logger

log = get_logger(__name__)


def content_hash(slide: SlideDraft, design_key: str = "") -> str:
    """Hash the parts of a slide that change how it is drawn.

    Args:
        slide: The slide's content as written by the content agent.
        design_key: The slide's non-default generation parameters (creativity,
            type, family style, quiz role, user instructions), or "" when all are defaults.
            Appended only when non-empty, so a default slide hashes exactly as it
            did before the parameters existed and its cache entry stays valid.

    Returns:
        A short hex digest; equal digests mean the generated .pptx can be reused.
    """
    payload = slide.design_json(exclude={"slide_number"})
    if design_key:
        payload += f"|design:{design_key}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def load(session_id: str) -> dict[int, dict]:
    """Read the stored slide-file map for a session.

    Args:
        session_id: Owning session.

    Returns:
        {slide_number: {"path": storage path, "hash": content hash, "fallback": bool}}.
    """
    stored = session_store.get_slide_artifacts(session_id)
    return {int(number): entry for number, entry in stored.items()}


def save(session_id: str, artifacts: dict[int, dict]) -> None:
    """Persist the slide-file map for the next turn.

    Args:
        session_id: Owning session.
        artifacts: {slide_number: {"path": ..., "hash": ..., "fallback": ...}}.

    Returns:
        None.
    """
    session_store.save_slide_artifacts(
        session_id, {str(number): entry for number, entry in artifacts.items()})


def put(session_id: str, slide_number: int, slide: SlideDraft, pptx_path: Path,
        fallback: bool = False, design_key: str = "") -> dict:
    """Upload one generated slide file and return its cache entry.

    Args:
        session_id: Owning session.
        slide_number: The slide this file renders.
        slide: The content the file was generated from, for the hash.
        pptx_path: Local path of the generated single-slide .pptx.
        fallback: True when the plain fallback slide was used.
        design_key: The slide's design key, as given to `content_hash`.

    Returns:
        The cache entry {"path", "hash", "fallback"} to store for this slide.
    """
    digest = content_hash(slide, design_key)
    container_path = PptCreatorConstants.SLIDE_CACHE_PATH.format(
        session_id=session_id, slide_number=slide_number, content_hash=digest)
    storage.save_bytes(container_path, pptx_path.read_bytes(),
                       container=StorageConstants.CONTAINER_DECKS)
    log.info("cached slide %d at %s", slide_number, container_path,
             extra={"session_id": session_id})
    return {"path": container_path, "hash": digest, "fallback": fallback}


def fetch(session_id: str, entry: dict, dest: Path) -> Path | None:
    """Download a cached slide file for stitching.

    Args:
        session_id: Owning session, for logging only.
        entry: The cache entry produced by `put`.
        dest: Local path to write the file to.

    Returns:
        The local path, or None when the blob has gone (the slide is then regenerated).
    """
    try:
        data = storage.read_file(entry["path"], container=StorageConstants.CONTAINER_DECKS)
    except Exception as exc:                                 # noqa: BLE001
        # A missing cache entry is recoverable: the caller rebuilds the slide.
        log.warning("cached slide %s unavailable: %s", entry.get("path"), exc,
                    extra={"session_id": session_id})
        return None
    dest.write_bytes(data)
    return dest


def evict(session_id: str, artifacts: dict[int, dict], keep: set[int]) -> dict[int, dict]:
    """Delete the blobs of slides that are no longer in the deck.

    Args:
        session_id: Owning session.
        artifacts: The current slide-file map.
        keep: Slide numbers that survive this turn.

    Returns:
        The map with the removed slides dropped.
    """
    for number, entry in list(artifacts.items()):
        if number in keep:
            continue
        try:
            storage.delete_file(entry["path"], container=StorageConstants.CONTAINER_DECKS)
        except Exception as exc:                             # noqa: BLE001
            # Best-effort cleanup: an orphaned blob is harmless, a failed turn is not.
            log.warning("could not delete cached slide %s: %s", entry.get("path"), exc,
                        extra={"session_id": session_id})
        artifacts.pop(number, None)
    return artifacts

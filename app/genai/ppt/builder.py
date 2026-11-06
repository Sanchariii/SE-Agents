"""Publish a finished deck: stitched .pptx -> translations -> blob -> deck version.

The deck itself is not rendered here. `PptCreatorAgent` generates each slide and
`DeckStitcher` merges them, so this module's job is publication: upload the English
file, produce each additional language by rewriting the text of that same file, and
record the new numbered version.
"""

import tempfile
from pathlib import Path

from app.config.constants import PptConstants, PptCreatorConstants, StorageConstants
from app.core.handlers.deck_version_handler import deck_version_handler
from app.core.handlers.session_store import session_store
from app.core.models.document_model import DeckArtifact
from app.genai.ppt import pptx_translator
from app.genai.ppt.template import registry
from app.utilities import events_util as events, storage_util as storage
from app.utilities.logger_util import get_logger

log = get_logger(__name__)


def _slug(text: str) -> str:
    slug = "".join(c.lower() if c.isalnum() else "_" for c in text.strip()).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    slug = slug[:60] or "training_deck"
    log.debug("slugged %r -> %r", text, slug)
    return slug


def persist_deck(session_id, draft, bundle, deck_path: Path) -> DeckArtifact:
    """Publish one finished deck: upload it, translate it, and record the version.

    Args:
        session_id: Owning session.
        draft: The deck's content, stored so the next turn can build on it.
        bundle: The turn's ContentBundle, carrying the plan (audience, languages…).
        deck_path: The stitched English .pptx produced by this turn.

    Returns:
        The DeckArtifact for the new version.
    """
    session_store.save_draft(session_id, draft.model_dump(mode="json"))
    plan = bundle.plan or {}
    languages = plan.get("languages") or [PptConstants.DEFAULT_LANGUAGE]
    classification = registry.normalize(plan.get("classification") or draft.template)
    deck_version = len(session_store.list_decks(session_id)) + 1
    files: dict[str, str] = {}
    log.info("publishing deck v%d %r from %s: %d language(s) %s, classification %s",
             deck_version, draft.title, deck_path.name, len(languages), languages,
             classification, extra={"session_id": session_id})

    with tempfile.TemporaryDirectory(prefix="deck_") as tmp:
        workdir = Path(tmp)
        for language in languages:
            filename = PptCreatorConstants.DECK_FILENAME.format(
                slug=_slug(draft.title), deck_version=deck_version,
                language=_slug(language))
            out_path = workdir / filename
            # English is the deck as designed; every other language is that same file
            # with its text runs rewritten, so the design is never re-rendered.
            if language.strip().lower().startswith("en"):
                log.debug("%s is the deck as designed; copying it", language,
                          extra={"session_id": session_id})
                pptx_translator.copy_deck(deck_path, out_path)
            else:
                log.info("rewriting the deck's text into %s", language,
                         extra={"session_id": session_id})
                pptx_translator.translate_pptx(session_id, deck_path, out_path, language)

            container_path = f"{session_id}/decks/{filename}"
            storage.save_bytes(container_path, out_path.read_bytes(),
                               container=StorageConstants.CONTAINER_DECKS)
            log.info("uploaded %s (%d bytes) to %s", language,
                     out_path.stat().st_size, container_path,
                     extra={"session_id": session_id})
            files[language] = container_path
            events.publish(session_id, "agent_step", agent="deck",
                           message=f"Built {language} deck v{deck_version}")

    primary = files.get(PptConstants.DEFAULT_LANGUAGE) or next(iter(files.values()))
    artifact = session_store.add_deck(session_id, primary, plan_snapshot=plan)
    try:
        deck_version_handler.record_version(
            session_id, artifact.deck_version, primary,
            theme=registry.label(classification),    # 'theme' column reused for template name
            plan_title=draft.title, files=files)
    except Exception:                                # noqa: BLE001
        # The files are uploaded and the artifact exists; only the version row is
        # missing, so the deck still ships and the next turn still numbers correctly.
        log.error("deck version metadata not recorded (session=%s v=%s); the deck is "
                  "published but will not appear in the version history",
                  session_id, artifact.deck_version, exc_info=True)

    log.info("deck v%d published: %d file(s) at %s", artifact.deck_version,
             len(files), primary, extra={"session_id": session_id})
    events.publish(session_id, "deck", agent="deck",
                   message=f"Deck v{artifact.deck_version} ready "
                           f"({registry.label(classification)}, {len(files)} language(s))",
                   deck_version=artifact.deck_version)
    return artifact
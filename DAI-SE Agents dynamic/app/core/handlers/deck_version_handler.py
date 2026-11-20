"""Deck versions: metadata in Postgres, the .pptx bytes in Blob storage."""

import json

from app.config.constants import (
    DeckFeedbackConstants, DeckVersionStatus, PgTables, StorageConstants,
)
from app.config.exceptions import ConflictError, NotFoundError
from app.core.models.deck_version_model import DeckVersion, DeckVersionFeedback
from app.utilities import storage_util as storage
from app.utilities.logger_util import get_logger
from app.utilities.pgsql_util import PgsqlUtil

log = get_logger(__name__)

_DDL = f"""
CREATE TABLE IF NOT EXISTS {PgTables.DECK_VERSIONS} (
    id           SERIAL PRIMARY KEY,
    session_id   TEXT NOT NULL,
    deck_version INT  NOT NULL,
    storage_path TEXT NOT NULL,
    theme        TEXT,
    plan_title   TEXT,
    files        TEXT DEFAULT '{{}}',
    status       TEXT DEFAULT '{DeckVersionStatus.GENERATED}',
    thumbs_up    BOOLEAN,
    user_comment VARCHAR({DeckFeedbackConstants.COMMENT_MAX_LENGTH}),
    created_at   TIMESTAMPTZ DEFAULT now(),
    UNIQUE (session_id, deck_version)
);
"""

_ADD_FEEDBACK_COLUMNS = (
    f"""ALTER TABLE {PgTables.DECK_VERSIONS}
        ADD COLUMN IF NOT EXISTS thumbs_up BOOLEAN""",
    f"""ALTER TABLE {PgTables.DECK_VERSIONS}
        ADD COLUMN IF NOT EXISTS user_comment
        VARCHAR({DeckFeedbackConstants.COMMENT_MAX_LENGTH})""",
)


class DeckVersionHandler:
    def __init__(self) -> None:
        self._db: PgsqlUtil | None = None

    def _pg(self) -> PgsqlUtil:
        if self._db is None:
            self._db = PgsqlUtil()
        return self._db

    def ensure_table(self) -> None:
        """Create the version table and add feedback columns to older schemas.

        Returns:
            None.
        """
        log.info("ensuring deck version table and feedback columns")
        self._pg().execute(_DDL)
        for statement in _ADD_FEEDBACK_COLUMNS:
            self._pg().execute(statement)
        log.info("deck version table and feedback columns are ready")

    def record_version(self, session_id: str, deck_version: int, storage_path: str,
                       theme: str | None = None, plan_title: str | None = None,
                       files: dict[str, str] | None = None) -> None:
        self._pg().execute(
            f"""INSERT INTO {PgTables.DECK_VERSIONS}
                (session_id, deck_version, storage_path, theme, plan_title, files)
                VALUES (:sid, :ver, :path, :theme, :title, :files)
                ON CONFLICT (session_id, deck_version)
                DO UPDATE SET storage_path = EXCLUDED.storage_path,
                              theme = EXCLUDED.theme,
                              plan_title = EXCLUDED.plan_title,
                              files = EXCLUDED.files""",
            {"sid": session_id, "ver": deck_version, "path": storage_path,
             "theme": theme, "title": plan_title, "files": json.dumps(files or {})})

    @staticmethod
    def _to_version(row: dict) -> DeckVersion:
        row = dict(row)
        if isinstance(row.get("files"), str):        # stored as a JSON text column
            try:
                row["files"] = json.loads(row["files"] or "{}")
            except json.JSONDecodeError:
                row["files"] = {}
        return DeckVersion(**row)

    def list_versions(self, session_id: str) -> list[DeckVersion]:
        rows = self._pg().query(
            f"""SELECT * FROM {PgTables.DECK_VERSIONS}
                WHERE session_id = :sid ORDER BY deck_version""",
            {"sid": session_id})
        return [self._to_version(r) for r in rows]

    def get_version(self, session_id: str, deck_version: int) -> DeckVersion | None:
        rows = self._pg().query(
            f"""SELECT * FROM {PgTables.DECK_VERSIONS}
                WHERE session_id = :sid AND deck_version = :ver""",
            {"sid": session_id, "ver": deck_version})
        return self._to_version(rows[0]) if rows else None

    @staticmethod
    def _to_feedback(row: dict) -> DeckVersionFeedback:
        """Convert a feedback query row into its API/domain model.

        Args:
            row: Database row containing deck-version feedback fields.

        Returns:
            Typed feedback for the deck version.
        """
        return DeckVersionFeedback(
            session_id=row["session_id"],
            deck_version=row["deck_version"],
            thumbs_up=row["thumbs_up"],
            comment=row.get("user_comment"),
        )

    def submit_feedback(
        self,
        session_id: str,
        deck_version: int,
        thumbs_up: bool,
        comment: str | None,
    ) -> DeckVersionFeedback:
        """Persist the only feedback submission allowed for a deck version.

        Args:
            session_id: Session that owns the generated deck.
            deck_version: Version receiving the feedback.
            thumbs_up: True for positive feedback and false for negative feedback.
            comment: Optional user comment, already bounded by request validation.

        Returns:
            The newly stored feedback.

        Raises:
            NotFoundError: The requested deck version does not exist.
            ConflictError: Feedback has already been submitted for the version.
        """
        context = {"session_id": session_id, "deck_version": deck_version}
        log.info("submitting deck version feedback", extra=context)
        rows = self._pg().execute_returning(
            f"""UPDATE {PgTables.DECK_VERSIONS}
                SET thumbs_up = :thumbs_up, user_comment = :comment
                WHERE session_id = :sid
                  AND deck_version = :ver
                  AND thumbs_up IS NULL
                RETURNING session_id, deck_version, thumbs_up, user_comment""",
            {
                "sid": session_id,
                "ver": deck_version,
                "thumbs_up": thumbs_up,
                "comment": comment,
            },
        )
        if rows:
            log.info("deck version feedback stored", extra=context)
            return self._to_feedback(rows[0])

        existing = self._pg().query(
            f"""SELECT thumbs_up FROM {PgTables.DECK_VERSIONS}
                WHERE session_id = :sid AND deck_version = :ver""",
            {"sid": session_id, "ver": deck_version},
        )
        if not existing:
            log.info("deck version not found for feedback", extra=context)
            raise NotFoundError("deck version not found")

        log.warning("duplicate deck version feedback rejected", extra=context)
        raise ConflictError("feedback already submitted for deck version")

    def get_feedback(
        self,
        session_id: str,
        deck_version: int,
    ) -> DeckVersionFeedback:
        """Retrieve feedback previously submitted for a deck version.

        Args:
            session_id: Session that owns the generated deck.
            deck_version: Version whose feedback is requested.

        Returns:
            Stored feedback for the deck version.

        Raises:
            NotFoundError: The version is missing or has no submitted feedback.
        """
        context = {"session_id": session_id, "deck_version": deck_version}
        log.info("retrieving deck version feedback", extra=context)
        rows = self._pg().query(
            f"""SELECT session_id, deck_version, thumbs_up, user_comment
                FROM {PgTables.DECK_VERSIONS}
                WHERE session_id = :sid AND deck_version = :ver""",
            {"sid": session_id, "ver": deck_version},
        )
        if not rows or rows[0]["thumbs_up"] is None:
            log.info("deck version feedback not found", extra=context)
            raise NotFoundError("deck feedback not found")
        log.info("deck version feedback retrieved", extra=context)
        return self._to_feedback(rows[0])

    def download(self, version: DeckVersion, path: str | None = None) -> bytes:
        return storage.read_file(path or version.storage_path,
                                 container=StorageConstants.CONTAINER_DECKS)

    def delete_version(self, session_id: str, deck_version: int) -> bool:
        version = self.get_version(session_id, deck_version)
        if version is None:
            return False
        container = StorageConstants.CONTAINER_DECKS
        for path in {version.storage_path, *version.files.values()}:
            storage.delete_file(path, container=container)
        self._pg().execute(
            f"""DELETE FROM {PgTables.DECK_VERSIONS}
                WHERE session_id = :sid AND deck_version = :ver""",
            {"sid": session_id, "ver": deck_version})
        return True

    def delete_all_for_session(self, session_id: str) -> int:
        versions = self.list_versions(session_id)
        container = StorageConstants.CONTAINER_DECKS
        for v in versions:
            for path in {v.storage_path, *v.files.values()}:
                storage.delete_file(path, container=container)
        self._pg().execute(
            f"DELETE FROM {PgTables.DECK_VERSIONS} WHERE session_id = :sid",
            {"sid": session_id})
        return len(versions)


deck_version_handler = DeckVersionHandler()

"""Durable Postgres registry for source-file metadata and ingestion status."""

from app.config.constants import PgTables
from app.core.models.document_model import DocumentRecord
from app.utilities.logger_util import get_logger
from app.utilities.pgsql_util import PgsqlUtil

log = get_logger(__name__)

_DDL = f"""
CREATE TABLE IF NOT EXISTS {PgTables.DOCUMENTS} (
    id           SERIAL PRIMARY KEY,
    session_id   TEXT NOT NULL,
    doc_id       TEXT NOT NULL,
    version      INT NOT NULL,
    filename     TEXT NOT NULL,
    mime_type    TEXT NOT NULL,
    sha256       TEXT NOT NULL DEFAULT '',
    storage_path TEXT NOT NULL,
    is_latest    BOOLEAN NOT NULL DEFAULT TRUE,
    status       TEXT NOT NULL,
    error        TEXT,
    uploaded_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, doc_id, version)
);
"""

_DDL_INDEX = (f"CREATE INDEX IF NOT EXISTS ix_{PgTables.DOCUMENTS}_session "
              f"ON {PgTables.DOCUMENTS} (session_id, is_latest, doc_id, version)")


class DocumentRegistryHandler:
    """Persist source document records independently of the Redis session cache."""

    def __init__(self) -> None:
        self._db: PgsqlUtil | None = None

    def _pg(self) -> PgsqlUtil:
        """Return the handler's shared Postgres utility.

        Returns:
            The lazily-created database utility.
        """
        if self._db is None:
            self._db = PgsqlUtil()
        return self._db

    def ensure_table(self) -> None:
        """Create the document registry table and its session lookup index.

        Returns:
            None.
        """
        self._pg().execute(_DDL)
        self._pg().execute(_DDL_INDEX)

    def record(self, record: DocumentRecord) -> None:
        """Insert or update one source document and preserve version semantics.

        Args:
            record: The source-file record, including its current ingestion status.

        Returns:
            None.
        """
        params = record.model_dump(mode="json")
        params["status"] = record.status.value
        self._pg().execute(
            f"""UPDATE {PgTables.DOCUMENTS} SET is_latest = FALSE
                WHERE session_id = :session_id AND doc_id = :doc_id AND version <> :version""",
            params)
        self._pg().execute(
            f"""INSERT INTO {PgTables.DOCUMENTS}
                (session_id, doc_id, version, filename, mime_type, sha256, storage_path,
                 is_latest, status, error, uploaded_at)
                VALUES (:session_id, :doc_id, :version, :filename, :mime_type, :sha256,
                        :storage_path, :is_latest, :status, :error, :uploaded_at)
                ON CONFLICT (session_id, doc_id, version)
                DO UPDATE SET filename = EXCLUDED.filename, mime_type = EXCLUDED.mime_type,
                    sha256 = EXCLUDED.sha256, storage_path = EXCLUDED.storage_path,
                    is_latest = EXCLUDED.is_latest, status = EXCLUDED.status,
                    error = EXCLUDED.error, uploaded_at = EXCLUDED.uploaded_at""",
            params)
        log.info("persisted source document %s v%s (%s) for session %s",
                 record.doc_id, record.version, record.status.value, record.session_id)

    def list_documents(self, session_id: str, latest_only: bool = True) -> list[DocumentRecord]:
        """Return a session's durable source-file registry in upload/version order.

        Args:
            session_id: The owning session id.
            latest_only: Whether to exclude superseded file versions.

        Returns:
            Source document records from Postgres.
        """
        where = "AND is_latest" if latest_only else ""
        rows = self._pg().query(
            f"""SELECT session_id, doc_id, version, filename, mime_type, sha256, storage_path,
                       is_latest, status, error, uploaded_at
                FROM {PgTables.DOCUMENTS}
                WHERE session_id = :sid {where} ORDER BY doc_id, version""",
            {"sid": session_id})
        return [DocumentRecord(**row) for row in rows]

    def delete(self, session_id: str, doc_id: str, version: int | None = None) -> int:
        """Delete one document or all of its versions from the durable registry.

        Args:
            session_id: The owning session id.
            doc_id: Stable id for the filename within its session.
            version: Optional version restriction.

        Returns:
            Count of rows removed.
        """
        condition = "AND version = :version" if version is not None else ""
        params = {"sid": session_id, "doc_id": doc_id, "version": version}
        count = self._pg().query(
            f"""SELECT COUNT(*) AS n FROM {PgTables.DOCUMENTS}
                WHERE session_id = :sid AND doc_id = :doc_id {condition}""", params)[0]["n"]
        self._pg().execute(
            f"""DELETE FROM {PgTables.DOCUMENTS}
                WHERE session_id = :sid AND doc_id = :doc_id {condition}""", params)
        return count

    def delete_all_for_session(self, session_id: str) -> int:
        """Delete every source-file record for the session.

        Args:
            session_id: The session being removed.

        Returns:
            Count of rows removed.
        """
        count = self._pg().query(
            f"SELECT COUNT(*) AS n FROM {PgTables.DOCUMENTS} WHERE session_id = :sid",
            {"sid": session_id})[0]["n"]
        self._pg().execute(f"DELETE FROM {PgTables.DOCUMENTS} WHERE session_id = :sid",
                           {"sid": session_id})
        return count


document_registry_handler = DocumentRegistryHandler()

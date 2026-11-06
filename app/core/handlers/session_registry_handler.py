"""Maps sessions (projects) to the user that owns them, and enforces that ownership."""

from app.config.constants import PgTables
from app.utilities.logger_util import get_logger
from app.utilities.pgsql_util import PgsqlUtil

log = get_logger(__name__)

_DDL = f"""
CREATE TABLE IF NOT EXISTS {PgTables.USER_SESSIONS} (
    id         SERIAL PRIMARY KEY,
    user_id    INT NOT NULL,
    session_id TEXT NOT NULL,
    title      TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (user_id, session_id)
);
"""

# Additive migration for tables created before `title` existed.
_ADD_TITLE = f"ALTER TABLE {PgTables.USER_SESSIONS} ADD COLUMN IF NOT EXISTS title TEXT"


class SessionRegistryHandler:
    def __init__(self) -> None:
        self._db: PgsqlUtil | None = None

    def _pg(self) -> PgsqlUtil:
        if self._db is None:
            self._db = PgsqlUtil()
        return self._db

    def ensure_table(self) -> None:
        self._pg().execute(_DDL)
        self._pg().execute(_ADD_TITLE)

    def record(self, user_id: int, session_id: str, title: str | None = None) -> None:
        # Keep any existing title when a later call (e.g. a document upload) has none.
        self._pg().execute(
            f"""INSERT INTO {PgTables.USER_SESSIONS} (user_id, session_id, title)
                VALUES (:u, :s, :t)
                ON CONFLICT (user_id, session_id)
                DO UPDATE SET title = COALESCE(:t, {PgTables.USER_SESSIONS}.title)""",
            {"u": user_id, "s": session_id, "t": title})

    def list_for_user(self, user_id: int) -> list[str]:
        rows = self._pg().query(
            f"SELECT session_id FROM {PgTables.USER_SESSIONS} WHERE user_id = :u", {"u": user_id})
        return [r["session_id"] for r in rows]

    def list_all_sessions(self) -> list[str]:
        """Every registered session id across all users. Used by the background sweep to
        reap abandoned uploads without needing to scan Redis for session keys."""
        rows = self._pg().query(f"SELECT DISTINCT session_id FROM {PgTables.USER_SESSIONS}")
        return [r["session_id"] for r in rows]

    def owns(self, user_id: int, session_id: str) -> bool:
        """Authorisation check for every per-session route: session ids are guessable,
        so ownership must be proven rather than assumed from a valid token."""
        rows = self._pg().query(
            f"""SELECT 1 FROM {PgTables.USER_SESSIONS}
                WHERE user_id = :u AND session_id = :s LIMIT 1""",
            {"u": user_id, "s": session_id})
        return bool(rows)

    def claimed_by_other(self, user_id: int, session_id: str) -> bool:
        """True when the session already belongs to a different user. Used on upload,
        where an unknown session id is a legitimate new project but someone else's is not."""
        rows = self._pg().query(
            f"""SELECT 1 FROM {PgTables.USER_SESSIONS}
                WHERE session_id = :s AND user_id <> :u LIMIT 1""",
            {"u": user_id, "s": session_id})
        return bool(rows)

    def list_detailed_for_user(self, user_id: int) -> list[dict]:
        rows = self._pg().query(
            f"""SELECT session_id, title, created_at FROM {PgTables.USER_SESSIONS}
                WHERE user_id = :u ORDER BY created_at""",
            {"u": user_id})
        return [{"session_id": r["session_id"], "title": r["title"],
                 "created_at": r["created_at"].isoformat() if r.get("created_at") else None}
                for r in rows]

    def update_title(self, user_id: int, session_id: str, title: str) -> bool:
        """Rename a project, identified by the (user_id, session_id) pair.

        Args:
            user_id: Owner of the project.
            session_id: Project (session) id.
            title: New title, already validated.

        Returns:
            True when a row was updated; False when this user has no such project.
        """
        # Matching on both columns means one user can never rename another's project.
        rows = self._pg().execute_returning(
            f"""UPDATE {PgTables.USER_SESSIONS} SET title = :t
                WHERE user_id = :u AND session_id = :s
                RETURNING session_id""",
            {"u": user_id, "s": session_id, "t": title})
        updated = bool(rows)
        log.info("session title update: user=%s session=%s updated=%s", user_id, session_id, updated)
        return updated

    def remove(self, user_id: int, session_id: str) -> None:
        self._pg().execute(
            f"DELETE FROM {PgTables.USER_SESSIONS} WHERE user_id = :u AND session_id = :s",
            {"u": user_id, "s": session_id})

    def remove_all_for_user(self, user_id: int) -> None:
        self._pg().execute(f"DELETE FROM {PgTables.USER_SESSIONS} WHERE user_id = :u", {"u": user_id})


session_registry_handler = SessionRegistryHandler()
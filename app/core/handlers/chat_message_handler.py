"""Durable chat history in Postgres — the system of record for the conversation.

Every user turn and every assistant final response is written here so the thread (and
the final response's structured metadata) survives a Redis eviction/restart or a
cleared browser cache. Redis (`session_store`) stays the fast working copy the agents
read mid-turn; Postgres is what the `/messages` API and any audit reads from.
"""

import json

from app.config.constants import PgTables, SessionMemoryConstants
from app.core.models.chat_message_model import PersistedChatMessage
from app.utilities.logger_util import get_logger
from app.utilities.pgsql_util import PgsqlUtil

log = get_logger(__name__)

_DDL = f"""
CREATE TABLE IF NOT EXISTS {PgTables.CHAT_MESSAGES} (
    id         SERIAL PRIMARY KEY,
    session_id TEXT NOT NULL,
    run_id     TEXT,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    result     TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);
"""

# Additive migration for tables created before workflow-run correlation existed.
_ADD_RUN_ID = f"ALTER TABLE {PgTables.CHAT_MESSAGES} ADD COLUMN IF NOT EXISTS run_id TEXT"

# Every read filters by session and orders by insertion.
_DDL_INDEX = (f"CREATE INDEX IF NOT EXISTS ix_{PgTables.CHAT_MESSAGES}_session "
              f"ON {PgTables.CHAT_MESSAGES} (session_id, id)")
_DDL_RUN_INDEX = (f"CREATE INDEX IF NOT EXISTS ix_{PgTables.CHAT_MESSAGES}_run "
                  f"ON {PgTables.CHAT_MESSAGES} (session_id, run_id, id)")


class ChatMessageHandler:
    def __init__(self) -> None:
        self._db: PgsqlUtil | None = None

    def _pg(self) -> PgsqlUtil:
        if self._db is None:
            self._db = PgsqlUtil()
        return self._db

    def ensure_table(self) -> None:
        """Create the table and its lookup index on first boot; a no-op thereafter."""
        self._pg().execute(_DDL)
        self._pg().execute(_ADD_RUN_ID)
        self._pg().execute(_DDL_INDEX)
        self._pg().execute(_DDL_RUN_INDEX)

    def record(self, session_id: str, role: str, content: str,
               result: dict | None = None, run_id: str | None = None) -> None:
        """Append one durable chat turn.

        Args:
            session_id: The session the turn belongs to.
            role: Who produced it — ``user`` | ``assistant`` | ``system``.
            content: The message text (the agent's reply for an assistant turn).
            result: The structured final-response payload (reply, intent, deck_version,
                deck_updated, gaps, …) for an assistant turn; ``None`` for user turns.
            run_id: Workflow invocation that produced the turn, when available.

        Returns:
            None.
        """
        self._pg().execute(
            f"""INSERT INTO {PgTables.CHAT_MESSAGES}
                (session_id, run_id, role, content, result)
                VALUES (:sid, :run_id, :role, :content, :result)""",
            {"sid": session_id, "run_id": run_id, "role": role, "content": content,
             "result": json.dumps(result) if result is not None else None})
        log.info("persisted %s chat turn for session %s run %s (%s chars)",
                 role, session_id, run_id or "-", len(content))

    def latest_session_memory(self, session_id: str) -> dict | None:
        """Return the newest persisted session snapshot, or None if there is none.

        Args:
            session_id: The session to look up.

        Returns:
            The snapshot dict from the newest assistant turn, or None when it is missing
            or was written with a different snapshot version.
        """
        # Skip rows without a snapshot, so one failed capture doesn't hide an older one.
        rows = self._pg().query(
            f"""SELECT result FROM {PgTables.CHAT_MESSAGES}
                WHERE session_id = :sid AND role = 'assistant' AND result LIKE :marker
                ORDER BY id DESC LIMIT 1""",
            {"sid": session_id,
             "marker": f'%"{SessionMemoryConstants.RESULT_KEY}": {{%'})
        if not rows:
            return None
        try:
            memory = (json.loads(rows[0]["result"]) or {}).get(SessionMemoryConstants.RESULT_KEY)
        except (json.JSONDecodeError, TypeError):
            log.warning("unreadable result row for session %s; no snapshot", session_id)
            return None
        # A snapshot from an older shape is ignored so it can't crash DeckDraft(**...).
        if not memory or memory.get("version") != SessionMemoryConstants.SNAPSHOT_VERSION:
            return None
        return memory

    @staticmethod
    def _to_message(row: dict) -> PersistedChatMessage:
        """Map a DB row to the model, decoding the JSON `result` text column."""
        row = dict(row)
        if isinstance(row.get("result"), str):
            try:
                row["result"] = json.loads(row["result"]) if row["result"] else None
            except json.JSONDecodeError:
                row["result"] = None
        return PersistedChatMessage(**row)

    def list_messages(self, session_id: str,
                      limit: int | None = None) -> list[PersistedChatMessage]:
        """Chat history in chronological order; `limit` returns only the last N turns
        (matching the Redis working copy's semantics), evaluated in SQL so a long thread
        isn't loaded just to be trimmed."""
        if limit:
            rows = self._pg().query(
                f"""SELECT * FROM (
                        SELECT id, session_id, run_id, role, content, result, created_at
                        FROM {PgTables.CHAT_MESSAGES}
                        WHERE session_id = :sid ORDER BY id DESC LIMIT :lim
                    ) t ORDER BY id""",
                {"sid": session_id, "lim": limit})
        else:
            rows = self._pg().query(
                f"""SELECT id, session_id, run_id, role, content, result, created_at
                    FROM {PgTables.CHAT_MESSAGES}
                    WHERE session_id = :sid ORDER BY id""",
                {"sid": session_id})
        return [self._to_message(r) for r in rows]

    def delete_all_for_session(self, session_id: str) -> int:
        """Drop every turn for a session (part of the session cascade delete); returns
        the number of rows removed."""
        count = self._pg().query(
            f"SELECT COUNT(*) AS n FROM {PgTables.CHAT_MESSAGES} WHERE session_id = :sid",
            {"sid": session_id})[0]["n"]
        self._pg().execute(
            f"DELETE FROM {PgTables.CHAT_MESSAGES} WHERE session_id = :sid",
            {"sid": session_id})
        return count


chat_message_handler = ChatMessageHandler()      # module-level singleton

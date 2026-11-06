"""Deck approval workflow persisted in Postgres."""

from datetime import datetime, timezone

from app.config.constants import ApprovalStatus, PgTables
from app.core.models.approval_model import Approval
from app.utilities.logger_util import get_logger
from app.utilities.pgsql_util import PgsqlUtil

log = get_logger(__name__)

_DDL = f"""
CREATE TABLE IF NOT EXISTS {PgTables.APPROVALS} (
    id           SERIAL PRIMARY KEY,
    session_id   TEXT NOT NULL,
    deck_version INT  NOT NULL,
    status       TEXT NOT NULL DEFAULT '{ApprovalStatus.PENDING}',
    submitted_by TEXT,
    decided_by   TEXT,
    decided_at   TIMESTAMPTZ,
    note         TEXT,
    created_at   TIMESTAMPTZ DEFAULT now(),
    UNIQUE (session_id, deck_version)
);
"""


class ApprovalHandler:
    def __init__(self) -> None:
        self._db: PgsqlUtil | None = None

    def _pg(self) -> PgsqlUtil:
        if self._db is None:
            self._db = PgsqlUtil()
        return self._db

    def ensure_table(self) -> None:
        self._pg().execute(_DDL)

    def submit(self, session_id: str, deck_version: int, submitted_by: str,
               note: str | None) -> Approval:
        self._pg().execute(
            f"""INSERT INTO {PgTables.APPROVALS}
                (session_id, deck_version, status, submitted_by, note)
                VALUES (:sid, :ver, :st, :by, :note)
                ON CONFLICT (session_id, deck_version)
                DO UPDATE SET status = EXCLUDED.status,
                              submitted_by = EXCLUDED.submitted_by,
                              note = EXCLUDED.note, decided_by = NULL, decided_at = NULL""",
            {"sid": session_id, "ver": deck_version, "st": ApprovalStatus.PENDING,
             "by": submitted_by, "note": note})
        return self.get(session_id, deck_version)  # type: ignore[return-value]

    def decide(self, session_id: str, deck_version: int, status: str,
               decided_by: str, note: str | None) -> Approval | None:
        self._pg().execute(
            f"""UPDATE {PgTables.APPROVALS}
                SET status = :st, decided_by = :by, decided_at = :ts,
                    note = COALESCE(:note, note)
                WHERE session_id = :sid AND deck_version = :ver""",
            {"st": status, "by": decided_by, "ts": datetime.now(timezone.utc),
             "note": note, "sid": session_id, "ver": deck_version})
        return self.get(session_id, deck_version)

    def get(self, session_id: str, deck_version: int) -> Approval | None:
        rows = self._pg().query(
            f"""SELECT * FROM {PgTables.APPROVALS}
                WHERE session_id = :sid AND deck_version = :ver""",
            {"sid": session_id, "ver": deck_version})
        return Approval(**rows[0]) if rows else None

    def list(self, status: str | None = None,
             session_id: str | None = None) -> list[Approval]:
        clauses, params = [], {}
        if status:
            clauses.append("status = :st")
            params["st"] = status
        if session_id:
            clauses.append("session_id = :sid")
            params["sid"] = session_id
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._pg().query(
            f"""SELECT a.*, u.title as project_title  FROM {PgTables.APPROVALS} a
              left join {PgTables.USER_SESSIONS} u on a.session_id = u.session_id
                {where} ORDER BY a.id DESC""", params)
        return [Approval(**r) for r in rows]


approval_handler = ApprovalHandler()

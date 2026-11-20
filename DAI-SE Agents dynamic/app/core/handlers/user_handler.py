"""Local mirror of directory users. Identities are federated, so no credentials here.

Users are provisioned just in time: the first successful Entra sign-in creates the
row, later sign-ins refresh it. Roles are re-derived from group membership on every
login, so this table records the last known grant rather than the authority.
"""

from app.config.constants import PgTables, Roles
from app.core.models.user_model import User, UserRecord
from app.utilities.logger_util import get_logger
from app.utilities.pgsql_util import PgsqlUtil

log = get_logger(__name__)

_DDL = f"""
CREATE TABLE IF NOT EXISTS {PgTables.USERS} (
    id            SERIAL PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    email         TEXT,
    display_name  TEXT,
    entra_oid     TEXT UNIQUE,
    roles         TEXT NOT NULL DEFAULT 'member',
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ DEFAULT now(),
    last_login_at TIMESTAMPTZ
);
"""

_COLUMNS = ("id, username, email, display_name, entra_oid, roles, is_active, "
            "created_at, last_login_at")


def _to_record(row: dict) -> UserRecord:
    """Map a database row onto the internal user model.

    Args:
        row: A row from the users table.

    Returns:
        The corresponding ``UserRecord``.
    """
    return UserRecord(
        id=row["id"], username=row["username"], email=row.get("email"),
        display_name=row.get("display_name"), entra_oid=row.get("entra_oid"),
        roles=[r for r in (row.get("roles") or "").split(",") if r],
        is_active=row.get("is_active", True), created_at=row.get("created_at"),
        last_login_at=row.get("last_login_at"),
    )


class UserHandler:
    def __init__(self) -> None:
        self._db: PgsqlUtil | None = None

    def _pg(self) -> PgsqlUtil:
        if self._db is None:
            self._db = PgsqlUtil()
        return self._db

    def ensure_table(self) -> None:
        """Create the canonical SSO users table when it does not already exist."""
        self._pg().execute(_DDL)

    def upsert_sso_user(self, *, entra_oid: str, username: str, email: str | None,
                        display_name: str | None, roles: list[str]) -> UserRecord:
        """Create or refresh the local row for a user who just signed in.

        An existing row is matched on the directory object id first, then on the
        principal name or e-mail so a pre-existing local account is adopted rather
        than duplicated.

        Args:
            entra_oid: Stable Entra object id of the user.
            username: userPrincipalName.
            email: Primary e-mail, when the directory publishes one.
            display_name: Human-readable name.
            roles: Effective roles granted by this sign-in.

        Returns:
            The stored user row.
        """
        clean = [r for r in roles if r in Roles.ALL] or [Roles.MEMBER]
        params = {"oid": entra_oid, "u": username,
                  "e": email, "d": display_name, "r": ",".join(clean),
                  "ul": (username or "").lower(), "el": (email or "").lower() or None}

        rows = self._pg().execute_returning(
            f"""UPDATE {PgTables.USERS}
                   SET entra_oid = :oid, username = :u, email = :e,
                       display_name = :d, roles = :r, last_login_at = now()
                 WHERE entra_oid = :oid
                    OR lower(username) = :ul
                    OR (:el IS NOT NULL AND lower(email) = :el)
             RETURNING {_COLUMNS}""", params)
        if rows:
            log.info("refreshed user '%s' (id=%s) from directory", username, rows[0]["id"])
            return _to_record(rows[0])

        # First sign-in for this identity: provision the local row just in time.
        rows = self._pg().execute_returning(
            f"""INSERT INTO {PgTables.USERS}
                    (username, email, display_name, entra_oid, roles, last_login_at)
                VALUES (:u, :e, :d, :oid, :r, now())
                ON CONFLICT (username) DO UPDATE
                    SET entra_oid = EXCLUDED.entra_oid, email = EXCLUDED.email,
                        display_name = EXCLUDED.display_name, roles = EXCLUDED.roles,
                        last_login_at = now()
             RETURNING {_COLUMNS}""", params)
        log.info("provisioned new user '%s' (id=%s)", username, rows[0]["id"])
        return _to_record(rows[0])

    def get_by_username(self, username: str) -> UserRecord | None:
        rows = self._pg().query(
            f"SELECT {_COLUMNS} FROM {PgTables.USERS} WHERE lower(username) = :u",
            {"u": username.lower()})
        return _to_record(rows[0]) if rows else None

    def get_by_id(self, user_id: int) -> UserRecord | None:
        rows = self._pg().query(
            f"SELECT {_COLUMNS} FROM {PgTables.USERS} WHERE id = :i", {"i": user_id})
        return _to_record(rows[0]) if rows else None

    def list_users(self) -> list[User]:
        rows = self._pg().query(f"SELECT {_COLUMNS} FROM {PgTables.USERS} ORDER BY id")
        return [User(**_to_record(r).model_dump()) for r in rows]


user_handler = UserHandler()

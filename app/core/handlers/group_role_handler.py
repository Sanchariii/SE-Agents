"""Entra group -> application role mapping, read from Postgres and cached in Redis.

The mapping table is directory-managed and changes rarely, so every login reads it
from Redis and only falls back to Postgres on a cache miss. That keeps the sign-in
path to a single fast round trip while still letting an administrator re-map a group
without a redeploy.
"""

from app.config.constants import AuthConstants, GroupRoleColumns, PgTables, RedisKeys, Roles
from app.utilities.logger_util import get_logger
from app.utilities.pgsql_util import PgsqlUtil
from app.utilities.redis_util import RedisUtil, get_redis

log = get_logger(__name__)

# Created only when absent: in a real environment the table is owned by the identity
# team and already populated. Column names mirror GroupRoleColumns' first spelling.
_DDL = f"""
CREATE TABLE IF NOT EXISTS {PgTables.GROUP_ROLE_MAPPING} (
    group_id     TEXT PRIMARY KEY,
    group_name   TEXT,
    display_name TEXT,
    role         TEXT NOT NULL
);
"""


def _pick(row: dict, candidates: tuple[str, ...]) -> str | None:
    """Read the first present column out of several accepted spellings.

    Args:
        row: A row from the mapping table.
        candidates: Accepted column names, most likely first.

    Returns:
        The column value as a string, or ``None`` when no candidate is present.
    """
    lowered = {str(k).lower(): v for k, v in row.items()}
    for name in candidates:
        value = lowered.get(name)
        if value not in (None, ""):
            return str(value).strip()
    return None


class GroupRoleHandler:
    """Loads and caches the ``group_id_role_mapping`` table."""

    def __init__(self, redis: RedisUtil | None = None) -> None:
        self._db: PgsqlUtil | None = None
        self._redis = redis

    def _pg(self) -> PgsqlUtil:
        if self._db is None:
            self._db = PgsqlUtil()
        return self._db

    def _r(self) -> RedisUtil:
        if self._redis is None:
            self._redis = get_redis()
        return self._redis

    def ensure_table(self) -> None:
        """Create the mapping table when it does not exist yet."""
        self._pg().execute(_DDL)

    def load_from_db(self) -> dict[str, str]:
        """Read every mapping row and normalise it to ``group_id -> app role``.

        Rows whose role is not recognised are skipped rather than silently granting
        something unintended.

        Returns:
            Mapping of directory group id to one of ``Roles.ALL``.
        """
        rows = self._pg().query(f"SELECT * FROM {PgTables.GROUP_ROLE_MAPPING}")
        mapping: dict[str, str] = {}
        for row in rows:
            group_id = _pick(row, GroupRoleColumns.GROUP_ID)
            raw_role = _pick(row, GroupRoleColumns.ROLE)
            if not group_id or not raw_role:
                continue
            role = Roles.GROUP_ROLE_ALIASES.get(raw_role.lower())
            if not role:
                log.warning("group '%s' maps to unknown role '%s'; ignoring",
                            group_id, raw_role)
                continue
            mapping[group_id] = role
        log.info("loaded %s group->role mapping(s) from Postgres", len(mapping))
        return mapping

    def get_mapping(self) -> dict[str, str]:
        """Return the group->role mapping, preferring the Redis cache.

        Cache faults are non-fatal: a Redis outage only costs one extra Postgres read.

        Returns:
            Mapping of directory group id to application role (empty if unavailable).
        """
        try:
            cached = self._r().get_json(RedisKeys.AUTH_GROUP_ROLES)
            if cached:
                return dict(cached)
        except Exception as exc:                    # noqa: BLE001
            log.warning("group->role cache read failed: %s", exc)

        mapping = self.load_from_db()
        try:
            self._r().set_json_ex(RedisKeys.AUTH_GROUP_ROLES, mapping,
                                  AuthConstants.GROUP_ROLE_CACHE_TTL_SECONDS)
        except Exception as exc:                    # noqa: BLE001
            log.warning("group->role cache write failed: %s", exc)
        return mapping

    def invalidate(self) -> None:
        """Drop the cached mapping so the next login re-reads Postgres."""
        self._r().delete(RedisKeys.AUTH_GROUP_ROLES)
        log.info("group->role cache invalidated")

    @staticmethod
    def highest_role(assigned_group_ids: list[str],
                     mapping: dict[str, str]) -> str | None:
        """Pick the strongest role granted by the user's group memberships.

        Args:
            assigned_group_ids: Group ids Graph confirmed the user belongs to.
            mapping: Group id -> role mapping from ``get_mapping``.

        Returns:
            The strongest role held, or ``None`` when no group grants one.
        """
        granted = {mapping[g] for g in assigned_group_ids if g in mapping}
        for role in Roles.PRECEDENCE:               # strongest first
            if role in granted:
                return role
        return None


group_role_handler = GroupRoleHandler()

"""Postgres access via a single pooled SQLAlchemy engine.

One engine per process: handlers share it instead of each opening their own pool,
which is what previously multiplied idle connections across the two services.
"""

from functools import lru_cache

from app.config.constants import PgConstants
from app.config.settings import get_settings
from app.utilities.logger_util import get_logger

log = get_logger(__name__)


@lru_cache
def _engine():
    from sqlalchemy import create_engine
    engine = create_engine(get_settings().pg_dsn, pool_pre_ping=True, future=True,
                           pool_size=PgConstants.POOL_SIZE,
                           max_overflow=PgConstants.MAX_OVERFLOW,
                           pool_recycle=PgConstants.POOL_RECYCLE_SECONDS)
    log.info("Postgres engine created (shared=true, pool_size=%s, max_overflow=%s, pool_recycle=%s)",
             engine.pool.size(), engine.pool._max_overflow, engine.pool._recycle)
    return engine


class PgsqlUtil:
    def __init__(self, dsn: str | None = None) -> None:
        if dsn is None:
            self.engine = _engine()
        else:
            from sqlalchemy import create_engine
            self.engine = create_engine(dsn, pool_pre_ping=True, future=True)
            log.info("Postgres engine created (shared=false, pool_size=%s, max_overflow=%s, pool_recycle=%s)",
                     self.engine.pool.size(), self.engine.pool._max_overflow, self.engine.pool._recycle)

    def execute(self, sql: str, params: dict | None = None):
        from sqlalchemy import text
        with self.engine.begin() as conn:
            return conn.execute(text(sql), params or {})

    def execute_many(self, sql: str, params: list[dict]) -> None:
        """Execute one parameterized statement for multiple rows in one transaction.

        Args:
            sql: Parameterized SQL statement.
            params: One parameter dictionary per row.

        Returns:
            None.
        """
        if not params:
            return
        from sqlalchemy import text
        with self.engine.begin() as conn:
            conn.execute(text(sql), params)

    def query(self, sql: str, params: dict | None = None) -> list[dict]:
        from sqlalchemy import text
        with self.engine.connect() as conn:
            rows = conn.execute(text(sql), params or {})
            return [dict(r._mapping) for r in rows]

    def execute_returning(self, sql: str, params: dict | None = None) -> list[dict]:
        """Run a writing statement in a transaction and read back its RETURNING rows.

        ``query`` opens a read-only connection that is never committed, so a write that
        needs its result (an upsert, say) has to go through here instead.

        Args:
            sql: Parameterised statement ending in ``RETURNING``.
            params: Bound parameters for the statement.

        Returns:
            The returned rows as dictionaries.
        """
        from sqlalchemy import text
        with self.engine.begin() as conn:
            rows = conn.execute(text(sql), params or {})
            return [dict(r._mapping) for r in rows]


def reset() -> None:
    _engine.cache_clear()
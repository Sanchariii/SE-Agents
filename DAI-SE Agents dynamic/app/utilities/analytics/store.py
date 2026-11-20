"""Postgres persistence for analytics facts and the model_pricing dimension.

Owns the idempotent DDL for the three fact tables and the pricing table, seeds
prices from ``model-pricing.json`` on startup, prices token usage, and batch-inserts
rows handed over by the background writer. All access goes through the shared pooled
``PgsqlUtil`` engine; this module never runs on the request event loop.
"""

import json
import threading

from app.config.constants import (
    AnalyticsConstants, AnalyticsPricingStatus, PgTables,
)
from app.utilities.logger_util import get_logger
from app.utilities.pgsql_util import PgsqlUtil

log = get_logger(__name__)


_WORKFLOW_DDL = f"""
CREATE TABLE IF NOT EXISTS {PgTables.ANALYTICS_WORKFLOW_RUNS} (
    run_id           TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL,
    user_id          INTEGER,
    turn_index       INTEGER,
    intent           TEXT,
    started_at       TIMESTAMPTZ NOT NULL,
    finished_at      TIMESTAMPTZ,
    wall_ms          INTEGER,
    status           TEXT NOT NULL,
    deck_version_id  INTEGER,
    slides_created   INTEGER DEFAULT 0,
    slides_updated   INTEGER DEFAULT 0,
    slides_translated INTEGER DEFAULT 0,
    created_at       TIMESTAMPTZ DEFAULT now()
);
"""

_AGENT_DDL = f"""
CREATE TABLE IF NOT EXISTS {PgTables.ANALYTICS_AGENT_RUNS} (
    id                  SERIAL PRIMARY KEY,
    agent_run_id        TEXT NOT NULL,
    workflow_run_id     TEXT,
    agent_name          TEXT NOT NULL,
    model               TEXT,
    started_at          TIMESTAMPTZ NOT NULL,
    finished_at         TIMESTAMPTZ,
    duration_ms         INTEGER,
    status              TEXT NOT NULL,
    llm_calls           INTEGER DEFAULT 0,
    input_tokens        INTEGER DEFAULT 0,
    output_tokens       INTEGER DEFAULT 0,
    cached_input_tokens INTEGER DEFAULT 0,
    cost_usd            NUMERIC(14, 6),
    work_units          INTEGER,
    work_unit_type      TEXT,
    pricing_status      TEXT,
    created_at          TIMESTAMPTZ DEFAULT now()
);
"""

_INGESTION_DDL = f"""
CREATE TABLE IF NOT EXISTS {PgTables.ANALYTICS_INGESTION_RUNS} (
    ingestion_run_id TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL,
    user_id          INTEGER,
    document_id      TEXT,
    started_at       TIMESTAMPTZ NOT NULL,
    finished_at      TIMESTAMPTZ,
    duration_ms      INTEGER,
    status           TEXT NOT NULL,
    bytes_processed  BIGINT,
    pages_processed  INTEGER,
    chunks_created   INTEGER,
    created_at       TIMESTAMPTZ DEFAULT now()
);
"""

_PRICING_DDL = f"""
CREATE TABLE IF NOT EXISTS {PgTables.MODEL_PRICING} (
    model                          TEXT PRIMARY KEY,
    input_cost_per_million         NUMERIC(12, 4),
    output_cost_per_million        NUMERIC(12, 4),
    cached_input_cost_per_million  NUMERIC(12, 4),
    currency                       TEXT DEFAULT '{AnalyticsConstants.DEFAULT_CURRENCY}',
    version                        TEXT,
    updated_at                     TIMESTAMPTZ DEFAULT now()
);
"""

_INDEXES = (
    f"CREATE INDEX IF NOT EXISTS ix_awr_started ON {PgTables.ANALYTICS_WORKFLOW_RUNS} (started_at)",
    f"CREATE INDEX IF NOT EXISTS ix_awr_session ON {PgTables.ANALYTICS_WORKFLOW_RUNS} (session_id, started_at)",
    f"CREATE INDEX IF NOT EXISTS ix_awr_user ON {PgTables.ANALYTICS_WORKFLOW_RUNS} (user_id, started_at)",
    f"CREATE INDEX IF NOT EXISTS ix_awr_status ON {PgTables.ANALYTICS_WORKFLOW_RUNS} (status, started_at)",
    f"CREATE INDEX IF NOT EXISTS ix_aar_workflow ON {PgTables.ANALYTICS_AGENT_RUNS} (workflow_run_id)",
    f"CREATE INDEX IF NOT EXISTS ix_aar_started ON {PgTables.ANALYTICS_AGENT_RUNS} (started_at)",
    f"CREATE INDEX IF NOT EXISTS ix_aar_agent ON {PgTables.ANALYTICS_AGENT_RUNS} (agent_name, started_at)",
    f"CREATE INDEX IF NOT EXISTS ix_aar_model ON {PgTables.ANALYTICS_AGENT_RUNS} (model, started_at)",
    f"CREATE INDEX IF NOT EXISTS ix_aar_status ON {PgTables.ANALYTICS_AGENT_RUNS} (status, started_at)",
    f"CREATE INDEX IF NOT EXISTS ix_air_started ON {PgTables.ANALYTICS_INGESTION_RUNS} (started_at)",
    f"CREATE INDEX IF NOT EXISTS ix_air_session ON {PgTables.ANALYTICS_INGESTION_RUNS} (session_id, started_at)",
    f"CREATE INDEX IF NOT EXISTS ix_air_user ON {PgTables.ANALYTICS_INGESTION_RUNS} (user_id, started_at)",
    f"CREATE INDEX IF NOT EXISTS ix_air_status ON {PgTables.ANALYTICS_INGESTION_RUNS} (status, started_at)",
)


class AnalyticsStore:
    """Owns analytics DDL, pricing, cost math and batch inserts."""

    def __init__(self) -> None:
        self._db: PgsqlUtil | None = None
        self._prices: dict[str, dict] = {}
        self._prices_lock = threading.Lock()

    def _pg(self) -> PgsqlUtil:
        """Return the lazily-created shared Postgres accessor."""
        if self._db is None:
            self._db = PgsqlUtil()
        return self._db

    def ensure_tables(self) -> None:
        """Create the analytics fact tables, the pricing table and their indexes.

        Idempotent so it is safe to call on every startup.

        Returns:
            None.
        """
        log.info("ensuring analytics tables")
        pg = self._pg()
        for ddl in (_WORKFLOW_DDL, _AGENT_DDL, _INGESTION_DDL, _PRICING_DDL):
            pg.execute(ddl)
        for index in _INDEXES:
            pg.execute(index)
        log.info("analytics tables are ready")

    # --- pricing ------------------------------------------------------------
    def seed_pricing(self) -> None:
        """Seed/refresh ``model_pricing`` from the repo-root pricing JSON.

        Idempotent upsert keyed on ``model``; the JSON carries only input/output
        cost, so cached-input cost is left null. Then the in-memory price cache is
        (re)loaded. Best effort: a missing or malformed file is logged, not raised.

        Returns:
            None.
        """
        path = AnalyticsConstants.PRICING_SEED_PATH
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            log.warning("model pricing seed not found at %s; pricing left empty", path)
            self._load_price_cache()
            return
        except (json.JSONDecodeError, OSError) as exc:
            log.error("failed to read model pricing seed %s: %s", path, exc)
            self._load_price_cache()
            return

        rows = [
            {"model": entry["model"],
             "input": entry.get("input_cost"),
             "output": entry.get("output_cost"),
             "currency": AnalyticsConstants.DEFAULT_CURRENCY,
             "version": AnalyticsConstants.PRICE_VERSION}
            for entry in entries
            if isinstance(entry, dict) and entry.get("model")
        ]
        if rows:
            self._pg().execute_many(
                f"""INSERT INTO {PgTables.MODEL_PRICING}
                    (model, input_cost_per_million, output_cost_per_million,
                     cached_input_cost_per_million, currency, version, updated_at)
                    VALUES (:model, :input, :output, NULL, :currency, :version, now())
                    ON CONFLICT (model) DO UPDATE SET
                        input_cost_per_million = EXCLUDED.input_cost_per_million,
                        output_cost_per_million = EXCLUDED.output_cost_per_million,
                        currency = EXCLUDED.currency,
                        version = EXCLUDED.version,
                        updated_at = now()""",
                rows)
        log.info("seeded %d model prices from %s", len(rows), path.name)
        self._load_price_cache()

    def _load_price_cache(self) -> None:
        """Load every pricing row into the in-memory cache used by :func:`price_for`."""
        try:
            rows = self._pg().query(
                f"""SELECT model, input_cost_per_million, output_cost_per_million,
                           cached_input_cost_per_million, currency
                    FROM {PgTables.MODEL_PRICING}""")
        except Exception as exc:                     # noqa: BLE001
            log.warning("could not load model pricing cache: %s", exc)
            return
        with self._prices_lock:
            self._prices = {r["model"]: r for r in rows}
        log.info("model pricing cache loaded (%d models)", len(rows))

    def price_for(self, model: str | None) -> dict | None:
        """Return the pricing row for ``model`` from cache, or ``None`` if unpriced.

        Args:
            model: Model/route name to price.

        Returns:
            The pricing dict (per-million costs), or ``None`` when the model has no
            configured price.
        """
        if not model:
            return None
        with self._prices_lock:
            return self._prices.get(model)

    def compute_cost(self, model: str | None, input_tokens: int, output_tokens: int,
                     cached_tokens: int = 0) -> tuple[float | None, str]:
        """Price token usage for one model, snapshotting the current rate.

        Args:
            model: Model/route name the tokens were spent on.
            input_tokens: Total input/prompt tokens.
            output_tokens: Total output/completion tokens.
            cached_tokens: Cached input tokens (billed at the cached rate when known).

        Returns:
            A ``(cost_usd, pricing_status)`` pair. ``cost_usd`` is ``None`` when the
            model is unpriced so cost is never silently treated as zero.
        """
        price = self.price_for(model)
        if price is None or price.get("input_cost_per_million") is None:
            return None, AnalyticsPricingStatus.UNPRICED
        in_rate = float(price["input_cost_per_million"])
        out_rate = float(price.get("output_cost_per_million") or 0.0)
        cached_rate = price.get("cached_input_cost_per_million")
        cached_rate = float(cached_rate) if cached_rate is not None else in_rate
        billable_input = max(input_tokens - cached_tokens, 0)
        cost = (billable_input * in_rate
                + cached_tokens * cached_rate
                + output_tokens * out_rate) / 1_000_000
        return round(cost, AnalyticsConstants.COST_PRECISION), AnalyticsPricingStatus.PRICED

    # --- inserts ------------------------------------------------------------
    def insert_workflow_runs(self, rows: list[dict]) -> None:
        """Batch-insert workflow-run rows.

        Args:
            rows: Workflow-run dictionaries matching the table columns.

        Returns:
            None.
        """
        if not rows:
            return
        self._pg().execute_many(
            f"""INSERT INTO {PgTables.ANALYTICS_WORKFLOW_RUNS}
                (run_id, session_id, user_id, turn_index, intent, started_at,
                 finished_at, wall_ms, status, deck_version_id,
                 slides_created, slides_updated, slides_translated)
                VALUES (:run_id, :session_id, :user_id, :turn_index, :intent, :started_at,
                        :finished_at, :wall_ms, :status, :deck_version_id,
                        :slides_created, :slides_updated, :slides_translated)
                ON CONFLICT (run_id) DO NOTHING""",
            rows)

    def insert_agent_runs(self, rows: list[dict]) -> None:
        """Batch-insert agent-run rows (one per agent-node x model).

        Args:
            rows: Agent-run dictionaries matching the table columns.

        Returns:
            None.
        """
        if not rows:
            return
        self._pg().execute_many(
            f"""INSERT INTO {PgTables.ANALYTICS_AGENT_RUNS}
                (agent_run_id, workflow_run_id, agent_name, model, started_at,
                 finished_at, duration_ms, status, llm_calls, input_tokens,
                 output_tokens, cached_input_tokens, cost_usd, work_units,
                 work_unit_type, pricing_status)
                VALUES (:agent_run_id, :workflow_run_id, :agent_name, :model, :started_at,
                        :finished_at, :duration_ms, :status, :llm_calls, :input_tokens,
                        :output_tokens, :cached_input_tokens, :cost_usd, :work_units,
                        :work_unit_type, :pricing_status)""",
            rows)

    def insert_ingestion_runs(self, rows: list[dict]) -> None:
        """Batch-insert ingestion-run rows.

        Args:
            rows: Ingestion-run dictionaries matching the table columns.

        Returns:
            None.
        """
        if not rows:
            return
        self._pg().execute_many(
            f"""INSERT INTO {PgTables.ANALYTICS_INGESTION_RUNS}
                (ingestion_run_id, session_id, user_id, document_id, started_at,
                 finished_at, duration_ms, status, bytes_processed,
                 pages_processed, chunks_created)
                VALUES (:ingestion_run_id, :session_id, :user_id, :document_id, :started_at,
                        :finished_at, :duration_ms, :status, :bytes_processed,
                        :pages_processed, :chunks_created)
                ON CONFLICT (ingestion_run_id) DO NOTHING""",
            rows)


#: Process-wide singleton (module-level, like the other handlers/utilities).
analytics_store = AnalyticsStore()

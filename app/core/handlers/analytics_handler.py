"""Read-only analytics aggregated from the backend's own tables.

Every method here runs blocking SQL against the shared pooled engine, so routes must
call them through ``asyncio.to_thread`` to keep the event loop free. Queries read the
analytics fact tables plus the existing business tables; nothing here writes.

Metrics are labelled by type so the UI can be honest about them:
``measured`` (observed), ``derived`` (computed from measured facts) and ``estimated``
(from an approved baseline). Missing values are returned as ``None`` — never zero — so
"no data" is never shown as a real result.
"""

from datetime import datetime, timedelta, timezone
from typing import Callable

from app.config.constants import (
    AnalyticsConstants, AnalyticsPricingStatus, AnalyticsStatus, PgTables,
)
from app.config.exceptions import UpstreamError
from app.utilities.analytics import dropped_event_count
from app.utilities.logger_util import get_logger
from app.utilities.pgsql_util import PgsqlUtil

log = get_logger(__name__)

_AWR = PgTables.ANALYTICS_WORKFLOW_RUNS
_AAR = PgTables.ANALYTICS_AGENT_RUNS
_AIR = PgTables.ANALYTICS_INGESTION_RUNS

# Runs the user stopped (status 'cancelled') count as usage only: their tokens and cost
# stay in every spend sum, but they are not runs - no run/turn counts, rates, latency or
# outcome metric includes them, and no new category is exposed. Every such query binds
# ``:cancelled`` (see ``_with_cancelled``) and filters with one of these fragments.
_COUNTED = "status <> :cancelled"
_W_COUNTED = "w.status <> :cancelled"


def _with_cancelled(params: dict) -> dict:
    """Return ``params`` plus the ``:cancelled`` bind used by the run filters."""
    return {**params, "cancelled": AnalyticsStatus.CANCELLED}


# Human-readable owner from the `users` row aliased `u`: the directory display name,
# falling back to the username so a known owner never renders as a placeholder.
_OWNER_NAME_SQL = "COALESCE(NULLIF(u.display_name, ''), u.username)"


class AnalyticsHandler:
    """Aggregates dashboard metrics from analytics facts and business tables."""

    def __init__(self) -> None:
        self._db: PgsqlUtil | None = None

    def _pg(self) -> PgsqlUtil:
        """Return the lazily-created shared Postgres accessor."""
        if self._db is None:
            self._db = PgsqlUtil()
        return self._db

    # --- helpers ------------------------------------------------------------
    def _scalar(self, sql: str, params: dict, key: str = "v"):
        """Run a one-row query and return the named column, or ``None`` if absent."""
        rows = self._pg().query(sql, params)
        return rows[0][key] if rows else None

    @staticmethod
    def _ratio(numerator, denominator) -> float | None:
        """Return numerator/denominator rounded, or ``None`` when the base is zero/absent."""
        if not denominator:
            return None
        return round((numerator or 0) / denominator, 4)

    # --- deprecated legacy summary -----------------------------------------
    def summary(self) -> dict:
        """Legacy overview retained for backward compatibility (deprecated).

        Superseded by :meth:`dashboard`. Kept working so existing callers do not
        break; new work should target ``/api/analytics/dashboard``.

        Returns:
            The original user/approval/deck counts, flagged as deprecated.
        """
        pg = self._pg()
        total_users = pg.query(f"SELECT COUNT(*) AS n FROM {PgTables.USERS}")[0]["n"]
        active_users = pg.query(
            f"SELECT COUNT(*) AS n FROM {PgTables.USERS} WHERE is_active")[0]["n"]
        by_status = {r["status"]: r["n"] for r in pg.query(
            f"SELECT status, COUNT(*) AS n FROM {PgTables.APPROVALS} GROUP BY status")}
        distinct_sessions = pg.query(
            f"SELECT COUNT(DISTINCT session_id) AS n FROM {PgTables.APPROVALS}")[0]["n"]
        return {
            "deprecated": True,
            "superseded_by": "/api/analytics/dashboard",
            "users": {"total": total_users, "active": active_users},
            "approvals": {
                "pending": by_status.get("pending", 0),
                "approved": by_status.get("approved", 0),
                "rejected": by_status.get("rejected", 0),
                "total": sum(by_status.values()),
            },
            "decks_submitted": distinct_sessions,
        }

    # --- dashboard sections -------------------------------------------------
    def dashboard(self, start: datetime, end: datetime, timezone_name: str = "UTC",
                  agent: str | None = None, model: str | None = None) -> dict:
        """Assemble the full sectioned dashboard for a time window.

        Args:
            start: Inclusive window start (UTC).
            end: Exclusive window end (UTC).
            timezone_name: Display timezone echoed back to the client.
            agent: Optional `agent_name` filter for the agent breakdown.
            model: Optional model filter for the agent and model breakdowns.

        Returns:
            A sectioned dashboard dict (overview, trends, adoption, performance,
            cost, quality, efficiency, agents, models, data_quality).
        """
        window = {"from": start.isoformat(), "to": end.isoformat(), "timezone": timezone_name}
        params = _with_cancelled({"start": start, "end": end})
        filters = {"agent": agent, "model": model}
        log.info("building analytics dashboard for window %s..%s (agent=%s, model=%s)",
                 window["from"], window["to"], agent, model)
        # Each section is built behind the same guard so a single failing query
        # produces one controlled error (with full context in the logs) instead of
        # an unhandled DB exception leaking internals to the client.
        sections: dict[str, Callable[[], object]] = {
            "overview": lambda: self._overview(params),
            "trends": lambda: self._trends(params),
            "adoption": lambda: self._adoption(params),
            "performance": lambda: self._performance(params),
            "cost": lambda: self._cost(params),
            "quality": lambda: self._quality(params),
            "efficiency": lambda: self._efficiency(params),
            "agents": lambda: self._agent_breakdown(params, filters),
            "models": lambda: self._model_breakdown(params, filters),
            "data_quality": lambda: self._data_quality(params),
        }
        result: dict = {"window": window, "filters": {"agent": agent, "model": model}}
        for name, build in sections.items():
            result[name] = self._build_section(name, build, window, filters)
        return result

    def _build_section(self, name: str, build: Callable[[], object],
                       window: dict, filters: dict) -> object:
        """Run one dashboard section, converting unexpected failures to a safe error.

        Args:
            name: The dashboard section name (e.g. ``"quality"``), used in logs and
                to locate the failing query.
            build: A zero-arg callable that computes and returns the section payload.
            window: The resolved ``{"from", "to", "timezone"}`` request window, logged
                for context when a section fails.
            filters: The active ``{"agent", "model"}`` filters, logged for context.

        Returns:
            The section payload returned by ``build``.

        Raises:
            UpstreamError: ``build`` raised; the original error is logged with full
                context (section, window, filters, exception type) and replaced with a
                generic 502 so database internals never reach the client.
        """
        try:
            return build()
        except Exception as exc:  # noqa: BLE001 - boundary: log context, don't leak internals
            # request/correlation id is stamped on the line by the logging filter.
            log.error("analytics dashboard section '%s' failed: window=%s..%s "
                      "agent=%s model=%s error=%s",
                      name, window["from"], window["to"], filters.get("agent"),
                      filters.get("model"), exc.__class__.__name__, exc_info=True)
            raise UpstreamError("analytics dashboard could not be generated") from exc

    def _bucket(self, start: datetime, end: datetime) -> str:
        """Choose a trend bucket granularity from the window length.

        Daily points stay readable up to ~90 days; longer ranges switch to weekly
        so a year-long window does not return hundreds of points.

        Args:
            start: Window start.
            end: Window end.

        Returns:
            ``'day'`` or ``'week'`` for ``date_trunc``.
        """
        return "day" if (end - start).days <= 90 else "week"

    def _trends(self, p: dict) -> dict:
        """Time-bucketed series of the headline metrics for trend charts.

        Answers "is adoption/output/spend growing?" with one point per bucket for
        active users, workflow runs, slides delivered and AI cost.

        Args:
            p: Query params carrying ``start``/``end``.

        Returns:
            A dict with the bucket granularity and a list of per-bucket points.
        """
        bucket = self._bucket(p["start"], p["end"])
        rows = self._pg().query(
            f"""WITH w AS (
                    SELECT date_trunc('{bucket}', started_at) AS bucket,
                           COUNT(*) FILTER (WHERE {_COUNTED}) AS workflow_runs,
                           COUNT(DISTINCT user_id) AS active_users,
                           COALESCE(SUM(slides_created + slides_updated)
                                    FILTER (WHERE status = :ok), 0) AS slides_delivered
                    FROM {_AWR}
                    WHERE started_at >= :start AND started_at < :end
                    GROUP BY 1),
                 c AS (
                    SELECT date_trunc('{bucket}', w2.started_at) AS bucket,
                           SUM(a.cost_usd) AS cost_usd
                    FROM {_AAR} a JOIN {_AWR} w2 ON a.workflow_run_id = w2.run_id
                    WHERE w2.started_at >= :start AND w2.started_at < :end
                    GROUP BY 1)
                SELECT w.bucket, w.workflow_runs, w.active_users, w.slides_delivered,
                       c.cost_usd
                FROM w LEFT JOIN c ON w.bucket = c.bucket
                ORDER BY w.bucket""",
            {**p, "ok": AnalyticsStatus.SUCCEEDED})
        return {
            "bucket": bucket,
            "points": [{
                "bucket": r["bucket"].isoformat() if r["bucket"] else None,
                "workflow_runs": r["workflow_runs"],
                "active_users": r["active_users"],
                "slides_delivered": int(r["slides_delivered"] or 0),
                "cost_usd": float(r["cost_usd"]) if r["cost_usd"] is not None else None,
            } for r in rows],
        }

    def _overview(self, p: dict) -> dict:
        """KPI cards: adoption, delivered output, typical latency, spend."""
        row = self._pg().query(
            f"""SELECT
                    COUNT(DISTINCT user_id)    AS active_users,
                    COUNT(DISTINCT session_id) AS projects,
                    COUNT(*) FILTER (WHERE {_COUNTED}) AS workflow_runs,
                    COALESCE(SUM(slides_created + slides_updated)
                             FILTER (WHERE status = :ok), 0) AS slides_delivered,
                    percentile_cont(0.5) WITHIN GROUP (ORDER BY wall_ms)
                             FILTER (WHERE status = :ok) AS median_turnaround_ms
                FROM {_AWR}
                WHERE started_at >= :start AND started_at < :end""",
            {**p, "ok": AnalyticsStatus.SUCCEEDED})[0]
        successful_projects = self._scalar(
            f"""SELECT COUNT(DISTINCT session_id) AS v FROM {_AWR}
                WHERE started_at >= :start AND started_at < :end
                  AND status = :ok AND deck_version_id IS NOT NULL""",
            {**p, "ok": AnalyticsStatus.SUCCEEDED})
        total_cost = self._scalar(
            f"""SELECT SUM(a.cost_usd) AS v FROM {_AAR} a JOIN {_AWR} w
                ON a.workflow_run_id = w.run_id
                WHERE w.started_at >= :start AND w.started_at < :end""", p)
        return {
            "active_users": {"value": row["active_users"], "type": "measured"},
            "projects_worked_on": {"value": row["projects"], "type": "measured"},
            "successful_projects": {"value": successful_projects, "type": "measured"},
            "slides_delivered": {"value": int(row["slides_delivered"]), "type": "measured"},
            "workflow_runs": {"value": row["workflow_runs"], "type": "measured"},
            "median_ai_turnaround_ms": {
                "value": int(row["median_turnaround_ms"]) if row["median_turnaround_ms"] is not None else None,
                "type": "measured"},
            "total_ai_cost_usd": {
                "value": float(total_cost) if total_cost is not None else None,
                "type": "measured"},
        }

    def _adoption(self, p: dict) -> dict:
        """Engagement: turns per project and the multi-turn project rate."""
        row = self._pg().query(
            f"""WITH per_project AS (
                    SELECT session_id, COUNT(*) AS turns
                    FROM {_AWR}
                    WHERE started_at >= :start AND started_at < :end AND {_COUNTED}
                    GROUP BY session_id)
                SELECT COUNT(*) AS projects,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY turns) AS median_turns,
                       AVG(turns) AS avg_turns,
                       COUNT(*) FILTER (WHERE turns >= 2) AS multi_turn_projects
                FROM per_project""", p)[0]
        projects = row["projects"] or 0
        return {
            "projects": {"value": projects, "type": "measured"},
            "median_turns_per_project": {
                "value": float(row["median_turns"]) if row["median_turns"] is not None else None,
                "type": "measured"},
            "avg_turns_per_project": {
                "value": round(float(row["avg_turns"]), 2) if row["avg_turns"] is not None else None,
                "type": "derived"},
            "multi_turn_projects": {"value": row["multi_turn_projects"], "type": "measured"},
            "multi_turn_rate": {
                "value": self._ratio(row["multi_turn_projects"], projects), "type": "derived",
                "count": row["multi_turn_projects"], "of": projects},
        }

    def _performance(self, p: dict) -> dict:
        """Latency split: user-visible wait (wall time) vs agent processing time."""
        wall = self._pg().query(
            f"""SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY wall_ms) AS p50,
                       percentile_cont({AnalyticsConstants.P95}) WITHIN GROUP (ORDER BY wall_ms) AS p95,
                       SUM(wall_ms) AS total_wall_ms
                FROM {_AWR}
                WHERE started_at >= :start AND started_at < :end AND wall_ms IS NOT NULL
                  AND {_COUNTED}""", p)[0]
        agent_ms = self._scalar(
            f"""SELECT SUM(a.duration_ms) AS v FROM {_AAR} a JOIN {_AWR} w
                ON a.workflow_run_id = w.run_id
                WHERE w.started_at >= :start AND w.started_at < :end
                  AND {_W_COUNTED}""", p)
        return {
            "ai_turnaround_ms": {
                "median": int(wall["p50"]) if wall["p50"] is not None else None,
                "p95": int(wall["p95"]) if wall["p95"] is not None else None,
                "total": int(wall["total_wall_ms"]) if wall["total_wall_ms"] is not None else None,
                "type": "measured",
                "note": "user-visible wait; agents may run in parallel"},
            "agent_processing_ms": {
                "total": int(agent_ms) if agent_ms is not None else None,
                "type": "measured",
                "note": "summed agent-active time; can exceed wall time under parallelism"},
        }

    def _cost(self, p: dict) -> dict:
        """Token and cost totals plus per-project, per-slide and per-call ratios."""
        row = self._pg().query(
            f"""SELECT SUM(a.cost_usd) AS cost,
                       SUM(a.input_tokens) AS input_tokens,
                       SUM(a.output_tokens) AS output_tokens,
                       SUM(a.cached_input_tokens) AS cached_tokens,
                       SUM(a.llm_calls) AS llm_calls
                FROM {_AAR} a JOIN {_AWR} w ON a.workflow_run_id = w.run_id
                WHERE w.started_at >= :start AND w.started_at < :end""", p)[0]
        # Denominators exclude projects with no delivered slides so per-unit stays honest.
        delivered = self._pg().query(
            f"""SELECT COUNT(DISTINCT session_id) AS projects,
                       COALESCE(SUM(slides_created + slides_updated)
                                FILTER (WHERE status = :ok), 0) AS slides
                FROM {_AWR}
                WHERE started_at >= :start AND started_at < :end""",
            {**p, "ok": AnalyticsStatus.SUCCEEDED})[0]
        cost = float(row["cost"]) if row["cost"] is not None else None
        tokens = (row["input_tokens"] or 0) + (row["output_tokens"] or 0)
        slides = int(delivered["slides"]) or 0
        projects = delivered["projects"] or 0
        return {
            "total_cost_usd": {"value": cost, "type": "measured"},
            "input_tokens": {"value": row["input_tokens"] or 0, "type": "measured"},
            "output_tokens": {"value": row["output_tokens"] or 0, "type": "measured"},
            "cached_input_tokens": {"value": row["cached_tokens"] or 0, "type": "measured"},
            "total_tokens": {"value": tokens, "type": "measured"},
            "llm_calls": {"value": row["llm_calls"] or 0, "type": "measured"},
            "cost_per_project_usd": {"value": self._ratio(cost, projects), "type": "derived",
                                     "denominator": projects},
            "cost_per_slide_usd": {"value": self._ratio(cost, slides), "type": "derived",
                                   "denominator": slides},
            "cost_per_call_usd": {"value": self._ratio(cost, row["llm_calls"]), "type": "derived",
                                  "denominator": row["llm_calls"] or 0},
            "tokens_per_slide": {"value": self._ratio(tokens, slides), "type": "derived",
                                 "denominator": slides},
            "tokens_per_call": {"value": self._ratio(tokens, row["llm_calls"]), "type": "derived",
                                "denominator": row["llm_calls"] or 0},
        }

    def _quality(self, p: dict) -> dict:
        """Fallback rate, rework rate and per-version user feedback."""
        agent = self._pg().query(
            f"""SELECT COUNT(*) FILTER (WHERE {_W_COUNTED}) AS runs,
                       COUNT(*) FILTER (WHERE a.status = :fb AND {_W_COUNTED}) AS fallback_runs,
                       COUNT(*) FILTER (WHERE a.status = :failed AND {_W_COUNTED}) AS failed_runs
                FROM {_AAR} a JOIN {_AWR} w ON a.workflow_run_id = w.run_id
                WHERE w.started_at >= :start AND w.started_at < :end""",
            {**p, "fb": AnalyticsStatus.FALLBACK, "failed": AnalyticsStatus.FAILED})[0]
        # Rework: a published project that then took another slide-changing turn.
        rework = self._pg().query(
            f"""WITH pub AS (
                    SELECT session_id, MIN(started_at) FILTER (
                        WHERE status = :ok AND deck_version_id IS NOT NULL) AS first_pub
                    FROM {_AWR}
                    WHERE started_at >= :start AND started_at < :end
                    GROUP BY session_id)
                SELECT COUNT(*) FILTER (WHERE first_pub IS NOT NULL) AS published,
                       COUNT(*) FILTER (WHERE first_pub IS NOT NULL AND EXISTS (
                           SELECT 1 FROM {_AWR} r
                           WHERE r.session_id = pub.session_id
                             AND r.started_at > pub.first_pub
                             AND r.status <> :cancelled
                             AND (r.slides_updated > 0 OR r.slides_created > 0))) AS reworked
                FROM pub""", {**p, "ok": AnalyticsStatus.SUCCEEDED})[0]
        feedback = self._pg().query(
            f"""SELECT COUNT(*) FILTER (WHERE thumbs_up IS NOT NULL) AS rated,
                       COUNT(*) FILTER (WHERE thumbs_up IS TRUE) AS thumbs_up,
                       COUNT(*) FILTER (WHERE thumbs_up IS FALSE) AS thumbs_down,
                       COUNT(*) FILTER (WHERE user_comment IS NOT NULL AND user_comment <> '')
                                AS with_comment
                FROM {PgTables.DECK_VERSIONS}
                WHERE created_at >= :start AND created_at < :end""", p)[0]
        return {
            "agent_runs": {"value": agent["runs"], "type": "measured"},
            "fallback_rate": {"value": self._ratio(agent["fallback_runs"], agent["runs"]),
                              "type": "derived", "count": agent["fallback_runs"],
                              "of": agent["runs"]},
            "agent_failure_rate": {"value": self._ratio(agent["failed_runs"], agent["runs"]),
                                   "type": "derived", "count": agent["failed_runs"],
                                   "of": agent["runs"]},
            "rework_rate": {"value": self._ratio(rework["reworked"], rework["published"]),
                            "type": "derived", "count": rework["reworked"],
                            "of": rework["published"]},
            "feedback": {
                "rated_versions": {"value": feedback["rated"], "type": "measured"},
                "thumbs_up": {"value": feedback["thumbs_up"], "type": "measured"},
                "thumbs_down": {"value": feedback["thumbs_down"], "type": "measured"},
                "thumbs_up_rate": {"value": self._ratio(feedback["thumbs_up"], feedback["rated"]),
                                   "type": "derived", "count": feedback["thumbs_up"],
                                   "of": feedback["rated"]},
                "comment_coverage": {"value": self._ratio(feedback["with_comment"], feedback["rated"]),
                                     "type": "derived", "count": feedback["with_comment"],
                                     "of": feedback["rated"]},
            },
        }

    def _efficiency(self, p: dict) -> dict:
        """Phase-2 estimated value: hours saved and cost/leverage ratios."""
        counts = self._pg().query(
            f"""SELECT
                    COALESCE(SUM(slides_created) FILTER (WHERE status = :ok), 0) AS new_slides,
                    COALESCE(SUM(slides_updated) FILTER (WHERE status = :ok), 0) AS revised_slides,
                    COALESCE(SUM(slides_translated) FILTER (WHERE status = :ok), 0) AS translated_slides,
                    COUNT(*) FILTER (WHERE status = :ok AND deck_version_id IS NOT NULL) AS decks,
                    COALESCE(SUM(wall_ms) FILTER (WHERE {_COUNTED}), 0) AS wall_ms
                FROM {_AWR}
                WHERE started_at >= :start AND started_at < :end""",
            {**p, "ok": AnalyticsStatus.SUCCEEDED})[0]
        pages = self._scalar(
            f"""SELECT SUM(pages_processed) AS v FROM {_AIR}
                WHERE started_at >= :start AND started_at < :end AND status <> :failed""",
            {**p, "failed": AnalyticsStatus.FAILED}) or 0
        c = AnalyticsConstants
        manual = (pages * c.BASELINE_READ_MINUTES_PER_PAGE
                  + counts["new_slides"] * c.BASELINE_AUTHOR_MINUTES_PER_SLIDE
                  + counts["revised_slides"] * c.BASELINE_EDIT_MINUTES_PER_SLIDE
                  + counts["translated_slides"] * c.BASELINE_TRANSLATE_MINUTES_PER_SLIDE
                  + counts["decks"] * c.BASELINE_ASSEMBLE_MINUTES_PER_DECK)
        ai_minutes = (counts["wall_ms"] / 60000.0
                      + counts["decks"] * c.BASELINE_REVIEW_MINUTES_PER_VERSION)
        hours_saved = max(manual - ai_minutes, 0) / 60.0
        cost = self._scalar(
            f"""SELECT SUM(a.cost_usd) AS v FROM {_AAR} a JOIN {_AWR} w
                ON a.workflow_run_id = w.run_id
                WHERE w.started_at >= :start AND w.started_at < :end""", p)
        cost = float(cost) if cost is not None else None
        # Confidence is low until page counts and baselines are calibrated.
        confidence = "low" if not pages else "medium"
        return {
            "estimated_hours_saved": {"value": round(hours_saved, 2), "type": "estimated"},
            "hours_saved_per_dollar": {"value": self._ratio(hours_saved, cost),
                                       "type": "estimated"},
            "time_leverage": {"value": self._ratio(manual, ai_minutes), "type": "estimated"},
            "estimate": {
                "is_estimated": True,
                "confidence": confidence,
                "baseline_version": c.BASELINE_VERSION,
            },
        }

    @staticmethod
    def _agent_filter_sql(filters: dict | None) -> tuple[str, dict]:
        """Build an optional ``AND`` clause for agent/model filtering.

        Args:
            filters: Optional dict with ``agent`` and/or ``model`` keys.

        Returns:
            A ``(sql_fragment, extra_params)`` pair; the fragment is empty when no
            filter is set.
        """
        clauses, extra = [], {}
        if filters and filters.get("agent"):
            clauses.append("a.agent_name = :f_agent")
            extra["f_agent"] = filters["agent"]
        if filters and filters.get("model"):
            clauses.append("a.model = :f_model")
            extra["f_model"] = filters["model"]
        return ((" AND " + " AND ".join(clauses)) if clauses else "", extra)

    def _agent_breakdown(self, p: dict, filters: dict | None = None) -> list[dict]:
        """Per agent_name x model: runs, tokens, cost, latency, success/fallback."""
        where, extra = self._agent_filter_sql(filters)
        rows = self._pg().query(
            f"""SELECT a.agent_name, a.model,
                       COUNT(*) FILTER (WHERE {_W_COUNTED}) AS runs,
                       COUNT(*) FILTER (WHERE a.status = :ok AND {_W_COUNTED}) AS succeeded,
                       COUNT(*) FILTER (WHERE a.status = :fb AND {_W_COUNTED}) AS fallback,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY a.duration_ms)
                           FILTER (WHERE {_W_COUNTED}) AS median_ms,
                       percentile_cont({AnalyticsConstants.P95}) WITHIN GROUP (ORDER BY a.duration_ms)
                           FILTER (WHERE {_W_COUNTED}) AS p95_ms,
                       SUM(a.llm_calls) AS llm_calls,
                       SUM(a.input_tokens) AS input_tokens,
                       SUM(a.output_tokens) AS output_tokens,
                       SUM(a.cached_input_tokens) AS cached_tokens,
                       SUM(a.cost_usd) AS cost,
                       -- per-run averages use the counted runs on both sides of the ratio
                       SUM(a.cost_usd) FILTER (WHERE {_W_COUNTED}) AS counted_cost,
                       SUM(a.input_tokens + a.output_tokens)
                           FILTER (WHERE {_W_COUNTED}) AS counted_tokens
                FROM {_AAR} a JOIN {_AWR} w ON a.workflow_run_id = w.run_id
                WHERE w.started_at >= :start AND w.started_at < :end{where}
                GROUP BY a.agent_name, a.model
                ORDER BY cost DESC NULLS LAST, runs DESC""",
            {**p, **extra, "ok": AnalyticsStatus.SUCCEEDED, "fb": AnalyticsStatus.FALLBACK})
        return [self._agent_row(r) for r in rows]

    def _model_breakdown(self, p: dict, filters: dict | None = None) -> list[dict]:
        """Roll agent activity up by model: runs, tokens, cost, latency, failures."""
        where, extra = self._agent_filter_sql(filters)
        rows = self._pg().query(
            f"""SELECT a.model,
                       COUNT(*) FILTER (WHERE {_W_COUNTED}) AS runs,
                       COUNT(*) FILTER (WHERE a.status = :failed AND {_W_COUNTED}) AS failed,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY a.duration_ms)
                           FILTER (WHERE {_W_COUNTED}) AS median_ms,
                       SUM(a.llm_calls) AS llm_calls,
                       SUM(a.input_tokens) AS input_tokens,
                       SUM(a.output_tokens) AS output_tokens,
                       SUM(a.cost_usd) AS cost
                FROM {_AAR} a JOIN {_AWR} w ON a.workflow_run_id = w.run_id
                WHERE w.started_at >= :start AND w.started_at < :end{where}
                GROUP BY a.model
                ORDER BY cost DESC NULLS LAST""",
            {**p, **extra, "failed": AnalyticsStatus.FAILED})
        return [{
            "model": r["model"],
            "runs": r["runs"],
            "failure_rate": self._ratio(r["failed"], r["runs"]),
            "median_duration_ms": int(r["median_ms"]) if r["median_ms"] is not None else None,
            "llm_calls": r["llm_calls"] or 0,
            "input_tokens": r["input_tokens"] or 0,
            "output_tokens": r["output_tokens"] or 0,
            "cost_usd": float(r["cost"]) if r["cost"] is not None else None,
        } for r in rows]

    def _agent_row(self, r: dict) -> dict:
        """Shape one agent x model aggregate row with per-run derived ratios."""
        cost = float(r["cost"]) if r["cost"] is not None else None
        counted_cost = r.get("counted_cost", r["cost"])
        counted_cost = float(counted_cost) if counted_cost is not None else None
        counted_tokens = r.get("counted_tokens")
        if counted_tokens is None:
            counted_tokens = (r["input_tokens"] or 0) + (r["output_tokens"] or 0)
        return {
            "agent_name": r["agent_name"],
            "model": r["model"],
            "runs": r["runs"],
            "success_rate": self._ratio(r["succeeded"], r["runs"]),
            "fallback_rate": self._ratio(r["fallback"], r["runs"]),
            "median_duration_ms": int(r["median_ms"]) if r["median_ms"] is not None else None,
            "p95_duration_ms": int(r["p95_ms"]) if r["p95_ms"] is not None else None,
            "llm_calls": r["llm_calls"] or 0,
            "input_tokens": r["input_tokens"] or 0,
            "output_tokens": r["output_tokens"] or 0,
            "cached_input_tokens": r["cached_tokens"] or 0,
            "cost_usd": cost,
            "tokens_per_run": self._ratio(counted_tokens, r["runs"]),
            "cost_per_run_usd": self._ratio(counted_cost, r["runs"]),
        }

    def _data_quality(self, p: dict) -> dict:
        """Trust indicators: unpriced runs, missing page counts, dropped events."""
        unpriced = self._scalar(
            f"""SELECT COUNT(*) AS v FROM {_AAR} a JOIN {_AWR} w
                ON a.workflow_run_id = w.run_id
                WHERE w.started_at >= :start AND w.started_at < :end
                  AND a.pricing_status = :unpriced""",
            {**p, "unpriced": AnalyticsPricingStatus.UNPRICED})
        missing_pages = self._scalar(
            f"""SELECT COUNT(*) AS v FROM {_AIR}
                WHERE started_at >= :start AND started_at < :end
                  AND status <> :failed AND pages_processed IS NULL""",
            {**p, "failed": AnalyticsStatus.FAILED})
        return {
            "unpriced_agent_runs": unpriced or 0,
            "missing_page_counts": missing_pages or 0,
            "dropped_metric_events": dropped_event_count(),
        }

    # --- project table + drill-down ----------------------------------------
    def projects(self, start: datetime, end: datetime, *, limit: int, offset: int,
                 sort: str, order: str) -> dict:
        """Paginated, sortable project table for the window.

        Args:
            start: Inclusive window start (UTC).
            end: Exclusive window end (UTC).
            limit: Page size.
            offset: Row offset.
            sort: One of ``last_active``, ``turns``, ``cost``, ``tokens``, ``duration``.
            order: ``asc`` or ``desc``.

        Returns:
            A dict with ``items`` and ``pagination``. Each item carries the owner's
            ``user_id`` and ``owner_name`` (display name, falling back to username).
        """
        # Qualified so the extra owner joins can never make a sort key ambiguous.
        sort_columns = {
            "last_active": "w.last_active", "turns": "w.turns", "cost": "a.cost_usd",
            "tokens": "a.total_tokens", "duration": "w.ai_turnaround_ms",
        }
        sort_sql = sort_columns.get(sort, "w.last_active")
        order_sql = "ASC" if order.lower() == "asc" else "DESC"
        params = _with_cancelled({"start": start, "end": end, "limit": limit,
                                  "offset": offset, "ok": AnalyticsStatus.SUCCEEDED})
        log.info("listing analytics projects for window %s..%s (limit=%s, offset=%s, "
                 "sort=%s %s)", start.isoformat(), end.isoformat(), limit, offset,
                 sort, order_sql)
        total = self._scalar(
            f"""SELECT COUNT(DISTINCT session_id) AS v FROM {_AWR}
                WHERE started_at >= :start AND started_at < :end""", params)
        rows = self._pg().query(
            f"""WITH w AS (
                    SELECT session_id,
                           MAX(user_id) AS user_id,
                           COUNT(*) FILTER (WHERE {_COUNTED}) AS turns,
                           MAX(started_at) AS last_active,
                           SUM(wall_ms) FILTER (WHERE {_COUNTED}) AS ai_turnaround_ms,
                           COALESCE(SUM(slides_created + slides_updated)
                                    FILTER (WHERE status = :ok), 0) AS slides_delivered,
                           BOOL_OR(status = :ok AND deck_version_id IS NOT NULL) AS published
                    FROM {_AWR}
                    WHERE started_at >= :start AND started_at < :end
                    GROUP BY session_id),
                 a AS (
                    SELECT w2.session_id,
                           SUM(ar.cost_usd) AS cost_usd,
                           SUM(ar.input_tokens + ar.output_tokens) AS total_tokens,
                           SUM(ar.duration_ms) FILTER (WHERE w2.status <> :cancelled) AS agent_ms
                    FROM {_AAR} ar JOIN {_AWR} w2 ON ar.workflow_run_id = w2.run_id
                    WHERE w2.started_at >= :start AND w2.started_at < :end
                    GROUP BY w2.session_id),
                 o AS (
                    -- Registered owner: fills the gap when the telemetry row's
                    -- best-effort user_id was not resolved at run time.
                    SELECT us.session_id, MIN(us.user_id) AS user_id
                    FROM {PgTables.USER_SESSIONS} us
                    WHERE us.session_id IN (SELECT session_id FROM w)
                    GROUP BY us.session_id)
                SELECT w.session_id,
                       COALESCE(w.user_id, o.user_id) AS user_id,
                       {_OWNER_NAME_SQL} AS owner_name,
                       w.turns, w.last_active,
                       w.ai_turnaround_ms, w.slides_delivered, w.published,
                       a.cost_usd, a.total_tokens, a.agent_ms
                FROM w
                LEFT JOIN a ON w.session_id = a.session_id
                LEFT JOIN o ON w.session_id = o.session_id
                LEFT JOIN {PgTables.USERS} u ON u.id = COALESCE(w.user_id, o.user_id)
                ORDER BY {sort_sql} {order_sql} NULLS LAST
                LIMIT :limit OFFSET :offset""", params)
        items = [{
            "session_id": r["session_id"],
            "user_id": r["user_id"],
            "owner_name": r.get("owner_name"),
            "turns": r["turns"],
            "multi_turn": (r["turns"] or 0) >= 2,
            "published": bool(r["published"]),
            "slides_delivered": int(r["slides_delivered"] or 0),
            "ai_turnaround_ms": int(r["ai_turnaround_ms"]) if r["ai_turnaround_ms"] is not None else None,
            "agent_processing_ms": int(r["agent_ms"]) if r["agent_ms"] is not None else None,
            "total_tokens": int(r["total_tokens"] or 0),
            "cost_usd": float(r["cost_usd"]) if r["cost_usd"] is not None else None,
            "last_active": r["last_active"].isoformat() if r["last_active"] else None,
        } for r in rows]
        log.info("analytics projects listed: %s of %s", len(items), total or 0)
        return {"items": items,
                "pagination": {"total": total or 0, "limit": limit, "offset": offset}}

    def _project_owner(self, session_id: str) -> dict:
        """Resolve a project's owner id and human-readable name.

        Prefers the user recorded on the session's latest workflow run and falls back
        to the ``user_sessions`` registration, then joins ``users`` for the name.

        Args:
            session_id: The project/session id.

        Returns:
            ``{"user_id", "owner_name"}``; both ``None`` when the owner is unknown.
        """
        rows = self._pg().query(
            f"""SELECT COALESCE(r.user_id, us.user_id) AS user_id,
                       {_OWNER_NAME_SQL} AS owner_name
                FROM (SELECT CAST(:sid AS TEXT) AS session_id) s
                LEFT JOIN LATERAL (
                    SELECT wr.user_id FROM {_AWR} wr
                    WHERE wr.session_id = s.session_id AND wr.user_id IS NOT NULL
                    ORDER BY wr.started_at DESC LIMIT 1) r ON TRUE
                LEFT JOIN LATERAL (
                    SELECT x.user_id FROM {PgTables.USER_SESSIONS} x
                    WHERE x.session_id = s.session_id
                    ORDER BY x.created_at LIMIT 1) us ON TRUE
                LEFT JOIN {PgTables.USERS} u ON u.id = COALESCE(r.user_id, us.user_id)""",
            {"sid": session_id})
        row = rows[0] if rows else {}
        return {"user_id": row.get("user_id"), "owner_name": row.get("owner_name")}

    def project_detail(self, session_id: str) -> dict:
        """Timeline of workflow runs and every agent-node run for one project.

        Args:
            session_id: The project/session id.

        Returns:
            A dict with the project owner (``user_id``/``owner_name``), the
            workflow-run timeline (each run's deck version with its ``plan_title``)
            and per-agent-node runs (each with its model, tokens, cost, duration and
            status).
        """
        log.info("building analytics project detail for session %s", session_id)
        owner = self._project_owner(session_id)
        # ppt_versions is keyed by (session_id, deck_version); deck_version_id on the
        # run holds that version number, so the join yields the version's plan title.
        runs = self._pg().query(
            f"""SELECT r.run_id, r.turn_index, r.intent, r.started_at, r.finished_at,
                       r.wall_ms, r.status, r.deck_version_id, v.plan_title,
                       r.slides_created, r.slides_updated, r.slides_translated
                FROM {_AWR} r
                LEFT JOIN {PgTables.DECK_VERSIONS} v
                       ON v.session_id = r.session_id AND v.deck_version = r.deck_version_id
                WHERE r.session_id = :sid AND r.status <> :cancelled
                ORDER BY r.started_at""", _with_cancelled({"sid": session_id}))
        # The drill-down is a run timeline; stopped runs are usage only (they are in
        # the project's cost/tokens) and never appear here as a run of their own.
        agents = self._pg().query(
            f"""SELECT a.agent_run_id, a.workflow_run_id, a.agent_name, a.model,
                       a.started_at, a.duration_ms, a.status, a.llm_calls,
                       a.input_tokens, a.output_tokens, a.cached_input_tokens,
                       a.cost_usd, a.work_units, a.work_unit_type, a.pricing_status
                FROM {_AAR} a JOIN {_AWR} w ON a.workflow_run_id = w.run_id
                WHERE w.session_id = :sid AND {_W_COUNTED}
                ORDER BY a.started_at""", _with_cancelled({"sid": session_id}))
        ingestion = self._pg().query(
            f"""SELECT ingestion_run_id, document_id, started_at, duration_ms, status,
                       bytes_processed, pages_processed, chunks_created
                FROM {_AIR}
                WHERE session_id = :sid
                ORDER BY started_at""", {"sid": session_id})
        log.info("analytics project detail for session %s: %s workflow runs, %s agent "
                 "runs, %s ingestion runs", session_id, len(runs), len(agents),
                 len(ingestion))
        return {
            "session_id": session_id,
            "user_id": owner["user_id"],
            "owner_name": owner["owner_name"],
            "workflow_runs": [self._detail_run(r) for r in runs],
            "agent_runs": [self._detail_agent(a) for a in agents],
            "ingestion_runs": [self._detail_ingestion(i) for i in ingestion],
        }

    @staticmethod
    def _detail_run(r: dict) -> dict:
        """Shape one workflow-run timeline entry."""
        return {
            "run_id": r["run_id"], "turn_index": r["turn_index"], "intent": r["intent"],
            "started_at": r["started_at"].isoformat() if r["started_at"] else None,
            "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
            "wall_ms": r["wall_ms"], "status": r["status"],
            "deck_version_id": r["deck_version_id"],
            "plan_title": r.get("plan_title"),
            "slides_created": r["slides_created"], "slides_updated": r["slides_updated"],
            "slides_translated": r["slides_translated"],
        }

    @staticmethod
    def _detail_agent(a: dict) -> dict:
        """Shape one agent-node run entry (per model), including tokens and cost."""
        return {
            "agent_run_id": a["agent_run_id"], "workflow_run_id": a["workflow_run_id"],
            "agent_name": a["agent_name"], "model": a["model"],
            "started_at": a["started_at"].isoformat() if a["started_at"] else None,
            "duration_ms": a["duration_ms"], "status": a["status"],
            "llm_calls": a["llm_calls"], "input_tokens": a["input_tokens"],
            "output_tokens": a["output_tokens"], "cached_input_tokens": a["cached_input_tokens"],
            "cost_usd": float(a["cost_usd"]) if a["cost_usd"] is not None else None,
            "work_units": a["work_units"], "work_unit_type": a["work_unit_type"],
            "pricing_status": a["pricing_status"],
        }

    @staticmethod
    def _detail_ingestion(i: dict) -> dict:
        """Shape one ingestion-run entry."""
        return {
            "ingestion_run_id": i["ingestion_run_id"], "document_id": i["document_id"],
            "started_at": i["started_at"].isoformat() if i["started_at"] else None,
            "duration_ms": i["duration_ms"], "status": i["status"],
            "bytes_processed": i["bytes_processed"], "pages_processed": i["pages_processed"],
            "chunks_created": i["chunks_created"],
        }


def default_window(days: int | None = None) -> tuple[datetime, datetime]:
    """Return the default ``(start, end)`` window ending now (UTC).

    Args:
        days: Look-back length; defaults to ``AnalyticsConstants.DEFAULT_WINDOW_DAYS``.

    Returns:
        A ``(start, end)`` UTC datetime pair.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days or AnalyticsConstants.DEFAULT_WINDOW_DAYS)
    return start, end


analytics_handler = AnalyticsHandler()

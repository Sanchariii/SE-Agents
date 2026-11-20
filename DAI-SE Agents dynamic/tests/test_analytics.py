"""Analytics capture and dashboard-API tests.

These run in-process with no database: capture is exercised by intercepting the
background queue, cost math and window validation are pure logic, and the routes are
tested with the auth dependency overridden and the handler monkeypatched. This keeps
the suite hermetic while still proving per-agent/per-model attribution, the
wait-vs-agent latency split, fallback marking and non-blocking overflow behaviour.
"""

import queue as _queue
from datetime import datetime, timedelta, timezone

import pytest

from app.config.constants import AnalyticsPricingStatus, AnalyticsStatus, Roles
from app.core.dependencies import auth
from app.utilities.analytics import capture, context
from app.utilities.analytics import queue as writer
from app.utilities.analytics.store import AnalyticsStore
from app.utilities.logger_util import ContextThreadPoolExecutor


# --- cost math --------------------------------------------------------------
def _store_with_prices(prices: dict) -> AnalyticsStore:
    store = AnalyticsStore()
    store._prices = prices                          # bypass DB seed for a unit test
    return store


def test_compute_cost_prices_input_output_and_cached():
    store = _store_with_prices({
        "gpt-4o": {"input_cost_per_million": 2.5, "output_cost_per_million": 10.0,
                   "cached_input_cost_per_million": 1.25},
    })
    cost, status = store.compute_cost("gpt-4o", input_tokens=1_000_000,
                                      output_tokens=1_000_000, cached_tokens=200_000)
    assert status == AnalyticsPricingStatus.PRICED
    # (800k * 2.5 + 200k * 1.25 + 1M * 10) / 1e6 = 2.0 + 0.25 + 10 = 12.25
    assert cost == pytest.approx(12.25)


def test_compute_cost_unpriced_model_returns_none():
    store = _store_with_prices({})
    cost, status = store.compute_cost("mystery-model", 1000, 1000, 0)
    assert cost is None
    assert status == AnalyticsPricingStatus.UNPRICED


# --- accumulator ------------------------------------------------------------
def test_accumulator_buckets_usage_by_model():
    acc = context.AgentRunAccumulator()
    acc.add("gpt-4o", 100, 50, 10)
    acc.add("gpt-4o", 100, 50, 0)
    acc.add("claude-sonnet-4-6", 200, 80, 0)
    assert set(acc.by_model) == {"gpt-4o", "claude-sonnet-4-6"}
    assert acc.by_model["gpt-4o"].llm_calls == 2
    assert acc.by_model["gpt-4o"].input_tokens == 200
    assert acc.by_model["gpt-4o"].cached_input_tokens == 10
    assert acc.total_calls() == 3


def test_record_llm_usage_is_a_noop_without_an_active_run():
    # Must not raise when called outside any agent span (e.g. a warmup call).
    context.record_llm_usage("gpt-4o", 10, 10, 0)


# --- agent_run span ---------------------------------------------------------
@pytest.fixture
def captured_agent_rows(monkeypatch):
    rows: list[dict] = []
    monkeypatch.setattr(writer, "enqueue_agent_run", rows.append)
    monkeypatch.setattr(capture.writer, "enqueue_agent_run", rows.append)
    return rows


@pytest.fixture
def captured_workflow_rows(monkeypatch):
    rows: list[dict] = []
    monkeypatch.setattr(capture.writer, "enqueue_workflow_run", rows.append)
    return rows


def test_agent_run_emits_one_row_per_model(captured_agent_rows, monkeypatch):
    monkeypatch.setattr(
        capture.analytics_store, "compute_cost",
        lambda model, i, o, c: (1.0, AnalyticsPricingStatus.PRICED))
    with capture.agent_run("planner") as span:
        span.set_work_units(1, "slide")
        context.record_llm_usage("claude-sonnet-4-6", 300, 100, 0)
        context.record_llm_usage("gpt-4o", 50, 20, 0)

    assert {r["model"] for r in captured_agent_rows} == {"claude-sonnet-4-6", "gpt-4o"}
    assert all(r["agent_name"] == "planner" for r in captured_agent_rows)
    # Duration is attributed to exactly one (primary) row so summing never double-counts.
    with_duration = [r for r in captured_agent_rows if r["duration_ms"] is not None]
    assert len(with_duration) == 1
    # Primary is the highest-token model (claude here) and carries the work units.
    assert with_duration[0]["model"] == "claude-sonnet-4-6"
    assert with_duration[0]["work_units"] == 1


def test_agent_run_without_llm_calls_emits_single_model_less_row(captured_agent_rows):
    with capture.agent_run("triage"):
        pass
    assert len(captured_agent_rows) == 1
    row = captured_agent_rows[0]
    assert row["model"] is None
    assert row["llm_calls"] == 0
    assert row["status"] == AnalyticsStatus.SUCCEEDED


def test_agent_run_marks_fallback(captured_agent_rows):
    with capture.agent_run("slide_content") as span:
        span.mark_fallback()
    assert captured_agent_rows[0]["status"] == AnalyticsStatus.FALLBACK


def test_agent_run_records_failure_and_reraises(captured_agent_rows):
    with pytest.raises(ValueError):
        with capture.agent_run("planner"):
            raise ValueError("boom")
    assert captured_agent_rows[0]["status"] == AnalyticsStatus.FAILED


# --- workflow_run span ------------------------------------------------------
def test_workflow_run_binds_id_and_records_outputs(captured_workflow_rows):
    with capture.workflow_run("sess-1", user_id=7, turn_index=2) as span:
        assert context.current_workflow_run_id() == span.run_id
        span.deck_version_id = 3
        span.slides_created = 4
    assert context.current_workflow_run_id() is None       # unbound on exit
    row = captured_workflow_rows[0]
    assert row["session_id"] == "sess-1"
    assert row["user_id"] == 7
    assert row["deck_version_id"] == 3
    assert row["slides_created"] == 4
    assert row["status"] == AnalyticsStatus.SUCCEEDED
    assert row["wall_ms"] >= 0


def test_workflow_run_failure_status(captured_workflow_rows):
    with pytest.raises(RuntimeError):
        with capture.workflow_run("sess-2"):
            raise RuntimeError("nope")
    assert captured_workflow_rows[0]["status"] == AnalyticsStatus.FAILED


# --- concurrency: context propagation into worker threads -------------------
def test_agent_and_workflow_ids_propagate_to_pool_workers(captured_agent_rows, monkeypatch):
    monkeypatch.setattr(
        capture.analytics_store, "compute_cost",
        lambda model, i, o, c: (None, AnalyticsPricingStatus.UNPRICED))

    def build_slide(_i: int) -> str | None:
        # Each worker opens its own agent span; usage must fold into that span only,
        # and the parent workflow id must be inherited via the copied context.
        with capture.agent_run("slide_content"):
            context.record_llm_usage("gpt-4o", 100, 50, 0)
            return context.current_workflow_run_id()

    with capture.workflow_run("sess-par") as span:
        with ContextThreadPoolExecutor(max_workers=3) as pool:
            seen = list(pool.map(build_slide, range(3)))

    # Every worker saw the same parent workflow id...
    assert seen == [span.run_id, span.run_id, span.run_id]
    # ...and produced its own isolated agent row (no cross-thread token bleed).
    slide_rows = [r for r in captured_agent_rows if r["agent_name"] == "slide_content"]
    assert len(slide_rows) == 3
    assert all(r["input_tokens"] == 100 and r["output_tokens"] == 50 for r in slide_rows)
    assert all(r["workflow_run_id"] == span.run_id for r in slide_rows)


# --- non-blocking overflow --------------------------------------------------
def test_enqueue_drops_and_counts_on_overflow(monkeypatch):
    tiny: "_queue.Queue" = _queue.Queue(maxsize=1)
    monkeypatch.setattr(writer, "_events", tiny)
    before = writer.dropped_event_count()
    writer.enqueue_agent_run({"first": True})       # fills the queue
    writer.enqueue_agent_run({"second": True})      # dropped, not blocked
    assert writer.dropped_event_count() == before + 1
    assert tiny.qsize() == 1


# --- window validation ------------------------------------------------------
def test_resolve_window_defaults_when_unset():
    from app.core.services.analytics_service import _resolve_window
    start, end = _resolve_window(None, None)
    assert start < end


def test_resolve_window_rejects_inverted_range():
    from app.config.exceptions import AppError
    from app.core.services.analytics_service import _resolve_window
    now = datetime.now(timezone.utc)
    with pytest.raises(AppError):
        _resolve_window(now.isoformat(), (now - timedelta(days=1)).isoformat())


def test_resolve_window_rejects_oversized_range():
    from app.config.exceptions import AppError
    from app.core.services.analytics_service import _resolve_window
    now = datetime.now(timezone.utc)
    with pytest.raises(AppError):
        _resolve_window((now - timedelta(days=800)).isoformat(), now.isoformat())


# --- routes -----------------------------------------------------------------
def test_dashboard_requires_admin_role(client, app_instance, claims_member):
    app_instance.dependency_overrides[auth.require_auth] = lambda: claims_member
    try:
        resp = client.get("/analytics/dashboard")
        assert resp.status_code == 403
    finally:
        app_instance.dependency_overrides.pop(auth.require_auth, None)


def test_dashboard_returns_handler_payload_for_admin(
        client, app_instance, monkeypatch, claims_admin):
    from app.core.services import analytics_service
    captured = {}

    def fake_dashboard(start, end, tz="UTC", agent=None, model=None):
        captured["agent"] = agent
        captured["model"] = model
        return {"overview": {"active_users": {"value": 5}}}

    monkeypatch.setattr(analytics_service.analytics_handler, "dashboard", fake_dashboard)
    app_instance.dependency_overrides[auth.require_auth] = lambda: claims_admin
    try:
        resp = client.get("/analytics/dashboard?agent=planner&model=gpt-4o")
        assert resp.status_code == 200
        assert resp.json()["overview"]["active_users"]["value"] == 5
        # Optional agent/model filters are threaded through to the handler.
        assert captured == {"agent": "planner", "model": "gpt-4o"}
    finally:
        app_instance.dependency_overrides.pop(auth.require_auth, None)


def test_bucket_granularity_switches_from_day_to_week():
    from app.core.handlers.analytics_handler import AnalyticsHandler
    h = AnalyticsHandler()
    now = datetime.now(timezone.utc)
    assert h._bucket(now - timedelta(days=30), now) == "day"
    assert h._bucket(now - timedelta(days=200), now) == "week"


def test_agent_filter_sql_builds_optional_clause():
    from app.core.handlers.analytics_handler import AnalyticsHandler
    where, extra = AnalyticsHandler._agent_filter_sql({"agent": "planner", "model": "gpt-4o"})
    assert "a.agent_name = :f_agent" in where
    assert "a.model = :f_model" in where
    assert extra == {"f_agent": "planner", "f_model": "gpt-4o"}
    # No filters -> empty clause, no params.
    assert AnalyticsHandler._agent_filter_sql(None) == ("", {})


def test_summary_is_marked_deprecated_in_schema(client, app_instance):
    schema = app_instance.openapi()
    assert schema["paths"]["/api/analytics/summary"]["get"]["deprecated"] is True


# --- quality section: ambiguous-column regression ---------------------------
import re as _re

from app.config.constants import PgTables

# Columns that exist on BOTH analytics_agent_runs and analytics_workflow_runs; an
# unaliased reference to one inside a query that joins both tables is exactly what
# makes PostgreSQL raise AmbiguousColumn.
_OVERLAPPING_COLUMNS = ("status", "started_at", "finished_at", "created_at")


def _unqualified_overlap(sql: str) -> str | None:
    """Return the first overlapping column referenced without a table alias, else None.

    ``(?<![\\w.])`` rejects a match preceded by a word char or dot, so ``a.status``
    and ``pricing_status`` are ignored while a bare ``status`` is flagged.
    """
    for col in _OVERLAPPING_COLUMNS:
        if _re.search(rf"(?<![\w.]){col}\b", sql):
            return col
    return None


class _AmbiguityCheckingPg:
    """Fake PgsqlUtil that mimics PostgreSQL's ambiguous-column rejection.

    For any query joining both analytics run tables, an unqualified overlapping
    column raises just as the live database would; otherwise canned rows keyed by a
    unique substring of each query are returned so aggregation logic can be asserted
    against seeded counts.
    """

    def __init__(self, rows_by_marker: dict) -> None:
        self._rows = rows_by_marker
        self.queries: list[str] = []

    def query(self, sql: str, params: dict) -> list[dict]:
        self.queries.append(sql)
        joins_both = (PgTables.ANALYTICS_AGENT_RUNS in sql
                      and PgTables.ANALYTICS_WORKFLOW_RUNS in sql)
        if joins_both:
            ambiguous = _unqualified_overlap(sql)
            if ambiguous:
                # Reproduce psycopg2.errors.AmbiguousColumn the original bug caused.
                raise RuntimeError(f'column reference "{ambiguous}" is ambiguous')
        for marker, rows in self._rows.items():
            if marker in sql:
                return rows
        return [{}]


def _quality_with_seeded_counts():
    """Build a handler whose _quality() sub-queries return the plan's seeded scenario.

    Workflow W1 (completed): agent A1 completed, A2 fallback.
    Workflow W2 (failed): agent A3 failed.
    => agent runs=3, fallback_runs=1 (A2), failed_runs=1 (A3).
    """
    from app.core.handlers.analytics_handler import AnalyticsHandler
    fake = _AmbiguityCheckingPg({
        "fallback_runs": [{"runs": 3, "fallback_runs": 1, "failed_runs": 1}],
        "reworked": [{"published": 1, "reworked": 0}],
        "with_comment": [{"rated": 0, "thumbs_up": 0, "thumbs_down": 0,
                          "with_comment": 0}],
    })
    handler = AnalyticsHandler()
    handler._db = fake                               # inject the fake PG accessor
    return handler, fake


def test_quality_join_does_not_raise_ambiguous_column_and_counts_seeded_rows():
    # Guards the production bug: with both joined tables exposing `status`, the fixed
    # a.status qualification must let the query run and map counts correctly. The old
    # unqualified `status` makes the fake raise AmbiguousColumn, failing this test.
    handler, _ = _quality_with_seeded_counts()
    now = datetime.now(timezone.utc)
    out = handler._quality({"start": now - timedelta(days=1), "end": now})
    assert out["agent_runs"]["value"] == 3
    assert out["fallback_rate"]["value"] == pytest.approx(0.3333)
    assert out["fallback_rate"]["count"] == 1
    assert out["agent_failure_rate"]["value"] == pytest.approx(0.3333)
    assert out["agent_failure_rate"]["count"] == 1


def test_quality_agent_join_qualifies_status_with_agent_alias():
    # Locks the metric meaning to agent-run status so a later switch to w.status or a
    # bare `status` cannot silently change what fallback/failure rates measure.
    handler, fake = _quality_with_seeded_counts()
    now = datetime.now(timezone.utc)
    handler._quality({"start": now - timedelta(days=1), "end": now})
    join_sql = next(q for q in fake.queries if "fallback_runs" in q)
    assert "a.status = :fb" in join_sql
    assert "a.status = :failed" in join_sql
    assert _unqualified_overlap(join_sql) is None


def test_quality_handles_empty_window_as_zero_metrics():
    # A no-data interval must return valid zero/None metrics, never a 500.
    from app.core.handlers.analytics_handler import AnalyticsHandler
    fake = _AmbiguityCheckingPg({
        "fallback_runs": [{"runs": 0, "fallback_runs": 0, "failed_runs": 0}],
        "reworked": [{"published": 0, "reworked": 0}],
        "with_comment": [{"rated": 0, "thumbs_up": 0, "thumbs_down": 0,
                          "with_comment": 0}],
    })
    handler = AnalyticsHandler()
    handler._db = fake
    now = datetime.now(timezone.utc)
    out = handler._quality({"start": now - timedelta(days=1), "end": now})
    assert out["agent_runs"]["value"] == 0
    # Zero denominator -> ratio is None (never a fake 0.0 or a divide-by-zero error).
    assert out["fallback_rate"]["value"] is None
    assert out["agent_failure_rate"]["value"] is None


class _EfficiencyPg:
    """Fake analytics database with seeded efficiency counts."""

    def __init__(self, counts: dict, pages: int, cost: float) -> None:
        self.counts = counts
        self.pages = pages
        self.cost = cost

    def query(self, sql: str, params: dict) -> list[dict]:
        """Return the seeded row matching the requested efficiency query."""
        if "AS new_slides" in sql:
            return [self.counts]
        if "SUM(pages_processed)" in sql:
            return [{"v": self.pages}]
        if "SUM(a.cost_usd)" in sql:
            return [{"v": self.cost}]
        raise AssertionError(f"Unexpected efficiency query: {sql}")


def _efficiency_with_seeded_counts(counts: dict, pages: int, cost: float):
    """Build a handler whose efficiency queries return the provided scenario."""
    from app.core.handlers.analytics_handler import AnalyticsHandler

    handler = AnalyticsHandler()
    handler._db = _EfficiencyPg(counts, pages, cost)
    return handler


def test_efficiency_uses_v1_baselines_for_hours_saved():
    """Calculate selected-window savings from pages, slide operations, and runtime."""
    counts = {
        "new_slides": 12,
        "revised_slides": 2,
        "translated_slides": 4,
        "decks": 1,
        "wall_ms": 30 * 60 * 1000,
    }
    handler = _efficiency_with_seeded_counts(counts, pages=40, cost=12.5)
    now = datetime.now(timezone.utc)

    out = handler._efficiency({"start": now - timedelta(days=1), "end": now})

    # Manual: 40*3 + 12*12 + 2*6 + 4*4 + 1*15 = 307 min.
    # AI-assisted: 30 min wall time + 1*10 min review = 40 min.
    assert out["estimated_hours_saved"]["value"] == pytest.approx(267 / 60)
    assert out["estimated_hours_saved"]["type"] == "estimated"
    assert out["hours_saved_per_dollar"]["value"] == pytest.approx(4.45 / 12.5)
    assert out["time_leverage"]["value"] == pytest.approx(307 / 40)
    assert out["estimate"]["baseline_version"] == "v1"
    assert out["estimate"]["confidence"] == "medium"


def test_efficiency_clips_negative_savings_to_zero():
    """Do not report negative savings when AI-assisted effort exceeds the baseline."""
    counts = {
        "new_slides": 0,
        "revised_slides": 0,
        "translated_slides": 0,
        "decks": 0,
        "wall_ms": 60 * 60 * 1000,
    }
    handler = _efficiency_with_seeded_counts(counts, pages=0, cost=1.0)
    now = datetime.now(timezone.utc)

    out = handler._efficiency({"start": now - timedelta(days=1), "end": now})

    assert out["estimated_hours_saved"]["value"] == 0
    assert out["estimate"]["confidence"] == "low"


def test_dashboard_section_failure_raises_controlled_upstream_error():
    # A failing section must surface as a controlled 502 (no raw DB internals), not an
    # unhandled exception bubbling to the client.
    from app.config.exceptions import UpstreamError
    from app.core.handlers.analytics_handler import AnalyticsHandler

    class _BoomPg:
        def query(self, sql: str, params: dict):
            raise RuntimeError('column reference "status" is ambiguous')

    handler = AnalyticsHandler()
    handler._db = _BoomPg()
    now = datetime.now(timezone.utc)
    with pytest.raises(UpstreamError) as exc:
        handler.dashboard(now - timedelta(days=1), now)
    # The client-facing message is generic; the raw DB error is not exposed.
    assert "ambiguous" not in str(exc.value)


def test_unhandled_error_response_preserves_correlation_id(app_instance, claims_admin):
    # The correlation middleware resets the context var before the 500 handler runs, so
    # the id must still reach the response via request.state.
    from fastapi.testclient import TestClient

    from app.core.dependencies import auth
    from app.core.services import analytics_service

    def _boom():
        raise RuntimeError("unexpected failure")

    app_instance.dependency_overrides[auth.require_auth] = lambda: claims_admin
    original = analytics_service.analytics_handler.summary
    analytics_service.analytics_handler.summary = _boom
    try:
        local = TestClient(app_instance, base_url="http://testserver/api",
                           raise_server_exceptions=False)
        resp = local.get("/analytics/summary",
                         headers={"x-correlation-id": "cid-regression-test"})
        assert resp.status_code == 500
        assert resp.json()["correlation_id"] == "cid-regression-test"
    finally:
        analytics_service.analytics_handler.summary = original
        app_instance.dependency_overrides.pop(auth.require_auth, None)


# --- project owner + plan title -----------------------------------------------
class _MarkerPg:
    """Fake PgsqlUtil returning canned rows for the first marker found in the SQL."""

    def __init__(self, rows_by_marker: list[tuple[str, list[dict]]]) -> None:
        self._rows = rows_by_marker
        self.queries: list[str] = []

    def query(self, sql: str, params: dict) -> list[dict]:
        self.queries.append(sql)
        for marker, rows in self._rows:
            if marker in sql:
                return rows
        return []


def _handler_with(fake: _MarkerPg):
    from app.core.handlers.analytics_handler import AnalyticsHandler
    handler = AnalyticsHandler()
    handler._db = fake                               # inject the fake PG accessor
    return handler


def test_projects_returns_owner_name_from_users_table():
    # The list used to expose only user_id, forcing the UI to show an owner placeholder.
    now = datetime.now(timezone.utc)
    fake = _MarkerPg([
        ("owner_name", [
            {"session_id": "s1", "user_id": 7, "owner_name": "Ada Lovelace", "turns": 2,
             "last_active": now, "ai_turnaround_ms": 1000, "slides_delivered": 3,
             "published": True, "cost_usd": 0.5, "total_tokens": 100, "agent_ms": 900},
            {"session_id": "s2", "user_id": None, "owner_name": None, "turns": 1,
             "last_active": now, "ai_turnaround_ms": None, "slides_delivered": 0,
             "published": False, "cost_usd": None, "total_tokens": None,
             "agent_ms": None},
        ]),
        ("COUNT(DISTINCT session_id) AS v", [{"v": 2}]),
    ])
    out = _handler_with(fake).projects(now - timedelta(days=1), now, limit=10,
                                       offset=0, sort="cost", order="desc")
    first, second = out["items"]
    assert (first["user_id"], first["owner_name"]) == (7, "Ada Lovelace")
    # An unknown owner stays None (never a fabricated name).
    assert (second["user_id"], second["owner_name"]) == (None, None)
    assert out["pagination"]["total"] == 2
    sql = next(q for q in fake.queries if "owner_name" in q)
    # Name comes from users (display_name, username fallback); owner falls back to the
    # session registration when the telemetry row has no user_id.
    assert f"LEFT JOIN {PgTables.USERS} u" in sql
    assert "COALESCE(NULLIF(u.display_name, ''), u.username)" in sql
    assert PgTables.USER_SESSIONS in sql
    assert "COALESCE(w.user_id, o.user_id)" in sql
    assert "ORDER BY a.cost_usd DESC" in sql


def test_project_detail_returns_owner_and_plan_title_per_version():
    now = datetime.now(timezone.utc)
    fake = _MarkerPg([
        ("LATERAL", [{"user_id": 7, "owner_name": "ada@contoso.com"}]),
        ("plan_title", [
            {"run_id": "r1", "turn_index": 1, "intent": "plan", "started_at": now,
             "finished_at": now, "wall_ms": 10, "status": "succeeded",
             "deck_version_id": 2, "plan_title": "Q3 Safety Training",
             "slides_created": 4, "slides_updated": 0, "slides_translated": 0},
            {"run_id": "r2", "turn_index": 2, "intent": "chat", "started_at": now,
             "finished_at": now, "wall_ms": 5, "status": "succeeded",
             "deck_version_id": None, "plan_title": None,
             "slides_created": 0, "slides_updated": 0, "slides_translated": 0},
        ]),
    ])
    out = _handler_with(fake).project_detail("s1")
    assert (out["user_id"], out["owner_name"]) == (7, "ada@contoso.com")
    with_deck, chat_only = out["workflow_runs"]
    assert (with_deck["deck_version_id"], with_deck["plan_title"]) == (2, "Q3 Safety Training")
    assert chat_only["plan_title"] is None
    runs_sql = next(q for q in fake.queries if "plan_title" in q)
    assert f"LEFT JOIN {PgTables.DECK_VERSIONS} v" in runs_sql
    assert "v.deck_version = r.deck_version_id" in runs_sql
    owner_sql = next(q for q in fake.queries if "LATERAL" in q)
    assert "COALESCE(NULLIF(u.display_name, ''), u.username)" in owner_sql


def test_project_detail_unknown_owner_is_none():
    fake = _MarkerPg([("LATERAL", [{"user_id": None, "owner_name": None}])])
    out = _handler_with(fake).project_detail("missing")
    assert out["user_id"] is None and out["owner_name"] is None
    assert out["workflow_runs"] == [] and out["agent_runs"] == []

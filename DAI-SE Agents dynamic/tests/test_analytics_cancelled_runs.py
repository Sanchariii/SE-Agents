"""Stopped runs in analytics: kept as usage (tokens, cost), never counted as a run.

A user stop records ``cancelled``. Spend sums keep that usage; run counts, success and
failure rates, latency and the project drill-down exclude it. No new response key or
category is introduced, so the dashboard has nothing new to render.
"""

from datetime import datetime, timezone

import pytest

from app.config.constants import AnalyticsStatus
from app.core.handlers.analytics_handler import AnalyticsHandler
from app.utilities.analytics import capture
from app.utilities.run_control import (FENCE_REASON_CANCELLED, FENCE_REASON_LOST,
                                       RunCancelledError, RunFencedError)


@pytest.fixture
def rows(monkeypatch):
    out = {"workflow": [], "agent": []}
    monkeypatch.setattr(capture.writer, "enqueue_workflow_run", out["workflow"].append)
    monkeypatch.setattr(capture.writer, "enqueue_agent_run", out["agent"].append)
    return out


def _run_spans(exc: BaseException) -> None:
    with pytest.raises(type(exc)):
        with capture.workflow_run("s1", user_id=3, turn_index=4, run_id="r1"):
            with capture.agent_run("planner"):
                raise exc


def test_a_user_stop_is_recorded_as_cancelled_usage_without_a_turn_number(rows):
    _run_spans(RunCancelledError("r1"))

    workflow, = rows["workflow"]
    assert workflow["status"] == AnalyticsStatus.CANCELLED
    assert workflow["run_id"] == "r1"               # joins the operational run id
    assert workflow["turn_index"] is None and workflow["deck_version_id"] is None
    assert rows["agent"][0]["status"] == AnalyticsStatus.CANCELLED


def test_a_forced_stop_is_cancelled_but_a_lost_worker_is_a_failure(rows):
    _run_spans(RunFencedError("r1", FENCE_REASON_CANCELLED))
    _run_spans(RunFencedError("r1", FENCE_REASON_LOST))

    assert [r["status"] for r in rows["workflow"]] == [AnalyticsStatus.CANCELLED,
                                                      AnalyticsStatus.FAILED]


def test_an_ordinary_error_is_still_a_failure(rows):
    _run_spans(RuntimeError("boom"))

    assert rows["workflow"][0]["status"] == AnalyticsStatus.FAILED
    assert rows["workflow"][0]["turn_index"] == 4


# --- dashboard queries --------------------------------------------------------------------
class _Row(dict):
    def __missing__(self, key):
        return 0


class _FakePg:
    def __init__(self) -> None:
        self.queries: list[tuple[str, dict]] = []

    def query(self, sql, params=None):
        self.queries.append((" ".join(sql.split()), params or {}))
        return [] if "date_trunc" in sql else [_Row()]


@pytest.fixture
def analytics():
    handler, fake = AnalyticsHandler(), _FakePg()
    handler._db = fake
    return handler, fake


_WINDOW = (datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 10, 1, tzinfo=timezone.utc))


def test_run_counts_rates_and_latency_exclude_stopped_runs(analytics):
    handler, fake = analytics

    result = handler.dashboard(*_WINDOW)

    sql = [s for s, _ in fake.queries]
    assert all(p.get("cancelled") == AnalyticsStatus.CANCELLED for _, p in fake.queries)
    assert any("COUNT(*) FILTER (WHERE status <> :cancelled) AS workflow_runs" in s for s in sql)
    assert any("FROM per_project" in s and "status <> :cancelled" in s for s in sql)
    assert any("percentile_cont(0.5)" in s and "a.duration_ms) FILTER (WHERE w.status <> :cancelled)"
               in s for s in sql)
    # The response shape is unchanged: no new section or key for stopped runs.
    assert "cancelled" not in str(sorted(result["overview"]))


def test_spend_sums_keep_stopped_run_usage(analytics):
    handler, fake = analytics

    handler.dashboard(*_WINDOW)

    total_cost = [s for s, _ in fake.queries if s.startswith("SELECT SUM(a.cost_usd) AS v")]
    assert total_cost and all(":cancelled" not in s for s in total_cost)
    cost_section = next(s for s, _ in fake.queries if "SUM(a.cached_input_tokens) AS cached_tokens,"
                        in s and "SUM(a.llm_calls) AS llm_calls FROM" in s)
    assert ":cancelled" not in cost_section


def test_the_project_drill_down_hides_stopped_runs(analytics):
    handler, fake = analytics

    handler.project_detail("s1")

    runs_sql = next(s for s, _ in fake.queries if "r.turn_index" in s)
    agents_sql = next(s for s, _ in fake.queries if "a.agent_run_id" in s)
    assert "r.status <> :cancelled" in runs_sql
    assert "w.status <> :cancelled" in agents_sql


def test_turn_numbers_ignore_stopped_runs(monkeypatch):
    from app.genai.workflows import deck_workflow
    from app.utilities import pgsql_util

    seen: list[tuple[str, dict]] = []

    class _Pg:
        def query(self, sql, params=None):
            seen.append((sql, params or {}))
            return [{"user_id": 3, "n": 2}]

    monkeypatch.setattr(pgsql_util, "PgsqlUtil", _Pg)

    assert deck_workflow._workflow_run_meta("s1") == (3, 3)
    count_sql, params = seen[1]
    assert "status <> :cancelled" in count_sql
    assert params["cancelled"] == AnalyticsStatus.CANCELLED

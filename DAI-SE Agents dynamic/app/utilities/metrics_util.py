"""Log-based operational counters for workflow robustness.

There is no metrics backend in this service, so a counter is one structured INFO line
(``metric=<name> k=v ...``) that App Insights / Log Analytics can count and group.
These are operational signals (retries, duplicate submissions, stale runs) - product
analytics live in ``app.utilities.analytics`` and are unaffected.
"""

from app.utilities.logger_util import get_logger

log = get_logger("metrics")


class Metrics:
    """Counter names, kept in one place so dashboards and code never drift."""
    RUNS_STARTED = "workflow_runs_started_total"
    RUNS_COMPLETED = "workflow_runs_completed_total"
    RUNS_FAILED = "workflow_runs_failed_total"
    INTERNAL_RETRIES = "workflow_internal_retries_total"
    STRUCTURED_PARSE_FAILURES = "structured_output_parse_failures_total"
    STRUCTURED_RECOVERIES = "structured_output_recoveries_total"
    DUPLICATE_RUN_BLOCKED = "duplicate_run_requests_blocked_total"
    IDEMPOTENT_REUSED = "idempotent_requests_reused_total"
    STALE_RUNS = "stale_runs_detected_total"
    STREAM_RECONNECTS = "run_stream_reconnects_total"


def incr(name: str, value: int = 1, **labels: object) -> None:
    """Emit one counter increment as a structured log line; never raises.

    Args:
        name: Counter name (see :class:`Metrics`).
        value: Increment.
        **labels: Low-cardinality dimensions (stage, reason, error_code, ...).
    """
    try:
        rendered = " ".join(f"{k}={v}" for k, v in labels.items() if v is not None)
        log.info("metric=%s value=%d %s", name, value, rendered)
    except Exception:  # noqa: BLE001 - telemetry must never break real work
        pass

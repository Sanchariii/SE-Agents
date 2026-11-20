"""Bounded in-process queue and background writer for analytics rows.

Run spans enqueue finished rows here instead of writing inline, so no telemetry
insert ever lands on the request event loop. A single daemon thread drains the
queue, batches rows by table and flushes them through :data:`analytics_store`.
On overflow rows are dropped with a warning (and counted for ``data_quality``)
rather than blocking the workflow that produced them.
"""

import queue
import threading
import time

from app.config.constants import AnalyticsConstants
from app.utilities.analytics.store import analytics_store
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

#: Table kind -> the store method that inserts a batch of that kind.
_WORKFLOW = "workflow"
_AGENT = "agent"
_INGESTION = "ingestion"

_events: "queue.Queue[tuple[str, dict]]" = queue.Queue(maxsize=AnalyticsConstants.QUEUE_MAX_SIZE)
_writer_thread: threading.Thread | None = None
_stop = threading.Event()

_dropped = 0
_dropped_lock = threading.Lock()


def dropped_event_count() -> int:
    """Return how many analytics rows have been dropped on queue overflow."""
    with _dropped_lock:
        return _dropped


def _drop(kind: str) -> None:
    """Count and log a dropped row without ever blocking the caller."""
    global _dropped
    with _dropped_lock:
        _dropped += 1
        total = _dropped
    log.warning("analytics queue full; dropped a %s row (total dropped=%d)", kind, total)


def _enqueue(kind: str, row: dict) -> None:
    """Offer one row to the writer queue, dropping it if the queue is full."""
    try:
        _events.put_nowait((kind, row))
    except queue.Full:
        _drop(kind)


def enqueue_workflow_run(row: dict) -> None:
    """Queue one workflow-run row for background insertion."""
    _enqueue(_WORKFLOW, row)


def enqueue_agent_run(row: dict) -> None:
    """Queue one agent-run row for background insertion."""
    _enqueue(_AGENT, row)


def enqueue_ingestion_run(row: dict) -> None:
    """Queue one ingestion-run row for background insertion."""
    _enqueue(_INGESTION, row)


def _flush(batch: list[tuple[str, dict]]) -> None:
    """Insert a mixed batch of rows grouped by table, swallowing insert errors."""
    if not batch:
        return
    grouped: dict[str, list[dict]] = {_WORKFLOW: [], _AGENT: [], _INGESTION: []}
    for kind, row in batch:
        grouped[kind].append(row)
    inserters = {
        _WORKFLOW: analytics_store.insert_workflow_runs,
        _AGENT: analytics_store.insert_agent_runs,
        _INGESTION: analytics_store.insert_ingestion_runs,
    }
    for kind, rows in grouped.items():
        if not rows:
            continue
        try:
            inserters[kind](rows)
        except Exception as exc:                     # noqa: BLE001
            # A failed insert must not crash the writer or the app; drop and continue.
            log.error("analytics batch insert failed for %s (%d rows): %s",
                      kind, len(rows), exc)


def _drain_available(first: tuple[str, dict]) -> list[tuple[str, dict]]:
    """Collect ``first`` plus everything already queued, up to the batch size."""
    batch = [first]
    while len(batch) < AnalyticsConstants.WRITER_BATCH_SIZE:
        try:
            batch.append(_events.get_nowait())
        except queue.Empty:
            break
    return batch


def _run() -> None:
    """Writer loop: block for a row, batch what is ready, flush, repeat until stopped."""
    log.info("analytics writer thread started")
    while not _stop.is_set():
        try:
            first = _events.get(timeout=AnalyticsConstants.WRITER_FLUSH_SECONDS)
        except queue.Empty:
            continue
        _flush(_drain_available(first))
    # Drain anything still queued at shutdown within the grace period.
    deadline = time.monotonic() + AnalyticsConstants.SHUTDOWN_FLUSH_SECONDS
    while time.monotonic() < deadline:
        try:
            first = _events.get_nowait()
        except queue.Empty:
            break
        _flush(_drain_available(first))
    log.info("analytics writer thread stopped")


def start_writer() -> None:
    """Start the background writer thread (idempotent).

    Returns:
        None.
    """
    global _writer_thread
    if _writer_thread is not None and _writer_thread.is_alive():
        return
    _stop.clear()
    _writer_thread = threading.Thread(target=_run, name="analytics-writer", daemon=True)
    _writer_thread.start()


def stop_writer() -> None:
    """Signal the writer to drain and stop, waiting up to the shutdown grace period.

    Returns:
        None.
    """
    global _writer_thread
    if _writer_thread is None:
        return
    _stop.set()
    _writer_thread.join(timeout=AnalyticsConstants.SHUTDOWN_FLUSH_SECONDS + 1.0)
    _writer_thread = None

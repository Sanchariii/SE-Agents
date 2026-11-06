"""Run-span context managers that turn work into analytics rows.

Three spans mirror the three lifecycles the dashboard reports on:

* :func:`workflow_run` — one user turn / deck-workflow execution.
* :func:`agent_run` — one named agent-node invocation; emits one row per model.
* :func:`ingestion_run` — one document ingestion attempt.

Each is best effort: it measures with a monotonic clock, records status in a
``finally`` (so failures and cancellations are still captured), enqueues the row on
the background writer, and never lets a telemetry error escape into real work.
"""

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from time import monotonic
from typing import Iterator

from app.config.constants import AgentNames, AnalyticsStatus
from app.utilities.analytics import context as ctx
from app.utilities.analytics import queue as writer
from app.utilities.analytics.store import analytics_store
from app.utilities.logger_util import agent_name_ctx, get_logger

log = get_logger(__name__)


def _now() -> datetime:
    """Return the current UTC time for a run timestamp."""
    return datetime.now(timezone.utc)


def _new_id() -> str:
    """Return a fresh run identifier."""
    return uuid.uuid4().hex


def _failure_status(exc: BaseException) -> str:
    """Status for a span that ended in ``exc``.

    A user stop is not a failure: errors flagged ``analytics_cancelled`` (see
    ``app.utilities.run_control``) record ``CANCELLED``, which the dashboard keeps as
    usage (tokens, cost) but leaves out of run counts, success/failure rates and
    latency. Checked by attribute so analytics never imports workflow code.
    """
    if getattr(exc, "analytics_cancelled", False):
        return AnalyticsStatus.CANCELLED
    return AnalyticsStatus.FAILED


class WorkflowRunHandle:
    """Mutable outputs a workflow span records before it closes."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.status = AnalyticsStatus.SUCCEEDED
        self.intent: str | None = None
        self.deck_version_id: int | None = None
        self.slides_created = 0
        self.slides_updated = 0
        self.slides_translated = 0


class AgentRunHandle:
    """Mutable outputs an agent span records before it closes."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.status = AnalyticsStatus.SUCCEEDED
        self.work_units: int | None = None
        self.work_unit_type: str | None = None

    def set_work_units(self, count: int, unit_type: str) -> None:
        """Record the agent's native output count for cost-per-unit metrics.

        Args:
            count: Number of units produced (slides, pages, ...).
            unit_type: The unit kind (see :class:`WorkUnitType`).

        Returns:
            None.
        """
        self.work_units = count
        self.work_unit_type = unit_type

    def mark_fallback(self) -> None:
        """Flag that the agent completed only via a non-model fallback path."""
        self.status = AnalyticsStatus.FALLBACK


class IngestionRunHandle:
    """Mutable outputs an ingestion span records before it closes."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.status = AnalyticsStatus.SUCCEEDED
        self.bytes_processed: int | None = None
        self.pages_processed: int | None = None
        self.chunks_created: int | None = None


@contextmanager
def workflow_run(session_id: str, user_id: int | None = None,
                 turn_index: int | None = None,
                 intent: str | None = None,
                 run_id: str | None = None) -> Iterator[WorkflowRunHandle]:
    """Measure one deck-workflow turn and enqueue a workflow-run row on exit.

    Binds the run id on the context so agent runs (including fanned-out slide
    workers) attribute themselves to this workflow. Any exception is recorded as a
    failed run and re-raised; a user stop is recorded as cancelled instead.

    Args:
        session_id: Project/session the turn belongs to.
        user_id: Owner at run time, when known.
        turn_index: 1-based turn number within the project, when known.
        intent: Approved intent category, when known up front (else set on the handle).
        run_id: The operational run id, so analytics rows join ``workflow_runs``;
            a fresh id is minted when absent.

    Yields:
        A :class:`WorkflowRunHandle` for recording outputs and final status.
    """
    handle = WorkflowRunHandle(run_id or _new_id())
    handle.intent = intent
    started_at = _now()
    start = monotonic()
    token = ctx.bind_workflow_run_id(handle.run_id)
    try:
        yield handle
    except BaseException as exc:
        handle.status = _failure_status(exc)
        raise
    finally:
        ctx.unbind_workflow_run_id(token)
        cancelled = handle.status == AnalyticsStatus.CANCELLED
        try:
            writer.enqueue_workflow_run({
                "run_id": handle.run_id,
                "session_id": session_id,
                "user_id": user_id,
                # A stopped turn is not a turn: it keeps no number in the project.
                "turn_index": None if cancelled else turn_index,
                "intent": handle.intent,
                "started_at": started_at,
                "finished_at": _now(),
                "wall_ms": int((monotonic() - start) * 1000),
                "status": handle.status,
                "deck_version_id": None if cancelled else handle.deck_version_id,
                "slides_created": handle.slides_created,
                "slides_updated": handle.slides_updated,
                "slides_translated": handle.slides_translated,
            })
        except Exception as exc:                     # noqa: BLE001
            log.warning("failed to enqueue workflow run for %s: %s", session_id, exc)


def _agent_rows(handle: AgentRunHandle, agent_name: str,
                accumulator: ctx.AgentRunAccumulator, started_at: datetime,
                finished_at: datetime, duration_ms: int) -> list[dict]:
    """Build one agent-run row per model bucket (or a single model-less row).

    Duration and work units are attributed to the primary (highest-token) row only,
    so summing ``duration_ms`` across rows never double-counts a single invocation.

    Args:
        handle: The closing agent handle carrying status and work units.
        agent_name: Stable agent-node name.
        accumulator: Per-model token totals gathered during the span.
        started_at: Span start (UTC).
        finished_at: Span end (UTC).
        duration_ms: Measured active duration.

    Returns:
        The rows to enqueue.
    """
    buckets = accumulator.by_model
    if not buckets:
        # Deterministic agent (no model call): still record time and status.
        return [{
            "agent_run_id": handle.run_id, "workflow_run_id": ctx.current_workflow_run_id(),
            "agent_name": agent_name, "model": None, "started_at": started_at,
            "finished_at": finished_at, "duration_ms": duration_ms, "status": handle.status,
            "llm_calls": 0, "input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0,
            "cost_usd": None, "work_units": handle.work_units,
            "work_unit_type": handle.work_unit_type, "pricing_status": None,
        }]

    # Primary = the model with the most total tokens; it carries duration + work units.
    primary = max(buckets, key=lambda m: buckets[m].input_tokens + buckets[m].output_tokens)
    rows = []
    for model, usage in buckets.items():
        cost, pricing_status = analytics_store.compute_cost(
            model or None, usage.input_tokens, usage.output_tokens, usage.cached_input_tokens)
        is_primary = model == primary
        rows.append({
            "agent_run_id": handle.run_id,
            "workflow_run_id": ctx.current_workflow_run_id(),
            "agent_name": agent_name,
            "model": model or None,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms if is_primary else None,
            "status": handle.status,
            "llm_calls": usage.llm_calls,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cached_input_tokens": usage.cached_input_tokens,
            "cost_usd": cost,
            "work_units": handle.work_units if is_primary else None,
            "work_unit_type": handle.work_unit_type if is_primary else None,
            "pricing_status": pricing_status,
        })
    return rows


@contextmanager
def agent_run(agent_name: str) -> Iterator[AgentRunHandle]:
    """Measure one agent-node invocation and enqueue its per-model rows on exit.

    LLM calls made inside the span (directly or in inherited worker threads) fold
    their token usage into a fresh accumulator, so totals are isolated per span and
    per thread. Any exception is recorded as a failed run and re-raised.

    Args:
        agent_name: Stable agent-node name (see :class:`AgentNames`).

    Yields:
        An :class:`AgentRunHandle` for recording work units and fallback status.
    """
    handle = AgentRunHandle(_new_id())
    accumulator = ctx.AgentRunAccumulator()
    started_at = _now()
    start = monotonic()
    token = ctx.bind_agent_run(accumulator)
    # Names the sub-agent on every gateway call made inside this span.
    name_token = agent_name_ctx.set(agent_name)
    try:
        yield handle
    except BaseException as exc:
        handle.status = _failure_status(exc)
        raise
    finally:
        agent_name_ctx.reset(name_token)
        ctx.unbind_agent_run(token)
        try:
            rows = _agent_rows(handle, agent_name, accumulator, started_at, _now(),
                               int((monotonic() - start) * 1000))
            for row in rows:
                writer.enqueue_agent_run(row)
        except Exception as exc:                     # noqa: BLE001
            log.warning("failed to enqueue agent run %s: %s", agent_name, exc)


@contextmanager
def ingestion_run(session_id: str, document_id: str | None = None,
                  user_id: int | None = None) -> Iterator[IngestionRunHandle]:
    """Measure one document ingestion attempt and enqueue its row on exit.

    Args:
        session_id: Project/session the document belongs to.
        document_id: The document being ingested, when known.
        user_id: Owner at ingestion time, when known.

    Yields:
        An :class:`IngestionRunHandle` for recording bytes/pages/chunks and status.
    """
    handle = IngestionRunHandle(_new_id())
    started_at = _now()
    start = monotonic()
    name_token = agent_name_ctx.set(AgentNames.INGESTION)
    try:
        yield handle
    except BaseException:
        handle.status = AnalyticsStatus.FAILED
        raise
    finally:
        agent_name_ctx.reset(name_token)
        try:
            writer.enqueue_ingestion_run({
                "ingestion_run_id": handle.run_id,
                "session_id": session_id,
                "user_id": user_id,
                "document_id": document_id,
                "started_at": started_at,
                "finished_at": _now(),
                "duration_ms": int((monotonic() - start) * 1000),
                "status": handle.status,
                "bytes_processed": handle.bytes_processed,
                "pages_processed": handle.pages_processed,
                "chunks_created": handle.chunks_created,
            })
        except Exception as exc:                     # noqa: BLE001
            log.warning("failed to enqueue ingestion run for %s: %s", session_id, exc)

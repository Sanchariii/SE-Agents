"""Best-effort analytics capture: token/cost accumulation, run spans, persistence.

This package is infrastructure (it only depends on other ``app.utilities`` modules
and ``app.config``), so ``genai`` and the ingestion pipeline can import it without
inverting the service -> handler -> utility layering. Read aggregation for the
dashboard lives in ``app.core.handlers.analytics_handler``; this side only writes.

Nothing here may block the request path: usage is folded into a context-local
accumulator, rows are handed to a bounded background queue, and every failure is
logged and swallowed so telemetry can never fail a user workflow.
"""

from app.utilities.analytics.capture import agent_run, ingestion_run, workflow_run
from app.utilities.analytics.context import record_llm_usage
from app.utilities.analytics.queue import (
    dropped_event_count, start_writer, stop_writer,
)
from app.utilities.analytics.store import analytics_store

__all__ = [
    "agent_run",
    "ingestion_run",
    "workflow_run",
    "record_llm_usage",
    "dropped_event_count",
    "start_writer",
    "stop_writer",
    "analytics_store",
]

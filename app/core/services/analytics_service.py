"""Analytics endpoints (admin only).

Thin HTTP surface over :data:`analytics_handler`. Every route enforces the admin
role, validates and bounds its query parameters, and offloads the blocking
aggregation SQL with ``asyncio.to_thread`` so the event loop is never blocked.
"""

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query

from app.config.constants import AnalyticsConstants, Roles, RouteTags
from app.config.exceptions import AppError
from app.core.dependencies.auth import require_role
from app.core.handlers.analytics_handler import analytics_handler, default_window
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

analytics_router = APIRouter(prefix="/analytics", tags=[RouteTags.ANALYTICS])


def _parse_dt(value: str | None, field: str) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware UTC datetime.

    Args:
        value: The raw query value, or ``None`` when omitted.
        field: The parameter name, used in the error message.

    Returns:
        The parsed UTC datetime, or ``None`` when ``value`` is ``None``.

    Raises:
        AppError: The value is not a valid ISO-8601 timestamp.
    """
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AppError(f"{field} must be an ISO-8601 timestamp") from exc
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(
        tzinfo=timezone.utc)


def _resolve_window(from_: str | None, to: str | None) -> tuple[datetime, datetime]:
    """Resolve and validate the ``(start, end)`` window for a request.

    Defaults to the last ``DEFAULT_WINDOW_DAYS`` when unset, enforces start < end,
    and caps the span at ``MAX_WINDOW_DAYS`` to keep raw-fact queries fast.

    Args:
        from_: Inclusive window start (ISO-8601), or ``None`` for the default.
        to: Exclusive window end (ISO-8601), or ``None`` for now.

    Returns:
        A validated ``(start, end)`` UTC pair.

    Raises:
        AppError: The window is inverted or exceeds the maximum span.
    """
    default_start, default_end = default_window()
    start = _parse_dt(from_, "from") or default_start
    end = _parse_dt(to, "to") or default_end
    if start >= end:
        raise AppError("'from' must be earlier than 'to'")
    if (end - start).days > AnalyticsConstants.MAX_WINDOW_DAYS:
        raise AppError(f"date range exceeds the maximum of "
                       f"{AnalyticsConstants.MAX_WINDOW_DAYS} days")
    return start, end


@analytics_router.get("/summary", deprecated=True)
async def summary(_: dict = Depends(require_role(Roles.ADMIN))):
    """Deprecated legacy summary — superseded by ``GET /analytics/dashboard``."""
    return await asyncio.to_thread(analytics_handler.summary)


@analytics_router.get("/dashboard")
async def dashboard(
    _: dict = Depends(require_role(Roles.ADMIN)),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    tz: str = Query("UTC"),
    agent: str | None = Query(None),
    model: str | None = Query(None),
):
    """Sectioned admin dashboard (KPIs, trends, adoption, performance, cost, quality,
    efficiency, agent x model + model breakdowns, data-quality) for a window.

    Optional `agent` and `model` query params filter the agent/model breakdowns.
    """
    start, end = _resolve_window(from_, to)
    return await asyncio.to_thread(
        analytics_handler.dashboard, start, end, tz, agent, model)


@analytics_router.get("/projects")
async def projects(
    _: dict = Depends(require_role(Roles.ADMIN)),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    limit: int = Query(AnalyticsConstants.DEFAULT_PAGE_SIZE, ge=1,
                       le=AnalyticsConstants.MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    sort: str = Query("last_active"),
    order: str = Query("desc"),
):
    """Paginated, sortable project table for the window (CSV-compatible rows)."""
    start, end = _resolve_window(from_, to)
    return await asyncio.to_thread(
        analytics_handler.projects, start, end,
        limit=limit, offset=offset, sort=sort, order=order)


@analytics_router.get("/projects/{session_id}")
async def project_detail(
    session_id: str,
    _: dict = Depends(require_role(Roles.ADMIN)),
):
    """Per-project drill-down: workflow-run timeline and every agent-node run."""
    return await asyncio.to_thread(analytics_handler.project_detail, session_id)

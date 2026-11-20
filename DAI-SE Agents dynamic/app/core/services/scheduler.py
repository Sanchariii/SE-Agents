"""In-process background scheduler for periodic housekeeping.

Currently runs one job: reaping abandoned direct-to-blob uploads (records still
``upload_in_progress`` past the SAS TTL). It uses APScheduler's ``BackgroundScheduler``,
which runs jobs in its own small thread pool — so the sweep's blocking Postgres/Redis/
Blob I/O never touches the FastAPI event loop (guaranteed non-blocking for requests),
and start/shutdown are clean and synchronous.

The whole thing is best-effort: any scheduler failure is logged and swallowed so it
can never stop the API from serving traffic (e.g. APScheduler not installed).
"""

from functools import lru_cache

from app.config.constants import SchedulerConstants
from app.core.handlers import document_handler
from app.utilities.logger_util import get_logger

log = get_logger(__name__)


@lru_cache
def _scheduler():
    """The single BackgroundScheduler for this process (built lazily so the import is
    optional and the app still boots if APScheduler is missing)."""
    from apscheduler.schedulers.background import BackgroundScheduler
    return BackgroundScheduler(timezone=SchedulerConstants.TIMEZONE)


def _reap_abandoned_uploads() -> None:
    """Job body: the cross-session sweep. Runs in the scheduler's worker thread, so it
    may block on I/O freely without affecting the API."""
    try:
        document_handler.sweep_abandoned_uploads()
    except Exception as exc:                           # noqa: BLE001
        log.warning("scheduled upload sweep failed: %s", exc)


def start() -> None:
    """Start the scheduler and register housekeeping jobs. Idempotent and best-effort:
    a failure here (including APScheduler not being installed) only logs a warning."""
    try:
        sched = _scheduler()
        if sched.running:
            return
        # coalesce + max_instances=1 => missed runs collapse into one and sweeps never overlap.
        sched.add_job(
            _reap_abandoned_uploads,
            trigger="interval",
            minutes=SchedulerConstants.UPLOAD_SWEEP_INTERVAL_MINUTES,
            id=SchedulerConstants.UPLOAD_SWEEP_JOB_ID,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        sched.start()
        log.info("background scheduler started; abandoned-upload sweep every %s min",
                 SchedulerConstants.UPLOAD_SWEEP_INTERVAL_MINUTES)
    except Exception as exc:                           # noqa: BLE001
        log.warning("background scheduler not started (housekeeping disabled): %s", exc)


def shutdown() -> None:
    """Stop the scheduler on app shutdown; never raises."""
    try:
        sched = _scheduler()
        if sched.running:
            sched.shutdown(wait=False)
            log.info("background scheduler stopped")
    except Exception as exc:                           # noqa: BLE001
        log.warning("scheduler shutdown error: %s", exc)

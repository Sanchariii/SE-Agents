"""Concurrency and latency counters for deck builds.

There is no metrics backend in this service, so these are emitted as log lines: the
same place the team already looks, and queryable with the same tooling. Every line is
prefixed `metric ` so a log query can pick them out without matching prose.

Two questions this answers, neither of which could be answered before:

* How many deck builds were running when this one was slow? Sessions share one worker
  pool, and overlapping builds were only ever inferred from timestamps.
* How long did one slide take, and how much of that was waiting rather than working?

The counters are process-wide and guarded by one lock. They are cheap and approximate
by design - a gauge read a moment after it is written may already be stale, which is
fine for contention analysis and would not be fine for billing.
"""

import threading
import time
from contextlib import contextmanager
from typing import Iterator

from app.utilities.logger_util import get_logger

log = get_logger(__name__)

_lock = threading.Lock()
#: Deck builds currently inside `deck_build`.
_active_builds = 0
#: The most builds ever running at once in this process, for the high-water mark.
_peak_builds = 0
#: Slides currently inside `slide_build`, across every deck.
_active_slides = 0


def snapshot() -> dict[str, int]:
    """The current counters, for a health endpoint or an ad-hoc check."""
    with _lock:
        return {"active_builds": _active_builds, "peak_builds": _peak_builds,
                "active_slides": _active_slides}


@contextmanager
def deck_build(session_id: str, slides: int) -> Iterator[None]:
    """Count one deck build for as long as it runs, and time it.

    Args:
        session_id: The session being built, so contention can be attributed.
        slides: How many slides this build will design.

    Yields:
        None. The counters are decremented even if the build raises.
    """
    global _active_builds, _peak_builds
    with _lock:
        _active_builds += 1
        _peak_builds = max(_peak_builds, _active_builds)
        concurrent, peak = _active_builds, _peak_builds
    if concurrent > 1:
        # Not an error - concurrent builds are normal usage - but it is the context
        # that explains a build taking three times as long as the same build alone.
        log.warning("metric deck_build.start session=%s slides=%d concurrent=%d "
                    "peak=%d | this build is sharing the worker pool with %d other(s)",
                    session_id, slides, concurrent, peak, concurrent - 1)
    else:
        log.info("metric deck_build.start session=%s slides=%d concurrent=%d peak=%d",
                 session_id, slides, concurrent, peak)
    started = time.monotonic()
    try:
        yield
    finally:
        elapsed = time.monotonic() - started
        with _lock:
            _active_builds -= 1
            remaining = _active_builds
        log.info("metric deck_build.finish session=%s slides=%d seconds=%.1f "
                 "started_with=%d still_running=%d seconds_per_slide=%.1f",
                 session_id, slides, elapsed, concurrent, remaining,
                 elapsed / slides if slides else 0.0)


@contextmanager
def slide_build(session_id: str, slide_number: int) -> Iterator[float]:
    """Time one slide, and record how contended the pool was while it ran.

    Args:
        session_id: Owning session.
        slide_number: The slide being built.

    Yields:
        The monotonic start time, so the caller can report partial timings.
    """
    global _active_slides
    with _lock:
        _active_slides += 1
        concurrent_slides, concurrent_builds = _active_slides, _active_builds
    started = time.monotonic()
    try:
        yield started
    finally:
        elapsed = time.monotonic() - started
        with _lock:
            _active_slides -= 1
        log.info("metric slide_build session=%s slide=%d seconds=%.1f "
                 "concurrent_slides=%d concurrent_builds=%d",
                 session_id, slide_number, elapsed, concurrent_slides,
                 concurrent_builds)

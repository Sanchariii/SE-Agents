"""Persist and assemble user-visible workflow activity events."""

import json
from collections import defaultdict
from datetime import datetime, timezone

from app.config.constants import PgTables
from app.core.models.chat_message_model import PersistedChatMessage
from app.core.models.document_model import ChatMessage
from app.core.models.workflow_event_model import (
    MessageTimelineItem,
    PersistedWorkflowEvent,
    TimelineItem,
    WorkflowEventTimelineItem,
)
from app.utilities.logger_util import get_logger
from app.utilities.pgsql_util import PgsqlUtil

log = get_logger(__name__)

_DDL = f"""
CREATE TABLE IF NOT EXISTS {PgTables.WORKFLOW_EVENTS} (
    id          SERIAL PRIMARY KEY,
    session_id  TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    event_index INT NOT NULL,
    event_type  TEXT NOT NULL,
    agent       TEXT NOT NULL,
    message     TEXT NOT NULL DEFAULT '',
    data        TEXT NOT NULL DEFAULT '{{}}',
    event_ts    TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, event_index)
);
"""

_DDL_INDEX = (f"CREATE INDEX IF NOT EXISTS ix_{PgTables.WORKFLOW_EVENTS}_session_run "
              f"ON {PgTables.WORKFLOW_EVENTS} (session_id, run_id, event_index)")

_SELECT_COLUMNS = """
id, session_id, run_id, event_index, event_type, agent, message, data,
event_ts, created_at
"""


class WorkflowEventHandler:
    """Durable storage and timeline shaping for emitted workflow events."""

    def __init__(self) -> None:
        """Initialize the handler with a lazy shared Postgres utility."""
        self._db: PgsqlUtil | None = None

    def _pg(self) -> PgsqlUtil:
        """Return the lazily-created shared Postgres utility.

        Returns:
            The process-wide pooled PostgreSQL wrapper.
        """
        if self._db is None:
            self._db = PgsqlUtil()
        return self._db

    def ensure_table(self) -> None:
        """Create the event table and lookup index when absent.

        Returns:
            None.
        """
        self._pg().execute(_DDL)
        self._pg().execute(_DDL_INDEX)

    def record_batch(self, session_id: str, run_id: str, events: list[dict]) -> None:
        """Persist a completed run's events in one idempotent transaction.

        Args:
            session_id: Session that owns the run.
            run_id: Unique workflow invocation identifier.
            events: Captured SSE event dictionaries in publication order.

        Returns:
            None.
        """
        if not events:
            log.warning("workflow run %s for session %s emitted no events",
                        run_id, session_id)
            return
        params = [
            {
                "session_id": session_id,
                "run_id": run_id,
                "event_index": event["event_index"],
                "event_type": event["type"],
                "agent": event["agent"],
                "message": event.get("message") or "",
                "data": json.dumps(event.get("data") or {}, default=str),
                "event_ts": event["ts"],
            }
            for event in events
        ]
        self._pg().execute_many(
            f"""INSERT INTO {PgTables.WORKFLOW_EVENTS}
                (session_id, run_id, event_index, event_type, agent, message, data, event_ts)
                VALUES (:session_id, :run_id, :event_index, :event_type, :agent,
                        :message, :data, :event_ts)
                ON CONFLICT (run_id, event_index) DO NOTHING""",
            params)
        log.info("persisted %s workflow event(s) for session %s run %s",
                 len(events), session_id, run_id)

    @staticmethod
    def _to_event(row: dict) -> PersistedWorkflowEvent:
        """Map a database row to a typed workflow event.

        Args:
            row: PostgreSQL row dictionary.

        Returns:
            Parsed persisted workflow event.
        """
        values = dict(row)
        payload = values.pop("data", {})
        if isinstance(payload, str):
            try:
                payload = json.loads(payload or "{}")
            except json.JSONDecodeError:
                log.warning("workflow event %s has malformed JSON data", values.get("id"))
                payload = {}
        values["data"] = payload
        values["type"] = values.pop("event_type")
        values["ts"] = values.pop("event_ts")
        return PersistedWorkflowEvent(**values)

    def list_events(self, session_id: str,
                    run_ids: list[str] | None = None) -> list[PersistedWorkflowEvent]:
        """List a session's persisted events, optionally restricted to workflow runs.

        Args:
            session_id: Session whose events are requested.
            run_ids: Optional run identifiers selected by message pagination.

        Returns:
            Events ordered by database insertion and per-run event index.
        """
        if run_ids is not None and not run_ids:
            return []
        if run_ids is None:
            rows = self._pg().query(
                f"""SELECT {_SELECT_COLUMNS}
                    FROM {PgTables.WORKFLOW_EVENTS}
                    WHERE session_id = :sid ORDER BY id""",
                {"sid": session_id})
        else:
            rows = self._pg().query(
                f"""SELECT {_SELECT_COLUMNS}
                    FROM {PgTables.WORKFLOW_EVENTS}
                    WHERE session_id = :sid
                      AND run_id = ANY(CAST(:run_ids AS TEXT[]))
                    ORDER BY id""",
                {"sid": session_id, "run_ids": list(dict.fromkeys(run_ids))})
        return [self._to_event(row) for row in rows]

    def list_after(self, run_id: str, after_index: int, limit: int) -> list[dict]:
        """Replay one run's durable events after an index, in the live SSE shape.

        Args:
            run_id: The run whose events are replayed.
            after_index: Return events with ``event_index`` greater than this.
            limit: Maximum events to return.

        Returns:
            Event dictionaries ordered by ``event_index``, shaped exactly like the
            events delivered live (``type``, ``agent``, ``message``, ``data``, ``ts``,
            ``session_id``, ``run_id``, ``event_index``).
        """
        rows = self._pg().query(
            f"""SELECT {_SELECT_COLUMNS}
                FROM {PgTables.WORKFLOW_EVENTS}
                WHERE run_id = :r AND event_index > :after
                ORDER BY event_index
                LIMIT :limit""",
            {"r": run_id, "after": after_index, "limit": limit})
        out = []
        for row in rows:
            event = self._to_event(row)
            out.append({"type": event.type, "agent": event.agent,
                        "message": event.message, "data": event.data,
                        "ts": event.ts.isoformat(), "session_id": event.session_id,
                        "run_id": event.run_id, "event_index": event.event_index})
        return out

    @staticmethod
    def build_timeline(
        messages: list[PersistedChatMessage | ChatMessage],
        events: list[PersistedWorkflowEvent],
    ) -> list[TimelineItem]:
        """Combine selected messages and their events into semantic run order.

        Each run is rendered as user/system messages, workflow events by
        ``event_index``, then assistant messages. Legacy messages without a run id
        remain standalone chronological entries.

        Args:
            messages: Chronologically ordered persisted or Redis chat turns.
            events: Persisted workflow events for the selected runs.

        Returns:
            Kind-discriminated timeline items ordered by run chronology.
        """
        run_messages: dict[str, list[PersistedChatMessage | ChatMessage]] = defaultdict(list)
        run_events: dict[str, list[PersistedWorkflowEvent]] = defaultdict(list)
        groups: list[tuple[float, int, str, str | int]] = []
        seen_runs: set[str] = set()

        for position, message in enumerate(messages):
            run_id = message.run_id
            timestamp = (
                message.created_at if isinstance(message, PersistedChatMessage) else message.ts
            ) or datetime.min.replace(tzinfo=timezone.utc)
            if run_id:
                run_messages[run_id].append(message)
                if run_id not in seen_runs:
                    groups.append((_timestamp_sort_value(timestamp), position,
                                   "run", run_id))
                    seen_runs.add(run_id)
            else:
                groups.append((_timestamp_sort_value(timestamp), position,
                               "legacy", position))

        for event in events:
            run_events[event.run_id].append(event)
            if event.run_id not in seen_runs:
                groups.append((_timestamp_sort_value(event.ts), len(groups),
                               "run", event.run_id))
                seen_runs.add(event.run_id)

        groups.sort(key=lambda group: (group[0], group[1]))
        timeline: list[TimelineItem] = []
        for _, _, group_kind, key in groups:
            if group_kind == "legacy":
                timeline.append(_message_timeline_item(messages[int(key)]))
                continue

            run_id = str(key)
            selected = run_messages.get(run_id, [])
            timeline.extend(_message_timeline_item(message)
                            for message in selected if message.role != "assistant")
            timeline.extend(
                WorkflowEventTimelineItem(
                    id=event.id,
                    run_id=event.run_id,
                    event_index=event.event_index,
                    type=event.type,
                    agent=event.agent,
                    message=event.message,
                    data=event.data,
                    ts=event.ts,
                )
                for event in sorted(run_events.get(run_id, []),
                                    key=lambda item: item.event_index)
            )
            timeline.extend(_message_timeline_item(message)
                            for message in selected if message.role == "assistant")
        return timeline

    def delete_all_for_session(self, session_id: str) -> int:
        """Delete every persisted workflow event for a session.

        Args:
            session_id: Session being deleted.

        Returns:
            Number of removed event rows.
        """
        count = self._pg().query(
            f"SELECT COUNT(*) AS n FROM {PgTables.WORKFLOW_EVENTS} WHERE session_id = :sid",
            {"sid": session_id})[0]["n"]
        self._pg().execute(
            f"DELETE FROM {PgTables.WORKFLOW_EVENTS} WHERE session_id = :sid",
            {"sid": session_id})
        return count


def _message_timeline_item(
    message: PersistedChatMessage | ChatMessage,
) -> MessageTimelineItem:
    """Convert either chat storage model into the public timeline shape.

    Args:
        message: Durable or Redis-backed chat turn.

    Returns:
        Discriminated message timeline item.
    """
    if isinstance(message, PersistedChatMessage):
        return MessageTimelineItem(
            id=message.id,
            run_id=message.run_id,
            role=message.role,
            content=message.content,
            result=message.result,
            ts=message.created_at or datetime.now(timezone.utc),
        )
    return MessageTimelineItem(
        run_id=message.run_id,
        role=message.role,
        content=message.content,
        result=message.result,
        ts=message.ts,
    )


def _timestamp_sort_value(timestamp: datetime) -> float:
    """Normalize aware or legacy-naive timestamps into a comparable UTC value.

    Args:
        timestamp: Timestamp used to position a timeline group.

    Returns:
        Unix timestamp suitable for stable sorting.
    """
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.timestamp()


workflow_event_handler = WorkflowEventHandler()

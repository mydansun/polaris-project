"""DbEventSink — persists events to the DB and fan-outs SSE notifications.

One instance per AgentRun.  Maintains a per-run sequence counter and
serializes writes through the shared asyncpg conn_lock.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any, Literal
from uuid import UUID, uuid4

import asyncpg
from redis.asyncio import Redis

from polaris_worker.agents.base import EventSink
from polaris_worker.queue import session_events_channel

logger = logging.getLogger(__name__)


# 500 ms coalescing window for the StatusBar stats updates.  Long enough
# to batch a burst of playwright snapshots or a multi-file apply_patch
# into one DB write + one SSE frame; short enough that the UI flash
# feels live.
_STATS_DEBOUNCE_SECONDS = 0.5


def _jsonb_safe_dumps(payload: dict[str, Any]) -> str:
    """json.dumps + strip every form of null byte PostgreSQL jsonb refuses.

    Codex's ``exec_command`` surfaces real command output — when a user
    pipes binary files (``find | xargs sed`` on non-text blobs, etc.) the
    aggregated output can contain literal NUL bytes.  ``json.dumps``
    encodes those as the six-character escape ``\\u0000``.  PostgreSQL's
    jsonb parser rejects that escape (``UntranslatableCharacterError:
    \\u0000 cannot be converted to text``) and the INSERT fails, taking
    the whole event with it.

    We can't preserve NULs in jsonb no matter what, so drop them at the
    storage boundary.  The user-facing impact is invisible: the command
    output already has binary junk where the NUL was, stripping one
    character changes nothing meaningful.
    """
    raw = json.dumps(payload, default=str)
    # Both the literal 0x00 (rare, but possible inside a JSON string if
    # json.dumps was handed bytes.decode with surrogates) and the JSON
    # escape sequence \u0000.
    return raw.replace("\\u0000", "").replace("\x00", "")


class DbEventSink(EventSink):
    """Concrete EventSink backed by Postgres (``events`` table) + Redis
    (session events channel)."""

    def __init__(
        self,
        *,
        conn: asyncpg.Connection,
        conn_lock: Any,  # asyncio.Lock — loose typing to avoid circular imports
        redis: Redis,
        session_id: UUID,
        run_id: UUID,
    ) -> None:
        self._conn = conn
        self._conn_lock = conn_lock
        self._redis = redis
        self._session_id = session_id
        self._run_id = run_id
        self._sequence = 0
        self._channel = session_events_channel(session_id)
        # StatusBar counters — coalesced flush every _STATS_DEBOUNCE_SECONDS.
        self._pending_file_delta: int = 0
        self._pending_pw_delta: int = 0
        self._stats_flush_task: asyncio.Task[None] | None = None

    async def _publish(self, event: dict[str, Any]) -> None:
        envelope = {
            "session_id": str(self._session_id),
            "run_id": str(self._run_id),
            **event,
        }
        try:
            await self._redis.publish(self._channel, json.dumps(envelope, default=str))
        except Exception:  # noqa: BLE001
            logger.warning("SSE publish failed for run=%s", self._run_id, exc_info=True)

    async def emit_event_started(
        self,
        *,
        kind: str,
        external_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> UUID:
        self._sequence += 1
        event_id = uuid4()
        payload = payload or {}
        async with self._conn_lock:
            await self._conn.execute(
                "INSERT INTO events "
                "(id, run_id, sequence, external_id, kind, status, payload_jsonb) "
                "VALUES ($1, $2, $3, $4, $5, 'started', $6::jsonb)",
                event_id,
                self._run_id,
                self._sequence,
                external_id,
                kind,
                _jsonb_safe_dumps(payload),
            )
        await self._publish(
            {
                "kind": "event_started",
                "event_kind": kind,
                "sequence": self._sequence,
                "external_id": external_id,
                "payload": payload,
            }
        )
        return event_id

    async def emit_event_completed(
        self,
        *,
        event_id: UUID,
        external_id: str | None = None,
        payload: dict[str, Any] | None = None,
        status: Literal["completed", "failed"] = "completed",
    ) -> None:
        payload = payload or {}
        async with self._conn_lock:
            # Merge the new payload over whatever was stored on event_started
            # so completion can add fields (final_text, return_code, etc.).
            await self._conn.execute(
                "UPDATE events SET status=$1, "
                "external_id=COALESCE($2, external_id), "
                "payload_jsonb = payload_jsonb || $3::jsonb, "
                "updated_at=now() "
                "WHERE id=$4",
                status,
                external_id,
                _jsonb_safe_dumps(payload),
                event_id,
            )
            row = await self._conn.fetchrow(
                "SELECT kind, external_id, payload_jsonb "
                "FROM events WHERE id=$1",
                event_id,
            )
        if row is None:
            return
        await self._publish(
            {
                "kind": "event_completed",
                "event_kind": row["kind"],
                "external_id": row["external_id"],
                "payload": json.loads(row["payload_jsonb"])
                if isinstance(row["payload_jsonb"], str)
                else (row["payload_jsonb"] or {}),
                "status": status,
            }
        )

    async def emit_message_delta(self, *, text: str) -> None:
        # Deltas aren't persisted — they're pure SSE fan-out for token
        # streaming in the chat UI.
        await self._publish({"kind": "agent_message_delta", "text": text})

    # ── StatusBar counters (file_change_count + playwright_call_count) ──

    async def bump_file_delta(self, delta: int = 1) -> None:
        """Accumulate file-change bumps from the inotify watcher.  Flushed
        in a coalescing window so a burst of fs events produces one DB
        UPDATE + one SSE frame."""
        if delta <= 0:
            return
        self._pending_file_delta += delta
        self._schedule_stats_flush()

    async def bump_playwright_delta(self, delta: int = 1) -> None:
        """Accumulate playwright MCP call bumps from on_item_completed."""
        if delta <= 0:
            return
        self._pending_pw_delta += delta
        self._schedule_stats_flush()

    def _schedule_stats_flush(self) -> None:
        if self._stats_flush_task is not None and not self._stats_flush_task.done():
            return  # already waiting to flush
        self._stats_flush_task = asyncio.create_task(self._flush_stats_after_debounce())

    async def _flush_stats_after_debounce(self) -> None:
        try:
            await asyncio.sleep(_STATS_DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            # Forced-flush path will drain the pending deltas itself.
            return
        await self._flush_stats_now()

    async def _flush_stats_now(self) -> None:
        """Apply any pending deltas immediately.  Safe to call repeatedly;
        no-op when there's nothing buffered."""
        file_d = self._pending_file_delta
        pw_d = self._pending_pw_delta
        self._pending_file_delta = 0
        self._pending_pw_delta = 0
        if file_d == 0 and pw_d == 0:
            return
        try:
            async with self._conn_lock:
                row = await self._conn.fetchrow(
                    "UPDATE sessions "
                    "SET file_change_count = file_change_count + $2, "
                    "    playwright_call_count = playwright_call_count + $3 "
                    "WHERE id=$1 "
                    "RETURNING file_change_count, playwright_call_count",
                    self._session_id,
                    file_d,
                    pw_d,
                )
        except Exception:  # noqa: BLE001
            logger.warning(
                "session stats UPDATE failed (session=%s, dropping delta f=%d p=%d)",
                self._session_id, file_d, pw_d, exc_info=True,
            )
            return
        if row is None:
            return
        await self._publish(
            {
                "kind": "session_stats_updated",
                "file_change_count": row["file_change_count"],
                "playwright_call_count": row["playwright_call_count"],
                "file_change_delta": file_d,
                "playwright_call_delta": pw_d,
            }
        )
        logger.info(
            "session stats flushed (session=%s, +file=%d → %d, +pw=%d → %d)",
            self._session_id,
            file_d,
            row["file_change_count"],
            pw_d,
            row["playwright_call_count"],
        )

    async def finalize_stats(self) -> None:
        """Flush any pending deltas synchronously.  Call on run / session
        teardown so the last 500ms of activity isn't lost."""
        if self._stats_flush_task is not None:
            self._stats_flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stats_flush_task
            self._stats_flush_task = None
        await self._flush_stats_now()

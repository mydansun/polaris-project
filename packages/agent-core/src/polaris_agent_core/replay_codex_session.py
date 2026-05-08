"""Replay variant of :class:`PolarisCodexSession`.

Reads recorded codex JSON-RPC frames from a fixture and dispatches them
to the same ``TurnItemSink`` the real session would.  Public surface
mirrors :class:`PolarisCodexSession` exactly, so the worker's
``_get_or_open_session`` can swap one for the other without any other
code change.

Behavior:

  * Recorded frames are stored as a flat list of ``{t, direction,
    frame}`` entries.  ``direction == "in"`` means server → client
    (notifications, responses, server-side requests).  ``direction ==
    "out"`` means client → server (requests, notifications) — these
    don't drive the sink and are skipped during replay.

  * ``run_turn`` walks forward from the current cursor until the next
    ``turn/completed`` notification, dispatching each in-frame to the
    appropriate sink method or, for server-side requests, the
    configured handler.

  * ``ensure_thread`` returns a stable synthetic thread id derived
    from the first recorded ``thread/started`` notification (or a
    deterministic fallback).  We don't try to round-trip the original
    request, since codex isn't there to verify.

  * Server-side ``item/tool/requestUserInput`` requests in the
    recording are forwarded to ``user_input_handler`` exactly as the
    real session would.  The recorded answer is *not* used as the
    return value — the worker's clarification flow does its real
    SSE/Redis dance and the user (driven by Playwright reading
    ``user_actions``) supplies answers via the live UI.  Codex's
    continued stream is then drawn straight from the recording, on
    the assumption the replay user picked the same answers as the
    recording user.  When that assumption breaks (operator picks a
    different option), the recorded stream may not match — that's a
    Phase 3.7 concern, not a Phase 3.1 concern.

Network calls are blocked by ``polaris_agent_core.replay_guard``.  Any
fallthrough to the real ``PolarisCodexSession`` would raise via the
existing guards on every external client, surfacing the bug
immediately rather than silently spending money.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from polaris_agent_core.codex_app_server import (
    PolarisAgentConfig,
    TurnItemSink,
)

logger = logging.getLogger(__name__)


# ── Fixture loading ────────────────────────────────────────────────────


def _load_fixture(path: Path) -> dict[str, Any]:
    if path.suffix == ".gz":
        with gzip.open(path, "rb") as f:
            return json.loads(f.read().decode("utf-8"))
    return json.loads(path.read_text(encoding="utf-8"))


# ── Replay session ─────────────────────────────────────────────────────


class ReplayCodexSession:
    """Drop-in replacement for :class:`PolarisCodexSession`.

    Public methods mirror the real session.  ``ws_url`` returns a
    sentinel ``replay://`` so any code that grep'ed it for routing
    can detect replay-mode operation.

    Lifecycle: a fresh session reads the fixture once at construction.
    Subsequent ``run_turn`` calls advance an internal cursor through
    the recorded frames; reaching the end exhausts the session
    (next call would return immediately with status='completed' and
    no items).
    """

    def __init__(
        self,
        config: PolarisAgentConfig,
        *,
        fixture_path: Path,
    ) -> None:
        self._config = config
        self._fixture_path = fixture_path
        self._fixture = _load_fixture(fixture_path)
        self._frames: list[dict[str, Any]] = (
            self._fixture.get("agent_io", {}).get("codex_frames") or []
        )
        self._cursor = 0
        # Cache the recorded thread id for ensure_thread().  Lifted from
        # the first recorded thread/started notification's params.
        self._recorded_thread_id: str | None = self._scan_recorded_thread_id()
        # Lock so two concurrent run_turn calls (orchestrator quirk)
        # don't double-advance the cursor.  Real PolarisCodexSession
        # doesn't need this because the underlying ws is the natural
        # serializer; here, the cursor is shared mutable state.
        self._turn_lock = asyncio.Lock()

    # ── Public surface (mirrors PolarisCodexSession) ─────────────────

    @property
    def ws_url(self) -> str:
        # Sentinel — anything that pattern-matches "ws://" can detect
        # replay-mode and skip its real-session-only logic.
        return "replay://"

    def is_alive(self) -> bool:
        # A replay session is "alive" as long as there are frames to
        # consume.  Cursor at end → exhausted, but still answers
        # ensure_thread cleanly.
        return True

    async def start(self) -> None:
        # No real WS to open.  Log so test failures distinguish a
        # never-started replay from a stalled one.
        logger.info(
            "replay codex session: scenario=%s frames=%d",
            self._fixture.get("scenario"),
            len(self._frames),
        )

    async def close(self) -> None:
        return None

    async def ensure_thread(self, existing_thread_id: str | None) -> str:
        if existing_thread_id:
            return existing_thread_id
        if self._recorded_thread_id:
            return self._recorded_thread_id
        # Stable synthetic id — same value across sessions of the
        # same fixture so DB lookups by thread_id produce consistent
        # rows.  Hash the fixture path; reasonably unique per scenario.
        return f"replay-thread-{abs(hash(str(self._fixture_path))) % (10**12):012d}"

    async def run_turn(
        self,
        *,
        thread_id: str,
        user_message: str,
        project_id: Any = None,
        workspace_id: Any = None,
        turn_id: Any = None,
        sink: TurnItemSink,
        mode: str = "plan",
        local_image_paths: list[str] | None = None,
    ) -> None:
        """Replay the next recorded turn into ``sink``.

        Args mirror the real session; ``user_message`` and the
        UUIDs are accepted for API compatibility but not used (the
        recorded transcript already contains the user message and
        turn boundaries).
        """
        async with self._turn_lock:
            await self._consume_recorded_turn(thread_id=thread_id, sink=sink)

    async def interrupt(self, thread_id: str) -> None:
        # No-op in replay: the recorded turn either completed or
        # interrupted on its own; we don't accelerate that.  Could
        # be wired in Phase 3.7 if a test wants to assert interrupt
        # semantics from the UI side.
        return None

    async def steer(self, thread_id: str, additional_text: str) -> None:
        # Steer recorded turns isn't supported in v1.  Replay tests
        # should drive plan/build only.
        logger.warning(
            "replay session: steer(%r) is a no-op in replay mode",
            additional_text[:80],
        )
        return None

    # ── Internal: cursor walk ────────────────────────────────────────

    async def _consume_recorded_turn(
        self, *, thread_id: str, sink: TurnItemSink
    ) -> None:
        """Walk frames until the next ``turn/completed``, dispatching to ``sink``.

        Skips outgoing frames (we're not the codex), skips response
        frames the real session would have unblocked locally, and
        forwards server-side requests to the configured handlers.
        """
        # Track whether we ever saw turn/started for this turn — if
        # the recording ended mid-turn, we surface that so tests
        # don't silently pass on a truncated fixture.
        saw_turn_started = False

        while self._cursor < len(self._frames):
            entry = self._frames[self._cursor]
            self._cursor += 1
            if entry.get("direction") != "in":
                continue  # outgoing frames are reconstructable; skip

            frame = entry.get("frame") or {}
            method = frame.get("method")
            params = frame.get("params") or {}
            mid = frame.get("id")

            # Server-side request: id + method present, codex asking us
            # for input/approval.  Real session dispatches to handlers
            # that drive the SSE/Redis flow; replay does the same so
            # the frontend sees the same UI.
            if isinstance(mid, (int, str)) and method:
                with contextlib.suppress(Exception):
                    await self._dispatch_server_request(method, params)
                continue

            # Plain notification: forward to sink.
            if not method:
                continue  # responses without method — already-routed locally
            if method == "turn/started":
                saw_turn_started = True
                turn = params.get("turn") or {}
                tid = turn.get("id")
                if isinstance(tid, str):
                    await sink.on_turn_started(tid)
                continue
            if method == "item/started":
                item = params.get("item")
                if isinstance(item, dict):
                    await sink.on_item_started(item)
                continue
            if method == "item/completed":
                item = params.get("item")
                if isinstance(item, dict):
                    await sink.on_item_completed(item)
                continue
            if method == "item/agentMessage/delta":
                delta = params.get("delta")
                if isinstance(delta, str):
                    await sink.on_agent_message_delta(delta)
                continue
            if method == "turn/completed":
                turn = params.get("turn") or {}
                status = turn.get("status") or "completed"
                err = None
                if isinstance(turn.get("error"), dict):
                    err = turn["error"].get("message")
                await sink.on_turn_completed(status, err)
                return
            # Other methods (rateLimits/tokenUsage/diff updates, plan
            # streaming deltas, mcpServer status updates) don't have
            # sink handlers in the real session either — they affect
            # state that surfaces via item/* completion events.  Drop.

        # Cursor exhausted without a turn/completed.  The real session
        # would have raised TurnTimeoutError or ConnectionLostError
        # here; closest analogue in replay is "fixture truncated".
        if saw_turn_started:
            await sink.on_turn_completed(
                "failed",
                f"replay fixture exhausted mid-turn at frame {self._cursor}/{len(self._frames)}",
            )
        else:
            # Never saw turn/started either — fixture has no more turns.
            # Treat as a clean no-op with status='completed'; orchestrator
            # will surface "no codex output" via the empty event count.
            await sink.on_turn_completed("completed", None)

    async def _dispatch_server_request(
        self, method: str, params: dict[str, Any]
    ) -> None:
        """Mirror :meth:`PolarisCodexSession._handle_server_request`.

        The real session returns a response to codex; replay just runs
        the side effects (SSE clarification card, dynamic-tool call
        handler) and discards the return value.  Codex's continued
        stream is read straight from the recording.
        """
        # Auto-accept approvals — the workspace container IS our
        # sandbox in record mode, so we trust everything.
        AUTO_ACCEPT_METHODS = {
            "item/commandExecution/requestApproval",
            "execCommandApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
            "mcpServer/elicitation/request",
            "item/dynamicToolCall/requestApproval",
        }
        if method in AUTO_ACCEPT_METHODS:
            return

        if method == "item/tool/requestUserInput":
            handler = self._config.user_input_handler
            if handler is None:
                logger.info(
                    "replay: requestUserInput received but no user_input_handler "
                    "configured — recording assumes a handler exists"
                )
                return
            questions = params.get("questions", [])
            with contextlib.suppress(Exception):
                await handler(questions, params)
            return

        if method == "item/tool/call":
            tool = params.get("tool")
            handler = self._config.dynamic_tool_handler
            if handler is None or not isinstance(tool, str):
                return
            args = params.get("arguments")
            if not isinstance(args, dict):
                args = {}
            # Run the dynamic tool handler for its side effects (e.g.
            # set_project_root persists to DB; focus_browser fires SSE).
            # Discard the response — codex's stream is recorded.
            with contextlib.suppress(Exception):
                await handler(tool, args, params)
            return

        # Unknown server-side request — log and continue.
        logger.debug(
            "replay: ignoring unrecognized server request method=%r", method
        )

    # ── Helpers ─────────────────────────────────────────────────────

    def _scan_recorded_thread_id(self) -> str | None:
        """Return the recorded thread id from the first ``thread/started``
        notification, if any.  Used as the deterministic ensure_thread
        return value so DB rows keyed on thread_id stay coherent across
        replay runs of the same fixture.
        """
        for entry in self._frames:
            if entry.get("direction") != "in":
                continue
            frame = entry.get("frame") or {}
            # The thread/start RESPONSE comes back as a plain id+result
            # frame (no method).  The thread id lives at result.thread.id.
            result = frame.get("result")
            if isinstance(result, dict):
                thread = result.get("thread")
                if isinstance(thread, dict):
                    tid = thread.get("id")
                    if isinstance(tid, str) and tid:
                        return tid
        return None

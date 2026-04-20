"""Generic Agent interface shared by CodexAgent and DiscoveryAgent.

A **Session** is one user-message exchange.  The orchestrator picks a sequence
of **Runs** to execute inside the session based on ``Session.mode`` (see
``orchestrator.AGENTS_BY_MODE``).  Each Run is one Agent's execution and
emits **Events** via an ``EventSink``.

Every agent implements the same ``run()`` coroutine, so the orchestrator
doesn't need to know the internals of Codex or the LangGraph discovery
pipeline — it just threads inputs/outputs between runs.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Awaitable, Callable, Literal, Protocol
from uuid import UUID

import asyncpg
from redis.asyncio import Redis


class AgentKind(StrEnum):
    """Adapter-level identifier for the agent implementation."""

    codex = "codex"
    discovery = "discovery"


# User-input callback raised by an agent when it needs to ask the user
# structured questions.  Returns the user's answers or an empty list on
# timeout / cancellation.  Wired up per-run by the orchestrator so the
# callback carries ``session_id`` / ``run_id`` context for the shared
# clarification channel.
UserInputFn = Callable[
    [list[dict[str, Any]]],  # frontend ClarificationQuestion shape
    Awaitable[list[dict[str, Any]]],  # [{question_id, answer}, ...]
]


@dataclass
class SessionContext:
    """Everything an agent needs that's scoped to the whole Session (not a
    specific Run).  Agents must NOT mutate the DB conn outside of the
    conn_lock."""

    session_id: UUID
    project_id: UUID
    workspace_id: UUID
    redis: Redis
    conn: asyncpg.Connection
    conn_lock: asyncio.Lock
    settings: Any  # polaris_worker.config.Settings — typed loosely to avoid circularity


@dataclass
class RunContext:
    """Per-Run state handed to an agent's ``run()`` coroutine.

    ``input`` is the agent-specific payload prepared by the orchestrator:
    for CodexAgent it contains ``user_message`` / ``codex_mode``; for
    DiscoveryAgent it contains ``user_message`` / ``seed_intent``.
    ``output`` starts empty and is populated on the returned RunOutcome.
    """

    run_id: UUID
    sequence: int
    input: dict[str, Any]
    user_input_fn: UserInputFn


@dataclass
class RunOutcome:
    """Value returned from an agent's ``run()``.

    ``output`` is persisted to ``agent_runs.output_jsonb`` and is the
    primary channel by which downstream runs receive information from this
    run (see orchestrator's ``threading_forward``).  ``final_message`` is
    only used by CodexAgent to populate ``Session.final_message`` at the
    tail of the session.
    """

    status: Literal["completed", "failed", "skipped"]
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    external_id: str | None = None
    final_message: str | None = None


class EventSink(Protocol):
    """Agent → DB/SSE event emitter.  One instance per Run.

    Agents call the ``emit_event_*`` methods to surface atomic progress items
    (e.g. one item per Codex item / one per LangGraph node transition).
    Kind must already be namespaced (``codex:agent_message`` etc.).
    """

    async def emit_event_started(
        self,
        *,
        kind: str,
        external_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> UUID:
        """Insert a new ``events`` row with status='started' and broadcast
        an ``event_started`` SSE message.  Returns the new event id so the
        caller can later complete or fail it."""

    async def emit_event_completed(
        self,
        *,
        event_id: UUID,
        external_id: str | None = None,
        payload: dict[str, Any] | None = None,
        status: Literal["completed", "failed"] = "completed",
    ) -> None:
        """Flip the ``events`` row to completed/failed and broadcast an
        ``event_completed`` SSE message."""

    async def emit_message_delta(self, *, text: str) -> None:
        """Broadcast a transient ``agent_message_delta`` SSE message (for
        streaming token rendering).  Not persisted."""

    async def bump_file_delta(self, delta: int = 1) -> None:
        """Increment the session's ``file_change_count`` by ``delta``.
        Writes are coalesced inside a short debounce window so a burst
        of filesystem events lands as one DB UPDATE + one SSE frame."""

    async def bump_playwright_delta(self, delta: int = 1) -> None:
        """Increment the session's ``playwright_call_count`` by ``delta``.
        Same coalescing path as ``bump_file_delta``."""

    async def finalize_stats(self) -> None:
        """Force-flush any pending counter deltas.  Call on agent-run
        teardown so the last debounce window isn't lost."""


class Agent(Protocol):
    """Contract every concrete agent adapter implements."""

    kind: AgentKind

    async def run(
        self,
        session: SessionContext,
        run: RunContext,
        sink: EventSink,
    ) -> RunOutcome:
        """Drive the agent from start to finish for one Run.  May call
        ``run.user_input_fn`` as many times as needed; may emit any number
        of events via ``sink``.  Must return a RunOutcome whose ``output``
        is the handoff to the next run (if any)."""

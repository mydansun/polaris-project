"""Session orchestrator — runs the Agent chain defined by Session.mode.

Entry point: :func:`process_session_job`, invoked by the worker main loop
for each message consumed from the Redis ``SESSION_JOBS_STREAM``.  For
each AgentKind in ``AGENTS_BY_MODE[mode]`` it inserts an ``agent_runs``
row, builds a ``DbEventSink``, then drives the agent's ``run()``.  Between
runs, :func:`threading_forward` shapes the next run's input from the
previous run's output (e.g. discovery → codex: brief becomes user_message).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Mapping
from functools import partial
from typing import Any
from uuid import UUID

import asyncpg
from redis.asyncio import Redis

from polaris_api.services.run_quota import release_run_slot
from polaris_worker.agents.base import (
    Agent,
    AgentKind,
    RunContext,
    RunOutcome,
    SessionContext,
)
from polaris_worker.clarification import build_design_intent_user_input_fn
from polaris_worker.config import Settings
from polaris_worker.queue import session_control_channel, session_events_channel
from polaris_worker.sink import DbEventSink

logger = logging.getLogger(__name__)


AGENTS_BY_MODE: dict[str, list[AgentKind]] = {
    "build_planned": [AgentKind.codex],
    "build_direct": [AgentKind.codex],
    "discover_then_build": [AgentKind.discovery, AgentKind.codex],
}

# ── Codex-mode translation ─────────────────────────────────────────────────
# Agent-internal: once a Session-level mode picks a codex run, the codex
# run needs its own mode (plan / default).  For build_planned + the codex
# tail of discover_then_build we use "plan"; for build_direct we use
# "default".  This preserves the existing Codex SDK contract.
_CODEX_MODE_BY_SESSION_MODE: dict[str, str] = {
    "build_planned": "plan",
    "build_direct": "default",
    "discover_then_build": "plan",
}


def _build_agent(kind: AgentKind) -> Agent:
    """Deferred import to break the module cycle (agents/codex.py imports
    from polaris_agent_core which is heavy)."""
    if kind == AgentKind.codex:
        from polaris_worker.agents.codex import CodexAgent

        return CodexAgent()
    if kind == AgentKind.discovery:
        from polaris_worker.agents.discovery import DiscoveryAgent

        return DiscoveryAgent()
    raise ValueError(f"unknown agent kind: {kind}")


def _initial_input(
    *,
    agent_kind: AgentKind,
    session_mode: str,
    user_message: str,
    seed_intent: dict | None,
) -> dict[str, Any]:
    """Build the ``run.input`` dict for the first run of each kind."""
    if agent_kind == AgentKind.discovery:
        return {"user_message": user_message, "seed_intent": seed_intent}
    if agent_kind == AgentKind.codex:
        return {
            "user_message": user_message,
            "codex_mode": _CODEX_MODE_BY_SESSION_MODE.get(session_mode, "plan"),
        }
    return {}


def threading_forward(
    *,
    next_kind: AgentKind,
    prev_outcome: RunOutcome,
    session_mode: str,
    base_input: dict[str, Any],
) -> dict[str, Any]:
    """Shape the next agent's input from the previous run's output.

    Currently:
      - discovery → codex: promote ``prev_outcome.output['brief']`` to
        ``user_message`` so the Codex run's plan round sees the compiled brief.
    """
    new_input = dict(base_input)
    if next_kind == AgentKind.codex and prev_outcome.output.get("brief"):
        new_input["user_message"] = prev_outcome.output["brief"]
    return new_input


async def _load_active_design_intent(
    conn: asyncpg.Connection,
    conn_lock: asyncio.Lock,
    project_id: UUID,
) -> dict | None:
    """Return the active design_intent.intent_jsonb for the project (used
    as ``seed_intent`` when the user re-discovers)."""
    async with conn_lock:
        row = await conn.fetchrow(
            "SELECT intent_jsonb FROM design_intents "
            "WHERE project_id=$1 AND status='active' LIMIT 1",
            project_id,
        )
    if row is None:
        return None
    data = row["intent_jsonb"]
    if isinstance(data, str):
        return json.loads(data)
    return data


async def _mark_session_running(
    conn: asyncpg.Connection, conn_lock: asyncio.Lock, session_id: UUID
) -> None:
    async with conn_lock:
        await conn.execute(
            "UPDATE sessions SET status='running', "
            "started_at=COALESCE(started_at, now()) WHERE id=$1",
            session_id,
        )


async def _finalize_session(
    conn: asyncpg.Connection,
    conn_lock: asyncio.Lock,
    redis: Redis,
    session_id: UUID,
    *,
    status: str,
    error: str | None = None,
    final_message: str | None = None,
) -> None:
    async with conn_lock:
        await conn.execute(
            "UPDATE sessions SET status=$1, error_message=$2, "
            "final_message=COALESCE($3, final_message), "
            "finished_at=now() WHERE id=$4",
            status,
            error,
            final_message,
            session_id,
        )
    with contextlib.suppress(Exception):
        await redis.publish(
            session_events_channel(session_id),
            json.dumps(
                {
                    "session_id": str(session_id),
                    "kind": "session_completed",
                    "status": status,
                    "error": error,
                    "final_message": final_message,
                }
            ),
        )


async def _insert_agent_run(
    conn: asyncpg.Connection,
    conn_lock: asyncio.Lock,
    *,
    session_id: UUID,
    sequence: int,
    agent_kind: AgentKind,
    input_payload: dict[str, Any],
) -> UUID:
    async with conn_lock:
        row = await conn.fetchrow(
            "INSERT INTO agent_runs "
            "(session_id, sequence, agent_kind, status, input_jsonb) "
            "VALUES ($1, $2, $3, 'running', $4::jsonb) RETURNING id",
            session_id,
            sequence,
            str(agent_kind),
            json.dumps(input_payload),
        )
        await conn.execute(
            "UPDATE agent_runs SET started_at=now() WHERE id=$1",
            row["id"],
        )
    return row["id"]


async def _finalize_run(
    conn: asyncpg.Connection,
    conn_lock: asyncio.Lock,
    redis: Redis,
    *,
    session_id: UUID,
    run_id: UUID,
    agent_kind: AgentKind,
    outcome: RunOutcome,
) -> None:
    async with conn_lock:
        await conn.execute(
            "UPDATE agent_runs SET status=$1, output_jsonb=$2::jsonb, "
            "external_id=COALESCE($3, external_id), error_message=$4, "
            "finished_at=now() WHERE id=$5",
            outcome.status,
            json.dumps(outcome.output),
            outcome.external_id,
            outcome.error,
            run_id,
        )
    with contextlib.suppress(Exception):
        await redis.publish(
            session_events_channel(session_id),
            json.dumps(
                {
                    "session_id": str(session_id),
                    "run_id": str(run_id),
                    "kind": "run_completed",
                    "agent_kind": str(agent_kind),
                    "status": outcome.status,
                    "error": outcome.error,
                    "external_id": outcome.external_id,
                },
                default=str,
            ),
        )


async def _announce_run_started(
    redis: Redis,
    *,
    session_id: UUID,
    run_id: UUID,
    agent_kind: AgentKind,
    sequence: int,
) -> None:
    with contextlib.suppress(Exception):
        await redis.publish(
            session_events_channel(session_id),
            json.dumps(
                {
                    "session_id": str(session_id),
                    "run_id": str(run_id),
                    "kind": "run_started",
                    "agent_kind": str(agent_kind),
                    "sequence": sequence,
                }
            ),
        )


async def _announce_session_started(redis: Redis, session_id: UUID) -> None:
    with contextlib.suppress(Exception):
        await redis.publish(
            session_events_channel(session_id),
            json.dumps(
                {"session_id": str(session_id), "kind": "session_started"}
            ),
        )


# ── Per-run UserInputFn factory ────────────────────────────────────────────


def _make_user_input_fn(
    *,
    redis: Redis,
    session_id: UUID,
    run_id: UUID,
    agent_kind: AgentKind,
):
    """Per-run clarification callback.  Both agents use the same SSE/Redis
    pubsub channel; the ``agent_kind`` tag lets the UI label the source."""
    # We reuse the shared ``build_design_intent_user_input_fn`` helper —
    # its body is agent-agnostic; the function name is historical.
    return build_design_intent_user_input_fn(
        redis=redis,
        session_id=session_id,
        run_id=run_id,
        source=str(agent_kind),
    )


# ── Main entry ─────────────────────────────────────────────────────────────


async def process_session_job(
    conn: asyncpg.Connection,
    redis: Redis,
    fields: Mapping[str, str],
    settings: Settings,
) -> None:
    """Consume one Redis session-job message end-to-end."""
    session_id_raw = fields.get("session_id")
    if not session_id_raw:
        logger.warning("session job missing session_id: %s", fields)
        return
    session_id = UUID(session_id_raw)

    # Load the session row and short-circuit if it's already terminal.
    row = await conn.fetchrow(
        "SELECT project_id, workspace_id, user_message, status, mode "
        "FROM sessions WHERE id=$1",
        session_id,
    )
    if row is None:
        logger.error("session %s not found; skipping", session_id)
        return
    if row["status"] not in ("queued", "running"):
        logger.info("session %s status=%s; skipping", session_id, row["status"])
        return

    project_id: UUID = row["project_id"]
    workspace_id: UUID = row["workspace_id"]
    user_message: str = row["user_message"]
    mode: str = row["mode"]

    log = logging.LoggerAdapter(
        logger,
        {
            "session_id": str(session_id),
            "workspace_id": str(workspace_id),
            "project_id": str(project_id),
        },
    )
    log.info("handling session (mode=%s) msg=%r", mode, user_message[:120])

    conn_lock = asyncio.Lock()
    session_ctx = SessionContext(
        session_id=session_id,
        project_id=project_id,
        workspace_id=workspace_id,
        redis=redis,
        conn=conn,
        conn_lock=conn_lock,
        settings=settings,
    )

    await _mark_session_running(conn, conn_lock, session_id)
    await _announce_session_started(redis, session_id)

    # Kick off a session-level control consumer so interrupt/steer
    # commands published to the session control channel can reach the
    # currently-running agent.  We hold a reference to the active run's
    # control handle so the consumer can forward it.
    active_handle: dict[str, Any] = {"agent": None, "run_id": None}
    control_task = asyncio.create_task(
        _consume_session_control(redis, session_id, active_handle)
    )

    seed_intent = await _load_active_design_intent(conn, conn_lock, project_id)
    seq = 0
    prev_outcome: RunOutcome | None = None
    last_agent_kind: AgentKind | None = None

    try:
        for agent_kind in AGENTS_BY_MODE.get(mode, [AgentKind.codex]):
            # Belt-and-suspenders: the API may have flipped sessions.status
            # to "interrupted" while the previous agent was finishing (or
            # between agents).  Skip the rest of the agent chain on a
            # fresh read.  Doesn't replace the in-agent cooperative cancel
            # (CodexAgent turn/interrupt, DiscoveryAgent task.cancel) —
            # those handle the "user clicked stop mid-run" path.
            async with conn_lock:
                status_row = await conn.fetchrow(
                    "SELECT status FROM sessions WHERE id=$1", session_id,
                )
            if status_row is not None and status_row["status"] == "interrupted":
                log.info("session already marked interrupted; skipping remaining agents")
                # API has already published the terminal SSE in its own
                # handler, but emit one more so late subscribers see a
                # consistent final frame.  Frontend closes EventSource on
                # the first terminal frame, so this is a no-op there.
                with contextlib.suppress(Exception):
                    await redis.publish(
                        session_events_channel(session_id),
                        json.dumps(
                            {
                                "session_id": str(session_id),
                                "kind": "session_completed",
                                "status": "interrupted",
                                "error": None,
                                "final_message": None,
                            }
                        ),
                    )
                return

            seq += 1
            base_input = _initial_input(
                agent_kind=agent_kind,
                session_mode=mode,
                user_message=user_message,
                seed_intent=seed_intent,
            )
            if prev_outcome is not None and last_agent_kind is not None:
                base_input = threading_forward(
                    next_kind=agent_kind,
                    prev_outcome=prev_outcome,
                    session_mode=mode,
                    base_input=base_input,
                )

            run_id = await _insert_agent_run(
                conn,
                conn_lock,
                session_id=session_id,
                sequence=seq,
                agent_kind=agent_kind,
                input_payload=base_input,
            )
            await _announce_run_started(
                redis,
                session_id=session_id,
                run_id=run_id,
                agent_kind=agent_kind,
                sequence=seq,
            )

            user_input_fn = _make_user_input_fn(
                redis=redis,
                session_id=session_id,
                run_id=run_id,
                agent_kind=agent_kind,
            )
            run_ctx = RunContext(
                run_id=run_id,
                sequence=seq,
                input=base_input,
                user_input_fn=user_input_fn,
            )
            sink = DbEventSink(
                conn=conn,
                conn_lock=conn_lock,
                redis=redis,
                session_id=session_id,
                run_id=run_id,
            )
            agent = _build_agent(agent_kind)
            active_handle["agent"] = agent
            active_handle["run_id"] = run_id

            try:
                outcome = await agent.run(session_ctx, run_ctx, sink)
            except Exception as exc:  # noqa: BLE001
                log.exception("agent %s raised", agent_kind)
                outcome = RunOutcome(
                    status="failed",
                    output={},
                    error=f"{type(exc).__name__}: {exc}",
                )

            await _finalize_run(
                conn,
                conn_lock,
                redis,
                session_id=session_id,
                run_id=run_id,
                agent_kind=agent_kind,
                outcome=outcome,
            )

            if outcome.status == "failed":
                await _finalize_session(
                    conn,
                    conn_lock,
                    redis,
                    session_id,
                    status="failed",
                    error=outcome.error or f"{agent_kind} failed",
                )
                return

            if outcome.status == "interrupted":
                # User hit Stop mid-run.  The agent already cleaned up
                # (CodexAgent via turn/interrupt, DiscoveryAgent via
                # task.cancel).  Finalise the session as interrupted so
                # the DB, SSE, and frontend state all agree.
                await _finalize_session(
                    conn,
                    conn_lock,
                    redis,
                    session_id,
                    status="interrupted",
                    error=None,
                )
                return

            prev_outcome = outcome
            last_agent_kind = agent_kind
            active_handle["agent"] = None
            active_handle["run_id"] = None

        # All runs completed.  Prefer the last codex run's final message
        # as the session-level summary; fall back to nothing.
        final_msg = prev_outcome.final_message if prev_outcome is not None else None
        await _finalize_session(
            conn,
            conn_lock,
            redis,
            session_id,
            status="completed",
            final_message=final_msg,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("orchestrator crashed")
        await _finalize_session(
            conn,
            conn_lock,
            redis,
            session_id,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        control_task.cancel()
        with contextlib.suppress(BaseException):
            await control_task
        # Release the run-quota slot acquired by the API at session
        # creation.  Do this regardless of outcome (completed / failed
        # / crash) so one stuck session doesn't tie up a slot until TTL.
        # Idempotent: release is just a pair of ZREMs.
        async with conn_lock:
            owner_row = await conn.fetchrow(
                "SELECT projects.user_id FROM sessions "
                "JOIN projects ON projects.id = sessions.project_id "
                "WHERE sessions.id = $1",
                session_id,
            )
        if owner_row is not None:
            with contextlib.suppress(Exception):
                await release_run_slot(
                    redis=redis,
                    user_id=owner_row["user_id"],
                    session_id=session_id,
                )


async def _consume_session_control(
    redis: Redis,
    session_id: UUID,
    active_handle: dict[str, Any],
) -> None:
    """Listen on the session's control channel and forward interrupt/steer
    to whichever agent is currently running.  Only CodexAgent knows how to
    handle these at the moment; DiscoveryAgent ignores them (the discovery
    graph is deterministic)."""
    channel = session_control_channel(session_id)
    pubsub = redis.pubsub()
    await pubsub.subscribe(channel)
    try:
        async for msg in pubsub.listen():
            if msg.get("type") != "message":
                continue
            data = msg.get("data")
            if isinstance(data, bytes):
                data = data.decode("utf-8", "replace")
            if not isinstance(data, str):
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            agent = active_handle.get("agent")
            if agent is None or not hasattr(agent, "handle_control"):
                continue
            with contextlib.suppress(Exception):
                await agent.handle_control(event)
    except asyncio.CancelledError:
        pass
    finally:
        with contextlib.suppress(Exception):
            await pubsub.unsubscribe(channel)
        with contextlib.suppress(Exception):
            await pubsub.aclose()

"""Sessions API — the chat surface over Polaris's multi-agent pipeline.

Each POST /projects/{id}/sessions creates a ``Session`` row, enqueues it
on the Redis session stream, and the worker drives it through the
orchestrator (discovery agent and/or Codex agent per the session's
``mode``).  Clients can stream progress via
GET /sessions/{id}/events (Redis pubsub → SSE) and can interrupt/steer
an in-flight session via the control pubsub channel.
"""
from __future__ import annotations

import asyncio
import json
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession as DbSession

from polaris_api.config import Settings, get_settings
from polaris_api.db import get_session
from polaris_api.deps import get_current_user
from polaris_api.models import (
    AgentRun,
    Event,
    Project,
    Session,
    User,
    Workspace,
)
from polaris_api.queue import (
    enqueue_session,
    session_control_channel,
    session_events_channel,
)
from polaris_api.redis_client import get_redis
from polaris_api.schemas import (
    AgentRunResponse,
    EventResponse,
    SessionCreate,
    SessionDetailResponse,
    SessionResponse,
    SessionSteerRequest,
)
from polaris_api.services.run_quota import (
    QuotaRejection,
    acquire_run_slot,
    release_run_slot,
)

router = APIRouter(tags=["sessions"])

DEFAULT_MODE = "build_planned"


async def _load_user_project(
    db: DbSession, project_id: UUID, user: User
) -> Project:
    project = await db.get(Project, project_id)
    if project is None or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


async def _load_user_session(
    db: DbSession, session_id: UUID, user: User
) -> Session:
    row = await db.get(Session, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")
    project = await db.get(Project, row.project_id)
    if project is None or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Session not found")
    return row


@router.post(
    "/projects/{project_id}/sessions",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_session(
    project_id: UUID,
    payload: SessionCreate,
    user: User = Depends(get_current_user),
    db: DbSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Session:
    project = await _load_user_project(db, project_id, user)

    workspace_result = await db.execute(
        select(Workspace)
        .where(Workspace.project_id == project.id)
        .order_by(Workspace.created_at.desc())
    )
    workspace = workspace_result.scalars().first()
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    next_seq_row = await db.execute(
        select(func.coalesce(func.max(Session.sequence), 0) + 1).where(
            Session.project_id == project.id
        )
    )
    sequence = int(next_seq_row.scalar_one())

    mode = payload.mode or DEFAULT_MODE
    # Pre-generate the id so the Redis quota entry + DB row + enqueued
    # job all share the same session_id before the row is flushed.
    session_id = uuid4()

    redis: Redis = get_redis()
    try:
        rejection = await acquire_run_slot(
            redis=redis, user_id=user.id, session_id=session_id, settings=settings
        )
        if rejection is not None:
            limit = (
                settings.max_global_runs
                if rejection == QuotaRejection.GLOBAL
                else settings.max_user_runs
            )
            raise HTTPException(
                status_code=429,
                detail={"reason": rejection.value, "limit": limit},
            )

        # Quota is held — any failure below must release it before
        # surfacing the exception so we don't leak slot.
        try:
            session_row = Session(
                id=session_id,
                project_id=project.id,
                workspace_id=workspace.id,
                sequence=sequence,
                user_message=payload.message,
                mode=mode,
                status="queued",
            )
            db.add(session_row)
            await db.commit()
            await db.refresh(session_row)
            await enqueue_session(
                redis, session_row.id, project.id, workspace.id, mode=mode
            )
        except Exception:
            await release_run_slot(
                redis=redis, user_id=user.id, session_id=session_id
            )
            raise
    finally:
        await redis.aclose()

    return session_row


@router.get("/projects/{project_id}/sessions", response_model=list[SessionResponse])
async def list_project_sessions(
    project_id: UUID,
    limit: int | None = None,
    before_sequence: int | None = None,
    user: User = Depends(get_current_user),
    db: DbSession = Depends(get_session),
) -> list[Session]:
    """List sessions for a project.

    Pagination (optional):
      - ``limit``: max rows to return (clamped to 1..100).
      - ``before_sequence``: only return sessions with ``sequence < N``.
    """
    await _load_user_project(db, project_id, user)
    query = select(Session).where(Session.project_id == project_id)
    if before_sequence is not None:
        query = query.where(Session.sequence < before_sequence)
    if limit is not None:
        limit = max(1, min(limit, 100))
        query = query.order_by(Session.sequence.desc()).limit(limit)
    else:
        query = query.order_by(Session.sequence.asc())
    result = await db.execute(query)
    rows = list(result.scalars().all())
    if limit is not None:
        rows.reverse()
    return rows


@router.get("/sessions/{session_id}", response_model=SessionDetailResponse)
async def get_session_detail(
    session_id: UUID,
    user: User = Depends(get_current_user),
    db: DbSession = Depends(get_session),
) -> SessionDetailResponse:
    session_row = await _load_user_session(db, session_id, user)

    runs_result = await db.execute(
        select(AgentRun)
        .where(AgentRun.session_id == session_id)
        .order_by(AgentRun.sequence.asc())
    )
    runs = list(runs_result.scalars().all())

    run_responses: list[AgentRunResponse] = []
    for run in runs:
        events_result = await db.execute(
            select(Event)
            .where(Event.run_id == run.id)
            .order_by(Event.sequence.asc())
        )
        events = [EventResponse.model_validate(e) for e in events_result.scalars().all()]
        run_responses.append(
            AgentRunResponse(
                id=run.id,
                session_id=run.session_id,
                sequence=run.sequence,
                agent_kind=run.agent_kind,  # type: ignore[arg-type]
                status=run.status,  # type: ignore[arg-type]
                external_id=run.external_id,
                started_at=run.started_at,
                finished_at=run.finished_at,
                events=events,
            )
        )

    return SessionDetailResponse(
        **SessionResponse.model_validate(session_row).model_dump(),
        runs=run_responses,
    )


@router.get("/sessions/{session_id}/events")
async def stream_session_events(
    session_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: DbSession = Depends(get_session),
) -> StreamingResponse:
    session_row = await _load_user_session(db, session_id, user)
    channel = session_events_channel(session_row.id)

    async def event_source():
        redis: Redis = get_redis()
        pubsub = redis.pubsub()
        await pubsub.subscribe(channel)
        try:
            yield f"event: ready\ndata: {json.dumps({'session_id': str(session_row.id)})}\n\n"
            while True:
                if await request.is_disconnected():
                    return
                try:
                    message = await asyncio.wait_for(
                        pubsub.get_message(ignore_subscribe_messages=True),
                        timeout=15.0,
                    )
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if message is None:
                    continue
                data = message.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8", "replace")
                if not isinstance(data, str):
                    continue
                yield f"data: {data}\n\n"
        finally:
            try:
                await pubsub.unsubscribe(channel)
            except Exception:
                pass
            await pubsub.aclose()
            await redis.aclose()

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/sessions/{session_id}/interrupt", response_model=SessionResponse)
async def interrupt_session(
    session_id: UUID,
    user: User = Depends(get_current_user),
    db: DbSession = Depends(get_session),
) -> Session:
    session_row = await _load_user_session(db, session_id, user)
    if session_row.status not in ("queued", "running"):
        return session_row

    redis: Redis = get_redis()
    try:
        await redis.publish(
            session_control_channel(session_row.id),
            json.dumps({"kind": "interrupt", "session_id": str(session_row.id)}),
        )
    finally:
        await redis.aclose()

    session_row.status = "interrupted"
    await db.commit()
    await db.refresh(session_row)

    # Broadcast the terminal SSE frame so any client subscribed to this
    # session's events stream flips its UI immediately, without waiting
    # for the worker to catch up on the control channel + actually stop
    # the agent.  The worker's own `_finalize_session` will also
    # publish the same shape once the run drains — frontend's
    # `onSessionTerminal` handler closes the EventSource on the first
    # `session_completed` frame, so the duplicate is a no-op.
    redis2: Redis = get_redis()
    try:
        await redis2.publish(
            session_events_channel(session_row.id),
            json.dumps(
                {
                    "session_id": str(session_row.id),
                    "kind": "session_completed",
                    "status": "interrupted",
                    "error": None,
                    "final_message": None,
                }
            ),
        )
    finally:
        await redis2.aclose()

    return session_row


@router.post("/sessions/{session_id}/steer", response_model=SessionResponse)
async def steer_session(
    session_id: UUID,
    payload: SessionSteerRequest,
    user: User = Depends(get_current_user),
    db: DbSession = Depends(get_session),
) -> Session:
    session_row = await _load_user_session(db, session_id, user)
    if session_row.status != "running":
        raise HTTPException(
            status_code=409,
            detail=f"Cannot steer session in status {session_row.status}",
        )

    redis: Redis = get_redis()
    try:
        await redis.publish(
            session_control_channel(session_row.id),
            json.dumps(
                {
                    "kind": "steer",
                    "session_id": str(session_row.id),
                    "message": payload.message,
                }
            ),
        )
    finally:
        await redis.aclose()

    return session_row

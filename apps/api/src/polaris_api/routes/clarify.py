"""Clarification request/response routes.

Any agent (Codex or discovery) can raise a clarification — the shared
``wait_for_answers`` helper in the worker publishes a SSE event and blocks
on the per-session Redis channel until the user POSTs answers here.

Clarifications are bound to both a Session and the specific AgentRun that
asked, so the channel lookup is explicit (no ``status='running'`` guessing).
The in-container ``polaris clarify`` CLI falls back to the latest running
run when it doesn't know its own ids.

Auth: session cookie (frontend) OR X-Polaris-Workspace-Token (CLI).
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polaris_api.config import Settings, get_settings
from polaris_api.db import get_session
from polaris_api.models import (
    AgentRun,
    Clarification,
    Project,
    Session,
    User,
    Workspace,
)
from polaris_api.queue import clarification_channel, session_events_channel
from polaris_api.redis_client import get_redis
from polaris_api.services.auth import verify_session_token

router = APIRouter(tags=["clarify"])


# ── Auth helper (same dual-path as dev_deps) ──────────────────────────────

async def _resolve_project_access(
    request: Request,
    project_id: UUID,
    db: AsyncSession,
    settings: Settings,
    workspace_token: str | None,
) -> Project:
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    token = request.cookies.get("polaris_session")
    if token:
        user_id = verify_session_token(token, settings)
        if user_id is not None:
            user = await db.get(User, UUID(user_id))
            if user is not None and project.user_id == user.id:
                return project

    if workspace_token:
        ws = (
            await db.execute(
                select(Workspace).where(
                    Workspace.project_id == project_id,
                    Workspace.workspace_token == workspace_token,
                )
            )
        ).scalars().first()
        if ws is not None:
            return project

    raise HTTPException(status_code=401, detail="Not authenticated")


async def _get_latest_running_run(
    db: AsyncSession, project_id: UUID
) -> tuple[Session, AgentRun]:
    """Find the most recently started AgentRun for a running session in this
    project.  Used as fallback when CLI doesn't provide explicit ids."""
    result = await db.execute(
        select(AgentRun, Session)
        .join(Session, AgentRun.session_id == Session.id)
        .where(
            Session.project_id == project_id,
            Session.status == "running",
            AgentRun.status == "running",
        )
        .order_by(AgentRun.created_at.desc())
        .limit(1)
    )
    row = result.first()
    if row is None:
        raise HTTPException(
            status_code=409,
            detail="No running agent run for this project",
        )
    run, session_row = row
    return session_row, run


# ── Schemas ───────────────────────────────────────────────────────────────


class ClarifyRequestBody(BaseModel):
    questions: list[dict] = Field(min_length=1, max_length=3)


class ClarifyResponseBody(BaseModel):
    request_id: str
    answers: dict[str, dict]
    # Explicit session/run binding from the web UI.  Optional for the
    # in-container CLI, which falls back to the latest running-run lookup.
    session_id: UUID | None = None
    run_id: UUID | None = None


# ── Routes ────────────────────────────────────────────────────────────────


@router.post("/projects/{project_id}/clarify/request")
async def create_clarification(
    project_id: UUID,
    body: ClarifyRequestBody,
    request: Request,
    x_polaris_workspace_token: str | None = Header(default=None),
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    """CLI posts structured questions. Persists + publishes SSE."""
    project = await _resolve_project_access(
        request, project_id, db, settings, x_polaris_workspace_token,
    )
    session_row, run = await _get_latest_running_run(db, project.id)

    request_id = str(uuid4())
    row = Clarification(
        id=uuid4(),
        request_id=request_id,
        project_id=project.id,
        session_id=session_row.id,
        run_id=run.id,
        agent_kind=run.agent_kind,
        status="pending",
        questions_jsonb=body.questions,
        answers_jsonb={},
    )
    db.add(row)
    await db.commit()

    redis: Redis = get_redis()
    try:
        await redis.publish(
            session_events_channel(session_row.id),
            json.dumps({
                "session_id": str(session_row.id),
                "run_id": str(run.id),
                "kind": "clarification_requested",
                "request": {
                    "request_id": request_id,
                    "questions": body.questions,
                    "source": run.agent_kind,
                },
            }),
        )
    finally:
        await redis.aclose()

    return {"request_id": request_id}


@router.get("/projects/{project_id}/clarify/pending")
async def get_pending_clarification(
    project_id: UUID,
    request: Request,
    x_polaris_workspace_token: str | None = Header(default=None),
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Frontend calls this on page load to recover any in-flight clarification."""
    await _resolve_project_access(
        request, project_id, db, settings, x_polaris_workspace_token,
    )

    result = await db.execute(
        select(Clarification)
        .where(
            Clarification.project_id == project_id,
            Clarification.status == "pending",
        )
        .order_by(Clarification.created_at.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return {"pending": None}
    return {
        "pending": {
            "request_id": row.request_id,
            "questions": row.questions_jsonb,
            "source": row.agent_kind,
            "session_id": str(row.session_id),
            "run_id": str(row.run_id),
        }
    }


@router.get("/projects/{project_id}/clarify/response")
async def poll_clarification(
    project_id: UUID,
    request_id: str,
    request: Request,
    x_polaris_workspace_token: str | None = Header(default=None),
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    """CLI polls until the user has answered."""
    await _resolve_project_access(
        request, project_id, db, settings, x_polaris_workspace_token,
    )

    result = await db.execute(
        select(Clarification).where(
            Clarification.project_id == project_id,
            Clarification.request_id == request_id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Clarification request not found")

    if row.status == "answered":
        return {"answered": True, "answers": row.answers_jsonb}
    return {"answered": False}


@router.post("/projects/{project_id}/clarify/response")
async def submit_clarification(
    project_id: UUID,
    body: ClarifyResponseBody,
    request: Request,
    x_polaris_workspace_token: str | None = Header(default=None),
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Frontend (or CLI) submits user's answers.

    Publishes answers to the per-session Redis clarification channel the
    worker is blocking on.  The worker matches by ``request_id``.
    """
    await _resolve_project_access(
        request, project_id, db, settings, x_polaris_workspace_token,
    )

    # Prefer explicit session_id/run_id from the body (web UI threads them
    # from the SSE event).  Fall back to the latest running-run lookup for
    # the in-container CLI, which doesn't know its own ids.
    if body.session_id is not None:
        session_row = await db.get(Session, body.session_id)
        if session_row is None or session_row.project_id != project_id:
            raise HTTPException(
                status_code=404, detail="Session not found for this project"
            )
        run: AgentRun | None = None
        if body.run_id is not None:
            run = await db.get(AgentRun, body.run_id)
            if run is None or run.session_id != session_row.id:
                raise HTTPException(
                    status_code=404, detail="Run not found in this session"
                )
    else:
        session_row, run = await _get_latest_running_run(db, project_id)

    redis: Redis = get_redis()
    try:
        await redis.publish(
            clarification_channel(session_row.id),
            json.dumps({
                "request_id": body.request_id,
                "answers": body.answers,
                "run_id": str(run.id) if run is not None else None,
            }, default=str),
        )
        ack_payload: dict = {
            "session_id": str(session_row.id),
            "kind": "clarification_answered",
            "request_id": body.request_id,
        }
        if run is not None:
            ack_payload["run_id"] = str(run.id)
        await redis.publish(
            session_events_channel(session_row.id),
            json.dumps(ack_payload),
        )
    finally:
        await redis.aclose()

    return {"ok": True}

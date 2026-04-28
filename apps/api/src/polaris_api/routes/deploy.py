"""Deployment routes — trigger publish, list deployments, stream live
progress via SSE, and rollback.

Auth accepts EITHER:
  * the session cookie (User-driven UI / curl from browser)
  * X-Polaris-Workspace-Token header (the in-container `polaris` CLI)

Both paths resolve to the same Project scope; the workspace-token path is
limited to the project owning that workspace.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polaris_api.config import Settings, get_settings
from polaris_api.db import SessionLocal, get_session
from polaris_api.deps import get_current_user
from polaris_api.models import Deployment, Project, User, Workspace
from polaris_api.schemas import (
    DeploymentDetailResponse,
    DeploymentResponse,
    DeploymentTriggerRequest,
)
from polaris_api.services.auth import verify_session_token
from polaris_api.services.publish import (
    PublishError,
    run_publish,
    run_rollback,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["deploy"])


# ─── Auth helper ────────────────────────────────────────────────────────────


async def _resolve_project_access(
    request: Request,
    project_id: UUID,
    session: AsyncSession,
    settings: Settings,
    workspace_token: str | None,
) -> Project:
    """Return the Project if the caller can act on it via either auth path.
    Raises 401 / 403 / 404 as appropriate."""
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    # Path 1: session cookie → User check
    token = request.cookies.get("polaris_session")
    if token:
        user_id = verify_session_token(token, settings)
        if user_id is not None:
            user = await session.get(User, UUID(user_id))
            if user is not None and project.user_id == user.id:
                return project

    # Path 2: workspace token → Workspace check
    if workspace_token:
        workspace = (
            await session.execute(
                select(Workspace).where(
                    Workspace.project_id == project_id,
                    Workspace.workspace_token == workspace_token,
                )
            )
        ).scalars().first()
        if workspace is not None:
            return project

    raise HTTPException(status_code=401, detail="Not authenticated for this project")


# ─── POST /projects/{id}/publish ────────────────────────────────────────────


@router.post(
    "/projects/{project_id}/publish",
    response_model=DeploymentResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_publish(
    project_id: UUID,
    request: Request,
    payload: DeploymentTriggerRequest | None = None,
    x_polaris_workspace_token: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Deployment:
    project = await _resolve_project_access(
        request, project_id, session, settings, x_polaris_workspace_token
    )

    # Don't let more than one in-flight publish per project — avoid
    # compose project-name collisions and image-tag races.
    in_flight = (
        await session.execute(
            select(Deployment).where(
                Deployment.project_id == project.id,
                Deployment.status.in_(["queued", "building", "deploying"]),
            )
        )
    ).scalars().first()
    if in_flight is not None:
        raise HTTPException(
            status_code=409,
            detail=f"publish already in-flight ({in_flight.status}, id={in_flight.id})",
        )

    dep = Deployment(project_id=project.id, status="queued")
    session.add(dep)
    await session.commit()
    await session.refresh(dep)

    # Spawn the actual pipeline as a background task with its OWN db session
    # (the request-scoped one will be closed once we return).  Using
    # async_sessionmaker tied to the main engine.
    deployment_id = dep.id

    async def _bg() -> None:
        async with SessionLocal() as bg_session:
            await run_publish(
                session=bg_session,
                deployment_id=deployment_id,
                settings=settings,
            )

    asyncio.create_task(_bg())

    return dep


# ─── GET /projects/{id}/deployments ─────────────────────────────────────────


@router.get(
    "/projects/{project_id}/deployments",
    response_model=list[DeploymentResponse],
)
async def list_deployments(
    project_id: UUID,
    request: Request,
    limit: int = 20,
    x_polaris_workspace_token: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[Deployment]:
    await _resolve_project_access(
        request, project_id, session, settings, x_polaris_workspace_token
    )
    limit = max(1, min(limit, 100))
    rows = (
        await session.execute(
            select(Deployment)
            .where(Deployment.project_id == project_id)
            .order_by(Deployment.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


# ─── GET /deployments/{id} ──────────────────────────────────────────────────


@router.get(
    "/deployments/{deployment_id}",
    response_model=DeploymentDetailResponse,
)
async def get_deployment(
    deployment_id: UUID,
    request: Request,
    x_polaris_workspace_token: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Deployment:
    dep = await session.get(Deployment, deployment_id)
    if dep is None:
        raise HTTPException(status_code=404, detail="Deployment not found")
    await _resolve_project_access(
        request, dep.project_id, session, settings, x_polaris_workspace_token
    )
    return dep


# ─── GET /deployments/{id}/events ───────────────────────────────────────────


@router.get("/deployments/{deployment_id}/events")
async def deployment_events(
    deployment_id: UUID,
    request: Request,
    x_polaris_workspace_token: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """SSE stream that polls the deployment row every 500ms and emits:
      * new log lines (diff from previously-sent build/smoke log)
      * status transitions
      * a terminal `ready` / `failed` event
    Simpler than pub/sub, and fine for demo throughput."""

    dep = await session.get(Deployment, deployment_id)
    if dep is None:
        raise HTTPException(status_code=404, detail="Deployment not found")
    await _resolve_project_access(
        request, dep.project_id, session, settings, x_polaris_workspace_token
    )


    async def _stream() -> AsyncIterator[bytes]:
        last_status = ""
        last_build_len = 0
        last_smoke_len = 0
        yield b": connected\n\n"
        try:
            while True:
                async with SessionLocal() as s:
                    dep_now = await s.get(Deployment, deployment_id)
                if dep_now is None:
                    break

                new_build = (dep_now.build_log or "")[last_build_len:]
                if new_build:
                    last_build_len += len(new_build)
                    payload = json.dumps({"type": "log", "channel": "build", "data": new_build})
                    yield f"data: {payload}\n\n".encode()

                new_smoke = (dep_now.smoke_log or "")[last_smoke_len:]
                if new_smoke:
                    last_smoke_len += len(new_smoke)
                    payload = json.dumps({"type": "log", "channel": "smoke", "data": new_smoke})
                    yield f"data: {payload}\n\n".encode()

                if dep_now.status != last_status:
                    last_status = dep_now.status
                    payload = json.dumps({"type": "status", "status": dep_now.status})
                    yield f"data: {payload}\n\n".encode()

                if dep_now.status == "ready":
                    payload = json.dumps(
                        {"type": "ready", "domain": dep_now.domain, "image": dep_now.image_tag}
                    )
                    yield f"data: {payload}\n\n".encode()
                    break
                if dep_now.status == "failed":
                    payload = json.dumps({"type": "failed", "error": dep_now.error or ""})
                    yield f"data: {payload}\n\n".encode()
                    break

                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─── POST /projects/{id}/rollback ───────────────────────────────────────────


@router.post(
    "/projects/{project_id}/rollback",
    response_model=DeploymentResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_rollback(
    project_id: UUID,
    request: Request,
    payload: dict[str, str],
    x_polaris_workspace_token: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Deployment:
    project = await _resolve_project_access(
        request, project_id, session, settings, x_polaris_workspace_token
    )
    target_hash = (payload or {}).get("git_commit_hash", "").strip()
    if not target_hash:
        raise HTTPException(status_code=400, detail="git_commit_hash is required")
    try:
        dep = await run_rollback(
            session=session,
            project_id=project.id,
            target_hash=target_hash,
            triggered_by="user",
            settings=settings,
        )
    except PublishError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return dep

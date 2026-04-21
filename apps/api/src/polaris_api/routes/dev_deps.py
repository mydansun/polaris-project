"""Dev-time dependency slot routes.

GET    /projects/{id}/workspace/dev-deps                  list enabled slots
POST   /projects/{id}/workspace/dev-deps  {service}       ensure + start
DELETE /projects/{id}/workspace/dev-deps/{service}        stop + remove

Auth: same double-path as the deploy routes — session cookie OR
`X-Polaris-Workspace-Token` header.  The in-container `polaris` CLI uses
the header path.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polaris_api.config import Settings, get_settings
from polaris_api.db import get_session
from polaris_api.models import Project, User, Workspace
from polaris_api.schemas import DevDepEnsureRequest, WorkspaceDepServiceResponse
from polaris_api.services.auth import verify_session_token
from polaris_api.services.dev_deps import (
    DevDepError,
    SUPPORTED_DEP_SERVICES,
    cleanup_workspace_dev_deps as _cleanup,  # noqa: F401  (re-exported for tests / delete flows)
    ensure_dev_dep,
    list_dev_deps,
    remove_dev_dep,
)

router = APIRouter(tags=["dev-deps"])


async def _resolve_workspace_access(
    request: Request,
    project_id: UUID,
    session: AsyncSession,
    settings: Settings,
    workspace_token: str | None,
) -> Workspace:
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    workspace = (
        await session.execute(
            select(Workspace)
            .where(Workspace.project_id == project_id)
            .order_by(Workspace.created_at.desc())
        )
    ).scalars().first()
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    # Auth path 1: session cookie
    token = request.cookies.get("polaris_session")
    if token:
        user_id = verify_session_token(token, settings)
        if user_id is not None:
            user = await session.get(User, UUID(user_id))
            if user is not None and project.user_id == user.id:
                return workspace

    # Auth path 2: workspace token header (used by in-container polaris CLI)
    if workspace_token and workspace.workspace_token == workspace_token:
        return workspace

    raise HTTPException(status_code=401, detail="Not authenticated for this workspace")


@router.get(
    "/projects/{project_id}/workspace/dev-deps",
    response_model=list[WorkspaceDepServiceResponse],
)
async def list_dev_deps_endpoint(
    project_id: UUID,
    request: Request,
    x_polaris_workspace_token: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[WorkspaceDepServiceResponse]:
    workspace = await _resolve_workspace_access(
        request, project_id, session, settings, x_polaris_workspace_token,
    )
    rows = await list_dev_deps(session, workspace.id)
    return [WorkspaceDepServiceResponse.model_validate(r) for r in rows]


@router.post(
    "/projects/{project_id}/workspace/dev-deps",
    response_model=WorkspaceDepServiceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def ensure_dev_dep_endpoint(
    project_id: UUID,
    payload: DevDepEnsureRequest,
    request: Request,
    x_polaris_workspace_token: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> WorkspaceDepServiceResponse:
    workspace = await _resolve_workspace_access(
        request, project_id, session, settings, x_polaris_workspace_token,
    )
    if payload.service not in SUPPORTED_DEP_SERVICES:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported dev dep: {payload.service!r}",
        )
    try:
        row = await ensure_dev_dep(session, workspace, payload.service)
    except DevDepError as exc:
        # Network-not-found and container/healthcheck failures land here.
        # 409 because the caller CAN retry after starting the workspace.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return WorkspaceDepServiceResponse.model_validate(row)


@router.delete(
    "/projects/{project_id}/workspace/dev-deps/{service}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_dev_dep_endpoint(
    project_id: UUID,
    service: str,
    request: Request,
    x_polaris_workspace_token: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> None:
    workspace = await _resolve_workspace_access(
        request, project_id, session, settings, x_polaris_workspace_token,
    )
    try:
        await remove_dev_dep(session, workspace, service)
    except DevDepError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

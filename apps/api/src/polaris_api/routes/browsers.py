from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polaris_api.config import Settings, get_settings
from polaris_api.db import get_session
from polaris_api.deps import get_current_user
from polaris_api.models import BrowserSession, Project, User, Workspace
from polaris_api.schemas import BrowserSessionResponse
from polaris_api.services.compose import ComposeError
from polaris_api.services.runtime import ensure_workspace_runtime, get_current_browser_session, stop_runtime

router = APIRouter(prefix="/projects/{project_id}/browser", tags=["browser"])


async def get_project_workspace(session: AsyncSession, project_id: UUID, user: User) -> Workspace:
    project = await session.get(Project, project_id)
    if project is None or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found")

    result = await session.execute(
        select(Workspace).where(Workspace.project_id == project_id).order_by(Workspace.created_at.desc())
    )
    workspace = result.scalars().first()
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace


@router.get(
    "/session",
    response_model=BrowserSessionResponse,
    responses={204: {"description": "No session yet (polling state)."}},
)
async def get_browser_session(
    project_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BrowserSession | Response:
    workspace = await get_project_workspace(session, project_id, user)
    # project_root gate: if the agent hasn't declared a project root yet,
    # the browser iframe has nothing meaningful to show.  This endpoint is
    # polled from the frontend (project-load fetch + 30s fallback poller);
    # returning 204 instead of 404 keeps devtools / server logs quiet —
    # devtools renders 204 as a success, not as a red 4xx line.  The
    # caller treats null as "no session yet".
    if workspace.project_root is None:
        return Response(status_code=204)
    browser_session = await get_current_browser_session(session, workspace)
    if browser_session is None:
        return Response(status_code=204)
    return browser_session


@router.post("/session", response_model=BrowserSessionResponse)
async def ensure_browser_session(
    project_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> BrowserSession:
    workspace = await get_project_workspace(session, project_id, user)
    try:
        state = await ensure_workspace_runtime(
            session=session,
            workspace=workspace,
            settings=settings,
        )
    except ComposeError as exc:
        raise HTTPException(status_code=500, detail=f"Workspace runtime failed: {exc}") from exc
    if state.browser_session is None:
        raise HTTPException(status_code=500, detail="Browser session was not created")
    # Same gate as GET: runtime might be ready, but we don't hand the URL
    # back to the frontend until the agent has declared a project root.
    if state.workspace.project_root is None:
        raise HTTPException(status_code=404, detail="Browser session not available yet")
    return state.browser_session


@router.delete("/session", response_model=BrowserSessionResponse)
async def stop_browser_session(
    project_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> BrowserSession:
    workspace = await get_project_workspace(session, project_id, user)
    browser_session = await get_current_browser_session(session, workspace)
    if browser_session is None:
        raise HTTPException(status_code=404, detail="Browser session not found")

    try:
        state = await stop_runtime(session=session, workspace=workspace, settings=settings)
    except ComposeError as exc:
        browser_session.status = "failed"
        browser_session.context_metadata_jsonb = {
            **browser_session.context_metadata_jsonb,
            "error": str(exc),
        }
        await session.commit()
        raise HTTPException(status_code=500, detail=f"Workspace runtime failed: {exc}") from exc

    if state.browser_session is None:
        raise HTTPException(status_code=404, detail="Browser session not found")
    return state.browser_session

from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polaris_api.db import get_session
from polaris_api.config import Settings, get_settings
from polaris_api.deps import get_current_user
from polaris_api.models import Project, ProjectVersion, User, Workspace
from polaris_api.schemas import (
    ProjectVersionResponse,
    SnapshotCreate,
    WorkspaceFileContent,
    WorkspaceFileEntry,
    WorkspaceFileWrite,
    WorkspaceIdeSessionResponse,
    WorkspaceRuntimeRequest,
    WorkspaceRuntimeResponse,
)
from polaris_api.services.compose import ComposeError
from polaris_api.services.runtime import (
    RuntimeState,
    browser_session_services,
    ensure_workspace_runtime,
    get_current_browser_session,
    restart_runtime,
    stop_runtime,
)
from polaris_api.services.workspaces import (
    WorkspaceConflictError,
    WorkspaceError,
    WorkspacePathError,
    create_snapshot,
    list_workspace_files,
    read_workspace_file,
    write_workspace_file,
)

router = APIRouter(prefix="/projects/{project_id}/workspace", tags=["workspace"])


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


def ide_session_response(workspace: Workspace) -> WorkspaceIdeSessionResponse:
    # Gate: no ``project_root`` means the agent hasn't yet declared where the
    # project lives, so the IDE iframe has nothing meaningful to show.  Return
    # null url until that signal arrives.  See also the ``project_root``
    # gating doc in routes/browsers.py.
    ide_url = workspace.ide_url if workspace.project_root is not None else None
    return WorkspaceIdeSessionResponse(
        workspace_id=workspace.id,
        project_id=workspace.project_id,
        ide_url=ide_url,
        ide_status=workspace.ide_status,
    )


def runtime_response(state: RuntimeState) -> WorkspaceRuntimeResponse:
    browser_session = state.browser_session
    # Same gate as ide_session_response: ``project_root IS NULL`` means the
    # workspace is technically running but has no meaningful user-facing
    # content yet — don't leak the URLs to the frontend.  Containers stay
    # up (Codex needs them), but iframes won't mount.
    has_project_root = state.workspace.project_root is not None
    ide_url = state.workspace.ide_url if has_project_root else None
    browser_url = (
        browser_session.vnc_url
        if browser_session is not None and has_project_root
        else None
    )
    return WorkspaceRuntimeResponse(
        workspace_id=state.workspace.id,
        project_id=state.workspace.project_id,
        status="ready" if state.workspace.ide_status == "ready" else state.workspace.ide_status,
        enabled_services=browser_session_services(browser_session)
        if not state.enabled_services
        else state.enabled_services,
        ide_url=ide_url,
        browser_url=browser_url,
        project_root=state.workspace.project_root,
        health=state.health
        or (
            browser_session.context_metadata_jsonb.get("health", {})
            if browser_session is not None and isinstance(browser_session.context_metadata_jsonb, dict)
            else {}
        ),
    )


@router.get("/runtime", response_model=WorkspaceRuntimeResponse)
async def get_runtime(
    project_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> WorkspaceRuntimeResponse:
    workspace = await get_project_workspace(session, project_id, user)
    browser_session = await get_current_browser_session(session, workspace)
    return runtime_response(RuntimeState(workspace=workspace, browser_session=browser_session))


@router.post("/runtime", response_model=WorkspaceRuntimeResponse)
async def ensure_runtime(
    project_id: UUID,
    payload: WorkspaceRuntimeRequest | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> WorkspaceRuntimeResponse:
    workspace = await get_project_workspace(session, project_id, user)
    try:
        state = await ensure_workspace_runtime(
            session=session,
            workspace=workspace,
            settings=settings,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ComposeError as exc:
        raise HTTPException(status_code=500, detail=f"Workspace runtime failed: {exc}") from exc
    return runtime_response(state)


@router.delete("/runtime", response_model=WorkspaceRuntimeResponse)
async def stop_workspace_runtime_endpoint(
    project_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> WorkspaceRuntimeResponse:
    workspace = await get_project_workspace(session, project_id, user)
    try:
        state = await stop_runtime(session=session, workspace=workspace, settings=settings)
    except ComposeError as exc:
        raise HTTPException(status_code=500, detail=f"Workspace runtime failed: {exc}") from exc
    return runtime_response(state)


@router.post("/runtime/restart", response_model=WorkspaceRuntimeResponse)
async def restart_workspace_runtime_endpoint(
    project_id: UUID,
    payload: WorkspaceRuntimeRequest | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> WorkspaceRuntimeResponse:
    """Stop + re-up the workspace compose project.  Persistent state
    (user code, codex-home volume) is preserved.  Dev-dep containers
    (postgres / redis, if any) are independent of workspace compose and
    are NOT affected — they stay running throughout the restart."""
    workspace = await get_project_workspace(session, project_id, user)
    try:
        state = await restart_runtime(
            session=session,
            workspace=workspace,
            settings=settings,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ComposeError as exc:
        raise HTTPException(status_code=500, detail=f"Workspace runtime failed: {exc}") from exc
    return runtime_response(state)


@router.get("/ide", response_model=WorkspaceIdeSessionResponse)
async def get_ide_session(
    project_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> WorkspaceIdeSessionResponse:
    workspace = await get_project_workspace(session, project_id, user)
    return ide_session_response(workspace)


@router.post("/ide/session", response_model=WorkspaceIdeSessionResponse)
async def ensure_ide_session(
    project_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> WorkspaceIdeSessionResponse:
    workspace = await get_project_workspace(session, project_id, user)
    # project_root gate: if the agent hasn't yet declared the project root,
    # the IDE has no meaningful folder to open.  Return 409 so the frontend
    # knows to back off and wait for the `project_root_changed` SSE event
    # instead of retrying.
    if workspace.project_root is None:
        raise HTTPException(
            status_code=409,
            detail="IDE not yet available — agent has not declared a project root",
        )
    try:
        state = await ensure_workspace_runtime(
            session=session,
            workspace=workspace,
            settings=settings,
        )
        workspace = state.workspace
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ComposeError as exc:
        workspace.ide_status = "failed"
        await session.commit()
        raise HTTPException(status_code=500, detail=f"Workspace runtime failed: {exc}") from exc
    return ide_session_response(workspace)


@router.delete("/ide/session", response_model=WorkspaceIdeSessionResponse)
async def stop_ide_session(
    project_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> WorkspaceIdeSessionResponse:
    workspace = await get_project_workspace(session, project_id, user)
    try:
        state = await stop_runtime(session=session, workspace=workspace, settings=settings)
        workspace = state.workspace
    except ComposeError as exc:
        workspace.ide_status = "failed"
        await session.commit()
        raise HTTPException(status_code=500, detail=f"Workspace runtime failed: {exc}") from exc
    return ide_session_response(workspace)


@router.get("/files", response_model=list[WorkspaceFileEntry])
async def list_files(
    project_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, object]]:
    workspace = await get_project_workspace(session, project_id, user)
    return list_workspace_files(Path(workspace.repo_path))


@router.get("/files/content", response_model=WorkspaceFileContent)
async def get_file_content(
    project_id: UUID,
    path: str = Query(min_length=1),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    workspace = await get_project_workspace(session, project_id, user)
    try:
        return read_workspace_file(Path(workspace.repo_path), path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="File not found") from exc
    except WorkspacePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/files/content", response_model=WorkspaceFileContent)
async def put_file_content(
    project_id: UUID,
    payload: WorkspaceFileWrite,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    workspace = await get_project_workspace(session, project_id, user)
    try:
        return write_workspace_file(
            Path(workspace.repo_path),
            payload.path,
            payload.content,
            payload.base_revision,
        )
    except WorkspaceConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except WorkspacePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/snapshot", response_model=ProjectVersionResponse, status_code=status.HTTP_201_CREATED)
async def snapshot_workspace(
    project_id: UUID,
    payload: SnapshotCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ProjectVersion:
    workspace = await get_project_workspace(session, project_id, user)
    # The git repo lives where `set_project_root` declared it, not at the
    # mount root. Without that signal there's nothing to snapshot.
    if not workspace.project_root:
        raise HTTPException(
            status_code=409,
            detail=(
                "No project root yet — snapshots require the agent to "
                "have called `set_project_root` (git init happens then)."
            ),
        )
    subdir = workspace.project_root.removeprefix("/workspace").lstrip("/")
    git_dir = Path(workspace.repo_path) / subdir if subdir else Path(workspace.repo_path)
    try:
        commit_hash = await create_snapshot(git_dir, payload.title)
    except WorkspaceError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    workspace.current_commit = commit_hash
    version = ProjectVersion(
        project_id=project_id,
        git_commit_hash=commit_hash,
        title=payload.title,
        description=payload.description,
        created_by_type=payload.created_by_type,
    )
    session.add(version)
    await session.commit()
    await session.refresh(version)
    return version


@router.get("/versions", response_model=list[ProjectVersionResponse])
async def list_versions(
    project_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ProjectVersion]:
    await get_project_workspace(session, project_id, user)
    result = await session.execute(
        select(ProjectVersion)
        .where(ProjectVersion.project_id == project_id)
        .order_by(ProjectVersion.created_at.desc())
    )
    return list(result.scalars().all())

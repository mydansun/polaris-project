import logging
import re
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from polaris_api.config import Settings, get_settings
from polaris_api.db import get_session
from polaris_api.deps import get_current_user
from polaris_api.models import (
    BrowserSession,
    Deployment,
    DesignIntent,
    Project,
    Session as SessionRow,
    User,
    Workspace,
)
from polaris_api.schemas import (
    DeploymentSummary,
    ProjectCreate,
    ProjectDetailResponse,
    ProjectResponse,
)
from polaris_api.services.compose import (
    stop_workspace_runtime,
    workspace_meta_path,
)
from polaris_api.services.dev_deps import cleanup_workspace_dev_deps
from polaris_api.services.ide import render_ide_session
from polaris_api.services.workspaces import WorkspaceError, initialize_workspace

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["projects"])


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "project"


async def allocate_slug(session: AsyncSession, user_id: UUID, name: str) -> str:
    base_slug = slugify(name)
    slug = base_slug
    suffix = 2
    while True:
        result = await session.execute(
            select(Project.id).where(Project.user_id == user_id, Project.slug == slug)
        )
        if result.scalar_one_or_none() is None:
            return slug
        slug = f"{base_slug}-{suffix}"
        suffix += 1


@router.post("", response_model=ProjectDetailResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ProjectDetailResponse:
    slug = await allocate_slug(session, user.id, payload.name)
    repo_path = str(Path(settings.workspace_root) / str(user.id) / slug)

    project = Project(
        user_id=user.id,
        name=payload.name,
        slug=slug,
        description=payload.description,
        stack_template=payload.stack_template,
        status="active",
    )
    session.add(project)
    await session.flush()

    workspace = Workspace(
        project_id=project.id,
        repo_path=repo_path,
        current_branch="main",
        status="provisioning",
        compose_profile="app-postgres-redis",
    )
    session.add(workspace)
    await session.commit()
    await session.refresh(project)
    await session.refresh(workspace)

    try:
        git_commit = await initialize_workspace(Path(workspace.repo_path))
    except WorkspaceError as exc:
        workspace.status = "failed"
        project.status = "failed"
        await session.commit()
        raise HTTPException(status_code=500, detail=f"Workspace provisioning failed: {exc}") from exc

    workspace.status = "ready"
    workspace.current_branch = git_commit.branch
    workspace.current_commit = git_commit.commit_hash
    ide_session = render_ide_session(
        "",
        project_id=project.id,
        workspace_id=workspace.id,
        workspace_path=workspace.repo_path,
    )
    workspace.ide_url = ide_session.ide_url
    workspace.ide_status = ide_session.ide_status
    await session.commit()
    await session.refresh(project)
    await session.refresh(workspace)
    return ProjectDetailResponse(**ProjectResponse.model_validate(project).model_dump(), workspace=workspace)


@router.get("", response_model=list[ProjectResponse])
async def list_projects(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ProjectResponse]:
    """Return the user's projects with each project's latest deployment
    (any status) and mood-board URL inlined.  Three queries total — no
    N+1.  Order: most recently updated first."""
    rows = (
        await session.execute(
            select(Project)
            .where(Project.user_id == user.id)
            .order_by(Project.updated_at.desc())
        )
    ).scalars().all()
    projects: list[Project] = list(rows)
    if not projects:
        return []

    project_ids = [p.id for p in projects]

    # Latest deployment per project (DISTINCT ON is postgres-specific
    # and exactly what we want — one row per project_id, picked by
    # newest created_at).
    deps_result = await session.execute(
        text(
            """
            SELECT DISTINCT ON (project_id)
                   id, project_id, status, domain, created_at, ready_at,
                   screenshot_url
              FROM deployments
             WHERE project_id = ANY(:pids)
             ORDER BY project_id, created_at DESC
            """
        ),
        {"pids": project_ids},
    )
    latest_by_pid: dict[UUID, DeploymentSummary] = {
        r.project_id: DeploymentSummary(
            id=r.id,
            status=r.status,
            domain=r.domain,
            created_at=r.created_at,
            ready_at=r.ready_at,
            screenshot_url=r.screenshot_url,
        )
        for r in deps_result
    }

    # Active design_intents — there can be at most one per project
    # (UNIQUE INDEX where status='active').
    intents_result = await session.execute(
        select(DesignIntent.project_id, DesignIntent.mood_board_url)
        .where(
            DesignIntent.project_id.in_(project_ids),
            DesignIntent.status == "active",
        )
    )
    mood_by_pid: dict[UUID, str | None] = {
        pid: url for (pid, url) in intents_result.all()
    }

    # Latest session per project — feeds the drawer status dot when
    # there's no deployment row yet (the agent crashed before publish).
    sessions_result = await session.execute(
        text(
            """
            SELECT DISTINCT ON (project_id) project_id, status
              FROM sessions
             WHERE project_id = ANY(:pids)
             ORDER BY project_id, created_at DESC
            """
        ),
        {"pids": project_ids},
    )
    session_status_by_pid: dict[UUID, str] = {
        r.project_id: r.status for r in sessions_result
    }

    # has_active_design_intent — same query the worker runs as
    # ``_load_active_design_intent``, batched.  The frontend reads
    # this to decide whether the next message should re-route through
    # discovery (no active intent → yes, prepend prior messages).
    has_intent_result = await session.execute(
        text(
            """
            SELECT DISTINCT project_id FROM design_intents
             WHERE project_id = ANY(:pids) AND status = 'active'
            """
        ),
        {"pids": project_ids},
    )
    has_intent_pids: set[UUID] = {r.project_id for r in has_intent_result}

    return [
        ProjectResponse(
            **ProjectResponse.model_validate(p).model_dump(
                exclude={
                    "latest_deployment",
                    "latest_session_status",
                    "has_active_design_intent",
                    "mood_board_url",
                }
            ),
            latest_deployment=latest_by_pid.get(p.id),
            latest_session_status=session_status_by_pid.get(p.id),
            has_active_design_intent=p.id in has_intent_pids,
            mood_board_url=mood_by_pid.get(p.id),
        )
        for p in projects
    ]


@router.get("/{project_id}", response_model=ProjectDetailResponse)
async def get_project(
    project_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ProjectDetailResponse:
    project = await session.get(Project, project_id)
    if project is None or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found")

    workspace_result = await session.execute(
        select(Workspace).where(Workspace.project_id == project_id).order_by(Workspace.created_at.desc())
    )
    workspace = workspace_result.scalars().first()

    latest_session_status_row = (
        await session.execute(
            select(SessionRow.status)
            .where(SessionRow.project_id == project_id)
            .order_by(SessionRow.created_at.desc())
            .limit(1)
        )
    ).first()
    latest_session_status = (
        latest_session_status_row[0] if latest_session_status_row else None
    )

    has_active_intent_row = (
        await session.execute(
            select(DesignIntent.id)
            .where(
                DesignIntent.project_id == project_id,
                DesignIntent.status == "active",
            )
            .limit(1)
        )
    ).first()
    has_active_design_intent = has_active_intent_row is not None

    base = ProjectResponse.model_validate(project).model_dump(
        exclude={"latest_session_status", "has_active_design_intent"}
    )
    return ProjectDetailResponse(
        **base,
        latest_session_status=latest_session_status,
        has_active_design_intent=has_active_design_intent,
        workspace=workspace,
    )


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Hard-delete a project: stop its workspace runtime, drop its
    dev-dep sidecars, and clear all DB rows.

    Cascade order mirrors ``scripts/seed/load.py::_delete_project_cascade``
    — three FKs from ``browser_sessions`` and ``sessions`` into
    ``workspaces`` aren't ON DELETE CASCADE, so we delete them manually
    in dependency order before letting the project's own ON DELETE
    CASCADE clean up deployments / design_intents / clarifications.

    Side effects (best-effort, errors logged not raised so the row drop
    isn't blocked by stale docker state):
      * ``docker compose down`` for each workspace's runtime
      * ``cleanup_workspace_dev_deps`` for each workspace
    """
    project = await session.get(Project, project_id)
    if project is None or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found")

    workspace_rows = (
        await session.execute(
            select(Workspace).where(Workspace.project_id == project_id)
        )
    ).scalars().all()

    for ws in workspace_rows:
        meta_path = workspace_meta_path(Path(settings.workspace_meta_root), ws.id)
        try:
            await stop_workspace_runtime(meta_path=meta_path, workspace_id=ws.id)
        except Exception:  # noqa: BLE001
            logger.warning(
                "delete_project: stop_workspace_runtime failed for %s",
                ws.id,
                exc_info=True,
            )
        try:
            await cleanup_workspace_dev_deps(session=session, workspace_id=ws.id)
        except Exception:  # noqa: BLE001
            logger.warning(
                "delete_project: cleanup_workspace_dev_deps failed for %s",
                ws.id,
                exc_info=True,
            )

    # Cascade order: browser_sessions → sessions → workspaces → project.
    await session.execute(
        delete(BrowserSession).where(BrowserSession.project_id == project_id)
    )
    await session.execute(
        delete(SessionRow).where(SessionRow.project_id == project_id)
    )
    await session.execute(
        delete(Workspace).where(Workspace.project_id == project_id)
    )
    await session.delete(project)
    await session.commit()
    logger.info(
        "delete_project: removed project=%s slug=%s (workspaces=%d)",
        project_id,
        project.slug,
        len(workspace_rows),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)

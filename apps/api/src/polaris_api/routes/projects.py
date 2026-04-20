import re
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polaris_api.config import Settings, get_settings
from polaris_api.db import get_session
from polaris_api.deps import get_current_user
from polaris_api.models import Project, User, Workspace
from polaris_api.schemas import ProjectCreate, ProjectDetailResponse, ProjectResponse
from polaris_api.services.ide import render_ide_session
from polaris_api.services.workspaces import WorkspaceError, initialize_workspace

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
) -> list[Project]:
    result = await session.execute(
        select(Project).where(Project.user_id == user.id).order_by(Project.created_at.desc())
    )
    return list(result.scalars().all())


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
    return ProjectDetailResponse(**ProjectResponse.model_validate(project).model_dump(), workspace=workspace)

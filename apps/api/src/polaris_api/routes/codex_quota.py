"""Read codex rate-limit usage for a project's workspace.

The chat pane polls this endpoint roughly once a minute to render a
small percentage bar next to the user avatar.  Service-level caching
(30 s) absorbs the polling rate; the upstream WS round-trip only fires
on cache misses.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as DbSession

from polaris_api.db import get_session
from polaris_api.deps import get_current_user
from polaris_api.models import Project, User, Workspace
from polaris_api.redis_client import get_redis
from polaris_api.services.codex_quota import get_codex_quota

router = APIRouter(tags=["codex-quota"])


@router.get("/projects/{project_id}/codex-quota")
async def read_project_codex_quota(
    project_id: UUID,
    user: User = Depends(get_current_user),
    db: DbSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> dict:
    project = await db.get(Project, project_id)
    if project is None or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found")

    workspace_id = await db.scalar(
        select(Workspace.id)
        .where(Workspace.project_id == project_id)
        .order_by(Workspace.created_at.desc())
        .limit(1)
    )
    if workspace_id is None:
        return {"available": False}

    snapshot = await get_codex_quota(workspace_id=workspace_id, redis=redis)
    if snapshot is None:
        return {"available": False}
    return {"available": True, **snapshot}

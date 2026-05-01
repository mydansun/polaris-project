"""Unsplash search proxy for the workspace-side MCP.

Auth mirrors :mod:`routes.dev_deps`: either a session cookie or
``X-Polaris-Workspace-Token`` — the in-container MCP uses the header.

No project scoping: Unsplash results are public images and the
``unsplash_images`` dedupe table is global by design.  The workspace
token just proves the caller is a legitimate Polaris workspace.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polaris_api.config import Settings, get_settings
from polaris_api.db import get_session
from polaris_api.models import User, Workspace
from polaris_api.schemas import StoredPhotoResponse, UnsplashSearchBody
from polaris_api.services.auth import verify_session_token
from polaris_api.services.unsplash import search_and_cache


router = APIRouter(tags=["unsplash"])


async def _require_workspace_or_user(
    request: Request,
    session: AsyncSession,
    settings: Settings,
    workspace_token: str | None,
) -> None:
    """Pass through if EITHER a valid session cookie OR a matching
    workspace token is presented.  Raises 401 otherwise."""
    cookie = request.cookies.get("polaris_session")
    if cookie:
        user_id = verify_session_token(cookie, settings)
        if user_id is not None:
            user = await session.get(User, user_id)
            if user is not None:
                return

    if workspace_token:
        row = (
            await session.execute(
                select(Workspace).where(Workspace.workspace_token == workspace_token)
            )
        ).scalars().first()
        if row is not None:
            return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unsplash search requires a session cookie or workspace token.",
    )


@router.post(
    "/workspace/unsplash/search",
    response_model=list[StoredPhotoResponse],
)
async def search_photos(
    body: UnsplashSearchBody,
    request: Request,
    x_polaris_workspace_token: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[StoredPhotoResponse]:
    await _require_workspace_or_user(
        request, session, settings, x_polaris_workspace_token
    )
    if not settings.unsplash_access_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="UNSPLASH_ACCESS_KEY not configured on the server.",
        )
    try:
        photos = await search_and_cache(
            query=body.query,
            per_page=body.per_page,
            orientation=body.orientation,
            color=body.color,
            content_filter=body.content_filter,
            session=session,
            settings=settings,
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Invalid Unsplash API key on the platform side.",
            ) from exc
        if exc.response.status_code == 403:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Unsplash rate limit exceeded.",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Unsplash API error: {exc.response.status_code}",
        ) from exc
    return [StoredPhotoResponse.model_validate(p) for p in photos]

from uuid import UUID

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from polaris_api.config import Settings, get_settings
from polaris_api.db import get_session
from polaris_api.models import User
from polaris_api.services.auth import verify_session_token

COOKIE_NAME = "polaris_session"


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> User:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user_id = verify_session_token(token, settings)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    user = await session.get(User, UUID(user_id))
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")

    return user

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polaris_api.config import Settings, get_settings
from polaris_api.db import get_session
from polaris_api.models import User
from polaris_api.schemas import RequestCodeBody, UserResponse, VerifyCodeBody
from polaris_api.services.auth import (
    check_rate_limit,
    create_session_token,
    create_verification_code,
    email_is_registered,
    get_or_create_user_by_email,
    validate_invite_code,
    verify_code,
    verify_session_token,
)
from polaris_api.services.email import send_verification_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

COOKIE_NAME = "polaris_session"


# ── Email verification code flow ──────────────────────────────────────────


@router.post("/request-code")
async def request_code(
    body: RequestCodeBody,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    email = body.email.strip().lower()

    # Unregistered emails require a valid invite code to proceed.
    registered = await email_is_registered(session, email)
    if not registered:
        if not body.invite_code:
            return JSONResponse(content={"ok": False, "reason": "invite_required"})
        if not validate_invite_code(body.invite_code, settings):
            raise HTTPException(status_code=403, detail="Invalid or expired invite code")

    if await check_rate_limit(session, email):
        raise HTTPException(status_code=429, detail="Too many requests. Try again later.")

    code = await create_verification_code(session, email)
    await session.commit()

    try:
        await send_verification_email(email, code, settings)
    except Exception:
        logger.exception("Failed to send verification email to %s", email)

    return JSONResponse(content={"ok": True})


@router.post("/verify-code")
async def verify_code_endpoint(
    body: VerifyCodeBody,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    email = body.email.strip().lower()
    code = body.code.strip()

    if not await verify_code(session, email, code):
        raise HTTPException(status_code=401, detail="Invalid or expired code")

    user = await get_or_create_user_by_email(session, email)
    await session.commit()

    token = create_session_token(user.id, settings)
    response = JSONResponse(content={"ok": True})
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.frontend_url.startswith("https://"),
        max_age=settings.session_ttl_days * 86400,
        path="/",
    )
    return response


# ── Session endpoints ─────────────────────────────────────────────────────


@router.get("/me", response_model=UserResponse)
async def get_me(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> UserResponse:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user_id = verify_session_token(token, settings)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")

    return UserResponse(
        id=user.id,
        email=user.email,
        name=user.name,
        avatar_url=user.avatar_url,
        created_at=user.created_at,
    )


@router.get("/config")
async def auth_config(
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    """Unauthenticated capability probe used by the frontend login screen.

    Returns whether the one-click dev-login shortcut is available on this
    instance.  Frontend hides the button when ``dev_login_enabled=false``
    so operators don't have to explain "this button does nothing" in
    staging / prod.
    """
    return JSONResponse(
        content={"dev_login_enabled": bool(settings.dev_user_email.strip())}
    )


@router.get("/dev-login")
async def dev_login(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    """Auto-login as the dev user.  Gated on `POLARIS_DEV_USER_EMAIL`
    being set — when empty the route 404s so the endpoint cannot be
    abused on a shared / staging / prod host."""
    dev_email = settings.dev_user_email.strip()
    if not dev_email:
        raise HTTPException(
            status_code=404, detail="dev-login not enabled on this instance"
        )
    result = await session.execute(
        select(User).where(User.email == dev_email)
    )
    user = result.scalar_one_or_none()
    if user is None:
        user = User(
            email=dev_email,
            name=settings.dev_user_name.strip() or dev_email,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

    token = create_session_token(user.id, settings)
    response = RedirectResponse(url=settings.frontend_url, status_code=302)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.frontend_url.startswith("https://"),
        max_age=settings.session_ttl_days * 86400,
        path="/",
    )
    return response


@router.post("/logout")
async def logout(request: Request) -> JSONResponse:
    response = JSONResponse(content={"ok": True})
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return response

import secrets
import string
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polaris_api.config import Settings
from polaris_api.models import User, VerificationCode

JWT_ALGORITHM = "HS256"

CODE_CHARS = string.digits
CODE_LENGTH = 6
CODE_TTL_MINUTES = 5
MAX_CODES_PER_HOUR = 5


# ── Session tokens ────────────────────────────────────────────────────────


def create_session_token(user_id: UUID, settings: Settings) -> str:
    payload = {
        "sub": str(user_id),
        "exp": datetime.now(UTC) + timedelta(days=settings.session_ttl_days),
        "iat": datetime.now(UTC),
    }
    return jwt.encode(payload, settings.session_secret, algorithm=JWT_ALGORITHM)


def verify_session_token(token: str, settings: Settings) -> str | None:
    try:
        payload = jwt.decode(token, settings.session_secret, algorithms=[JWT_ALGORITHM])
        return payload.get("sub")
    except jwt.PyJWTError:
        return None


# ── Verification codes ────────────────────────────────────────────────────


def generate_code() -> str:
    return "".join(secrets.choice(CODE_CHARS) for _ in range(CODE_LENGTH))


async def check_rate_limit(session: AsyncSession, email: str) -> bool:
    """Return True if the email has exceeded the hourly code limit."""
    one_hour_ago = datetime.now(UTC) - timedelta(hours=1)
    result = await session.execute(
        select(sa_func.count(VerificationCode.id)).where(
            VerificationCode.email == email,
            VerificationCode.created_at > one_hour_ago,
        )
    )
    return result.scalar_one() >= MAX_CODES_PER_HOUR


async def create_verification_code(session: AsyncSession, email: str) -> str:
    code = generate_code()
    vc = VerificationCode(
        email=email,
        code=code,
        expires_at=datetime.now(UTC) + timedelta(minutes=CODE_TTL_MINUTES),
    )
    session.add(vc)
    await session.flush()
    return code


async def verify_code(session: AsyncSession, email: str, code: str) -> bool:
    result = await session.execute(
        select(VerificationCode)
        .where(
            VerificationCode.email == email,
            VerificationCode.code == code,
            VerificationCode.used_at.is_(None),
            VerificationCode.expires_at > datetime.now(UTC),
        )
        .order_by(VerificationCode.created_at.desc())
        .limit(1)
    )
    vc = result.scalar_one_or_none()
    if vc is None:
        return False
    vc.used_at = datetime.now(UTC)
    await session.flush()
    return True


# ── User lookup / auto-register ───────────────────────────────────────────


async def email_is_registered(session: AsyncSession, email: str) -> bool:
    result = await session.execute(select(User.id).where(User.email == email).limit(1))
    return result.scalar_one_or_none() is not None


async def get_or_create_user_by_email(session: AsyncSession, email: str) -> User:
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is not None:
        user.last_login_at = datetime.now(UTC)
    else:
        user = User(
            email=email,
            name=email.split("@")[0],
            last_login_at=datetime.now(UTC),
        )
        session.add(user)
    await session.flush()
    return user


def validate_invite_code(code: str, settings: Settings) -> bool:
    """Check the invite code against the configured env var."""
    expected = settings.invite_code.strip()
    if not expected:
        return False
    return code.strip() == expected

"""Tests for the Redis-backed run quota.

Uses a real Redis at ``POLARIS_REDIS_URL`` (the dev-plane Redis is already
running for most Polaris work) because the quota logic leans on atomic
Lua EVAL semantics that we want to exercise end-to-end.  Each test uses a
unique key prefix so concurrent test runs don't collide with each other
or with live dev traffic.
"""
from __future__ import annotations

import os
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis

from polaris_api.config import Settings
from polaris_api.services import run_quota as rq


REDIS_URL = os.environ.get("POLARIS_REDIS_URL", "redis://127.0.0.1:6379/0")


@pytest_asyncio.fixture
async def redis_client():
    client: Redis = Redis.from_url(REDIS_URL, decode_responses=False)
    try:
        await client.ping()
    except Exception:
        pytest.skip(f"Redis not reachable at {REDIS_URL}")
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def isolated_quota(monkeypatch, redis_client):
    """Rebind GLOBAL_KEY and user-key helper to a per-test namespace so
    the real Redis never sees collisions between tests."""
    prefix = f"polaris-test-{uuid4().hex}"
    global_key = f"{prefix}:global"
    monkeypatch.setattr(rq, "GLOBAL_KEY", global_key)
    monkeypatch.setattr(
        rq, "_user_key", lambda user_id: f"{prefix}:user:{user_id}"
    )
    yield prefix
    # Best-effort cleanup — delete anything under our prefix.
    async for key in redis_client.scan_iter(match=f"{prefix}:*"):
        await redis_client.delete(key)


def _settings(global_limit: int, user_limit: int, ttl: int = 1800) -> Settings:
    # Settings reads its fields from env; constructing directly bypasses
    # that and just sets attributes.  BaseSettings still requires a valid
    # call so we build one and overwrite the knobs we care about.
    s = Settings()
    object.__setattr__(s, "max_global_runs", global_limit)
    object.__setattr__(s, "max_user_runs", user_limit)
    object.__setattr__(s, "run_quota_ttl_seconds", ttl)
    return s


@pytest.mark.asyncio
async def test_global_limit_rejects_after_capacity(redis_client, isolated_quota):
    settings = _settings(global_limit=3, user_limit=10)
    # 3 different users, 1 session each — fill global.
    for _ in range(3):
        result = await rq.acquire_run_slot(
            redis=redis_client,
            user_id=uuid4(),
            session_id=uuid4(),
            settings=settings,
        )
        assert result is None

    # The 4th lands on a fresh user (clean user bucket) → only global can
    # be the limiter.
    result = await rq.acquire_run_slot(
        redis=redis_client,
        user_id=uuid4(),
        session_id=uuid4(),
        settings=settings,
    )
    assert result == rq.QuotaRejection.GLOBAL


@pytest.mark.asyncio
async def test_user_limit_rejects_even_when_global_available(
    redis_client, isolated_quota
):
    settings = _settings(global_limit=10, user_limit=2)
    user = uuid4()
    for _ in range(2):
        result = await rq.acquire_run_slot(
            redis=redis_client,
            user_id=user,
            session_id=uuid4(),
            settings=settings,
        )
        assert result is None

    blocked = await rq.acquire_run_slot(
        redis=redis_client,
        user_id=user,
        session_id=uuid4(),
        settings=settings,
    )
    assert blocked == rq.QuotaRejection.USER


@pytest.mark.asyncio
async def test_user_rejection_rolls_back_global(redis_client, isolated_quota):
    """When the global slot is grabbed but the user bucket is full, the
    global slot must be released before returning — otherwise we'd leak
    one global slot per rejection until TTL."""
    settings = _settings(global_limit=10, user_limit=1)
    user = uuid4()
    # Fill the user bucket first.
    ok = await rq.acquire_run_slot(
        redis=redis_client, user_id=user, session_id=uuid4(), settings=settings
    )
    assert ok is None
    global_card_before = await redis_client.zcard(rq.GLOBAL_KEY)

    blocked = await rq.acquire_run_slot(
        redis=redis_client, user_id=user, session_id=uuid4(), settings=settings
    )
    assert blocked == rq.QuotaRejection.USER

    # Global ZCARD must be unchanged despite the brief claim+rollback.
    global_card_after = await redis_client.zcard(rq.GLOBAL_KEY)
    assert global_card_after == global_card_before


@pytest.mark.asyncio
async def test_release_frees_both_sets_idempotently(
    redis_client, isolated_quota
):
    settings = _settings(global_limit=5, user_limit=5)
    user = uuid4()
    session = uuid4()
    await rq.acquire_run_slot(
        redis=redis_client, user_id=user, session_id=session, settings=settings
    )
    assert await redis_client.zcard(rq.GLOBAL_KEY) == 1
    user_key = rq._user_key(user)
    assert await redis_client.zcard(user_key) == 1

    await rq.release_run_slot(
        redis=redis_client, user_id=user, session_id=session
    )
    assert await redis_client.zcard(rq.GLOBAL_KEY) == 0
    assert await redis_client.zcard(user_key) == 0

    # Second release is a no-op — safe to call whether or not TTL already
    # beat us to the cleanup.
    await rq.release_run_slot(
        redis=redis_client, user_id=user, session_id=session
    )
    assert await redis_client.zcard(rq.GLOBAL_KEY) == 0


@pytest.mark.asyncio
async def test_expired_entries_are_purged_on_next_acquire(
    redis_client, isolated_quota
):
    """Acquires at the cap should succeed once the prior entries' scores
    are in the past — the Lua script's ZREMRANGEBYSCORE evicts them
    before the ZCARD check."""
    settings = _settings(global_limit=2, user_limit=10)

    # Fill two slots with scores already in the past (simulating worker
    # crashes).  We bypass the helper and write directly so the TTL is
    # immediately expired.
    past = 1
    await redis_client.zadd(rq.GLOBAL_KEY, {str(uuid4()): past})
    await redis_client.zadd(rq.GLOBAL_KEY, {str(uuid4()): past})
    assert await redis_client.zcard(rq.GLOBAL_KEY) == 2

    # Global is at capacity, but all entries are expired → next acquire
    # purges them and succeeds.
    result = await rq.acquire_run_slot(
        redis=redis_client,
        user_id=uuid4(),
        session_id=uuid4(),
        settings=settings,
    )
    assert result is None
    assert await redis_client.zcard(rq.GLOBAL_KEY) == 1

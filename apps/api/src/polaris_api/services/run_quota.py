"""Redis-backed run concurrency quota.

Two sorted sets — ``polaris:runs:global`` (platform-wide) and
``polaris:runs:user:<user_id>`` (per-user) — track in-flight Sessions.
Members are session_ids; scores are ``now + TTL`` expiry timestamps so a
crashed worker's entry auto-expires rather than holding a slot forever.

The acquire path runs a single Lua script per key: ZREMRANGEBYSCORE
(purge expired) + ZCARD (check under limit) + ZADD (claim a slot) is
atomic, so two concurrent requests can't both squeeze past the cap.

API side calls ``acquire_run_slot`` synchronously in POST /sessions; the
worker calls ``release_run_slot`` in its orchestrator finally block.  A
rollback path (global succeeds, user fails) releases the global slot
before returning so we never leave orphans behind.
"""
from __future__ import annotations

import time
from enum import Enum
from uuid import UUID

from redis.asyncio import Redis

from polaris_api.config import Settings

GLOBAL_KEY = "polaris:runs:global"


def _user_key(user_id: UUID) -> str:
    return f"polaris:runs:user:{user_id}"


# Atomic: purge expired, check capacity, add on success.  Returns 1 on
# success, 0 when the set is already at limit.
_ACQUIRE_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if tonumber(redis.call('ZCARD', KEYS[1])) >= tonumber(ARGV[3]) then
    return 0
end
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[4])
return 1
"""


class QuotaRejection(str, Enum):
    GLOBAL = "global_quota"
    USER = "user_quota"


async def acquire_run_slot(
    *,
    redis: Redis,
    user_id: UUID,
    session_id: UUID,
    settings: Settings,
) -> QuotaRejection | None:
    """Try to claim one global + one per-user slot for ``session_id``.

    Returns ``None`` on success (both slots held), or a ``QuotaRejection``
    identifying the bucket that rejected.  On user-bucket rejection the
    global slot is rolled back before returning, so a failed call leaves
    nothing behind.
    """
    now = int(time.time())
    exp = now + settings.run_quota_ttl_seconds
    sid = str(session_id)

    global_ok = await redis.eval(  # type: ignore[misc]
        _ACQUIRE_LUA,
        1,
        GLOBAL_KEY,
        now,
        exp,
        settings.max_global_runs,
        sid,
    )
    if not int(global_ok):
        return QuotaRejection.GLOBAL

    user_ok = await redis.eval(  # type: ignore[misc]
        _ACQUIRE_LUA,
        1,
        _user_key(user_id),
        now,
        exp,
        settings.max_user_runs,
        sid,
    )
    if not int(user_ok):
        await redis.zrem(GLOBAL_KEY, sid)
        return QuotaRejection.USER

    return None


async def release_run_slot(
    *,
    redis: Redis,
    user_id: UUID,
    session_id: UUID,
) -> None:
    """Drop ``session_id`` from both sorted sets.  Idempotent — safe to
    call when the entry may already be gone (e.g. TTL purge beat us)."""
    sid = str(session_id)
    await redis.zrem(GLOBAL_KEY, sid)
    await redis.zrem(_user_key(user_id), sid)

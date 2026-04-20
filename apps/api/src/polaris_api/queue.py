from uuid import UUID

from redis.asyncio import Redis

SESSION_JOBS_STREAM = "polaris:jobs:sessions"


def session_events_channel(session_id: UUID | str) -> str:
    return f"polaris:sessions:{session_id}:events"


def session_control_channel(session_id: UUID | str) -> str:
    return f"polaris:sessions:{session_id}:control"


def clarification_channel(session_id: UUID | str) -> str:
    return f"polaris:sessions:{session_id}:clarification"


async def enqueue_session(
    redis: Redis,
    session_id: UUID,
    project_id: UUID,
    workspace_id: UUID,
    mode: str,
) -> str:
    fields: dict[str, str] = {
        "session_id": str(session_id),
        "project_id": str(project_id),
        "workspace_id": str(workspace_id),
        "mode": mode,
    }
    return await redis.xadd(SESSION_JOBS_STREAM, fields)

"""Redis stream / pubsub names used by the worker side.

Must stay in sync with ``polaris_api.queue``.  Both sides import a thin
helper instead of hard-coding the string, so renaming the namespace
(``turns`` → ``sessions``) stayed localized.
"""

from __future__ import annotations

from uuid import UUID

SESSION_JOBS_STREAM = "polaris:jobs:sessions"


def session_events_channel(session_id: UUID | str) -> str:
    return f"polaris:sessions:{session_id}:events"


def session_control_channel(session_id: UUID | str) -> str:
    return f"polaris:sessions:{session_id}:control"


def clarification_channel(session_id: UUID | str) -> str:
    return f"polaris:sessions:{session_id}:clarification"

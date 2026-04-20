"""Worker main loop — consumes Redis session jobs and dispatches to the
orchestrator.

Everything agent-specific (Codex WS session cache, item → event kind map,
set_project_root tool handler, ...) lives in ``polaris_worker.agents.*``.
The orchestrator (``polaris_worker.orchestrator.process_session_job``)
decides which agents run per-session based on ``Session.mode``.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

import asyncpg
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from polaris_api.services.compose import (
    stop_workspace_runtime,
    workspace_meta_path,
)

from polaris_worker.monorepo import ensure_monorepo_python_paths

ensure_monorepo_python_paths()

from polaris_worker.agents.codex import _drop_session, _sessions  # noqa: E402
from polaris_worker.config import Settings, asyncpg_url  # noqa: E402
from polaris_worker.orchestrator import process_session_job  # noqa: E402
from polaris_worker.queue import SESSION_JOBS_STREAM  # noqa: E402


logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
#  Stream consumer group + main loop
# ──────────────────────────────────────────────────────────────────────────


async def ensure_group(redis: Redis, group_name: str) -> None:
    try:
        await redis.xgroup_create(
            SESSION_JOBS_STREAM, group_name, id="0", mkstream=True
        )
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def _idle_workspace_scavenger(settings: Settings) -> None:
    """Periodically stop compose runtimes for workspaces that haven't seen a
    Session in the last ``idle_workspace_timeout_seconds`` seconds.

    Runs on its own asyncpg connection so it never contends with session
    processing.  Persistent state (user code bind-mount, codex-home named
    volume, dependency-service volumes) is preserved — only the containers
    go down.  The next ``ensure_workspace_runtime`` call brings them back
    up with identical state.
    """
    interval = max(30, int(settings.idle_workspace_timeout_seconds // 4))
    timeout = int(settings.idle_workspace_timeout_seconds)
    meta_root = settings.workspace_meta_root
    conn = await asyncpg.connect(asyncpg_url(settings.database_url))
    try:
        while True:
            try:
                cutoff = datetime.now(UTC) - timedelta(seconds=timeout)
                rows = await conn.fetch(
                    """
                    SELECT w.id AS workspace_id,
                           GREATEST(
                               w.updated_at,
                               COALESCE(
                                   (SELECT MAX(created_at) FROM sessions
                                    WHERE workspace_id = w.id),
                                   w.updated_at
                               )
                           ) AS last_activity
                    FROM workspaces w
                    WHERE w.ide_status = 'ready'
                      AND NOT EXISTS (
                          SELECT 1 FROM sessions s
                          WHERE s.workspace_id = w.id
                            AND s.status IN ('queued', 'running')
                      )
                    """,
                )
                for row in rows:
                    last = row["last_activity"]
                    if last is not None and last >= cutoff:
                        continue
                    workspace_id: UUID = row["workspace_id"]
                    meta_path = workspace_meta_path(meta_root, workspace_id)
                    try:
                        await stop_workspace_runtime(
                            meta_path=meta_path,
                            workspace_id=workspace_id,
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "idle scavenger: stop_workspace_runtime failed for %s",
                            workspace_id,
                        )
                        continue
                    await conn.execute(
                        "UPDATE workspaces SET ide_status='stopped', ide_url=NULL "
                        "WHERE id=$1",
                        workspace_id,
                    )
                    await _drop_session(workspace_id)
                    logger.info(
                        "idle-stopped workspace %s (last activity %s)",
                        workspace_id,
                        last.isoformat() if last else "never",
                    )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("idle scavenger iteration failed")
            await asyncio.sleep(interval)
    finally:
        await conn.close()


async def run_worker(settings: Settings, once: bool = False) -> None:
    """Top-level worker entry.  Owns one asyncpg connection used by every
    orchestrator invocation (per-session ``conn_lock`` serializes writes
    from the sink + dynamic tool handler)."""
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    conn = await asyncpg.connect(asyncpg_url(settings.database_url))
    scavenger_task: asyncio.Task[None] | None = None
    try:
        await ensure_group(redis, settings.consumer_group)
        logger.info(
            "worker started, consumer=%s group=%s",
            settings.consumer_name,
            settings.consumer_group,
        )
        if not once:
            scavenger_task = asyncio.create_task(_idle_workspace_scavenger(settings))
        while True:
            response = await redis.xreadgroup(
                groupname=settings.consumer_group,
                consumername=settings.consumer_name,
                streams={SESSION_JOBS_STREAM: ">"},
                count=1,
                block=1000 if once else 5000,
            )
            if not response:
                if once:
                    return
                continue
            for _stream_name, messages in response:
                for message_id, fields in messages:
                    try:
                        await process_session_job(conn, redis, fields, settings)
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "process_session_job failed for %s", message_id
                        )
                    finally:
                        try:
                            await redis.xack(
                                SESSION_JOBS_STREAM,
                                settings.consumer_group,
                                message_id,
                            )
                        except Exception:  # noqa: BLE001
                            logger.exception("xack failed for %s", message_id)
            if once:
                return
    finally:
        if scavenger_task is not None:
            scavenger_task.cancel()
            with contextlib.suppress(BaseException):
                await scavenger_task
        for ws_id in list(_sessions.keys()):
            await _drop_session(ws_id)
        await conn.close()
        await redis.aclose()

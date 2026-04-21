"""Dev-time dependency slots.

Each workspace can opt into running extra containers (postgres, redis, …)
alongside its workspace compose.  We deliberately do NOT put these in
the workspace compose file — re-rendering on every add/remove would
change the workspace service definition (env var injection) and restart
the workspace container, killing the in-flight Codex turn that invoked
`polaris dev-up`.

Instead each dep is an independent `docker run` container attached to
the workspace's per-project docker network via `--network-alias <svc>`,
so `postgres:5432` resolves the way Codex expects without touching the
workspace container at all.

State lives in `workspace_dep_services` (one row per (workspace, service)
tuple).  Lifecycle: explicit via `dev-up` / `dev-down`; cascaded cleanup
on workspace delete / `make clear`.

Credentials are hardcoded (`app/app/app`) because dev is not a security
boundary and `.env` is gitignored.  Prod (publish) side generates a real
random `POSTGRES_PASSWORD` via `services/publish.py::materialize_secrets`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polaris_api.models import Workspace, WorkspaceDepService
from polaris_api.services.compose import compose_project_name


logger = logging.getLogger(__name__)


class DevDepError(Exception):
    """Any recoverable error in the dev-deps layer."""


# ─── Configuration ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DevDepConfig:
    image: str
    alias: str                      # --network-alias; also the workspace DNS name
    env: dict[str, str]             # -e env vars on the dep container
    volume_mount: str               # where the volume mounts inside the container
    healthcheck: list[str]          # docker --health-cmd (argv form)
    connection_env: dict[str, str]  # env vars the CLI writes into user .env


DEV_DEP_CONFIGS: dict[str, DevDepConfig] = {
    "postgres": DevDepConfig(
        image="postgres:16-alpine",
        alias="postgres",
        env={
            "POSTGRES_USER": "app",
            "POSTGRES_PASSWORD": "app",
            "POSTGRES_DB": "app",
        },
        volume_mount="/var/lib/postgresql/data",
        healthcheck=["CMD-SHELL", "pg_isready -U app -d app"],
        connection_env={
            "DATABASE_URL": "postgresql://app:app@postgres:5432/app",
        },
    ),
    "redis": DevDepConfig(
        image="redis:7-alpine",
        alias="redis",
        env={},
        volume_mount="/data",
        healthcheck=["CMD", "redis-cli", "ping"],
        connection_env={
            "REDIS_URL": "redis://redis:6379/0",
        },
    ),
}


SUPPORTED_DEP_SERVICES = frozenset(DEV_DEP_CONFIGS)


def _short_hash(workspace_id: UUID) -> str:
    return str(workspace_id).replace("-", "")[:24]


def _container_name(workspace_id: UUID, service: str) -> str:
    return f"polaris-ws-{_short_hash(workspace_id)}-{service}"


def _volume_name(workspace_id: UUID, service: str) -> str:
    return f"polaris-ws-{_short_hash(workspace_id)}-{service}-data"


def _network_name(workspace_id: UUID) -> str:
    # Matches the docker compose auto-created `<project>_default` network
    # from the workspace compose (`compose_project_name` = `polaris-<24hex>`).
    return f"{compose_project_name(workspace_id)}_default"


# ─── Subprocess helpers ────────────────────────────────────────────────────


async def _docker(*args: str, check: bool = True, timeout: float = 60) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "docker", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise DevDepError(f"docker {' '.join(args)} timed out after {timeout}s") from None
    out = stdout.decode(errors="replace").strip()
    err = stderr.decode(errors="replace").strip()
    if check and proc.returncode != 0:
        raise DevDepError(f"docker {' '.join(args)} failed: {err or out}")
    return proc.returncode or 0, out, err


async def _network_exists(network: str) -> bool:
    rc, _, _ = await _docker("network", "inspect", network, check=False, timeout=10)
    return rc == 0


async def _container_status(container: str) -> str | None:
    """Returns 'running' | 'exited' | 'created' | ... or None if missing."""
    rc, out, _ = await _docker(
        "inspect", "--format", "{{.State.Status}}", container,
        check=False, timeout=10,
    )
    if rc != 0:
        return None
    return out.strip() or None


async def _container_health(container: str) -> str | None:
    """Returns 'healthy' | 'unhealthy' | 'starting' | None."""
    rc, out, _ = await _docker(
        "inspect", "--format", "{{if .State.Health}}{{.State.Health.Status}}{{end}}",
        container,
        check=False, timeout=10,
    )
    if rc != 0:
        return None
    return (out.strip() or None)


async def _wait_healthy(container: str, timeout_seconds: float = 60.0) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_seconds
    last = "unknown"
    while loop.time() < deadline:
        health = await _container_health(container)
        if health == "healthy":
            return
        if health is not None:
            last = health
        await asyncio.sleep(1)
    raise DevDepError(
        f"{container} never became healthy (last status: {last}) after {timeout_seconds:.0f}s"
    )


# ─── Public API ────────────────────────────────────────────────────────────


async def ensure_dev_dep(
    session: AsyncSession,
    workspace: Workspace,
    service: str,
) -> WorkspaceDepService:
    """Start or reuse the <service> container for this workspace.

    Idempotent: if a running container already matches the DB row, no-op.
    If the row exists but the container is gone, recreates it.  If the
    workspace network doesn't exist yet, refuses with a clear error.
    """
    if service not in SUPPORTED_DEP_SERVICES:
        raise DevDepError(
            f"unsupported dev dep: {service!r}. Supported: "
            f"{sorted(SUPPORTED_DEP_SERVICES)}"
        )

    config = DEV_DEP_CONFIGS[service]
    container = _container_name(workspace.id, service)
    volume = _volume_name(workspace.id, service)
    network = _network_name(workspace.id)

    if not await _network_exists(network):
        raise DevDepError(
            f"workspace network {network!r} not found — ensure the workspace "
            "runtime is up before requesting dev deps."
        )

    # Look up existing row.
    existing = (
        await session.execute(
            select(WorkspaceDepService).where(
                WorkspaceDepService.workspace_id == workspace.id,
                WorkspaceDepService.service == service,
            )
        )
    ).scalars().first()

    # Fast path: row exists AND container still running.
    if existing is not None:
        status = await _container_status(existing.container_name)
        if status == "running":
            existing.status = "running"
            await session.commit()
            await session.refresh(existing)
            return existing
        # Container is gone or stopped — remove + recreate below.
        await _docker("rm", "-f", existing.container_name, check=False, timeout=30)

    # Ensure volume exists (idempotent).
    await _docker("volume", "create", volume, timeout=10)

    # Build `docker run` args. --health-cmd expects a single shell string
    # regardless of whether the config was authored in CMD or CMD-SHELL
    # form (that's a docker-compose abstraction — docker run doesn't
    # distinguish).  Join argv[1:] with spaces for both.
    health_cmd = " ".join(config.healthcheck[1:])
    run_args: list[str] = [
        "run", "-d",
        "--name", container,
        "--network", network,
        "--network-alias", config.alias,
        "--restart", "unless-stopped",
        "-v", f"{volume}:{config.volume_mount}",
        "--health-cmd", health_cmd,
        "--health-interval", "5s",
        "--health-timeout", "3s",
        "--health-retries", "12",
        "--health-start-period", "10s",
    ]
    for k, v in config.env.items():
        run_args.extend(["-e", f"{k}={v}"])
    run_args.append(config.image)

    await _docker(*run_args, timeout=60)
    await _wait_healthy(container, timeout_seconds=60.0)

    # Upsert row.
    if existing is None:
        row = WorkspaceDepService(
            id=uuid4(),
            workspace_id=workspace.id,
            service=service,
            container_name=container,
            volume_name=volume,
            image=config.image,
            network=network,
            status="running",
            env_jsonb=dict(config.connection_env),
        )
        session.add(row)
    else:
        existing.container_name = container
        existing.volume_name = volume
        existing.image = config.image
        existing.network = network
        existing.status = "running"
        existing.env_jsonb = dict(config.connection_env)
        row = existing

    await session.commit()
    await session.refresh(row)
    logger.info("dev dep %s running for workspace %s", service, workspace.id)
    return row


async def remove_dev_dep(
    session: AsyncSession,
    workspace: Workspace,
    service: str,
) -> None:
    """Stop + remove the container, drop the volume, delete the DB row."""
    row = (
        await session.execute(
            select(WorkspaceDepService).where(
                WorkspaceDepService.workspace_id == workspace.id,
                WorkspaceDepService.service == service,
            )
        )
    ).scalars().first()
    if row is None:
        return
    # Even if the container / volume are gone (manual cleanup), these rm
    # calls with check=False are safe.
    await _docker("rm", "-f", row.container_name, check=False, timeout=30)
    await _docker("volume", "rm", row.volume_name, check=False, timeout=10)
    await session.delete(row)
    await session.commit()
    logger.info("dev dep %s removed from workspace %s", service, workspace.id)


async def list_dev_deps(
    session: AsyncSession,
    workspace_id: UUID,
) -> list[WorkspaceDepService]:
    rows = (
        await session.execute(
            select(WorkspaceDepService)
            .where(WorkspaceDepService.workspace_id == workspace_id)
            .order_by(WorkspaceDepService.created_at)
        )
    ).scalars().all()
    # Light refresh of status — docker is the source of truth.
    for row in rows:
        status = await _container_status(row.container_name)
        if status is None:
            row.status = "stopped"
        elif status == "running":
            row.status = "running"
        else:
            row.status = status
    await session.commit()
    return list(rows)


async def cleanup_workspace_dev_deps(
    session: AsyncSession,
    workspace_id: UUID,
) -> None:
    """Called on workspace / project delete.  Stops + removes all dep
    containers + volumes, deletes DB rows."""
    rows = (
        await session.execute(
            select(WorkspaceDepService).where(
                WorkspaceDepService.workspace_id == workspace_id,
            )
        )
    ).scalars().all()
    for row in rows:
        await _docker("rm", "-f", row.container_name, check=False, timeout=30)
        await _docker("volume", "rm", row.volume_name, check=False, timeout=10)
        await session.delete(row)
    await session.commit()


def dep_connection_env(service: str) -> dict[str, str]:
    """Public helper for the CLI's `polaris dev-up` echo / .env writer."""
    if service not in DEV_DEP_CONFIGS:
        return {}
    return dict(DEV_DEP_CONFIGS[service].connection_env)

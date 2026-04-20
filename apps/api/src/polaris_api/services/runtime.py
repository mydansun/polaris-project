import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polaris_api.config import Settings
from polaris_api.models import BrowserSession, Workspace
from polaris_api.services.compose import (
    ComposeError,
    compose_project_name,
    exec_workspace_runtime,
    start_workspace_runtime,
    stop_workspace_runtime,
    workspace_meta_path,
)
from polaris_api.services.ide import render_public_ide_url

logger = logging.getLogger(__name__)
ACTIVE_BROWSER_STATUSES = ["starting", "ready"]


@dataclass(frozen=True)
class RuntimeState:
    workspace: Workspace
    browser_session: BrowserSession | None
    enabled_services: list[str] = field(default_factory=list)
    health: dict[str, str] = field(default_factory=dict)


def browser_expiry(settings: Settings) -> datetime | None:
    if settings.browser_session_ttl_minutes <= 0:
        return None
    return datetime.now(UTC) + timedelta(minutes=settings.browser_session_ttl_minutes)


async def get_current_browser_session(
    session: AsyncSession,
    workspace: Workspace,
) -> BrowserSession | None:
    if workspace.current_browser_session_id is not None:
        current = await session.get(BrowserSession, workspace.current_browser_session_id)
        if current is not None:
            return current

    result = await session.execute(
        select(BrowserSession)
        .where(BrowserSession.workspace_id == workspace.id)
        .order_by(BrowserSession.created_at.desc())
    )
    return result.scalars().first()


def browser_session_services(browser_session: BrowserSession | None) -> list[str]:
    """Dev-time dependency services are now tracked by the dev_deps service
    (workspace_dep_services table), not on BrowserSession metadata.  This
    helper is a compat shim for routes that still ask — returns stored
    legacy values filtered to the static dep vocabulary we now support.
    Callers should migrate to `GET /workspace/dev-deps`."""
    if browser_session is None:
        return []
    services = browser_session.context_metadata_jsonb.get("enabled_services", [])
    if not isinstance(services, list):
        return []
    supported = {"postgres", "redis"}
    return sorted(
        service
        for service in services
        if isinstance(service, str) and service in supported
    )


async def wait_for_container_http(
    *,
    meta_path: Path,
    workspace_id,
    service: str,
    port: int = 3000,
    path: str = "/",
    timeout_seconds: float = 60,
) -> None:
    """Poll `http://localhost:<port><path>` inside the given compose service via curl.

    Used instead of host-port probes: IDE/VNC are reached through the shared
    docker network, so we check readiness from inside the container where
    localhost:3000 is the actual service socket. Any HTTP response (even 401)
    counts as ready — connection refused / timeout does not.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    last_error: ComposeError | None = None
    probe = (
        "sh",
        "-c",
        f"curl -sS -m 2 -o /dev/null http://localhost:{port}{path}",
    )
    while loop.time() < deadline:
        try:
            await exec_workspace_runtime(
                meta_path=meta_path,
                workspace_id=workspace_id,
                service=service,
                command=probe,
                timeout_seconds=5,
            )
            return
        except ComposeError as exc:
            last_error = exc
            await asyncio.sleep(0.5)
    detail = f": {last_error}" if last_error is not None else ""
    raise ComposeError(
        f"{service} HTTP probe on port {port} failed after {timeout_seconds:.0f}s{detail}"
    )


async def wait_for_runtime_command(
    *,
    meta_path: Path,
    workspace_id,
    service: str,
    command: tuple[str, ...],
    timeout_seconds: float = 30,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    last_error: ComposeError | None = None
    while loop.time() < deadline:
        try:
            await exec_workspace_runtime(
                meta_path=meta_path,
                workspace_id=workspace_id,
                service=service,
                command=command,
            )
            return
        except ComposeError as exc:
            last_error = exc
            await asyncio.sleep(0.5)
    detail = f": {last_error}" if last_error is not None else ""
    raise ComposeError(f"{service} did not become healthy within {timeout_seconds:.0f}s{detail}")


async def wait_for_runtime_health(
    *,
    meta_path: Path,
    workspace_id,
) -> dict[str, str]:
    # IDE and chromium-vnc aren't bound to host ports anymore; probe them via
    # `compose exec` + curl inside the container. We only need TCP accept + any
    # HTTP response to consider them serving — not a 2xx — because OpenVSCode
    # redirects to a login page and noVNC returns 401 until configured.
    #
    # Dev-time dep services (postgres / redis) are NOT part of workspace
    # compose anymore — their health is managed by services/dev_deps.py
    # and checked there at `polaris dev-up` time.
    health: dict[str, str] = {}
    await wait_for_container_http(
        meta_path=meta_path, workspace_id=workspace_id, service="workspace"
    )
    health["workspace"] = "ok"
    await wait_for_container_http(
        meta_path=meta_path, workspace_id=workspace_id, service="chromium-vnc"
    )
    health["chromium-vnc"] = "ok"
    return health


async def ensure_workspace_runtime(
    *,
    session: AsyncSession,
    workspace: Workspace,
    settings: Settings,
) -> RuntimeState:
    repo_path = Path(workspace.repo_path)
    meta_path = workspace_meta_path(Path(settings.workspace_meta_root), workspace.id)
    meta_path.mkdir(parents=True, exist_ok=True)
    # Mint a workspace_token on first ensure if not set. This token is the
    # shared secret the in-container `polaris` CLI uses to authenticate back
    # to the platform API for publish / rollback / status calls.
    if not workspace.workspace_token:
        workspace.workspace_token = secrets.token_urlsafe(32)
    browser_session = await get_current_browser_session(session, workspace)

    workspace.ide_url = render_public_ide_url(
        settings.ide_public_url_template,
        project_id=workspace.project_id,
        workspace_id=workspace.id,
    )
    workspace.ide_status = "starting"
    if browser_session is None:
        browser_session = BrowserSession(
            project_id=workspace.project_id,
            workspace_id=workspace.id,
            status="starting",
            vnc_url=render_public_ide_url(
                settings.browser_public_url_template,
                project_id=workspace.project_id,
                workspace_id=workspace.id,
            ),
            context_metadata_jsonb={},
            expires_at=browser_expiry(settings),
        )
        session.add(browser_session)
        await session.flush()
    browser_session.status = "starting"
    browser_session.vnc_url = render_public_ide_url(
        settings.browser_public_url_template,
        project_id=workspace.project_id,
        workspace_id=workspace.id,
    )
    browser_session.context_metadata_jsonb = {
        **browser_session.context_metadata_jsonb,
        # Playwright MCP inside the workspace container reaches chromium-vnc
        # via the per-project docker bridge; stored here for observability.
        "cdp_endpoint": "http://chromium-vnc:9222",
        "service": "chromium-vnc",
        "runtime": "workspace",
    }
    workspace.current_browser_session_id = browser_session.id
    workspace.compose_profile = "workspace"
    await session.commit()
    await session.refresh(workspace)
    await session.refresh(browser_session)

    try:
        await start_workspace_runtime(
            repo_path=repo_path,
            meta_path=meta_path,
            workspace_id=workspace.id,
            workspace_image=settings.workspace_image,
            browser_image=settings.browser_image,
            host_codex_auth_path=Path(settings.host_codex_auth_path),
            traefik_public_network=settings.traefik_public_network_name,
            domain=settings.domain,
            project_id=workspace.project_id,
            workspace_token=workspace.workspace_token,
            api_url_for_workspace=settings.api_url_for_workspace,
        )
        health = await wait_for_runtime_health(
            meta_path=meta_path,
            workspace_id=workspace.id,
        )
    except ComposeError as exc:
        workspace.ide_status = "failed"
        browser_session.status = "failed"
        browser_session.context_metadata_jsonb = {
            **browser_session.context_metadata_jsonb,
            "error": str(exc),
        }
        await session.commit()
        raise

    workspace.status = "ready"
    workspace.ide_status = "ready"
    browser_session.status = "ready"
    browser_session.context_metadata_jsonb = {
        **browser_session.context_metadata_jsonb,
        "health": health,
    }
    await session.commit()
    await session.refresh(workspace)
    await session.refresh(browser_session)
    return RuntimeState(
        workspace=workspace,
        browser_session=browser_session,
        enabled_services=[],
        health=health,
    )


async def stop_runtime(
    *,
    session: AsyncSession,
    workspace: Workspace,
    settings: Settings,
) -> RuntimeState:
    browser_session = await get_current_browser_session(session, workspace)
    meta_path = workspace_meta_path(Path(settings.workspace_meta_root), workspace.id)
    await stop_workspace_runtime(meta_path=meta_path, workspace_id=workspace.id)
    workspace.ide_status = "stopped"
    workspace.ide_url = None
    if browser_session is not None:
        browser_session.status = "stopped"
    workspace.current_browser_session_id = None
    await session.commit()
    await session.refresh(workspace)
    if browser_session is not None:
        await session.refresh(browser_session)
    return RuntimeState(workspace=workspace, browser_session=browser_session)


async def restart_runtime(
    *,
    session: AsyncSession,
    workspace: Workspace,
    settings: Settings,
) -> RuntimeState:
    """Stop the workspace compose runtime and bring it back up.  Used
    when the user asks for a fresh workspace (dev server wedged,
    supervisord acting up).  Persistent state preserved:
      - bind-mounted code at workspace.repo_path
      - codex-home named volume (session history)
      - dev-dep containers (postgres / redis) are NOT touched by this
        path — they're independent of the workspace compose.
    """
    await stop_runtime(session=session, workspace=workspace, settings=settings)
    return await ensure_workspace_runtime(
        session=session,
        workspace=workspace,
        settings=settings,
    )

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID


logger = logging.getLogger(__name__)


# Source for the Chromium new-tab page: welcome.html gets bind-mounted into a
# sidecar nginx serving `http://welcome/` in the compose network, and
# extension/ is loaded as a Chromium extension.  Path is a monorepo-internal
# invariant — config.py used to expose an env override for it, but no
# deployment ever needed to relocate it.
WELCOME_PAGE_DIST = Path(__file__).resolve().parents[5] / "packages" / "welcome-page" / "dist"


class ComposeError(Exception):
    pass


@dataclass(frozen=True)
class ComposeResult:
    compose_path: Path
    project_name: str


@dataclass(frozen=True)
class WorkspaceRuntimeComposeResult(ComposeResult):
    services: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ComposeCommandResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False



def compose_project_name(workspace_id: UUID) -> str:
    return f"polaris-{str(workspace_id).replace('-', '')[:24]}"


def workspace_meta_path(meta_root: Path, workspace_id: UUID) -> Path:
    return meta_root / str(workspace_id)


def workspace_runtime_compose_path(meta_path: Path) -> Path:
    return meta_path / "compose.workspace.yaml"


def copy_welcome_assets(meta_path: Path, dist_root: Path | None) -> bool:
    """Copy welcome-page build output into the browser-config dir.

    Returns True when the Chromium extension directory was copied (enabling
    the new-tab override). Returns False and logs a warning when the dist
    output is missing — Chromium will fall back to ``about:blank``.
    """
    if dist_root is None:
        logger.warning("welcome-page dist root is unset; chromium will boot to about:blank")
        return False
    welcome_html = dist_root / "welcome.html"
    extension_src = dist_root / "extension"
    if not welcome_html.exists():
        logger.warning(
            "welcome-page dist missing at %s; run `pnpm --filter @polaris/welcome-page build`",
            welcome_html,
        )
        return False
    browser_config = meta_path / "browser-config"
    browser_config.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(welcome_html, browser_config / "welcome.html")
    if extension_src.is_dir():
        extension_dest = browser_config / "extension"
        if extension_dest.exists():
            shutil.rmtree(extension_dest)
        shutil.copytree(extension_src, extension_dest)
        return True
    return False



def render_workspace_runtime_compose(
    *,
    repo_path: Path,
    meta_path: Path,
    workspace_id: UUID,
    workspace_image: str,
    browser_image: str,
    host_codex_auth_path: Path,
    shared_network: str = "polaris-internal",
    traefik_public_network: str = "traefik-public",
    domain: str = "polaris-dev.xyz",
    project_id: UUID | None = None,
    workspace_token: str | None = None,
    api_url_for_workspace: str = "http://host.docker.internal:8000",
) -> WorkspaceRuntimeComposeResult:
    # Dev-time dependency services (postgres / redis / …) are intentionally
    # NOT rendered into this compose file.  They're managed as independent
    # docker containers by services/dev_deps.py, attached to this compose's
    # default network via `--network-alias`.  Keeping them out keeps this
    # compose stable — workspace service definition never changes when a
    # dep is added/removed, so the workspace container never gets recreated
    # (which would kill the Codex session that invoked `polaris dev-up`).
    compose_path = workspace_runtime_compose_path(meta_path)
    compose_path.parent.mkdir(parents=True, exist_ok=True)
    browser_config = meta_path / "browser-config"
    browser_config.mkdir(parents=True, exist_ok=True)
    extension_ready = copy_welcome_assets(meta_path, WELCOME_PAGE_DIST)

    workspace_mount = f"{repo_path.resolve()}:/workspace:cached"
    browser_config_mount = f"{browser_config.resolve()}:/config:cached"
    # Single-user dev setup: share the host user's Codex auth rw so the
    # in-container codex app-server authenticates as that user and token
    # refreshes propagate.  Multi-tenant variants should per-tenant this.
    codex_auth_mount = f"{host_codex_auth_path.resolve()}:/home/workspace/.codex/auth.json:rw"
    codex_home_volume = f"polaris-ws-{str(workspace_id).replace('-', '')[:24]}-codex-home"
    codex_home_mount = f"{codex_home_volume}:/home/workspace/.codex"
    services = ["workspace", "chromium-vnc"]

    workspace_environment = [
        "      HOME: \"/home/workspace\"",
        # Publish CLI integration: the in-container `polaris` CLI calls
        # back to the platform API via these three env vars.
        f"      POLARIS_API_URL: {json.dumps(api_url_for_workspace)}",
    ]
    if project_id is not None:
        workspace_environment.append(
            f"      POLARIS_PROJECT_ID: {json.dumps(str(project_id))}"
        )
    if workspace_token:
        workspace_environment.append(
            f"      POLARIS_WORKSPACE_TOKEN: {json.dumps(workspace_token)}"
        )
    # DATABASE_URL / REDIS_URL are deliberately NOT injected here.  The
    # `polaris dev-up <service>` CLI writes them to a project-root .env file
    # after spinning up the matching dep container.  Framework loaders
    # (Prisma, Next.js, python-dotenv) pick them up natively.

    hash_id = str(workspace_id).replace("-", "")[:24]
    ws_container_name = f"polaris-ws-{hash_id}"
    br_container_name = f"polaris-br-{hash_id}"
    # Traefik router names must be unique across all services traefik sees;
    # hash_id is already scoped to this workspace, which makes it a fine key.
    ws_router = f"ws-{hash_id}"
    br_router = f"br-{hash_id}"
    ws_host = f"ide-{hash_id}.{domain}"
    br_host = f"browser-{hash_id}.{domain}"

    lines = [
        "services:",
        "  workspace:",
        f"    image: {json.dumps(workspace_image)}",
        f"    container_name: {json.dumps(ws_container_name)}",
        "    init: true",
        "    restart: unless-stopped",
        "    working_dir: /workspace",
        "    extra_hosts:",
        # host-gateway lets the in-container polaris CLI reach the API
        # on the host at host.docker.internal:8000 even on Linux where
        # that name doesn't auto-resolve.
        "      - \"host.docker.internal:host-gateway\"",
        "    networks:",
        "      - default",
        f"      - {shared_network}",
        f"      - {traefik_public_network}",
        "    environment:",
        *workspace_environment,
        "    volumes:",
        f"      - {json.dumps(workspace_mount)}",
        f"      - {json.dumps(codex_home_mount)}",
        f"      - {json.dumps(codex_auth_mount)}",
        "    labels:",
        f"      polaris.workspace_id: {json.dumps(str(workspace_id))}",
        "      polaris.service: \"workspace\"",
        "      traefik.enable: \"true\"",
        f"      traefik.docker.network: {json.dumps(traefik_public_network)}",
        f"      traefik.http.routers.{ws_router}.rule: {json.dumps(f'Host(`{ws_host}`)')}",
        f"      traefik.http.routers.{ws_router}.entrypoints: \"websecure\"",
        f"      traefik.http.routers.{ws_router}.tls: \"true\"",
        f"      traefik.http.services.{ws_router}.loadbalancer.server.port: \"3000\"",
        "  chromium-vnc:",
        f"    image: {json.dumps(browser_image)}",
        f"    container_name: {json.dumps(br_container_name)}",
        "    restart: unless-stopped",
        "    shm_size: \"1gb\"",
        "    security_opt:",
        "      - seccomp=unconfined",
        "    networks:",
        "      - default",
        f"      - {shared_network}",
        f"      - {traefik_public_network}",
        # No `ports:` — CDP is only consumed by @playwright/mcp running inside
        # the workspace container (same docker network), not by any host-side
        # process, so we don't bind it to a host port.
        "    environment:",
        "      PUID: \"1000\"",
        "      PGID: \"1000\"",
        "      TZ: \"Etc/UTC\"",
        "      HARDEN_DESKTOP: \"true\"",
        "      HARDEN_OPENBOX: \"true\"",
        "      SELKIES_AUDIO_ENABLED: \"false|locked\"",
        "      SELKIES_MICROPHONE_ENABLED: \"false|locked\"",
        "      SELKIES_GAMEPAD_ENABLED: \"false|locked\"",
        "      SELKIES_COMMAND_ENABLED: \"false|locked\"",
        "      SELKIES_FILE_TRANSFERS: \"none\"",
        "      SELKIES_ENABLE_SHARING: \"false|locked\"",
        "      SELKIES_SECOND_SCREEN: \"false|locked\"",
        "      SELKIES_UI_SHOW_CORE_BUTTONS: \"false|locked\"",
        "      SELKIES_UI_SIDEBAR_SHOW_AUDIO_SETTINGS: \"false|locked\"",
        "      SELKIES_UI_SIDEBAR_SHOW_FILES: \"false|locked\"",
        "      SELKIES_UI_SIDEBAR_SHOW_APPS: \"false|locked\"",
        "      SELKIES_UI_SIDEBAR_SHOW_SHARING: \"false|locked\"",
        "      SELKIES_UI_SIDEBAR_SHOW_GAMEPADS: \"false|locked\"",
        "      SELKIES_USE_BROWSER_CURSORS: \"true|locked\"",
    ]
    chrome_flags: list[str] = [
        "--remote-debugging-port=9222",
        "--remote-debugging-address=0.0.0.0",
        # Chromium >= 111 rejects CDP WebSocket connections unless the Origin
        # header matches an explicit allowlist.  Playwright MCP attaches from
        # the workspace container via http://chromium-vnc:9222, and `*` keeps
        # that handshake happy.  No LAN exposure since there's no host port
        # binding any more.
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    has_welcome_html = (browser_config / "welcome.html").exists()
    if extension_ready:
        chrome_flags.append("--load-extension=/config/extension")
    if has_welcome_html:
        # A sibling `welcome` nginx service serves welcome.html at
        # http://welcome/ on the compose default network.  Set it as
        # the startup URL, homepage, and new-tab page so every surface
        # shows the Polaris welcome page instead of Chrome defaults.
        chrome_flags.append("--homepage=http://welcome/")
        chrome_flags.append("--new-tab-page=http://welcome/")
        chrome_flags.append("http://welcome/")
    lines.append(f"      CHROME_CLI: {json.dumps(' '.join(chrome_flags))}")
    lines.extend(
        [
        "    volumes:",
        f"      - {json.dumps(browser_config_mount)}",
        "    labels:",
        f"      polaris.workspace_id: {json.dumps(str(workspace_id))}",
        "      polaris.service: \"chromium-vnc\"",
        "      traefik.enable: \"true\"",
        f"      traefik.docker.network: {json.dumps(traefik_public_network)}",
        f"      traefik.http.routers.{br_router}.rule: {json.dumps(f'Host(`{br_host}`)')}",
        f"      traefik.http.routers.{br_router}.entrypoints: \"websecure\"",
        f"      traefik.http.routers.{br_router}.tls: \"true\"",
        f"      traefik.http.services.{br_router}.loadbalancer.server.port: \"3000\"",
        ]
    )
    if has_welcome_html:
        # chromium-vnc depends on welcome so the startup URL http://welcome/
        # resolves by the time Chromium navigates.  nginx:alpine starts in
        # <1s, well before Chromium + VNC server (~5s), so this is just a
        # belt-and-braces ordering guarantee.
        lines.extend([
            "    depends_on:",
            "      - welcome",
        ])

    # Welcome sidecar: serves welcome.html at http://welcome/ on the
    # compose default network.  Chromium opens this as its startup page
    # (clean URL bar).  Only rendered when the welcome page dist exists.
    if has_welcome_html:
        welcome_html_mount = f"{(browser_config / 'welcome.html').resolve()}:/usr/share/nginx/html/index.html:ro"
        lines.extend([
            "  welcome:",
            "    image: nginx:alpine",
            "    restart: unless-stopped",
            "    volumes:",
            f"      - {json.dumps(welcome_html_mount)}",
            "    networks:",
            "      - default",
        ])
        services.append("welcome")

    # codex-home volume persists ~/.codex/sessions across container recreates
    # so thread/resume keeps working.  Always declared.
    lines.append("volumes:")
    lines.append(f"  {codex_home_volume}:")

    # Two external networks:
    #   - shared_network (polaris-internal): inter-project comms, pre-existing
    #   - traefik_public_network (traefik-public): edge router picks up labels
    # "default" stays for intra-compose (workspace ↔ chromium) when that's the
    # path of least resistance.
    lines.extend(
        [
            "networks:",
            "  default: {}",
            f"  {shared_network}:",
            "    external: true",
            f"    name: {shared_network}",
            f"  {traefik_public_network}:",
            "    external: true",
            f"    name: {traefik_public_network}",
        ]
    )

    compose_path.write_text("\n".join(lines) + "\n")
    return WorkspaceRuntimeComposeResult(
        compose_path=compose_path,
        project_name=compose_project_name(workspace_id),
        services=tuple(services),
    )


async def run_compose(
    compose_path: Path,
    project_name: str,
    *args: str,
    timeout_seconds: int = 300,
    stdin_bytes: bytes | None = None,
) -> str:
    result = await run_compose_capture(
        compose_path,
        project_name,
        *args,
        timeout_seconds=timeout_seconds,
        stdin_bytes=stdin_bytes,
    )
    if result.exit_code != 0:
        raise ComposeError(
            result.stderr or result.stdout or f"Docker Compose failed with {result.exit_code}"
        )
    return result.stdout.strip()


async def run_compose_capture(
    compose_path: Path,
    project_name: str,
    *args: str,
    timeout_seconds: int = 300,
    stdin_bytes: bytes | None = None,
) -> ComposeCommandResult:
    command = [
        "docker",
        "compose",
        "-p",
        project_name,
        "-f",
        str(compose_path),
        *args,
    ]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(input=stdin_bytes), timeout=timeout_seconds
        )
    except TimeoutError:
        process.kill()
        stdout, stderr = await process.communicate()
        timeout_message = f"Docker Compose timed out after {timeout_seconds}s"
        decoded_stderr = stderr.decode(errors="replace").strip()
        if decoded_stderr:
            decoded_stderr = f"{decoded_stderr}\n{timeout_message}"
        else:
            decoded_stderr = timeout_message
        return ComposeCommandResult(
            stdout=stdout.decode(errors="replace").strip(),
            stderr=decoded_stderr,
            exit_code=124,
            timed_out=True,
        )

    return ComposeCommandResult(
        stdout=stdout.decode(errors="replace").strip(),
        stderr=stderr.decode(errors="replace").strip(),
        exit_code=process.returncode if process.returncode is not None else 1,
        timed_out=False,
    )


async def start_workspace_runtime(
    *,
    repo_path: Path,
    meta_path: Path,
    workspace_id: UUID,
    workspace_image: str,
    browser_image: str,
    host_codex_auth_path: Path,
    shared_network: str = "polaris-internal",
    traefik_public_network: str = "traefik-public",
    domain: str = "polaris-dev.xyz",
    project_id: UUID | None = None,
    workspace_token: str | None = None,
    api_url_for_workspace: str = "http://host.docker.internal:8000",
) -> WorkspaceRuntimeComposeResult:
    result = render_workspace_runtime_compose(
        repo_path=repo_path,
        meta_path=meta_path,
        workspace_id=workspace_id,
        workspace_image=workspace_image,
        browser_image=browser_image,
        host_codex_auth_path=host_codex_auth_path,
        shared_network=shared_network,
        traefik_public_network=traefik_public_network,
        domain=domain,
        project_id=project_id,
        workspace_token=workspace_token,
        api_url_for_workspace=api_url_for_workspace,
    )
    try:
        await run_compose(result.compose_path, result.project_name, "up", "-d", *result.services)
    except ComposeError:
        # A prior aborted attempt may have left orphan containers or the
        # per-project `_default` network behind, which makes `up` fail with
        # "name already in use" or "network already exists". Recover by tearing
        # the project down (idempotent) and retrying once.
        try:
            await run_compose(
                result.compose_path,
                result.project_name,
                "down",
                "--remove-orphans",
                timeout_seconds=60,
            )
        except ComposeError:
            pass
        await run_compose(result.compose_path, result.project_name, "up", "-d", *result.services)
    return result


async def stop_workspace_runtime(*, meta_path: Path, workspace_id: UUID) -> None:
    compose_path = workspace_runtime_compose_path(meta_path)
    if not compose_path.exists():
        return
    await run_compose(compose_path, compose_project_name(workspace_id), "down")


async def exec_workspace_runtime(
    *,
    meta_path: Path,
    workspace_id: UUID,
    service: str,
    command: tuple[str, ...],
    workdir: str | None = None,
    timeout_seconds: int = 60,
    stdin: str | None = None,
) -> str:
    result = await exec_workspace_runtime_capture(
        meta_path=meta_path,
        workspace_id=workspace_id,
        service=service,
        command=command,
        workdir=workdir,
        timeout_seconds=timeout_seconds,
        stdin=stdin,
    )
    if result.exit_code != 0:
        raise ComposeError(
            result.stderr or result.stdout or f"Docker Compose failed with {result.exit_code}"
        )
    return result.stdout


async def exec_workspace_runtime_capture(
    *,
    meta_path: Path,
    workspace_id: UUID,
    service: str,
    command: tuple[str, ...],
    workdir: str | None = None,
    timeout_seconds: int = 60,
    stdin: str | None = None,
) -> ComposeCommandResult:
    compose_path = workspace_runtime_compose_path(meta_path)
    if not compose_path.exists():
        raise ComposeError("Workspace runtime compose file does not exist")
    exec_args = ["exec", "-T"]
    if workdir is not None:
        exec_args.extend(["--workdir", workdir])
    exec_args.extend([service, *command])
    stdin_bytes = stdin.encode() if stdin is not None else None
    return await run_compose_capture(
        compose_path,
        compose_project_name(workspace_id),
        *exec_args,
        timeout_seconds=timeout_seconds,
        stdin_bytes=stdin_bytes,
    )

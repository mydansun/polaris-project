"""Publish pipeline: take a user project repo at a git commit, build a
docker image, smoke-test it, promote it behind traefik at
<uuid>.<prod_domain_base>.

Each publish is tracked by a single `deployments` row.  Status transitions:
  queued → building → deploying → ready       (happy path)
          ↘ failed                             (bad Dockerfile / smoke fail /
                                                traefik cannot reach new ctr)

This module is invoked from `routes/deploy.py` as an asyncio task so the
HTTP request returns immediately with the Deployment row ID; clients follow
progress via SSE (`GET /deployments/<id>/events`), which reads build_log +
status changes from the DB and streams them.

Simplifications for the MVP (documented in the plan risks section):
  * Build + run on the same docker host as the platform.  Prod-on-remote-VM
    is a followup — the shape of this file won't change, only the docker
    context used by subprocess.
  * Smoke test probes `/` from inside the ephemeral preview network via a
    disposable `curlimages/curl` container (avoids host port assignment
    races when two publishes run concurrently).
  * Secrets materialise once, on first ready publish, and are written to a
    host-side file the compose file references via env_file.  Rotating is
    a future feature.
  * No registry GC.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polaris_api.config import Settings
from polaris_api.models import Deployment, Project, ProjectVersion, Workspace
from polaris_api.schemas import PolarisManifest


logger = logging.getLogger(__name__)


# ─── Errors ─────────────────────────────────────────────────────────────────


class PublishError(Exception):
    """Any fatal error during a publish attempt."""


# ─── Helpers ────────────────────────────────────────────────────────────────


def publish_project_root(settings: Settings, project_id: UUID) -> Path:
    return Path(settings.publish_projects_root) / str(project_id)


def image_tag(settings: Settings, project_id: UUID, short_hash: str) -> str:
    return f"{settings.registry_url}/polaris/{project_id}:{short_hash}"


def project_domain(settings: Settings, project_id: UUID) -> str:
    return f"{project_id}.{settings.prod_domain_base}"


def compose_project_name(project_id: UUID) -> str:
    # Stay under docker compose's 63-char project-name cap.
    return f"polaris-pub-{str(project_id).replace('-', '')[:24]}"


def preview_project_name(project_id: UUID, short_hash: str) -> str:
    return f"polaris-pvw-{str(project_id).replace('-', '')[:16]}-{short_hash[:6]}"


async def _run(
    *args: str,
    cwd: Path | None = None,
    timeout: float = 60,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run a subprocess; return (exit_code, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **(env or {})},
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise PublishError(f"command timed out: {' '.join(args)}") from None
    out = stdout.decode(errors="replace")
    err = stderr.decode(errors="replace")
    if check and proc.returncode != 0:
        raise PublishError(
            f"command failed (exit {proc.returncode}): {' '.join(args)}\n{err or out}"
        )
    return proc.returncode or 0, out, err


async def _run_streaming(
    *args: str,
    cwd: Path | None = None,
    timeout: float = 900,
    log_sink: list[str],
    env: dict[str, str] | None = None,
) -> int:
    """Stream child output line-by-line into `log_sink` for SSE tailing.
    Returns exit code; does not raise on non-zero."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, **(env or {})},
    )
    assert proc.stdout is not None
    try:
        async with asyncio.timeout(timeout):
            async for raw in proc.stdout:
                log_sink.append(raw.decode(errors="replace"))
            return await proc.wait()
    except TimeoutError:
        proc.kill()
        await proc.wait()
        log_sink.append(f"\n[timed out after {timeout:.0f}s]\n")
        return 124


# ─── Manifest loading + auto-scaffold ──────────────────────────────────────


def load_manifest(repo_path: Path) -> PolarisManifest:
    manifest_path = repo_path / "polaris.yaml"
    if not manifest_path.is_file():
        raise PublishError(
            "no polaris.yaml at repo root — run `polaris scaffold-publish` first"
        )
    try:
        raw = yaml.safe_load(manifest_path.read_text())
    except yaml.YAMLError as exc:
        raise PublishError(f"polaris.yaml is not valid YAML: {exc}") from exc
    try:
        return PolarisManifest.model_validate(raw or {})
    except Exception as exc:  # pydantic ValidationError
        raise PublishError(f"polaris.yaml schema invalid: {exc}") from exc


# Mirrors the detection logic in infra/workspace/polaris-cli/polaris.py so
# clicking Publish without having run the CLI produces the same artifacts
# the CLI would have produced.  Kept intentionally redundant — CLI is the
# preferred path (agent picks from a menu); platform side is the safety
# net for users who click Publish without running the CLI first.

_STACK_DEFAULTS: dict[str, dict[str, str]] = {
    "spa": {
        "port": "80",
        "build": "npm run build",
        "start": "",  # nginx CMD from the runner image
    },
    "node": {
        "port": "3000",
        "build": "npm run build",
        "start": "npm start",
    },
    "python": {
        "port": "8000",
        "build": "",
        "start": "python -m uvicorn app:app --host 0.0.0.0 --port 8000",
    },
    "static": {
        "port": "80",
        "build": "",
        "start": "",
    },
}

_STACK_DETECT_REASONS: dict[str, str] = {
    "spa":    'package.json has "vite" in (dev)dependencies',
    "node":   "package.json present (no vite dep found)",
    "python": "requirements.txt or pyproject.toml present",
    "static": "index.html present, no package.json",
    "custom": "no recognized marker files",
}


def _detect_stack(project_root: Path) -> str:
    """Pick the fallback publish stack based on files at the repo root.

    Vite-shaped SPAs are distinguished from a plain Node server by
    peeking at ``package.json`` dependencies for a ``vite`` key.  Other
    markers follow a simple first-match-wins order.
    """
    pkg = project_root / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
        deps = {
            **(data.get("dependencies") or {}),
            **(data.get("devDependencies") or {}),
        }
        if "vite" in deps:
            return "spa"
        return "node"
    if (project_root / "requirements.txt").exists() \
       or (project_root / "pyproject.toml").exists():
        return "python"
    if (project_root / "index.html").exists():
        return "static"
    return "custom"


def _render_template_text(text: str, replacements: dict[str, str]) -> str:
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


def auto_scaffold_if_missing(
    project_root: Path,
    templates_root: Path,
    log_sink: list[str],
) -> None:
    """Server-side equivalent of `polaris scaffold-publish` — fires when
    the user (or Codex) clicks Publish on a project that has no
    `polaris.yaml` yet.

    Detects the stack from marker files (same rules as the CLI), loads
    the matching template from `<templates_root>/<stack>/`, and writes
    any of {Dockerfile, compose.prod.yml, polaris.yaml} that aren't
    already present.  Never overwrites — if the user hand-wrote one of
    these, we respect it and fill in only the missing siblings.

    Stack=custom (no marker files) has no sensible defaults and errors
    with a clear message pointing at the CLI for the user to supply
    them manually.
    """
    if (project_root / "polaris.yaml").is_file():
        return

    stack = _detect_stack(project_root)
    if stack == "custom":
        raise PublishError(
            "no polaris.yaml at project root, and the stack isn't auto-"
            "detectable (no package.json / requirements.txt / "
            "pyproject.toml / index.html). Run `polaris scaffold-publish` "
            "inside the workspace with explicit --stack/--build/--start/"
            "--port flags, commit, then try publish again."
        )
    tpl_dir = templates_root / stack
    if not tpl_dir.is_dir():
        raise PublishError(f"publish template missing on platform: {tpl_dir}")

    defaults = _STACK_DEFAULTS[stack]
    replacements = {
        "__POLARIS_SERVICE__": "web",
        "__POLARIS_PORT__": defaults["port"],
        "__POLARIS_BUILD_CMD__": defaults["build"] or "true",  # Dockerfile RUN
        "__POLARIS_START_CMD__": defaults["start"],
        "__POLARIS_START_CMD_JSON__": (
            json.dumps(defaults["start"].split()) if defaults["start"] else '["true"]'
        ),
    }

    written: list[str] = []
    for name in ("Dockerfile", "compose.prod.yml", "polaris.yaml"):
        src = tpl_dir / name
        if not src.is_file():
            continue
        dst = project_root / name
        if dst.exists():
            continue
        dst.write_text(_render_template_text(src.read_text(), replacements))
        written.append(name)

    reason = _STACK_DETECT_REASONS.get(stack, "")
    reason_text = f" (reason: {reason})" if reason else ""
    log_sink.append(
        f"[auto-scaffold] detected stack={stack}{reason_text}; "
        f"wrote {', '.join(written) if written else '(no files needed)'}. "
        f"If this doesn't fit, run `polaris scaffold-publish` in the "
        f"workspace to see all options, then `--stack=<choice>` and "
        f"re-publish.\n"
    )


# ─── Secrets ────────────────────────────────────────────────────────────────


def materialize_secrets(project_id: UUID, settings: Settings, manifest: PolarisManifest) -> Path:
    """Write `<publish_projects_root>/<uuid>/secrets.env` on first publish;
    reuse it on subsequent publishes (passwords do NOT rotate — cross-
    publish data volumes stay openable).  Returns the secrets file path.

    For `"postgres" in manifest.deps`: generates POSTGRES_USER / POSTGRES_DB
    / POSTGRES_PASSWORD once, then derives DATABASE_URL using the SAME
    password token so the user's app and the postgres container (both
    reading this same env_file) always agree on the credential.
    """
    project_dir = publish_project_root(settings, project_id)
    project_dir.mkdir(parents=True, exist_ok=True)
    secrets_file = project_dir / "secrets.env"

    existing: dict[str, str] = {}
    if secrets_file.exists():
        for line in secrets_file.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                existing[key.strip()] = value
    changed = False

    # Dep-driven credentials — these feed BOTH the dep container (via
    # env_file on the postgres/redis service) AND the user app (via the
    # same env_file injected by compose.polaris.yml override).
    if "postgres" in manifest.deps:
        if "POSTGRES_USER" not in existing:
            existing["POSTGRES_USER"] = "app"
            changed = True
        if "POSTGRES_DB" not in existing:
            existing["POSTGRES_DB"] = "app"
            changed = True
        if "POSTGRES_PASSWORD" not in existing:
            existing["POSTGRES_PASSWORD"] = secrets.token_hex(32)
            changed = True
    # redis dep has no auth in our default image setup; nothing to seed.

    # User-declared secrets in polaris.yaml.secrets.  For well-known names
    # that map onto a running dep service, we compose the URL from the
    # credentials above (shared token).  Otherwise random fallback.
    for name in manifest.secrets:
        if name in existing:
            continue
        if name == "DATABASE_URL" and "postgres" in manifest.deps:
            existing[name] = (
                f"postgresql://{existing['POSTGRES_USER']}:"
                f"{existing['POSTGRES_PASSWORD']}@postgres:5432/"
                f"{existing['POSTGRES_DB']}"
            )
        elif name == "REDIS_URL" and "redis" in manifest.deps:
            existing[name] = "redis://redis:6379/0"
        else:
            existing[name] = secrets.token_urlsafe(48)
        changed = True

    if changed or not secrets_file.exists():
        # Escape `$` as `$$` so Docker Compose env_file doesn't
        # interpret them as variable interpolation (e.g. bcrypt hashes).
        def _esc(v: str) -> str:
            return v.replace("$", "$$")

        lines = [f"{k}={_esc(v)}" for k, v in existing.items()]
        # Also bake in any static non-sensitive env declared in manifest.env.
        for k, v in manifest.env.items():
            if k not in existing:
                lines.append(f"{k}={_esc(v)}")
        secrets_file.write_text("\n".join(lines) + "\n")
        secrets_file.chmod(0o600)
    return secrets_file


# ─── User compose sanitizer ─────────────────────────────────────────────────


def sanitize_prod_compose(archive_dir: Path, log_sink: list[str]) -> None:
    """Strip any host-published ports from the user's ``compose.prod.yml``.

    Prod containers must be reachable only via the ``traefik-public``
    docker network — host port publishing (e.g. ``ports: ["80:80"]``)
    collides with the platform's Traefik which already holds the host
    ingress (80 / 443).  Drop the whole ``ports:`` list on every service
    unconditionally; keep ``expose:`` (internal-only hint, no host
    binding) untouched.

    No-op when ``compose.prod.yml`` is absent or unparseable — downstream
    ``docker compose`` will surface the real error with its own wording.
    Each removal appends a line to ``log_sink`` so the user sees it in
    the PublishPanel live log stream.
    """
    compose_path = archive_dir / "compose.prod.yml"
    if not compose_path.is_file():
        return
    try:
        doc = yaml.safe_load(compose_path.read_text()) or {}
    except yaml.YAMLError as exc:
        log_sink.append(f"[sanitize] skipped: invalid YAML ({exc})\n")
        return
    services = doc.get("services")
    if not isinstance(services, dict):
        return

    changed = False
    for name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        ports = svc.get("ports")
        if ports:
            log_sink.append(
                f"[sanitize] stripped host ports from service '{name}': "
                f"{ports!r} — traefik reaches the container via the "
                f"traefik-public network, no host publish needed.\n"
            )
            svc.pop("ports", None)
            changed = True

    # Strip user-declared subnets. The host's docker address pool is a
    # shared resource (default pools give only ~31 total bridge networks);
    # a user reserving a /16 or /24 eats slots other publishes need. Let
    # compose allocate from the daemon's default pool. Named networks
    # themselves are fine — only the ipam block is removed.
    networks = doc.get("networks")
    if isinstance(networks, dict):
        for net_name, net_cfg in networks.items():
            if not isinstance(net_cfg, dict):
                continue
            if "ipam" in net_cfg:
                log_sink.append(
                    f"[sanitize] stripped ipam block from network "
                    f"'{net_name}' — subnet assignment is platform-managed.\n"
                )
                net_cfg.pop("ipam", None)
                changed = True

    if changed:
        compose_path.write_text(yaml.safe_dump(doc, sort_keys=False))


# ─── Compose overrides ──────────────────────────────────────────────────────


def render_prod_override(
    project_id: UUID,
    manifest: PolarisManifest,
    image: str,
    secrets_file: Path,
    traefik_public_network: str,
    domain: str,
) -> str:
    """Generate `compose.polaris.yml` — slots in image tag, traefik labels,
    external network membership, secrets file location, AND — when
    `manifest.deps` is non-empty — additional service blocks (postgres /
    redis) with their own per-project named volumes and healthchecks,
    plus `depends_on.<dep>.condition: service_healthy` on the user's
    publish service so migrations don't race the DB coming up."""
    hash_id = str(project_id).replace("-", "")[:24]
    router = f"pub-{hash_id}"
    service = manifest.publish.service
    port = manifest.publish.port

    # User service block
    user_lines: list[str] = [
        "services:",
        f"  {service}:",
        "    # Image swapped from `build:` to the registry tag promoted by",
        "    # the platform — prod never builds, only pulls.",
        f"    image: {image}",
        "    pull_policy: always",
        # Survive Docker daemon restarts + host reboots. Deps (postgres /
        # redis below) already carry this; without it on the user service,
        # published sites vanish on any docker restart.
        "    restart: unless-stopped",
        "    env_file:",
        f"      - {secrets_file}",
        "    networks:",
        f"      - {traefik_public_network}",
        "      - default",
        "    labels:",
        "      traefik.enable: \"true\"",
        f"      traefik.docker.network: \"{traefik_public_network}\"",
        f"      traefik.http.routers.{router}.rule: \"Host(`{domain}`)\"",
        f"      traefik.http.routers.{router}.entrypoints: \"websecure\"",
        f"      traefik.http.routers.{router}.tls: \"true\"",
        f"      traefik.http.services.{router}.loadbalancer.server.port: \"{port}\"",
    ]
    if manifest.deps:
        user_lines.append("    depends_on:")
        for dep in manifest.deps:
            user_lines.append(f"      {dep}:")
            user_lines.append("        condition: service_healthy")

    # Dep service blocks (only on traefik `default` network, NOT public)
    dep_lines: list[str] = []
    volume_names: list[str] = []
    if "postgres" in manifest.deps:
        vol = f"polaris-pub-{hash_id}-postgres-data"
        volume_names.append(vol)
        dep_lines.extend([
            "  postgres:",
            "    image: postgres:16-alpine",
            "    restart: unless-stopped",
            "    env_file:",
            f"      - {secrets_file}",
            "    volumes:",
            f"      - {vol}:/var/lib/postgresql/data",
            "    healthcheck:",
            "      test: [\"CMD-SHELL\", \"pg_isready -U $${POSTGRES_USER:-app} -d $${POSTGRES_DB:-app}\"]",
            "      interval: 5s",
            "      timeout: 5s",
            "      retries: 12",
            "    networks:",
            "      - default",
        ])
    if "redis" in manifest.deps:
        vol = f"polaris-pub-{hash_id}-redis-data"
        volume_names.append(vol)
        dep_lines.extend([
            "  redis:",
            "    image: redis:7-alpine",
            "    restart: unless-stopped",
            "    volumes:",
            f"      - {vol}:/data",
            "    healthcheck:",
            "      test: [\"CMD\", \"redis-cli\", \"ping\"]",
            "      interval: 5s",
            "      timeout: 5s",
            "      retries: 12",
            "    networks:",
            "      - default",
        ])

    tail: list[str] = []
    if volume_names:
        tail.append("volumes:")
        for v in volume_names:
            tail.append(f"  {v}:")
    tail.extend([
        "networks:",
        f"  {traefik_public_network}:",
        "    external: true",
        f"    name: {traefik_public_network}",
    ])
    return "\n".join(user_lines + dep_lines + tail) + "\n"


def render_preview_override(
    manifest: PolarisManifest, image: str, secrets_file: Path
) -> str:
    """Compose override for the smoke-test stage: isolated network, no
    traefik labels, image pulled (not built again), env_file pinned to
    the absolute secrets.env path (the user's compose.prod.yml doesn't
    reference env_file itself — the platform owns that injection).

    When `manifest.deps` is non-empty, we also spin up matching dep
    service blocks (postgres / redis) with ephemeral volumes — smoke
    test doesn't persist data, so volumes are auto-cleaned on `compose
    down -v` after smoke ends.  The user service gets `depends_on` so
    migrations in its CMD don't race the DB startup."""
    service = manifest.publish.service
    lines: list[str] = [
        "services:",
        f"  {service}:",
        f"    image: {image}",
        "    pull_policy: never  # local image, not yet pushed",
        "    env_file:",
        f"      - {secrets_file}",
    ]
    if manifest.deps:
        lines.append("    depends_on:")
        for dep in manifest.deps:
            lines.append(f"      {dep}:")
            lines.append("        condition: service_healthy")

    if "postgres" in manifest.deps:
        lines.extend([
            "  postgres:",
            "    image: postgres:16-alpine",
            "    env_file:",
            f"      - {secrets_file}",
            "    healthcheck:",
            "      test: [\"CMD-SHELL\", \"pg_isready -U $${POSTGRES_USER:-app} -d $${POSTGRES_DB:-app}\"]",
            "      interval: 5s",
            "      timeout: 5s",
            "      retries: 12",
        ])
    if "redis" in manifest.deps:
        lines.extend([
            "  redis:",
            "    image: redis:7-alpine",
            "    healthcheck:",
            "      test: [\"CMD\", \"redis-cli\", \"ping\"]",
            "      interval: 5s",
            "      timeout: 5s",
            "      retries: 12",
        ])

    lines.extend([
        "networks:",
        "  default: {}",
    ])
    return "\n".join(lines) + "\n"


async def _ensure_git_identity(git_dir: Path) -> None:
    """Guarantee `git commit` won't bail on a missing user.email/user.name.

    `git config user.email` (no scope flag) probes repo-local → global →
    system in order; exit non-zero means NOTHING is set anywhere.  In
    that case we drop a repo-local default so publish's auto-commit
    succeeds.  We never touch `--global` (the dev's own machine) and
    never override an identity the user already configured.

    The `set_project_root` worker-side handler sets a repo-local identity
    when it runs `git init` — so this function is a belt-and-braces guard
    for repos that were initialized outside that handler (e.g. Codex
    manually `git init`'d somewhere).
    """
    rc_email, _, _ = await _run(
        "git", "-C", str(git_dir), "config", "user.email",
        timeout=5, check=False,
    )
    rc_name, _, _ = await _run(
        "git", "-C", str(git_dir), "config", "user.name",
        timeout=5, check=False,
    )
    if rc_email != 0:
        await _run(
            "git", "-C", str(git_dir), "config", "--local",
            "user.email", "publish@polaris.local",
            timeout=5,
        )
    if rc_name != 0:
        await _run(
            "git", "-C", str(git_dir), "config", "--local",
            "user.name", "Polaris Publisher",
            timeout=5,
        )


# ─── Pipeline stages ────────────────────────────────────────────────────────


async def git_archive(repo_path: Path, commit: str, dest: Path) -> None:
    """Materialize a commit-frozen source tree into dest (must not exist)."""
    if dest.exists():
        raise PublishError(f"archive target already exists: {dest}")
    dest.mkdir(parents=True)
    # `git archive` → tar → extract into dest.  Use shell pipe.
    cmd = f'git -C "{repo_path}" archive "{commit}" | tar -x -C "{dest}"'
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise PublishError(f"git archive failed: {stderr.decode(errors='replace')}")


async def docker_build(
    context: Path, tag: str, log_sink: list[str], timeout: float
) -> None:
    log_sink.append(f"▶ docker build -t {tag} {context}\n")
    code = await _run_streaming(
        "docker", "build", "--pull", "-t", tag, str(context),
        timeout=timeout, log_sink=log_sink,
    )
    if code != 0:
        raise PublishError(f"docker build failed (exit {code})")


async def smoke_test(
    *,
    project_id: UUID,
    short_hash: str,
    archive_dir: Path,
    manifest: PolarisManifest,
    image: str,
    secrets_file: Path,
    smoke_log: list[str],
    timeout: float,
) -> None:
    """Stand up a preview compose on an isolated network, curl the publish
    service via a disposable container on the same network, tear down."""
    preview_override = archive_dir / "compose.preview.yml"
    preview_override.write_text(
        render_preview_override(manifest, image, secrets_file)
    )

    project = preview_project_name(project_id, short_hash)
    compose_args = [
        "docker", "compose",
        "-p", project,
        "-f", str(archive_dir / "compose.prod.yml"),
        "-f", str(preview_override),
    ]

    probe_succeeded = False
    try:
        smoke_log.append(f"▶ compose up ({project})\n")
        code = await _run_streaming(
            *compose_args, "up", "-d", "--wait",
            timeout=120, log_sink=smoke_log,
        )
        if code != 0:
            raise PublishError("preview compose up failed")

        # Probe the publish service from inside the project's default net.
        network = f"{project}_default"
        service = manifest.publish.service
        probe_cmd = [
            "docker", "run", "--rm",
            "--network", network,
            "curlimages/curl:latest",
            "curl", "-fsS", "-m", "5", "-o", "/dev/null",
            "-w", "HTTP %{http_code}\n",
            f"http://{service}:{manifest.publish.port}/",
        ]
        deadline = asyncio.get_event_loop().time() + timeout
        last_err = "never started probing"
        while asyncio.get_event_loop().time() < deadline:
            rc, out, err = await _run(*probe_cmd, timeout=15, check=False)
            if rc == 0:
                smoke_log.append(f"✓ smoke probe: {out.strip()}\n")
                probe_succeeded = True
                return
            last_err = (err or out).strip()
            await asyncio.sleep(2)
        raise PublishError(
            f"smoke probe never succeeded: {last_err}. "
            f"See the '{manifest.publish.service} container logs' section "
            f"in the build log for the actual crash reason."
        )
    finally:
        # On smoke failure, dump the user service's container logs into
        # smoke_log BEFORE tearing the preview down — the SSE stream
        # already surfaces smoke_log to the workspace Codex session, so
        # this is how the agent sees the actual crash reason (e.g.
        # "sh: 1: next: not found") instead of just the opaque curl error.
        # Best-effort; if docker logs itself errors, swallow.
        # Capture user-container logs best-effort. Wrap the entire block
        # (not just the _run) so nothing short-circuits the compose-down
        # below — a leaked preview network consumes a whole /20 of the
        # daemon's address pool until manually pruned.
        if not probe_succeeded:
            try:
                container = f"{project}-{manifest.publish.service}-1"
                smoke_log.append(
                    f"\n▶ captured tail of `{container}` container logs:\n"
                )
                _rc, out, err = await _run(
                    "docker", "logs", "--tail", "200", container,
                    timeout=10, check=False,
                )
                combined = (out or "") + (err or "")
                smoke_log.append(combined if combined else "(no logs captured)\n")
                smoke_log.append(
                    "\nHint: look for an exit code, stack trace, or "
                    "'command not found' line above — that's usually the real "
                    "root cause, not the curl/probe error.\n"
                )
            except Exception as exc:  # noqa: BLE001
                smoke_log.append(f"(log capture failed: {exc})\n")
        # Always tear down the preview, even on smoke failure, so the next
        # publish attempt doesn't collide on the compose project name or
        # leak the network's subnet.
        try:
            await _run(*compose_args, "down", "-v", "--remove-orphans", timeout=60, check=False)
        except Exception:
            pass


async def docker_push(tag: str, log_sink: list[str]) -> None:
    log_sink.append(f"▶ docker push {tag}\n")
    code = await _run_streaming("docker", "push", tag, timeout=600, log_sink=log_sink)
    if code != 0:
        raise PublishError(f"docker push failed (exit {code})")


async def promote(
    *,
    project_id: UUID,
    manifest: PolarisManifest,
    image: str,
    archive_dir: Path,
    secrets_file: Path,
    settings: Settings,
    log_sink: list[str],
) -> None:
    project_dir = publish_project_root(settings, project_id)
    project_dir.mkdir(parents=True, exist_ok=True)

    # Persist compose files alongside the archive for auditability /
    # out-of-band debugging (docker-compose.override by hand etc.).
    shutil.copy(archive_dir / "compose.prod.yml", project_dir / "compose.prod.yml")
    override_path = project_dir / "compose.polaris.yml"
    override_path.write_text(
        render_prod_override(
            project_id=project_id,
            manifest=manifest,
            image=image,
            secrets_file=secrets_file,
            traefik_public_network=settings.traefik_public_network_name,
            domain=project_domain(settings, project_id),
        )
    )

    compose_args = [
        "docker", "compose",
        "-p", compose_project_name(project_id),
        "-f", str(project_dir / "compose.prod.yml"),
        "-f", str(override_path),
    ]
    log_sink.append(f"▶ compose up ({compose_project_name(project_id)})\n")
    code = await _run_streaming(
        *compose_args, "up", "-d", "--wait",
        timeout=180, log_sink=log_sink,
    )
    if code != 0:
        raise PublishError("promote compose up failed — old version stays live")


# ─── Entry point ────────────────────────────────────────────────────────────


async def run_publish(
    *,
    session: AsyncSession,
    deployment_id: UUID,
    settings: Settings,
) -> None:
    """Main orchestrator. Loads the deployment row, walks pipeline stages,
    updates row status + logs. Catches all exceptions and reflects them on
    the row; does NOT re-raise (caller is an asyncio task)."""

    # Helper: persist status/log updates as we go so SSE can tail them.
    async def _update(dep: Deployment, **fields: object) -> None:
        for k, v in fields.items():
            setattr(dep, k, v)
        await session.commit()
        await session.refresh(dep)

    dep = await session.get(Deployment, deployment_id)
    if dep is None:
        logger.error("publish: deployment %s not found", deployment_id)
        return

    build_log: list[str] = []
    smoke_log: list[str] = []
    archive_parent: Path | None = None

    try:
        # Pre-flight: make sure the publish-projects root exists and is
        # writable.  Default `~/.polaris/projects` is user-land so this
        # should succeed; surfaces a clear error if someone set the env
        # var to a dir that needs sudo (e.g. `/srv/polaris-projects` on a
        # dev machine without a pre-created world-writable mount).
        publish_root = Path(settings.publish_projects_root)
        try:
            publish_root.mkdir(parents=True, exist_ok=True)
            probe = publish_root / ".polaris-writable-probe"
            probe.touch()
            probe.unlink()
        except PermissionError as exc:
            raise PublishError(
                f"publish projects root {publish_root} isn't writable by "
                f"the API process: {exc}. Set POLARIS_PUBLISH_PROJECTS_ROOT "
                "to a user-owned path (e.g. ~/.polaris/projects) or "
                "sudo-create + chown the current one."
            ) from exc
        except OSError as exc:
            raise PublishError(
                f"publish projects root {publish_root} unusable: {exc}"
            ) from exc

        project = await session.get(Project, dep.project_id)
        if project is None:
            raise PublishError("project not found")
        workspace = (
            await session.execute(
                select(Workspace)
                .where(Workspace.project_id == project.id)
                .order_by(Workspace.created_at.desc())
            )
        ).scalars().first()
        if workspace is None or not workspace.repo_path:
            raise PublishError("workspace not found for project")

        repo_path = Path(workspace.repo_path)
        if not repo_path.is_dir():
            raise PublishError(f"workspace missing on host: {repo_path}")

        # The git repo lives at the project root Codex reported via
        # `set_project_root` — NOT at the bind-mount root. Without that
        # signal we don't know where the project starts, so bail early
        # with a clear message.
        if not workspace.project_root:
            raise PublishError(
                "no project root yet — Codex hasn't called "
                "`set_project_root`. Ask the agent to scaffold your "
                "project first, then try publish again."
            )
        subdir = workspace.project_root.removeprefix("/workspace").lstrip("/")
        git_dir = repo_path / subdir if subdir else repo_path
        if not git_dir.is_dir() or not (git_dir / ".git").is_dir():
            raise PublishError(
                f"project root is not a git repo: {git_dir}. The "
                "`set_project_root` handler should have initialized one — "
                "try re-running a scaffold turn."
            )

        # If the user clicked Publish without ever running
        # `polaris scaffold-publish`, drop the stack-appropriate template
        # files in now — same logic the CLI uses.  Any files the user
        # already hand-wrote are respected.
        auto_scaffold_if_missing(
            git_dir, Path(settings.publish_templates_root), build_log,
        )

        # Publish = ship the current working tree. Auto-commit any
        # uncommitted changes so users don't have to remember to commit
        # before clicking Publish. No-op on clean trees.
        await _run("git", "-C", str(git_dir), "add", "-A", timeout=30)
        _, status_out, _ = await _run(
            "git", "-C", str(git_dir), "status", "--porcelain",
            timeout=10, check=False,
        )
        if status_out.strip():
            # Git refuses to commit without a configured identity. Guard
            # against dev machines that never ran `git config --global ...`.
            await _ensure_git_identity(git_dir)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            await _run(
                "git", "-C", str(git_dir), "commit",
                "-m", f"polaris: publish {stamp}",
                timeout=30,
            )

        _, short_hash_raw, _ = await _run(
            "git", "-C", str(git_dir), "rev-parse", "--short=12", "HEAD",
            timeout=10,
        )
        short_hash = short_hash_raw.strip()
        if not short_hash:
            raise PublishError(
                "HEAD has no commits even after auto-commit — workspace "
                "appears empty."
            )

        manifest = load_manifest(git_dir)

        image = image_tag(settings, project.id, short_hash)
        domain = project_domain(settings, project.id)
        await _update(dep, git_commit_hash=short_hash, image_tag=image, domain=domain)

        # Record a ProjectVersion if the hash is new.
        existing = (
            await session.execute(
                select(ProjectVersion).where(
                    ProjectVersion.project_id == project.id,
                    ProjectVersion.git_commit_hash == short_hash,
                )
            )
        ).scalars().first()
        if existing is None:
            existing = ProjectVersion(
                project_id=project.id,
                git_commit_hash=short_hash,
                title=f"publish {short_hash}",
                description=None,
                created_by_type="system",
            )
            session.add(existing)
            await session.flush()
        dep.project_version_id = existing.id
        await session.commit()

        # ── archive ────────────────────────────────────────────────────
        # The git repo IS the project root now — no post-extract subdir
        # narrowing needed. `archive_dir` is what docker build + smoke
        # + promote all use as their compose cwd.
        archive_parent = Path(tempfile.mkdtemp(prefix="polaris-publish-"))
        archive_dir = archive_parent / "src"
        await git_archive(git_dir, short_hash, archive_dir)

        # Also persist a frozen tarball on the prod side.
        project_dir = publish_project_root(settings, project.id)
        (project_dir / "archives").mkdir(parents=True, exist_ok=True)
        tarball = project_dir / "archives" / f"{short_hash}.tar.gz"
        rc, _, err = await _run(
            "sh", "-c",
            f'git -C "{git_dir}" archive --format=tar.gz "{short_hash}" > "{tarball}"',
            timeout=120, check=False,
        )
        if rc != 0:
            # Non-fatal — tarball is for audit, build proceeds from tmp dir.
            build_log.append(f"[warn] archive persist failed: {err.strip()}\n")

        # Strip any host-published ports from the user's compose.prod.yml
        # before anything downstream consumes it.  Traefik owns the host
        # ingress (80/443), so `ports:` in the user file would collide.
        # See services/publish.py::sanitize_prod_compose for policy.
        sanitize_prod_compose(archive_dir, build_log)

        # ── build ──────────────────────────────────────────────────────
        await _update(dep, status="building")
        await docker_build(archive_dir, image, build_log, settings.publish_build_timeout_seconds)
        await _update(dep, build_log="".join(build_log))

        # ── secrets (materialize before smoke — the preview override
        # points at this file as env_file) ─────────────────────────────
        secrets_file = materialize_secrets(project.id, settings, manifest)

        # ── smoke ──────────────────────────────────────────────────────
        await _update(dep, status="deploying")
        await smoke_test(
            project_id=project.id,
            short_hash=short_hash,
            archive_dir=archive_dir,
            manifest=manifest,
            image=image,
            secrets_file=secrets_file,
            smoke_log=smoke_log,
            timeout=settings.publish_smoke_timeout_seconds,
        )
        await _update(dep, smoke_log="".join(smoke_log))

        # ── push + promote ─────────────────────────────────────────────
        await docker_push(image, build_log)
        await promote(
            project_id=project.id,
            manifest=manifest,
            image=image,
            archive_dir=archive_dir,
            secrets_file=secrets_file,
            settings=settings,
            log_sink=build_log,
        )

        # Mark prior ready deployments as superseded.
        prior_ready = (
            await session.execute(
                select(Deployment).where(
                    Deployment.project_id == project.id,
                    Deployment.status == "ready",
                    Deployment.id != dep.id,
                )
            )
        ).scalars().all()
        for older in prior_ready:
            older.superseded_by_id = dep.id

        dep.status = "ready"
        dep.ready_at = datetime.now(UTC)
        dep.build_log = "".join(build_log)
        await session.commit()
        logger.info("publish: deployment %s ready at %s", dep.id, domain)
    except PublishError as exc:
        logger.warning("publish: deployment %s failed: %s", deployment_id, exc)
        dep.status = "failed"
        dep.error = str(exc)
        dep.build_log = "".join(build_log) if build_log else None
        dep.smoke_log = "".join(smoke_log) if smoke_log else None
        try:
            await session.commit()
        except Exception:
            await session.rollback()
    except Exception as exc:
        logger.exception("publish: deployment %s crashed", deployment_id)
        dep.status = "failed"
        dep.error = f"unexpected error: {exc}"
        try:
            await session.commit()
        except Exception:
            await session.rollback()
    finally:
        if archive_parent is not None:
            shutil.rmtree(archive_parent, ignore_errors=True)


# ─── Rollback ───────────────────────────────────────────────────────────────


async def run_rollback(
    *,
    session: AsyncSession,
    project_id: UUID,
    target_hash: str,
    triggered_by: str,
    settings: Settings,
) -> Deployment:
    """Find an earlier ready Deployment for `project_id` matching `target_hash`,
    create a new Deployment row, and redeploy its image_tag behind traefik.
    Rollback never rebuilds — the registry holds the old image."""

    target = (
        await session.execute(
            select(Deployment)
            .where(
                Deployment.project_id == project_id,
                Deployment.status == "ready",
            )
            .order_by(Deployment.created_at.desc())
        )
    ).scalars().all()
    match = next(
        (d for d in target if d.git_commit_hash and d.git_commit_hash.startswith(target_hash)),
        None,
    )
    if match is None:
        raise PublishError(
            f"no prior ready deployment found for commit starting with {target_hash!r}"
        )

    project = await session.get(Project, project_id)
    if project is None:
        raise PublishError("project not found")

    manifest_raw: Path | None = None
    archive_dir = publish_project_root(settings, project_id) / "archives" / (
        (match.git_commit_hash or "") + ".tar.gz"
    )
    if not archive_dir.exists():
        raise PublishError(
            f"rollback target tarball is missing: {archive_dir} — can't recover manifest"
        )

    # Extract the tarball to read its polaris.yaml and compose.prod.yml.
    tmp = Path(tempfile.mkdtemp(prefix="polaris-rollback-"))
    try:
        rc, _, err = await _run(
            "tar", "-xzf", str(archive_dir), "-C", str(tmp), timeout=60, check=False
        )
        if rc != 0:
            raise PublishError(f"rollback tar extract failed: {err}")

        manifest = load_manifest(tmp)
        secrets_file = materialize_secrets(project_id, settings, manifest)
        # No need to copy secrets_file next to tmp/compose.prod.yml —
        # the template no longer references `./.env.polaris.prod`; promote's
        # compose.polaris.yml override points at the absolute secrets_file.

        new_dep = Deployment(
            project_id=project_id,
            project_version_id=match.project_version_id,
            git_commit_hash=match.git_commit_hash,
            image_tag=match.image_tag,
            domain=match.domain,
            status="deploying",
        )
        session.add(new_dep)
        await session.flush()

        log: list[str] = [f"▶ rollback to {match.git_commit_hash}\n"]
        await promote(
            project_id=project_id,
            manifest=manifest,
            image=match.image_tag or "",
            archive_dir=tmp,
            secrets_file=secrets_file,
            settings=settings,
            log_sink=log,
        )

        # Chain: every earlier ready deployment points forward to this one.
        prior = (
            await session.execute(
                select(Deployment).where(
                    Deployment.project_id == project_id,
                    Deployment.status == "ready",
                    Deployment.id != new_dep.id,
                )
            )
        ).scalars().all()
        for older in prior:
            older.superseded_by_id = new_dep.id

        new_dep.status = "ready"
        new_dep.ready_at = datetime.now(UTC)
        new_dep.build_log = "".join(log)
        await session.commit()
        await session.refresh(new_dep)
        return new_dep
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

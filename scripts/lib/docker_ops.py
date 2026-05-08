"""Tiny wrappers around `docker` / `docker compose` for the CLI scripts.

We deliberately do NOT use the docker-py SDK — every operation maps 1:1
to a CLI command, and shelling out keeps the dependency surface small
(no need to ship libssh / docker-py wheels for what amounts to printf +
subprocess.run).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Sequence


def have(binary: str) -> bool:
    """True iff ``binary`` is on PATH."""
    return shutil.which(binary) is not None


def run(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = False,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess, streaming stdout/stderr live unless ``capture``.

    Returns a CompletedProcess (with .stdout populated when ``capture``).
    Raises CalledProcessError on non-zero exit when ``check=True``.
    """
    import os

    env = None
    if env_extra:
        env = {**os.environ, **env_extra}
    return subprocess.run(  # noqa: S603 — args is list, not shell
        list(args),
        cwd=cwd,
        check=check,
        text=True,
        capture_output=capture,
        env=env,
    )


def docker_daemon_up() -> bool:
    """``docker info`` exits 0 iff the daemon is reachable."""
    if not have("docker"):
        return False
    p = subprocess.run(  # noqa: S603, S607
        ["docker", "info"],
        capture_output=True,
        text=True,
    )
    return p.returncode == 0


def image_exists(tag: str) -> bool:
    """True iff ``tag`` is in the local image store."""
    p = subprocess.run(  # noqa: S603, S607
        ["docker", "image", "inspect", tag],
        capture_output=True,
        text=True,
    )
    return p.returncode == 0


def compose(
    compose_file: Path,
    *args: str,
    cwd: Path | None = None,
    check: bool = True,
    env_extra: dict[str, str] | None = None,
    env_file: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke ``docker compose -f <file> <args>``.

    ``env_file`` forwards as ``--env-file`` so YAML interpolation reads
    the per-mode `.env.<mode>` instead of the default `.env` (which
    doesn't exist when each mode owns its own env file).
    """
    cmd = ["docker", "compose", "-f", str(compose_file)]
    if env_file is not None:
        cmd.extend(["--env-file", str(env_file)])
    cmd.extend(args)
    return run(
        cmd,
        cwd=cwd or compose_file.parent,
        check=check,
        env_extra=env_extra,
    )


def list_polaris_runtime_containers() -> list[str]:
    """Names of containers spawned at runtime by api/worker (workspace,
    browser, published).  Filter on label so static service names from
    compose.dev.yaml never match."""
    p = subprocess.run(  # noqa: S603, S607
        [
            "docker",
            "ps",
            "--all",
            "--format",
            "{{.Names}}",
            "--filter",
            "label=polaris.runtime=workspace",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    primary = [n for n in p.stdout.splitlines() if n.strip()]
    # Fallback name-pattern match for older spawns predating the label.
    p2 = subprocess.run(  # noqa: S603, S607
        ["docker", "ps", "--all", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    legacy = [
        n
        for n in p2.stdout.splitlines()
        if n.startswith(("polaris-ws-", "polaris-br-", "polaris-pub-", "polaris-pvw-"))
        or n.endswith("-welcome-1")
        and n.startswith("polaris-")
    ]
    seen: set[str] = set()
    out: list[str] = []
    for name in [*primary, *legacy]:
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out

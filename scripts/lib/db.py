"""Resolve a host-reachable postgres URL for dev tooling.

The compose.dev.yaml binds postgres to a RANDOM loopback port (no fixed
host port — avoids collisions with any other postgres on the dev box).
Tools that need DB access from the host (seed.py, ad-hoc scripts, the
integration tests) can't hardcode 127.0.0.1:5432; instead they call
``asyncpg_url()`` which queries ``docker port`` for the live mapping.

Override is honoured for CI / non-compose scenarios:
    POLARIS_DATABASE_URL=postgresql+asyncpg://...  → used as-is (after
    stripping the SQLAlchemy dialect prefix).
"""
from __future__ import annotations

import os
import subprocess
from typing import Final

POSTGRES_CONTAINER: Final = "polaris-postgres-1"
DEFAULT_USER: Final = "root"
DEFAULT_PASSWORD: Final = "123456"
DEFAULT_DB: Final = "polaris"


class PostgresUnreachable(RuntimeError):
    """Couldn't determine where postgres is listening on the host."""


def _detect_via_docker() -> tuple[str, str] | None:
    """Run ``docker port`` and parse the IPv4 line.  Returns ``(host,
    port)`` or ``None`` if the container isn't running / no binding."""
    try:
        result = subprocess.run(  # noqa: S603, S607
            ["docker", "port", POSTGRES_CONTAINER, "5432"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    # Output format examples:
    #   "127.0.0.1:55432\n[::]:55432\n"
    #   "127.0.0.1:55432 -> 5432/tcp\n"  (some docker versions)
    for raw in result.stdout.splitlines():
        # Strip arrows / extras
        line = raw.split(" ")[0].strip()
        if not line:
            continue
        # Skip IPv6 ([::]:port) — prefer IPv4 mapping
        if line.startswith("["):
            continue
        if ":" not in line:
            continue
        host, port = line.rsplit(":", 1)
        if port.isdigit():
            return host or "127.0.0.1", port
    return None


def asyncpg_url(*, db: str | None = None) -> str:
    """Return a ``postgres://`` URL suitable for asyncpg.

    Order:
      1. ``POLARIS_DATABASE_URL`` env if set (dialect prefix stripped)
      2. ``docker port polaris-postgres-1 5432`` discovery
      3. Raise ``PostgresUnreachable``.
    """
    if override := os.environ.get("POLARIS_DATABASE_URL"):
        return override.replace("postgresql+asyncpg://", "postgres://", 1)

    discovered = _detect_via_docker()
    if discovered is None:
        raise PostgresUnreachable(
            f"couldn't detect a host port for {POSTGRES_CONTAINER}.  "
            "Is `docker compose -f compose.dev.yaml up -d postgres` running?  "
            "Override with POLARIS_DATABASE_URL if you have postgres running outside compose."
        )
    host, port = discovered
    name = db or DEFAULT_DB
    return f"postgres://{DEFAULT_USER}:{DEFAULT_PASSWORD}@{host}:{port}/{name}"

"""Repo-root resolution + per-mode file lookups for the dev tooling.

The CLIs (``scripts/{build,up,down}.py``) are always invoked from the
repo root via ``uv run scripts/X.py`` or ``./scripts/X.py``, but we want
them to also work if someone runs them from anywhere else.  Resolve the
repo root by walking up from this file:

    scripts/lib/paths.py
       ^      ^
       parents[0] = lib
       parents[1] = scripts
       parents[2] = <repo root>

``mode`` is the deployment flavour.  Two are supported:

  * ``dev``   — the day-to-day local-loop stack (Vite HMR, uvicorn
                ``--reload``, source bind-mounted into api/worker/web).
  * ``stage`` — single-host self-hosted deploy (nginx-served static web
                bundle, uvicorn workers, no HMR).  Assumes a team dev
                machine or a host behind an external firewall — see the
                banner at the top of ``compose.stage.yaml``.

dev / stage carry independent ``.env.<mode>`` files and independent
named volumes (``polaris-postgres-data`` vs ``polaris-stage-postgres-data``)
so two stacks on one host don't clobber each other.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

MODES: tuple[str, ...] = ("dev", "stage")


def _check_mode(mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")


def env_file(mode: str = "dev") -> Path:
    """Path to ``.env.<mode>`` at the repo root."""
    _check_mode(mode)
    return REPO_ROOT / f".env.{mode}"


def compose_file(mode: str = "dev") -> Path:
    """Path to ``compose.<mode>.yaml`` — deliberately non-default name
    so docker compose's auto-discovery doesn't pick it up unprompted."""
    _check_mode(mode)
    return REPO_ROOT / f"compose.{mode}.yaml"


def data_root() -> Path:
    """Where workspace state lives.  Always under the repo so a
    ``mv repo`` carries the data with it."""
    return REPO_ROOT / ".data"

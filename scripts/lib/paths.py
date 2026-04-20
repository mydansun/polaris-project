"""Repo-root resolution for the dev tooling scripts.

The CLIs (``scripts/{build,up,down}.py``) are always invoked from the
repo root via ``uv run scripts/X.py`` or ``./scripts/X.py``, but we want
them to also work if someone runs them from anywhere else.  Resolve the
repo root by walking up from this file:

    scripts/lib/paths.py
       ^      ^
       parents[0] = lib
       parents[1] = scripts
       parents[2] = <repo root>
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[2]


def env_file() -> Path:
    """Path to the canonical ``.env`` (one per repo, at the root)."""
    return REPO_ROOT / ".env"


def compose_file() -> Path:
    """Path to ``compose.dev.yaml`` — deliberately non-default name."""
    return REPO_ROOT / "compose.dev.yaml"


def data_root() -> Path:
    """Where workspace state lives.  Always under the repo so a
    ``mv repo`` carries the data with it."""
    return REPO_ROOT / ".data"

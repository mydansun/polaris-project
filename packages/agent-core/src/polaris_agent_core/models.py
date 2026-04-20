"""Shared contracts for polaris agent-core.

Now minimal: the only shared contract the worker needs here is the
``AppRuntime`` enum (still referenced by frontend via shared-types).  Once
``detect_app_server`` moved fully inside the workspace container (Codex
reads package.json etc. itself), the WorkspaceCommand* protocol was
dropped — Codex's built-in exec_command replaces our host-side audited
transport.
"""
from __future__ import annotations

from enum import StrEnum


class AppRuntime(StrEnum):
    VITE = "vite"
    NEXT = "next"
    NODE_GENERIC = "node_generic"
    UVICORN = "uvicorn"
    FLASK = "flask"
    DJANGO = "django"
    UNKNOWN = "unknown"

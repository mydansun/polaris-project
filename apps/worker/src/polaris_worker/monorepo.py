from __future__ import annotations

import sys
from pathlib import Path


def ensure_monorepo_python_paths() -> None:
    # The repo does not yet have a unified Python workspace, so the worker needs
    # a local import shim to reach sibling packages during monorepo execution.
    repo_root = Path(__file__).resolve().parents[4]
    sibling_src_paths = [
        repo_root / "apps" / "api" / "src",
        repo_root / "packages" / "agent-core" / "src",
    ]
    for src_path in reversed(sibling_src_paths):
        src_path_str = str(src_path)
        if src_path_str not in sys.path:
            sys.path.insert(0, src_path_str)

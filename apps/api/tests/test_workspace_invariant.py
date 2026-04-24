"""Guard tests for the "empty workspace after init" invariant.

This invariant is load-bearing: in-place scaffolders (`npm create vite .`,
`create-react-app .`, `vite create --inplace .`, …) refuse to run when
the cwd contains ANYTHING — including `.git`.  Any file written at
`initialize_workspace` time — .gitignore, README, LICENSE, templates,
or even a `git init` — silently breaks those scaffolders on a fresh
project's first turn.

If you are tempted to add such a write here, STOP.  The correct venue is
`apps/worker/src/polaris_worker/runner.py::_ensure_project_git` (or the
baseline-gitignore helper in `services/gitignore_baseline.py`) which
fires inside the `set_project_root` dynamic-tool handler, after Codex
has declared the real project root.

Run:
    cd apps/api && .venv/bin/pytest tests/test_workspace_invariant.py -v
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from polaris_api.services.workspaces import (
    WorkspaceError,
    assert_workspace_bare,
    initialize_workspace,
)


def test_assert_workspace_bare_accepts_empty_dir(tmp_path: Path) -> None:
    # Should not raise on a brand-new empty directory.
    assert_workspace_bare(tmp_path)


def test_assert_workspace_bare_rejects_extra_file(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("node_modules/\n")
    with pytest.raises(WorkspaceError, match="workspace invariant violated"):
        assert_workspace_bare(tmp_path)


def test_assert_workspace_bare_rejects_extra_dir(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    with pytest.raises(WorkspaceError, match="workspace invariant violated"):
        assert_workspace_bare(tmp_path)


def test_assert_workspace_bare_rejects_pre_seeded_git(tmp_path: Path) -> None:
    """Even `.git/` is forbidden: git init now runs in set_project_root,
    not here.  The test is the tripwire for anyone who thinks "oh I'll
    just put .git back the way it used to be" — the system has evolved
    past that, don't undo it."""
    (tmp_path / ".git").mkdir()
    with pytest.raises(WorkspaceError, match="workspace invariant violated"):
        assert_workspace_bare(tmp_path)


def test_assert_workspace_bare_rejects_missing_dir(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    with pytest.raises(WorkspaceError, match="workspace missing"):
        assert_workspace_bare(missing)


def test_initialize_workspace_leaves_empty_dir(tmp_path: Path) -> None:
    """End-to-end: after a successful init the repo MUST be EMPTY (no
    `.git`, no anything).  If this test starts failing after a refactor,
    something has re-introduced pre-scaffold seeding.  Move it into the
    `set_project_root` handler and restore this invariant."""
    repo = tmp_path / "repo"
    result = asyncio.run(initialize_workspace(repo))
    assert result.commit_hash is None
    assert result.branch == "main"
    entries = sorted(p.name for p in repo.iterdir())
    assert entries == [], (
        f"initialize_workspace must leave an EMPTY dir, found: {entries!r}. "
        "If you need to seed files (or run `git init`) for a new feature, "
        "do it in the set_project_root dynamic-tool handler instead."
    )

"""Tests for ``_normalize_project_root`` — the validator behind the
codex ``set_project_root`` dynamic tool.

Anything that escapes ``/workspace`` (via ``..``, absolute paths
elsewhere, weird whitespace) must be rejected: the IDE iframe and
file watcher key off this value, and a path outside the bind-mount
silently breaks both.
"""

from __future__ import annotations

import pytest

from polaris_worker.agents.codex import _normalize_project_root


# ── Accepted shapes ─────────────────────────────────────────────────────


def test_accepts_workspace_root_exactly():
    assert _normalize_project_root("/workspace") == "/workspace"


def test_accepts_subdirectory():
    assert _normalize_project_root("/workspace/myapp") == "/workspace/myapp"


def test_accepts_nested_subdirectory():
    assert _normalize_project_root("/workspace/a/b/c") == "/workspace/a/b/c"


def test_normalizes_redundant_separators():
    # Codex sometimes hands back ``//`` between components — accept
    # but normalize.  This is the path that gets iframe'd, so it
    # needs to be cosmetically clean too.
    assert _normalize_project_root("/workspace//myapp") == "/workspace/myapp"


def test_normalizes_trailing_slash():
    assert _normalize_project_root("/workspace/myapp/") == "/workspace/myapp"


def test_normalizes_dot_segments():
    # ``/workspace/./foo`` → ``/workspace/foo`` — fine, still under
    # /workspace.
    assert _normalize_project_root("/workspace/./foo") == "/workspace/foo"


def test_normalizes_safe_double_dot_segments():
    # ``/workspace/foo/../bar`` resolves to ``/workspace/bar`` —
    # still under the bind-mount, accept.
    assert _normalize_project_root("/workspace/foo/../bar") == "/workspace/bar"


# ── Rejected shapes ─────────────────────────────────────────────────────


def test_rejects_dotdot_escape():
    # The whole point of normalization: ``/workspace/../etc`` would
    # silently iframe ``/etc``.  Must reject.
    assert _normalize_project_root("/workspace/../etc") is None


def test_rejects_workspace_parent():
    # Even the implicit parent: ``/workspace/..`` resolves to ``/``.
    assert _normalize_project_root("/workspace/..") is None


def test_rejects_unrelated_absolute_path():
    assert _normalize_project_root("/etc/passwd") is None
    assert _normalize_project_root("/root") is None


def test_rejects_relative_path():
    # Codex's contract is "absolute path under /workspace"; relative
    # paths happen when the agent forgets and they'd be interpreted
    # against an unknown cwd.
    assert _normalize_project_root("workspace") is None
    assert _normalize_project_root("./workspace") is None
    assert _normalize_project_root("myapp") is None


def test_rejects_workspace_prefix_lookalike():
    # ``/workspace2`` and ``/workspaces`` are NOT under ``/workspace``
    # — the str check must guard against prefix-only matches.
    assert _normalize_project_root("/workspace2") is None
    assert _normalize_project_root("/workspaces/foo") is None
    assert _normalize_project_root("/workspaceX") is None


def test_rejects_empty_and_whitespace():
    assert _normalize_project_root("") is None
    # posixpath.normpath("   ") is "   " — normpath doesn't strip
    # whitespace, so the prefix check correctly rejects.
    assert _normalize_project_root("   ") is None


def test_rejects_non_string_inputs():
    # The handler is invoked from JSON-RPC where the type can't be
    # statically guaranteed — guard against ints / dicts / None.
    assert _normalize_project_root(None) is None  # type: ignore[arg-type]
    assert _normalize_project_root(123) is None  # type: ignore[arg-type]
    assert _normalize_project_root({"path": "/workspace"}) is None  # type: ignore[arg-type]
    assert _normalize_project_root([]) is None  # type: ignore[arg-type]


def test_rejects_root_alone():
    # ``/`` is suspicious — not under /workspace.
    assert _normalize_project_root("/") is None


@pytest.mark.parametrize(
    "p",
    [
        "/workspace/foo/../../etc",  # too many ..; escapes
        "/workspace/../workspace/foo",  # round-trips; technically fine
    ],
)
def test_dotdot_only_accepted_when_resolved_path_stays_under_workspace(p: str):
    # First case escapes — must reject.  Second resolves back to
    # /workspace/foo — must accept.  This documents the contract:
    # we don't ban ``..`` syntactically, we ban escape SEMANTICS.
    out = _normalize_project_root(p)
    if p.startswith("/workspace/foo/../../"):
        assert out is None
    else:
        assert out == "/workspace/foo"

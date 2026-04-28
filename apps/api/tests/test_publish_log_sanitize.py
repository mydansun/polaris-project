"""Unit tests for ``publish._sanitize_log``.

Distinct from ``test_publish_sanitize.py`` (which covers the YAML port
stripper).  This helper rewrites host-side workspace paths to
``/workspace`` before logs are exposed in chat — a single missed call
site leaks the platform's host filesystem layout, so these tests
guard the substitution and its no-op edges.
"""

from __future__ import annotations

from pathlib import Path

from polaris_api.services.publish import _sanitize_log


def test_replaces_host_path_with_workspace_alias():
    repo = Path("/home/sun/.data/workspaces/u123/myslug")
    text = "build failed in /home/sun/.data/workspaces/u123/myslug/src/index.ts"
    out = _sanitize_log(text, repo)
    assert out == "build failed in /workspace/src/index.ts"


def test_replaces_every_occurrence():
    repo = Path("/srv/work")
    text = "cd /srv/work && npm run build (cwd=/srv/work)"
    out = _sanitize_log(text, repo)
    # Both occurrences must be rewritten — leaks happen one-at-a-time
    # otherwise.
    assert out == "cd /workspace && npm run build (cwd=/workspace)"
    assert "/srv/work" not in out


def test_strips_trailing_slash_from_repo_path():
    # Path("/srv/work/") normalizes the trailing slash off the str()
    # form, but defensive callers sometimes hand us a plain str — the
    # helper rstrips "/" before matching so both shapes work.
    repo = Path("/srv/work/")
    out = _sanitize_log("error at /srv/work/file.ts", repo)
    assert out == "error at /workspace/file.ts"


def test_returns_input_when_path_not_present():
    repo = Path("/elsewhere")
    text = "no host path mentioned here"
    assert _sanitize_log(text, repo) == text


def test_none_text_passes_through():
    assert _sanitize_log(None, Path("/anything")) is None


def test_none_repo_path_skips_substitution():
    # Some publish call sites lack a repo path (early failures before
    # the workspace dir is resolved); the helper must not crash and
    # must leave the text intact.
    text = "raw error with /home/sun/in/it"
    assert _sanitize_log(text, None) == text

"""Lint-style tests that fail when known-bad literals creep back in.

These tests scan source files (NOT docs) under the application code
paths.  They protect the post-Stage-1 invariants:

  * No hardcoded ``host.docker.internal:8000`` — workspace + api now
    talk via the ``api`` service DNS on the shared polaris-shared
    network.  The ONE legitimate use is `extra_hosts: host-gateway`
    aliasing inside generated workspace compose YAML (so legacy
    overrides keep working) — that's an inert symbol, not a URL.
  * No hardcoded ``polaris-dev.xyz`` outside (a) the
    ``apps/api/src/polaris_api/config.py`` defaults, (b) tests, (c)
    docs.  Routing must template ``${POLARIS_DOMAIN}``.
  * No ``Path.home()`` in production source — tooling-config paths
    must be supplied via env so containerized api works.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from lib import paths


REPO = paths.REPO_ROOT


def _iter_source_files(roots: list[str], exts: list[str]) -> list[Path]:
    out: list[Path] = []
    for root in roots:
        base = REPO / root
        if not base.exists():
            continue
        for ext in exts:
            out.extend(base.rglob(f"*.{ext}"))
    # Filter out test-tree, vendored, and build artifacts.
    excluded = ("tests/", "/tests/", "node_modules/", ".venv/", "__pycache__/")
    return [
        p
        for p in out
        if not any(seg in str(p) for seg in excluded)
    ]


_COMMENT_LEADING = re.compile(r"^\s*(#|//)")


def _is_comment_line(line: str, suffix: str) -> bool:
    """Best-effort comment detection.  Doesn't try to parse multi-line
    docstrings — if a hit happens inside a triple-quoted string, the
    test will flag it (and the fix is usually to phrase the docstring
    without the literal).  The goal here is to skip *line comments*
    that are inevitable in real code (override examples, regression
    notes, etc.)."""
    if _COMMENT_LEADING.match(line):
        return True
    return False


def _scan_for(needle: str, files: list[Path]) -> list[tuple[Path, int, str]]:
    hits: list[tuple[Path, int, str]] = []
    pattern = re.compile(re.escape(needle))
    for p in files:
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        in_triple_string = False
        triple_marker = ""
        for ln, line in enumerate(text.splitlines(), 1):
            # Cheap triple-quoted string tracker — toggles at every
            # ``"""`` or ``'''`` we see.  Misses some edge cases (one-
            # line triple-strings) but those don't typically contain
            # the literals we care about.
            for marker in ('"""', "'''"):
                count = line.count(marker)
                if count == 0:
                    continue
                if not in_triple_string:
                    in_triple_string = True
                    triple_marker = marker
                elif marker == triple_marker:
                    if count % 2 == 1:
                        in_triple_string = False
            if in_triple_string:
                continue
            if _is_comment_line(line, p.suffix):
                continue
            if pattern.search(line):
                hits.append((p, ln, line.strip()))
    return hits


# ── host.docker.internal:8000 — only in legacy compose.yaml + workspace
#    compose extra_hosts (alias, harmless) ─────────────────────────────


def test_no_host_docker_internal_8000_url_in_python():
    """A URL like http://host.docker.internal:8000 in production .py
    code is a regression; it should be ``http://api:8000`` now.

    Workspace compose YAML rendering still emits ``host.docker.internal:host-gateway``
    in extra_hosts (as a legacy fallback for users who override
    POLARIS_API_URL_FOR_WORKSPACE), but never as part of a ``http://``
    URL — that's what we scan for here."""
    files = _iter_source_files(["apps", "packages"], ["py"])
    bad = _scan_for("http://host.docker.internal:8000", files)
    assert not bad, "found host.docker.internal:8000 URL: " + "\n".join(
        f"{p}:{ln}: {line}" for p, ln, line in bad
    )


def test_no_host_docker_internal_8000_url_in_traefik_dynamic():
    files = list((REPO / "infra" / "traefik" / "dynamic").rglob("*.yaml"))
    bad = _scan_for("http://host.docker.internal:8000", files)
    assert not bad, "found host.docker.internal:8000 URL in traefik dynamic config: " + "\n".join(
        f"{p}:{ln}: {line}" for p, ln, line in bad
    )


# ── polaris-dev.xyz — only in: config defaults, .env.example, docs/, tests, README ──


def test_polaris_dev_xyz_only_in_allowed_locations():
    """Find polaris-dev.xyz literals in production source.

    Allowed: config.py defaults (override knob), README/docs (human
    text), .env.example (template), tests (intentional fixtures),
    git-managed cli script wizards (default suggestion only).
    """
    # Scan all production source code (apps + packages + infra,
    # excluding tests/docs).
    files = _iter_source_files(
        ["apps", "packages"], ["py", "ts", "tsx", "js", "json"]
    )
    files += list((REPO / "infra").rglob("*.yaml"))
    files += list((REPO / "infra").rglob("*.toml"))
    files += list((REPO / "infra").rglob("*.py"))
    # Test scaffolds (Playwright config, e2e specs) are allowed to
    # carry the dev domain as a fallback default — they're tooling,
    # not prod source.
    files = [p for p in files if "playwright.config" not in str(p) and "/tests/" not in str(p) and "/e2e/" not in str(p)]

    allowed_files = {
        # Default values — explicit env override knob makes these fine
        REPO / "apps" / "api" / "src" / "polaris_api" / "config.py",
        REPO / "packages" / "design-intent" / "src" / "polaris_design_intent" / "config.py",
        # Static template — workspace's polaris CLI default URL was
        # already updated; if a polaris-dev.xyz literal lives here, it's
        # a regression.  Keep the file out of `allowed`.
    }

    bad: list[tuple[Path, int, str]] = []
    for p, ln, line in _scan_for("polaris-dev.xyz", files):
        # Skip allowed files
        if p in allowed_files:
            continue
        # Skip test trees just in case our excluder missed something.
        if "tests/" in str(p) or "/tests/" in str(p):
            continue
        bad.append((p, ln, line))
    assert not bad, (
        "polaris-dev.xyz literal in prod source — should template ${POLARIS_DOMAIN}: "
        + "\n".join(f"{p}:{ln}: {line}" for p, ln, line in bad)
    )


# ── Path.home() in prod source ────────────────────────────────────────────


def test_no_path_home_in_prod_python():
    files = _iter_source_files(["apps", "packages"], ["py"])
    bad = _scan_for("Path.home()", files)
    assert not bad, (
        "Path.home() in prod code — host paths must come via env, not "
        "the api container's HOME: "
        + "\n".join(f"{p}:{ln}: {line}" for p, ln, line in bad)
    )

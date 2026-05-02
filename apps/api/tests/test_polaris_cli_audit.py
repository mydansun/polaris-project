"""Unit tests for the workspace-side polaris-cli static audit.

The CLI is a standalone single-file script (not a package), so we load
it by path via importlib.  This keeps the script-form deployment shape
intact while still giving us pytest coverage of the pure-function bits.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLI_PATH = _REPO_ROOT / "infra/workspace/polaris-cli/polaris.py"


@pytest.fixture(scope="module")
def polaris_cli():
    spec = importlib.util.spec_from_file_location("polaris_cli", _CLI_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so the script's ``import re`` inside functions
    # doesn't shadow anything.
    sys.modules["polaris_cli"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── _check_bare_node_bins ────────────────────────────────────────────────


def test_detects_bare_next(polaris_cli):
    errs = polaris_cli._check_bare_node_bins(
        "npx prisma db push && npm run db:bootstrap && next start --hostname 0.0.0.0"
    )
    assert errs
    assert "next" in errs[0]
    assert "exit 127" in errs[0]


def test_allows_npm_start(polaris_cli):
    assert polaris_cli._check_bare_node_bins("npm start") == []


def test_allows_npm_run_and_chained(polaris_cli):
    assert polaris_cli._check_bare_node_bins(
        "npx prisma migrate deploy && npm run db:seed && npm start"
    ) == []


def test_allows_npx_next(polaris_cli):
    assert polaris_cli._check_bare_node_bins(
        "npx next start --hostname 0.0.0.0 --port 3000"
    ) == []


def test_allows_explicit_path(polaris_cli):
    assert polaris_cli._check_bare_node_bins(
        "./node_modules/.bin/next start --port 3000"
    ) == []


def test_detects_bare_vite(polaris_cli):
    errs = polaris_cli._check_bare_node_bins("vite preview --port 4173")
    assert errs and "vite" in errs[0]


def test_detects_bare_tsx(polaris_cli):
    errs = polaris_cli._check_bare_node_bins("tsx server.ts")
    assert errs and "tsx" in errs[0]


def test_allows_env_prefix_then_npm(polaris_cli):
    assert polaris_cli._check_bare_node_bins(
        "NODE_ENV=production PORT=3000 npm start"
    ) == []


def test_ignores_pure_shell(polaris_cli):
    assert polaris_cli._check_bare_node_bins(
        "sleep 5 && echo ok && node server.js"
    ) == []


def test_empty_start_is_ok(polaris_cli):
    assert polaris_cli._check_bare_node_bins("") == []
    assert polaris_cli._check_bare_node_bins("   ") == []


# ── _audit_polaris_yaml ──────────────────────────────────────────────────


def _write_manifest(tmp_path: Path, yaml_body: str) -> None:
    (tmp_path / "polaris.yaml").write_text(yaml_body)


def test_audit_fails_on_bare_bin_for_node_stack(polaris_cli, tmp_path):
    _write_manifest(
        tmp_path,
        """\
version: 1
stack: node
build: "npm run build"
start: "next start --hostname 0.0.0.0 --port 3000"
port: 3000
publish:
  service: web
  port: 3000
""",
    )
    errs = polaris_cli._audit_polaris_yaml(tmp_path)
    assert errs and "next" in errs[0]


def test_audit_passes_on_npm_start(polaris_cli, tmp_path):
    _write_manifest(
        tmp_path,
        """\
version: 1
stack: node
build: "npm run build"
start: "npm start"
port: 3000
publish:
  service: web
  port: 3000
""",
    )
    assert polaris_cli._audit_polaris_yaml(tmp_path) == []


def test_audit_skips_python_stack(polaris_cli, tmp_path):
    # "next" here would be nonsense for python but the rule shouldn't
    # fire — we only scan node / spa.
    _write_manifest(
        tmp_path,
        """\
version: 1
stack: python
build: ""
start: "python -m uvicorn app:app --host 0.0.0.0 --port 8000"
port: 8000
publish:
  service: web
  port: 8000
""",
    )
    assert polaris_cli._audit_polaris_yaml(tmp_path) == []


def test_audit_missing_manifest_is_silent(polaris_cli, tmp_path):
    # Publish path has its own missing-manifest handling; the audit
    # shouldn't double-yell.
    assert polaris_cli._audit_polaris_yaml(tmp_path) == []


def test_audit_reports_invalid_yaml(polaris_cli, tmp_path):
    (tmp_path / "polaris.yaml").write_text("::: not yaml :::")
    errs = polaris_cli._audit_polaris_yaml(tmp_path)
    assert errs and "not valid YAML" in errs[0]

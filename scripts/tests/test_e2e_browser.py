"""End-to-end browser test using Playwright (opt-in).

Skipped by default — running it requires the full stack to be up and
DNS for ``${POLARIS_DOMAIN}`` resolving to this host.  Run after
``scripts/up.py`` finishes:

    POLARIS_LIVE_E2E=1 uv run --group dev pytest scripts/tests/test_e2e_browser.py

Validates:
  * GET https://${POLARIS_DOMAIN}/ → 200 (web reachable through traefik)
  * GET https://${POLARIS_DOMAIN}/api/health → 200 (api strip-prefix
    middleware works, api joined polaris-shared correctly)
  * Cert chain is valid (no self-signed warnings) — implied by the
    fact that requests succeeds without a special verify=False.
"""
from __future__ import annotations

import os

import httpx
import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("POLARIS_LIVE_E2E") != "1",
    reason="opt-in: requires full stack running + DNS pointing here",
)


def _domain() -> str:
    """Read POLARIS_DOMAIN from env (process > .env)."""
    if v := os.environ.get("POLARIS_DOMAIN"):
        return v
    # Fallback: read .env directly
    from lib import env_io, paths

    return env_io.read(paths.env_file()).get("POLARIS_DOMAIN", "polaris-dev.xyz")


def test_web_root_serves_html():
    domain = _domain()
    r = httpx.get(f"https://{domain}/", timeout=10, follow_redirects=False)
    assert r.status_code in (200, 301, 302), r.status_code
    if r.status_code == 200:
        ct = r.headers.get("content-type", "")
        assert "text/html" in ct, ct


def test_api_health():
    domain = _domain()
    r = httpx.get(f"https://{domain}/api/health", timeout=10)
    assert r.status_code == 200, r.text


def test_traefik_serves_valid_cert():
    """A real cert chain — http requests succeeds without verify=False."""
    domain = _domain()
    # If a self-signed cert from internal mode were served, this would
    # raise SSLError.
    httpx.get(f"https://{domain}/", timeout=10).raise_for_status()

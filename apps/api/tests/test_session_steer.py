"""Tests for POST /api/sessions/{id}/steer — the mid-turn steer route
that lets the user inject additional input into a still-running codex
turn.

Live integration — gated on POLARIS_LIVE_E2E=1 because we exercise
the real ``run_quota`` + redis pubsub path.
"""
from __future__ import annotations

import os
import secrets
import uuid

import httpx
import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("POLARIS_LIVE_E2E") != "1",
    reason="opt-in: requires running compose stack",
)


@pytest.fixture
def base_url() -> str:
    return os.environ.get("POLARIS_E2E_BASE_URL", "https://polaris-dev.xyz")


@pytest.fixture
def authed_client(base_url: str):
    client = httpx.Client(verify=True, timeout=20)
    r = client.get(f"{base_url}/api/auth/dev-login", follow_redirects=False)
    assert r.status_code in (302, 303)
    yield client
    client.close()


def _create_project(client: httpx.Client, base_url: str) -> str:
    name = f"polaris-spec-steer-{secrets.token_hex(4)}"
    r = client.post(f"{base_url}/api/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_steer_requires_authentication(base_url: str) -> None:
    """Unauthenticated steer → 401, not 404 — leaking session
    existence to anonymous callers would be a minor info disclosure."""
    fake = uuid.uuid4()
    with httpx.Client(verify=True, timeout=10) as c:
        r = c.post(f"{base_url}/api/sessions/{fake}/steer", json={"message": "hi"})
    assert r.status_code == 401


def test_steer_404s_for_unknown_session(
    base_url: str, authed_client: httpx.Client
) -> None:
    fake = uuid.uuid4()
    r = authed_client.post(
        f"{base_url}/api/sessions/{fake}/steer", json={"message": "hi"}
    )
    assert r.status_code == 404


def test_steer_409s_for_terminal_session(
    base_url: str, authed_client: httpx.Client
) -> None:
    """Steering a session that's already completed/failed/interrupted
    must 409 — frontend treats this as "the turn finished while you
    were typing" and can fall back to creating a fresh session."""
    pid = _create_project(authed_client, base_url)
    try:
        # Create a session via the sessions endpoint.  We immediately
        # interrupt it so it lands in a terminal state — exercising the
        # 409 branch without needing codex to actually run.
        cs = authed_client.post(
            f"{base_url}/api/projects/{pid}/sessions",
            json={"message": "stub", "mode": "discover_then_build"},
        )
        assert cs.status_code in (200, 201), cs.text
        sid = cs.json()["id"]

        # Interrupt → terminal.
        ir = authed_client.post(f"{base_url}/api/sessions/{sid}/interrupt")
        assert ir.status_code == 200, ir.text

        # Steer → must 409 with a message that names the terminal status.
        sr = authed_client.post(
            f"{base_url}/api/sessions/{sid}/steer", json={"message": "extra"}
        )
        assert sr.status_code == 409, sr.text
        body = sr.json()
        assert "interrupted" in str(body.get("detail", "")).lower()
    finally:
        # Cascade-delete cleans up the session row + workspace + dev deps.
        authed_client.delete(f"{base_url}/api/projects/{pid}")

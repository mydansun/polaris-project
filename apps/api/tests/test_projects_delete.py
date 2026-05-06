"""Tests for DELETE /api/projects/{id} — the project drawer's trash button.

Live integration — gated on ``POLARIS_LIVE_E2E=1`` because we mutate
real DB rows + tear down compose stacks.  Tests create a fresh project
(named ``__delete-test-<random>``) so they never touch user data, and
delete it at the end so re-runs stay green even on a freshly-seeded
stack.
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
    """Authenticated httpx.Client via dev-login."""
    client = httpx.Client(verify=True, timeout=20)
    r = client.get(f"{base_url}/api/auth/dev-login", follow_redirects=False)
    assert r.status_code in (302, 303), r.status_code
    yield client
    client.close()


def _create_project(client: httpx.Client, base_url: str) -> str:
    # Prefix avoids leading underscores: a literal ``_`` in SQL LIKE is
    # a single-char wildcard, which has burned us in ad-hoc cleanup
    # queries that matched every project.  Use ``polaris-spec-`` instead.
    name = f"polaris-spec-delete-test-{secrets.token_hex(4)}"
    r = client.post(f"{base_url}/api/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_delete_project_removes_it_from_the_list(
    base_url: str, authed_client: httpx.Client
) -> None:
    pid = _create_project(authed_client, base_url)

    # It's in the list right after create.
    r = authed_client.get(f"{base_url}/api/projects")
    assert r.status_code == 200
    assert pid in {p["id"] for p in r.json()}

    # Delete returns 204.
    r = authed_client.delete(f"{base_url}/api/projects/{pid}")
    assert r.status_code == 204, r.text
    assert r.content == b""

    # And it's gone from the list.
    r = authed_client.get(f"{base_url}/api/projects")
    assert pid not in {p["id"] for p in r.json()}


def test_delete_project_404_when_not_found(
    base_url: str, authed_client: httpx.Client
) -> None:
    fake_id = uuid.uuid4()
    r = authed_client.delete(f"{base_url}/api/projects/{fake_id}")
    assert r.status_code == 404, r.text


def test_delete_project_subsequent_get_404(
    base_url: str, authed_client: httpx.Client
) -> None:
    """After delete, the per-project detail endpoint also 404s — guards
    against stale orphan rows masking the cascade bug."""
    pid = _create_project(authed_client, base_url)
    authed_client.delete(f"{base_url}/api/projects/{pid}")
    r = authed_client.get(f"{base_url}/api/projects/{pid}")
    assert r.status_code == 404


def test_delete_project_idempotent_on_second_call(
    base_url: str, authed_client: httpx.Client
) -> None:
    """Deleting the same project twice — second call returns 404, not
    500 — so the frontend can retry on flaky network without poisoning
    the user with a server error."""
    pid = _create_project(authed_client, base_url)
    r = authed_client.delete(f"{base_url}/api/projects/{pid}")
    assert r.status_code == 204
    r = authed_client.delete(f"{base_url}/api/projects/{pid}")
    assert r.status_code == 404


def test_delete_project_unauthenticated_returns_401(base_url: str) -> None:
    """No session cookie → 401 (not 404 — leaking project existence is
    a minor info-disclosure)."""
    fake_id = uuid.uuid4()
    with httpx.Client(verify=True, timeout=10) as c:
        r = c.delete(f"{base_url}/api/projects/{fake_id}")
    assert r.status_code == 401

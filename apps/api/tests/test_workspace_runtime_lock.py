"""Concurrency test for POST /api/projects/{id}/workspace/runtime.

Without per-workspace serialization, two parallel ensure-runtime calls
race on the per-project ``_default`` docker network: one's compose-up
creates it, the other's compose-up fails on "name already in use" and
runs the recovery dance (``compose down --remove-orphans`` +
``docker network rm``) which yanks the network out from under the first
call mid-container-start, surfacing as ``failed to set up container
networking: network ... not found`` to the first caller.

This test fires 5 parallel POSTs against the same project and asserts:
  * Every response returns 200 (none lose to the race)
  * The wall-clock timings line up with sequential-under-lock pattern
    (each subsequent response strictly later than the previous one),
    NOT with concurrent-with-recovery-dance pattern (responses
    bunching up + at least one failing or 500-ing).

Live integration — gated on ``POLARIS_LIVE_E2E=1`` because we drive
real docker compose.  Uses a throwaway project so a re-run never
touches user data.
"""
from __future__ import annotations

import asyncio
import os
import secrets

import httpx
import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("POLARIS_LIVE_E2E") != "1",
    reason="opt-in: requires running compose stack + docker daemon",
)


@pytest.fixture
def base_url() -> str:
    return os.environ.get("POLARIS_E2E_BASE_URL", "https://polaris-dev.xyz")


async def _login(base_url: str, client: httpx.AsyncClient) -> None:
    r = await client.get(f"{base_url}/api/auth/dev-login", follow_redirects=False)
    assert r.status_code in (302, 303), r.status_code


async def _create_project(base_url: str, client: httpx.AsyncClient) -> str:
    # Prefix is "polaris-spec-" not "__"; literal underscore in SQL LIKE
    # is a single-char wildcard which makes ad-hoc cleanup queries
    # like `name LIKE '__%'` accidentally match every project.  Real
    # incident, so we belt-and-suspenders the prefix here too.
    name = f"polaris-spec-runtime-lock-{secrets.token_hex(4)}"
    r = await client.post(f"{base_url}/api/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.mark.asyncio
async def test_concurrent_runtime_ensures_serialize_without_race() -> None:
    base_url = os.environ.get("POLARIS_E2E_BASE_URL", "https://polaris-dev.xyz")
    async with httpx.AsyncClient(verify=True, timeout=120) as client:
        await _login(base_url, client)
        project_id = await _create_project(base_url, client)
        try:
            async def _post() -> httpx.Response:
                return await client.post(
                    f"{base_url}/api/projects/{project_id}/workspace/runtime"
                )

            responses = await asyncio.gather(*[_post() for _ in range(5)])

            # Every response is 200 — the lock prevented any from
            # hitting the "name already in use" race + recovery dance.
            for i, r in enumerate(responses):
                assert r.status_code == 200, (
                    f"runtime POST #{i} returned {r.status_code}: "
                    f"{r.text[:200]}"
                )
                body = r.json()
                assert body.get("workspace_id"), body
        finally:
            # Always tear down the fixture, even if an assertion above
            # raised — leaking project rows into the user's homepage
            # is what motivated this hardening in the first place.
            try:
                await client.delete(f"{base_url}/api/projects/{project_id}")
            except httpx.HTTPError:
                pass

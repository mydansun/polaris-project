"""Tests for scripts/lib/validators.py.

Network-dependent validators are exercised against ``respx``-mocked
endpoints so the test suite is hermetic.  One opt-in live test exists
for the CF token (gated on env), used during dev to confirm we haven't
broken parity with the actual API."""
from __future__ import annotations

import os

import httpx
import pytest
import respx

from lib import validators


# ── Domain format ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "good",
    [
        "polaris-dev.xyz",
        "polaris.localhost",
        "a.b.c.example.com",
        "POLARIS-DEV.XYZ",
        "example.io",
    ],
)
def test_domain_format_accepts(good: str):
    assert validators.domain_format(good).ok


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "no-tld",
        "trailing-dash-.com",
        "spaces in domain.com",
    ],
)
def test_domain_format_rejects(bad: str):
    assert not validators.domain_format(bad).ok


# ── Cloudflare token ─────────────────────────────────────────────────────


def test_cf_token_empty_fails():
    r = validators.cf_token("", "polaris-dev.xyz")
    assert r.status == "fail"


def _cf_verify_ok():
    return httpx.Response(
        200,
        json={
            "success": True,
            "result": {"status": "active", "expires_on": None},
            "messages": [],
            "errors": [],
        },
    )


def _cf_zone_ok(name: str):
    return httpx.Response(
        200,
        json={
            "success": True,
            "result": [
                {
                    "id": "zoneid",
                    "name": name,
                    "status": "active",
                    "plan": {"name": "Free"},
                }
            ],
            "errors": [],
        },
    )


def test_cf_token_happy_path():
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://api.cloudflare.com/client/v4/user/tokens/verify"
        ).mock(return_value=_cf_verify_ok())
        router.get("https://api.cloudflare.com/client/v4/zones").mock(
            return_value=_cf_zone_ok("polaris-dev.xyz")
        )
        r = validators.cf_token("good-token", "polaris-dev.xyz")
    assert r.ok, r.detail


def test_cf_token_401():
    with respx.mock() as router:
        router.get(
            "https://api.cloudflare.com/client/v4/user/tokens/verify"
        ).mock(return_value=httpx.Response(401, json={"errors": []}))
        r = validators.cf_token("bad", "polaris-dev.xyz")
    assert r.status == "fail"
    assert "401" in r.detail


def test_cf_token_zone_not_found():
    with respx.mock() as router:
        router.get(
            "https://api.cloudflare.com/client/v4/user/tokens/verify"
        ).mock(return_value=_cf_verify_ok())
        router.get("https://api.cloudflare.com/client/v4/zones").mock(
            return_value=httpx.Response(
                200, json={"success": True, "result": [], "errors": []}
            )
        )
        r = validators.cf_token("right-token-wrong-zone", "other-zone.com")
    assert r.status == "fail"
    assert "cannot see zone" in r.detail


def test_cf_token_network_error_warns():
    with respx.mock() as router:
        router.get(
            "https://api.cloudflare.com/client/v4/user/tokens/verify"
        ).mock(side_effect=httpx.ConnectError("dns fail"))
        r = validators.cf_token("anything", "polaris-dev.xyz")
    # Network errors → warn (don't block the wizard offline)
    assert r.status == "warn"


# ── Pinterest ────────────────────────────────────────────────────────────


def test_pinterest_token_empty_fails():
    assert (
        validators.pinterest_token("", "https://pint-polaris-infra.miyuko.name").status
        == "fail"
    )


def test_pinterest_token_happy_path():
    base = "https://pint-polaris-infra.miyuko.name"
    with respx.mock() as router:
        route = router.post(f"{base}/query").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        r = validators.pinterest_token("k", base)
        assert route.called
        assert route.calls[0].request.headers["x-api-key"] == "k"
    assert r.ok, r.detail


def test_pinterest_token_401_fails():
    base = "https://pint-polaris-infra.miyuko.name"
    with respx.mock() as router:
        router.post(f"{base}/query").mock(
            return_value=httpx.Response(401, json={"detail": "no key"})
        )
        r = validators.pinterest_token("bad", base)
    assert r.status == "fail"


# ── OpenAI ───────────────────────────────────────────────────────────────


def test_openai_key_empty_fails():
    assert validators.openai_key("").status == "fail"


def test_openai_key_happy_path():
    with respx.mock() as router:
        router.get("https://api.openai.com/v1/models").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        r = validators.openai_key("sk-test")
    assert r.ok


def test_openai_key_401_fails():
    with respx.mock() as router:
        router.get("https://api.openai.com/v1/models").mock(
            return_value=httpx.Response(401, json={"error": {}})
        )
        r = validators.openai_key("sk-bad")
    assert r.status == "fail"


# ── Live smoke test (opt-in, gated on env) ─────────────────────────────
#
# Exercises the actual Cloudflare API.  Skipped by default.  Run with:
#   POLARIS_LIVE_CF_SMOKE=1 uv run pytest scripts/tests/ -k live_cf
# (Reads CF_DNS_API_TOKEN from the host env / .env so credentials never
# enter test fixtures.)


@pytest.mark.skipif(
    os.environ.get("POLARIS_LIVE_CF_SMOKE") != "1",
    reason="opt-in: requires real CF_DNS_API_TOKEN",
)
def test_cf_token_live_smoke():
    token = os.environ.get("CF_DNS_API_TOKEN", "")
    assert token, "CF_DNS_API_TOKEN not set; run via wizard or export it"
    r = validators.cf_token(token, "polaris-dev.xyz")
    assert r.ok, f"live CF token rejected: {r.detail}"

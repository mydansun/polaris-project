"""Live token / endpoint validators used by the up.py wizard.

Every validator returns a :class:`ValidationResult` so callers can drive
"OK ✓ / WARN ⚠ / FAIL ✗" UX without parsing strings.  Validators that
need network access ALWAYS time out within a few seconds — the wizard
should never hang waiting on a misconfigured token.

Network failures are reported as ``warn`` (best-effort), not ``fail``,
unless the response is unambiguous (401, malformed JSON, etc).  Reason:
running ``up.py`` on a flight without internet shouldn't gate on token
validation; the user can still proceed and fix later.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import httpx

Status = Literal["ok", "warn", "fail"]

_HTTP_TIMEOUT = 10.0


@dataclass
class ValidationResult:
    status: Status
    detail: str  # one-line, suitable for inline display next to the prompt

    @property
    def ok(self) -> bool:
        return self.status == "ok"


# ── Format / shape (offline) ────────────────────────────────────────────

_DOMAIN_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
_DOMAIN_RE = re.compile(
    rf"^(?=.{{1,253}}$)({_DOMAIN_LABEL}\.)+[a-z]{{2,63}}$",
    re.IGNORECASE,
)


def domain_format(value: str) -> ValidationResult:
    """Cheap format check — does it look like a FQDN?"""
    v = (value or "").strip().rstrip(".")
    if not v:
        return ValidationResult("fail", "domain cannot be empty")
    if not _DOMAIN_RE.match(v):
        return ValidationResult(
            "fail", f"{v!r} doesn't look like a valid domain"
        )
    return ValidationResult("ok", "format ok")


# ── Cloudflare token ─────────────────────────────────────────────────────


def cf_token(token: str, zone: str) -> ValidationResult:
    """Verify a Cloudflare API token end-to-end.

    Mirrors the smoke test we ran by hand:
      1. ``GET /user/tokens/verify`` — token active?
      2. ``GET /zones?name=<zone>`` — token can read the zone?
      3. *(implicit)* DNS:Edit on that zone — covered by the smoke
         create+delete in the standalone smoke test; the wizard skips
         the mutation step (don't poke production DNS during config).
    """
    if not token.strip():
        return ValidationResult("fail", "token is empty")
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT, headers=headers) as c:
            verify = c.get(
                "https://api.cloudflare.com/client/v4/user/tokens/verify"
            )
    except httpx.HTTPError as e:
        return ValidationResult("warn", f"network error: {e}")
    if verify.status_code == 401:
        return ValidationResult("fail", "401 — token rejected")
    if verify.status_code != 200:
        return ValidationResult(
            "warn", f"verify returned HTTP {verify.status_code}"
        )
    body = verify.json()
    if not body.get("success") or body.get("result", {}).get("status") != "active":
        return ValidationResult(
            "fail", f"token not active: {body.get('messages')!r}"
        )

    # Zone scope check
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT, headers=headers) as c:
            zr = c.get(
                "https://api.cloudflare.com/client/v4/zones",
                params={"name": zone},
            )
    except httpx.HTTPError as e:
        return ValidationResult("warn", f"zone lookup network error: {e}")
    if zr.status_code != 200:
        return ValidationResult("warn", f"zone lookup HTTP {zr.status_code}")
    zb = zr.json()
    results = zb.get("result", []) or []
    if not results:
        return ValidationResult(
            "fail",
            f"token cannot see zone {zone!r} — wrong token or wrong zone",
        )
    return ValidationResult("ok", f"token + Zone:Read on {zone} ok")


# ── Pinterest scraper ────────────────────────────────────────────────────


def pinterest_token(api_key: str, base_url: str) -> ValidationResult:
    """Hit the scraper's ``/health`` with X-API-Key.

    ``/health`` is open (200 without auth) but we want to verify the
    token so we hit ``/query`` with a tiny bogus body and accept either
    200 (success) or any non-401 (server-side decisions are not our
    concern; only 401 means "token rejected").
    """
    if not api_key.strip():
        return ValidationResult("fail", "api key is empty")
    base = base_url.rstrip("/") or "https://pint-polaris-infra.miyuko.name"
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as c:
            # Use /query (not /health) because /health is unauthenticated
            # so it'd 200 even with a wrong key.
            resp = c.post(
                f"{base}/query",
                headers={"X-API-Key": api_key},
                json={"query": "ping", "hops": 1},
            )
    except httpx.HTTPError as e:
        return ValidationResult("warn", f"network error: {e}")
    if resp.status_code == 401:
        return ValidationResult("fail", "401 — X-API-Key rejected")
    if resp.status_code >= 500:
        return ValidationResult(
            "warn", f"scraper HTTP {resp.status_code} (server side)"
        )
    return ValidationResult("ok", f"X-API-Key accepted (HTTP {resp.status_code})")


# ── OpenAI key ───────────────────────────────────────────────────────────


def openai_key(api_key: str) -> ValidationResult:
    """Hit ``GET /v1/models`` — cheapest endpoint that proves the key."""
    if not api_key.strip():
        return ValidationResult("fail", "api key is empty")
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as c:
            resp = c.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.HTTPError as e:
        return ValidationResult("warn", f"network error: {e}")
    if resp.status_code == 401:
        return ValidationResult("fail", "401 — key rejected")
    if resp.status_code >= 500:
        return ValidationResult(
            "warn", f"OpenAI HTTP {resp.status_code} (server side)"
        )
    if resp.status_code != 200:
        return ValidationResult("warn", f"unexpected HTTP {resp.status_code}")
    return ValidationResult("ok", "key accepted by OpenAI")

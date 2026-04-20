"""Tests for scripts/lib/spec.py — required-key conditional logic, secret
auto-generation, lookup helpers."""
from __future__ import annotations

import re

import pytest

from lib import spec


def test_required_keys_includes_cf_token_unconditionally():
    keys = spec.required_keys({})
    assert "CF_DNS_API_TOKEN" in keys


def test_required_keys_always_include_core():
    keys = spec.required_keys({})
    for must in (
        "POLARIS_DOMAIN",
        "CF_DNS_API_TOKEN",
        "OPENAI_SECRET",
        "POLARIS_PINTEREST_TOOL_API_KEY",
        "SESSION_SECRET",
    ):
        assert must in keys


def test_codex_auth_path_optional_with_runtime_default():
    """No longer required — up.py auto-creates an empty stub.  But the
    field still exists in the catalog with a runtime-resolved default."""
    assert "POLARIS_HOST_CODEX_AUTH_PATH" not in spec.required_keys({})
    f = spec.by_key("POLARIS_HOST_CODEX_AUTH_PATH")
    assert f is not None
    assert f.required is False
    rd = spec.runtime_default(f)
    assert rd and rd.endswith("/.codex/auth.json")


def test_session_secret_autogen_when_blank():
    out = spec.autogenerate_blank({})
    assert "SESSION_SECRET" in out
    assert re.fullmatch(r"[0-9a-f]{64}", out["SESSION_SECRET"])


def test_session_secret_autogen_replaces_placeholder():
    out = spec.autogenerate_blank(
        {"SESSION_SECRET": "polaris-dev-secret-change-me"}
    )
    assert "SESSION_SECRET" in out
    assert out["SESSION_SECRET"] != "polaris-dev-secret-change-me"


def test_session_secret_left_alone_if_set():
    out = spec.autogenerate_blank({"SESSION_SECRET": "user-provided-value"})
    assert "SESSION_SECRET" not in out


def test_by_key_lookup():
    f = spec.by_key("POLARIS_DOMAIN")
    assert f is not None
    assert f.required is True
    assert f.default == "polaris-dev.xyz"
    assert spec.by_key("DOES_NOT_EXIST") is None


def test_secrets_marked_for_masking():
    masked = {f.key for f in spec.FIELDS if f.secret}
    expected = {
        "CF_DNS_API_TOKEN",
        "OPENAI_SECRET",
        "POLARIS_PINTEREST_TOOL_API_KEY",
        "SESSION_SECRET",
        "UNSPLASH_ACCESS_KEY",
    }
    assert expected.issubset(masked)

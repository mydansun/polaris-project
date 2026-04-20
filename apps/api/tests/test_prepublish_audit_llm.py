"""Tests for the LLM-assisted prepublish audit's pure helpers.

The endpoint itself (``routes/audit.py``) is thin — it authenticates,
clamps inputs, makes one LLM call, and parses the response.  We test
the deterministic bits (parser + clamp + input formatter) directly;
the network-bound endpoint is covered by manual smoke.
"""
from __future__ import annotations

import json

from polaris_api.services.audit_prompt import (
    MAX_FIELD_CHARS,
    clamp,
    format_audit_inputs,
    parse_audit_response,
)


# ── parse_audit_response ─────────────────────────────────────────────────


_VALID = [
    {
        "severity": "error",
        "hint": "start invokes bare `next` — prod PATH lacks node_modules/.bin",
        "fix": "use `npm start` or `npx next start`",
    },
    {
        "severity": "warning",
        "hint": "port 3000 in polaris.yaml but start binds 8000",
        "fix": "align polaris.yaml::port with --port flag",
    },
]


def test_parse_plain_json_array():
    assert parse_audit_response(json.dumps(_VALID)) == _VALID


def test_parse_strips_json_fence():
    raw = "```json\n" + json.dumps(_VALID) + "\n```"
    assert parse_audit_response(raw) == _VALID


def test_parse_strips_generic_fence():
    raw = "```\n" + json.dumps(_VALID) + "\n```"
    assert parse_audit_response(raw) == _VALID


def test_parse_returns_empty_on_non_json():
    assert parse_audit_response("I think your app looks fine :)") == []


def test_parse_returns_empty_on_non_array():
    assert parse_audit_response('{"issues": []}') == []


def test_parse_drops_items_missing_severity():
    bad = [{"hint": "no sev"}, _VALID[0]]
    parsed = parse_audit_response(json.dumps(bad))
    assert len(parsed) == 1
    assert parsed[0]["hint"] == _VALID[0]["hint"]


def test_parse_drops_items_with_invalid_severity():
    bad = [{"severity": "info", "hint": "something"}, _VALID[0]]
    parsed = parse_audit_response(json.dumps(bad))
    assert len(parsed) == 1
    assert parsed[0]["severity"] == "error"


def test_parse_drops_items_with_empty_hint():
    bad = [{"severity": "error", "hint": ""}, _VALID[0]]
    parsed = parse_audit_response(json.dumps(bad))
    assert len(parsed) == 1


def test_parse_fix_defaults_to_empty_when_missing():
    just_hint = [{"severity": "error", "hint": "some error"}]
    parsed = parse_audit_response(json.dumps(just_hint))
    assert parsed == [{"severity": "error", "hint": "some error", "fix": ""}]


# ── clamp ────────────────────────────────────────────────────────────────


def test_clamp_leaves_short_text_alone():
    assert clamp("hello") == "hello"


def test_clamp_truncates_with_marker():
    big = "x" * (MAX_FIELD_CHARS + 500)
    out = clamp(big)
    assert len(out) > MAX_FIELD_CHARS  # the marker adds some extra
    assert out.startswith("x" * 100)
    assert "[truncated 500 chars]" in out


def test_clamp_respects_custom_limit():
    out = clamp("hello world", limit=5)
    assert out.startswith("hello")
    assert "truncated" in out


# ── format_audit_inputs ──────────────────────────────────────────────────


def test_format_includes_all_three_blocks():
    out = format_audit_inputs(
        polaris_yaml="version: 1\nstack: node\n",
        dockerfile="FROM node:22\nCMD npm start\n",
        package_json_scripts={"start": "next start"},
    )
    assert "polaris.yaml:" in out
    assert "Dockerfile:" in out
    assert "package.json scripts:" in out
    assert "version: 1" in out
    assert "FROM node:22" in out
    assert '"start": "next start"' in out


def test_format_handles_empty_fields():
    out = format_audit_inputs(
        polaris_yaml="",
        dockerfile="",
        package_json_scripts={},
    )
    assert "(empty)" in out
    assert "(none)" in out

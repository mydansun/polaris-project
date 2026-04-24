"""Unit tests for the small pure helpers in ``polaris_agent_core.codex_app_server``.

The codex client carries two purely-functional utilities embedded in
an otherwise transport-heavy module — easy to pin without booting a
WebSocket.  ``parse_command`` is the shlex split used by historical
exec wrappers; ``_dyn_response`` is the envelope every dynamic-tool
response must conform to.
"""

from __future__ import annotations

import json

import pytest

from polaris_agent_core.codex_app_server import (
    PolarisCodexError,
    _dyn_response,
    parse_command,
)


# ── parse_command ──────────────────────────────────────────────────────


def test_parse_command_splits_simple_argv():
    assert parse_command("npm install") == ["npm", "install"]


def test_parse_command_respects_quotes():
    # Codex sometimes hands back commands with quoted arguments
    # (paths with spaces, regex patterns).  shlex must keep them
    # together as one token.
    assert parse_command('git -c user.name="OpenAI Polaris" commit -m "init"') == [
        "git",
        "-c",
        "user.name=OpenAI Polaris",
        "commit",
        "-m",
        "init",
    ]


def test_parse_command_handles_single_quotes():
    assert parse_command("echo 'hello world'") == ["echo", "hello world"]


def test_parse_command_collapses_runs_of_whitespace():
    # shlex tokenizes on whitespace — ensure tabs/multiple spaces
    # don't produce empty tokens.
    assert parse_command("echo   hi\tthere") == ["echo", "hi", "there"]


def test_parse_command_raises_on_empty_string():
    # Codex shouldn't ever call exec on "" — surface the bug rather
    # than silently exec'ing no-op which could mask a real failure.
    with pytest.raises(PolarisCodexError):
        parse_command("")


def test_parse_command_raises_on_whitespace_only():
    with pytest.raises(PolarisCodexError):
        parse_command("   \t  ")


def test_parse_command_unmatched_quote_raises():
    # shlex throws ValueError on unclosed quotes — that must surface
    # to the caller as some kind of exception, not silently produce
    # garbage tokens.
    with pytest.raises(Exception):
        parse_command('echo "unclosed')


# ── _dyn_response ──────────────────────────────────────────────────────


def test_dyn_response_envelope_shape_success():
    out = _dyn_response(True, {"foo": "bar"})
    assert out["success"] is True
    assert out["contentItems"] == [
        {"type": "inputText", "text": '{"foo": "bar"}'}
    ]


def test_dyn_response_envelope_shape_failure():
    out = _dyn_response(False, {"error": "boom"})
    assert out["success"] is False
    # Even failures must carry the same envelope — codex's app-server
    # rejects responses lacking ``contentItems``.
    assert isinstance(out["contentItems"], list)
    assert len(out["contentItems"]) == 1


def test_dyn_response_serializes_non_json_native_values():
    # ``default=str`` lets us pass UUIDs, paths, datetimes through
    # without a TypeError.  Verify with a sample non-native value.
    from pathlib import Path

    out = _dyn_response(True, {"path": Path("/workspace/foo")})
    text = out["contentItems"][0]["text"]
    parsed = json.loads(text)
    assert parsed == {"path": "/workspace/foo"}


def test_dyn_response_handles_empty_payload():
    out = _dyn_response(True, {})
    assert out["contentItems"][0]["text"] == "{}"


def test_dyn_response_text_is_valid_json():
    # The text field is a JSON string (not a dict) — codex parses it
    # client-side; emitting raw repr() output would break that.
    out = _dyn_response(True, {"a": 1, "b": [1, 2, 3]})
    parsed = json.loads(out["contentItems"][0]["text"])
    assert parsed == {"a": 1, "b": [1, 2, 3]}

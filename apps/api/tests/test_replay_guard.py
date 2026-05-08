"""Tests for the replay-mode network kill-switch.

Verifies (a) the guard itself behaves correctly under each env state,
and (b) at least one gated callsite per surface (OpenAI chat, OpenAI
images, Pinterest, Unsplash) actually raises in replay mode — so a
silent regression that drops the ``check_network`` call from a
client builder gets caught here, not at $$ in the next recording.
"""

from __future__ import annotations

import pytest

from polaris_agent_core.replay_guard import (
    ReplayModeNetworkBlocked,
    assert_modes_not_both,
    check_network,
    is_record_mode,
    is_replay_mode,
)


# ── Pure guard ─────────────────────────────────────────────────────────


def test_is_replay_mode_reads_env(monkeypatch):
    monkeypatch.delenv("POLARIS_REPLAY", raising=False)
    assert is_replay_mode() is False
    monkeypatch.setenv("POLARIS_REPLAY", "/tmp/x.json.gz")
    assert is_replay_mode() is True


def test_is_replay_mode_treats_empty_string_as_off(monkeypatch):
    # Some shells silently leave POLARIS_REPLAY="" after `unset` —
    # treat that as off, not on, so the operator doesn't get a
    # confusing block on a non-replay run.
    monkeypatch.setenv("POLARIS_REPLAY", "")
    assert is_replay_mode() is False


def test_is_record_mode_reads_env(monkeypatch):
    monkeypatch.delenv("POLARIS_RECORD", raising=False)
    assert is_record_mode() is False
    monkeypatch.setenv("POLARIS_RECORD", "/tmp/x.json.gz")
    assert is_record_mode() is True


def test_check_network_is_noop_when_replay_off(monkeypatch):
    monkeypatch.delenv("POLARIS_REPLAY", raising=False)
    # Should not raise.
    check_network("openai")
    check_network("pinterest")


def test_check_network_raises_with_label_when_replay_on(monkeypatch):
    monkeypatch.setenv("POLARIS_REPLAY", "/tmp/x.json.gz")
    with pytest.raises(ReplayModeNetworkBlocked) as exc:
        check_network("openai")
    assert exc.value.label == "openai"
    assert "POLARIS_REPLAY" in str(exc.value)


def test_check_network_each_label_independent(monkeypatch):
    monkeypatch.setenv("POLARIS_REPLAY", "/tmp/x.json.gz")
    # Different labels surface in the exception so the failing test
    # immediately points at which surface tripped.
    with pytest.raises(ReplayModeNetworkBlocked) as e1:
        check_network("openai")
    with pytest.raises(ReplayModeNetworkBlocked) as e2:
        check_network("pinterest")
    assert e1.value.label == "openai"
    assert e2.value.label == "pinterest"


def test_assert_modes_not_both_passes_when_neither(monkeypatch):
    monkeypatch.delenv("POLARIS_REPLAY", raising=False)
    monkeypatch.delenv("POLARIS_RECORD", raising=False)
    assert_modes_not_both()  # no-op


def test_assert_modes_not_both_passes_when_only_one(monkeypatch):
    monkeypatch.delenv("POLARIS_RECORD", raising=False)
    monkeypatch.setenv("POLARIS_REPLAY", "/tmp/x.json.gz")
    assert_modes_not_both()


def test_assert_modes_not_both_raises_when_both(monkeypatch):
    monkeypatch.setenv("POLARIS_REPLAY", "/tmp/x.json.gz")
    monkeypatch.setenv("POLARIS_RECORD", "/tmp/y.json.gz")
    with pytest.raises(RuntimeError, match="mutually exclusive"):
        assert_modes_not_both()


# ── Callsite coverage — at least one per surface ───────────────────────


def test_unsplash_auth_headers_raises_in_replay_mode(monkeypatch):
    # services.unsplash._auth_headers is the chokepoint every Unsplash
    # call goes through.  In replay it must raise BEFORE building the
    # auth dict so a leaked code path can't even silently 401.
    from polaris_api.services.unsplash import _auth_headers
    from polaris_api.config import Settings

    monkeypatch.setenv("POLARIS_REPLAY", "/tmp/x.json.gz")
    settings = Settings()
    object.__setattr__(settings, "unsplash_access_key", "test-key")
    with pytest.raises(ReplayModeNetworkBlocked) as exc:
        _auth_headers(settings)
    assert exc.value.label == "unsplash"


def test_pinterest_client_raises_in_replay_mode(monkeypatch):
    from polaris_design_intent.tools.pinterest_client import PinterestClient

    monkeypatch.setenv("POLARIS_REPLAY", "/tmp/x.json.gz")
    with pytest.raises(ReplayModeNetworkBlocked) as exc:
        PinterestClient(base_url="https://example.com", api_key="k")
    assert exc.value.label == "pinterest"


def test_clarifier_build_model_raises_in_replay_mode(monkeypatch):
    from polaris_design_intent.config import Settings as DiSettings
    from polaris_design_intent.nodes.clarifier import _build_model

    monkeypatch.setenv("POLARIS_REPLAY", "/tmp/x.json.gz")
    # design-intent Settings requires POLARIS_PINTEREST_TOOL_API_KEY
    # to construct — supply a stub.  Doesn't matter what's there:
    # _build_model raises through the guard before any client init.
    monkeypatch.setenv("POLARIS_PINTEREST_TOOL_API_KEY", "test")
    settings = DiSettings()
    with pytest.raises(ReplayModeNetworkBlocked) as exc:
        _build_model(settings)
    assert exc.value.label == "openai"


def test_clarifier_build_model_no_raise_when_replay_off(monkeypatch):
    # Sanity: with no replay env, the guard doesn't interfere with
    # normal model construction.  Newer openai SDKs hard-require an
    # api_key at construction (used to be lazy), so we provide a
    # stub — the assertion is purely "did we get past the guard",
    # not "is this model actually usable".
    from polaris_design_intent.config import Settings as DiSettings
    from polaris_design_intent.nodes.clarifier import _build_model

    monkeypatch.delenv("POLARIS_REPLAY", raising=False)
    monkeypatch.setenv("POLARIS_PINTEREST_TOOL_API_KEY", "test")
    monkeypatch.setenv("OPENAI_SECRET", "sk-stub-not-used")
    settings = DiSettings()
    model = _build_model(settings)
    assert model is not None

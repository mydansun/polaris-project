"""Tests for scripts/lib/wizard.py — the interactive bits + the
"preserve existing values" contract."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from lib import env_io, spec, wizard


# ── helper: capture each prompt the wizard issues ────────────────────────


def _patched_ask(answers: dict[str, str]):
    """Build a ``_ask_one`` stand-in that:
      - records every (field.key, current_env_snapshot) it sees
      - returns the canned answer for that key, or "" if unscripted
    """
    captured: list[tuple[str, dict[str, str]]] = []

    def fake(field, current_env):
        captured.append((field.key, dict(current_env)))
        return answers.get(field.key, "")

    return fake, captured


# ── interactive: only_missing=True (the up.py default) ───────────────────


def test_wizard_skips_fields_already_set_in_env_file(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_io.write(
        env_path,
        {
            "POLARIS_DOMAIN": "polaris-dev.xyz",
            "CF_DNS_API_TOKEN": "preset-cf",
            "OPENAI_SECRET": "sk-preset",
            "POLARIS_PINTEREST_TOOL_API_KEY": "preset-pkey",
            "SESSION_SECRET": "x" * 64,
            # codex auth path is now optional — but still preserved when set
            "POLARIS_HOST_CODEX_AUTH_PATH": "/preset/codex/auth.json",
        },
    )

    fake, captured = _patched_ask({})
    with patch.object(wizard, "_ask_one", side_effect=fake):
        result = wizard.run_wizard(env_path=env_path, only_missing=True)

    asked = [k for k, _ in captured]
    # All required fields are pre-populated → wizard asks nothing required.
    # It MAY still ask for the not-required fields (defaults / blanks) but
    # those are also empty in our seed env so it'd ask once each.
    for required in (
        "POLARIS_DOMAIN",
        "CF_DNS_API_TOKEN",
        "OPENAI_SECRET",
        "POLARIS_PINTEREST_TOOL_API_KEY",
        "SESSION_SECRET",
        "POLARIS_HOST_CODEX_AUTH_PATH",
    ):
        assert required not in asked, (
            f"wizard re-prompted for already-set required field {required}"
        )
    # And the preset values survive the round-trip on disk.
    rewritten = env_io.read(env_path)
    assert rewritten["POLARIS_DOMAIN"] == "polaris-dev.xyz"
    assert rewritten["CF_DNS_API_TOKEN"] == "preset-cf"
    assert rewritten["OPENAI_SECRET"] == "sk-preset"
    assert rewritten["POLARIS_PINTEREST_TOOL_API_KEY"] == "preset-pkey"
    assert rewritten["SESSION_SECRET"] == "x" * 64
    # Returned dict matches what landed on disk.
    assert result["CF_DNS_API_TOKEN"] == "preset-cf"


def test_wizard_only_prompts_for_missing_fields(tmp_path: Path):
    """With most required fields preset but POLARIS_DOMAIN missing, the
    wizard prompts for POLARIS_DOMAIN and the optional defaults — never
    for the keys the user already filled."""
    env_path = tmp_path / ".env"
    env_io.write(
        env_path,
        {
            # POLARIS_DOMAIN deliberately missing.
            "CF_DNS_API_TOKEN": "preset-cf",
            "OPENAI_SECRET": "sk-preset",
            "POLARIS_PINTEREST_TOOL_API_KEY": "preset-pkey",
            "SESSION_SECRET": "x" * 64,
            # codex auth path is now optional — but still preserved when set
            "POLARIS_HOST_CODEX_AUTH_PATH": "/preset/codex/auth.json",
            "POLARIS_PINTEREST_TOOL_BASE": "https://existing.example",
            "POLARIS_DEV_USER_EMAIL": "preset@dev.local",
            "POLARIS_DEV_USER_NAME": "Preset",
            "UNSPLASH_ACCESS_KEY": "preset-unsplash",
        },
    )

    fake, captured = _patched_ask({"POLARIS_DOMAIN": "polaris-dev.xyz"})
    with patch.object(wizard, "_ask_one", side_effect=fake):
        wizard.run_wizard(env_path=env_path, only_missing=True)

    asked = [k for k, _ in captured]
    # POLARIS_PROD_DOMAIN_BASE is offered too because it's a Field with no
    # default — auto-derived after the wizard via ``autogenerate_blank``.
    # The user can hit-enter to accept the derived value.
    assert "POLARIS_DOMAIN" in asked, (
        f"expected wizard to ask for POLARIS_DOMAIN; got {asked!r}"
    )
    assert "OPENAI_SECRET" not in asked, (
        "wizard re-asked for already-set OPENAI_SECRET"
    )
    assert "CF_DNS_API_TOKEN" not in asked


def test_wizard_reconfigure_prompts_every_field(tmp_path: Path):
    """only_missing=False ⇒ wizard prompts for every field even if the
    user already has values.  Existing values become the default shown."""
    env_path = tmp_path / ".env"
    env_io.write(
        env_path,
        {
            "POLARIS_DOMAIN": "polaris-dev.xyz",
            "CF_DNS_API_TOKEN": "preset-cf",
            "OPENAI_SECRET": "sk-preset",
            "POLARIS_PINTEREST_TOOL_API_KEY": "preset-pkey",
            "SESSION_SECRET": "x" * 64,
            # codex auth path is now optional — but still preserved when set
            "POLARIS_HOST_CODEX_AUTH_PATH": "/preset/codex/auth.json",
        },
    )

    fake, captured = _patched_ask({})
    with patch.object(wizard, "_ask_one", side_effect=fake):
        wizard.run_wizard(env_path=env_path, only_missing=False)

    asked = {k for k, _ in captured}
    # Every declared FIELD got asked exactly once.
    for f in spec.FIELDS:
        assert f.key in asked


def test_process_env_seeds_defaults_when_file_missing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """User exported CF_DNS_API_TOKEN in the shell, .env doesn't have it
    → wizard treats it as already-set and skips."""
    env_path = tmp_path / ".env"
    env_io.write(
        env_path,
        {
            "POLARIS_DOMAIN": "polaris-dev.xyz",
            "OPENAI_SECRET": "sk-preset",
            "POLARIS_PINTEREST_TOOL_API_KEY": "preset-pkey",
            "SESSION_SECRET": "x" * 64,
            # codex auth path is now optional — but still preserved when set
            "POLARIS_HOST_CODEX_AUTH_PATH": "/preset/codex/auth.json",
        },
    )
    monkeypatch.setenv("CF_DNS_API_TOKEN", "from-process-env")

    fake, captured = _patched_ask({})
    with patch.object(wizard, "_ask_one", side_effect=fake):
        wizard.run_wizard(env_path=env_path, only_missing=True)

    asked = [k for k, _ in captured]
    assert "CF_DNS_API_TOKEN" not in asked
    assert env_io.read(env_path)["CF_DNS_API_TOKEN"] == "from-process-env"


# ── non-interactive ──────────────────────────────────────────────────────


def test_non_interactive_passes_when_all_required_present(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_io.write(
        env_path,
        {
            "POLARIS_DOMAIN": "polaris-dev.xyz",
            "CF_DNS_API_TOKEN": "x",
            "OPENAI_SECRET": "x",
            "POLARIS_PINTEREST_TOOL_API_KEY": "x",
            "SESSION_SECRET": "x" * 64,
            # codex auth path is now optional — but still preserved when set
            "POLARIS_HOST_CODEX_AUTH_PATH": "/preset/codex/auth.json",
        },
    )
    # Should not raise.
    result = wizard.run_wizard(env_path=env_path, interactive=False)
    assert result["POLARIS_DOMAIN"] == "polaris-dev.xyz"


def test_non_interactive_exits_2_on_missing(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_io.write(env_path, {"POLARIS_DOMAIN": "polaris-dev.xyz"})  # rest missing
    with pytest.raises(SystemExit) as excinfo:
        wizard.run_wizard(env_path=env_path, interactive=False)
    assert excinfo.value.code == 2


# ── auto-generation ──────────────────────────────────────────────────────


def test_session_secret_autogenerated_when_blank(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_io.write(
        env_path,
        {
            "POLARIS_DOMAIN": "polaris-dev.xyz",
            "CF_DNS_API_TOKEN": "x",
            "OPENAI_SECRET": "x",
            "POLARIS_PINTEREST_TOOL_API_KEY": "x",
            # SESSION_SECRET deliberately absent.
            # codex auth path is now optional — but still preserved when set
            "POLARIS_HOST_CODEX_AUTH_PATH": "/preset/codex/auth.json",
        },
    )

    # Have the wizard auto-fill SESSION_SECRET when prompted by returning "".
    fake, _ = _patched_ask({})
    with patch.object(wizard, "_ask_one", side_effect=fake):
        result = wizard.run_wizard(env_path=env_path, only_missing=True)

    secret = result.get("SESSION_SECRET", "")
    assert len(secret) == 64
    assert all(c in "0123456789abcdef" for c in secret)

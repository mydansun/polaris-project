"""Tests for scripts/lib/db.py — postgres host-port discovery."""
from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from lib import db


def _completed(stdout: str, returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["docker", "port"], returncode=returncode, stdout=stdout, stderr=""
    )


# ── env override wins ────────────────────────────────────────────────


def test_asyncpg_url_uses_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(
        "POLARIS_DATABASE_URL",
        "postgresql+asyncpg://u:p@h:5432/d?sslmode=require",
    )
    assert db.asyncpg_url() == "postgres://u:p@h:5432/d?sslmode=require"


# ── docker port parsing ──────────────────────────────────────────────


def test_asyncpg_url_parses_ipv4_line(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("POLARIS_DATABASE_URL", raising=False)
    with patch.object(
        subprocess, "run", return_value=_completed("127.0.0.1:55432\n[::]:55432\n")
    ):
        url = db.asyncpg_url()
    assert url == "postgres://root:123456@127.0.0.1:55432/polaris"


def test_asyncpg_url_handles_arrow_format(monkeypatch: pytest.MonkeyPatch):
    """Some docker versions print ``127.0.0.1:5432 -> 5432/tcp``."""
    monkeypatch.delenv("POLARIS_DATABASE_URL", raising=False)
    with patch.object(
        subprocess,
        "run",
        return_value=_completed("127.0.0.1:62500 -> 5432/tcp\n"),
    ):
        url = db.asyncpg_url()
    assert url == "postgres://root:123456@127.0.0.1:62500/polaris"


def test_asyncpg_url_skips_ipv6(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("POLARIS_DATABASE_URL", raising=False)
    with patch.object(
        subprocess,
        "run",
        return_value=_completed("[::]:55432\n127.0.0.1:55432\n"),
    ):
        url = db.asyncpg_url()
    # Picks the IPv4 line, not [::].
    assert "127.0.0.1:55432" in url


# ── failure modes ────────────────────────────────────────────────────


def test_asyncpg_url_raises_when_container_absent(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("POLARIS_DATABASE_URL", raising=False)
    with patch.object(
        subprocess, "run", return_value=_completed("", returncode=1)
    ):
        with pytest.raises(db.PostgresUnreachable):
            db.asyncpg_url()


def test_asyncpg_url_raises_on_garbage_output(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("POLARIS_DATABASE_URL", raising=False)
    with patch.object(subprocess, "run", return_value=_completed("not a port line")):
        with pytest.raises(db.PostgresUnreachable):
            db.asyncpg_url()


def test_asyncpg_url_raises_when_docker_missing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("POLARIS_DATABASE_URL", raising=False)
    with patch.object(subprocess, "run", side_effect=FileNotFoundError):
        with pytest.raises(db.PostgresUnreachable):
            db.asyncpg_url()


# ── db override ──────────────────────────────────────────────────────


def test_asyncpg_url_uses_custom_db_name(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("POLARIS_DATABASE_URL", raising=False)
    with patch.object(
        subprocess, "run", return_value=_completed("127.0.0.1:55432\n")
    ):
        url = db.asyncpg_url(db="testdb")
    assert url.endswith("/testdb")

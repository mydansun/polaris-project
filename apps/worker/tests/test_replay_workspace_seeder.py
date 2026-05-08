"""Tests for the replay workspace seeder.

Verifies the path-derivation helper, idempotency probe, and the
seed_workspace flow against patched docker subprocess calls.  Real
docker is exercised by the Phase 3.6 e2e test, not here.
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from polaris_worker.replay.workspace_seeder import (
    SEED_MARKER_PATH,
    seed_workspace,
    seed_workspace_from_fixture,
    workspace_tarball_path,
)


_WS_ID = UUID("12345678-90ab-cdef-1234-567890abcdef")


# ── workspace_tarball_path ─────────────────────────────────────────────


def test_tarball_path_from_gzipped_fixture():
    fix = Path("/repo/tests/fixtures/replay/raw/golf-landing-page.json.gz")
    assert workspace_tarball_path(fix) == Path(
        "/repo/tests/fixtures/replay/assets/golf-landing-page-workspace.tar.gz"
    )


def test_tarball_path_from_plain_fixture():
    fix = Path("/repo/tests/fixtures/replay/raw/dummy.json")
    assert workspace_tarball_path(fix) == Path(
        "/repo/tests/fixtures/replay/assets/dummy-workspace.tar.gz"
    )


def test_tarball_path_handles_dotted_scenario_names():
    # Versioned scenarios like "golf.v2.json.gz" must resolve to the
    # full multi-dot stem, not just up to the first dot.
    fix = Path("/repo/raw/golf.v2.json.gz")
    out = workspace_tarball_path(fix)
    assert out.name == "golf.v2-workspace.tar.gz"


# ── seed_workspace ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_seed_returns_false_when_tarball_missing(tmp_path):
    out = await seed_workspace(_WS_ID, tmp_path / "missing.tar.gz")
    assert out is False


@pytest.mark.asyncio
async def test_seed_returns_false_when_container_not_running(tmp_path):
    tar = tmp_path / "ws.tar.gz"
    tar.write_bytes(b"\x1f\x8b...")  # any bytes — we never read

    async def _exec_inspect_running_false(*args, **kwargs):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"false\n", b""))
        return proc

    with patch(
        "asyncio.create_subprocess_exec", new=AsyncMock(
            side_effect=_exec_inspect_running_false
        )
    ):
        out = await seed_workspace(_WS_ID, tar)
    assert out is False


@pytest.mark.asyncio
async def test_seed_skips_when_marker_exists(tmp_path):
    """Idempotent: a workspace already seeded in a prior session_open
    must not re-untar (harmless but logs noise + chowns rerun)."""
    tar = tmp_path / "ws.tar.gz"
    tar.write_bytes(b"x")

    call_log: list[list[str]] = []

    async def _record_call(*args, **kwargs):
        call_log.append(list(args))
        proc = AsyncMock()
        # First call: docker inspect (running) → "true"
        # Second call: docker exec test -f marker → returncode 0 (exists)
        if "inspect" in args:
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"true\n", b""))
        elif "test" in args:
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
        else:
            # Should NOT reach the untar step
            raise AssertionError(f"unexpected docker invocation: {args}")
        return proc

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=_record_call)):
        out = await seed_workspace(_WS_ID, tar)
    assert out is True
    # Only inspect + test were called; no untar.
    cmds = ["/".join(c[1:3]) for c in call_log]
    assert any("inspect" in c for c in cmds)
    assert any("exec/" in c for c in cmds)
    # No third docker exec for the actual untar.
    assert len(call_log) == 2


@pytest.mark.asyncio
async def test_seed_runs_full_flow_when_marker_missing(tmp_path):
    tar = tmp_path / "ws.tar.gz"
    tar.write_bytes(b"x" * 100)

    call_count = {"n": 0}

    async def _record_call(*args, **kwargs):
        call_count["n"] += 1
        proc = AsyncMock()
        if "inspect" in args:
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"true\n", b""))
        elif "test" in args:
            # Marker missing → seed proceeds
            proc.returncode = 1
            proc.communicate = AsyncMock(return_value=(b"", b""))
        elif "sh" in args:
            # The untar invocation; assert command shape
            joined = " ".join(args)
            assert "tar -xzf" in joined
            assert "/workspace" in joined
            assert SEED_MARKER_PATH in joined
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
        else:
            raise AssertionError(f"unexpected docker invocation: {args}")
        return proc

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=_record_call)):
        out = await seed_workspace(_WS_ID, tar)
    assert out is True
    # 3 calls: inspect + test (marker probe) + the untar exec
    assert call_count["n"] == 3


@pytest.mark.asyncio
async def test_seed_returns_false_on_untar_failure(tmp_path):
    tar = tmp_path / "ws.tar.gz"
    tar.write_bytes(b"x")

    async def _record_call(*args, **kwargs):
        proc = AsyncMock()
        if "inspect" in args:
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"true\n", b""))
        elif "test" in args:
            proc.returncode = 1
            proc.communicate = AsyncMock(return_value=(b"", b""))
        elif "sh" in args:
            # Simulate untar failure
            proc.returncode = 2
            proc.communicate = AsyncMock(return_value=(b"", b"tar error"))
        return proc

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=_record_call)):
        out = await seed_workspace(_WS_ID, tar)
    assert out is False


# ── seed_workspace_from_fixture ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_seed_from_fixture_derives_path_and_seeds(tmp_path):
    # Build the expected layout: raw/<scenario>.json.gz + assets/<scenario>-workspace.tar.gz
    raw_dir = tmp_path / "raw"
    assets_dir = tmp_path / "assets"
    raw_dir.mkdir()
    assets_dir.mkdir()
    fixture = raw_dir / "test-scenario.json.gz"
    fixture.write_bytes(b"x")
    tarball = assets_dir / "test-scenario-workspace.tar.gz"
    tarball.write_bytes(b"y")

    async def _record_call(*args, **kwargs):
        proc = AsyncMock()
        if "inspect" in args:
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"true\n", b""))
        elif "test" in args:
            proc.returncode = 1
            proc.communicate = AsyncMock(return_value=(b"", b""))
        elif "sh" in args:
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=_record_call)):
        out = await seed_workspace_from_fixture(_WS_ID, fixture)
    assert out is True


@pytest.mark.asyncio
async def test_seed_from_fixture_returns_false_when_tarball_absent(tmp_path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (tmp_path / "assets").mkdir()  # exists but no scenario tarball
    fixture = raw_dir / "test-scenario.json.gz"
    fixture.write_bytes(b"x")
    out = await seed_workspace_from_fixture(_WS_ID, fixture)
    assert out is False

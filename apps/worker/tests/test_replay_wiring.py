"""Tests for the worker-side wiring that flips between live and replay shims.

These don't try to drive a full session — they just assert the
selection logic: ``POLARIS_REPLAY`` set → ReplayCodexSession; not set
→ would have called the real path (which we stop short of by patching
the IP resolver).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

import polaris_worker.agents.codex as codex_mod


@pytest.fixture(autouse=True)
def _reset_session_cache():
    """Worker's `_sessions` dict is module-global; clear it between
    tests so cached entries don't leak across cases."""
    codex_mod._sessions.clear()
    codex_mod._session_locks.clear()
    yield
    codex_mod._sessions.clear()
    codex_mod._session_locks.clear()


class _FakeSettings:
    codex_model = "gpt-5"
    codex_approval_policy = "never"
    codex_turn_timeout_seconds = 900
    codex_liveness_check_interval_seconds = 30


_GOLF_FIXTURE = (
    Path(__file__).parent.parent.parent.parent
    / "tests/fixtures/replay/raw/golf-landing-page.json.gz"
)


@pytest.mark.asyncio
async def test_get_or_open_session_returns_replay_session_when_env_set(monkeypatch):
    """POLARIS_REPLAY=<fixture> short-circuits the codex session
    factory and returns a ReplayCodexSession."""
    if not _GOLF_FIXTURE.exists():
        pytest.skip(f"golf fixture missing at {_GOLF_FIXTURE}")
    monkeypatch.setenv("POLARIS_REPLAY", str(_GOLF_FIXTURE))

    from polaris_agent_core.replay_codex_session import ReplayCodexSession

    workspace_id = uuid4()
    session = await codex_mod._get_or_open_session(workspace_id, _FakeSettings())
    assert isinstance(session, ReplayCodexSession)
    assert session.ws_url == "replay://"


@pytest.mark.asyncio
async def test_get_or_open_session_caches_replay_session(monkeypatch):
    """Repeated calls for the same workspace_id reuse the same
    ReplayCodexSession — otherwise each turn would re-read the
    fixture and reset its cursor."""
    if not _GOLF_FIXTURE.exists():
        pytest.skip(f"golf fixture missing at {_GOLF_FIXTURE}")
    monkeypatch.setenv("POLARIS_REPLAY", str(_GOLF_FIXTURE))

    workspace_id = uuid4()
    s1 = await codex_mod._get_or_open_session(workspace_id, _FakeSettings())
    s2 = await codex_mod._get_or_open_session(workspace_id, _FakeSettings())
    assert s1 is s2


@pytest.mark.asyncio
async def test_get_or_open_session_skips_real_path_in_replay_mode(monkeypatch):
    """In replay mode, ``_resolve_container_ip`` (which docker exec's)
    must NOT be called — replay sessions don't need a workspace
    container's IP for codex (the workspace container still runs for
    the IDE iframe, but the codex inside it goes unused).  This test
    fails loudly if a future refactor accidentally falls through to
    the live path."""
    if not _GOLF_FIXTURE.exists():
        pytest.skip(f"golf fixture missing at {_GOLF_FIXTURE}")
    monkeypatch.setenv("POLARIS_REPLAY", str(_GOLF_FIXTURE))
    workspace_id = uuid4()

    with patch.object(codex_mod, "_resolve_container_ip", new=AsyncMock(
        side_effect=AssertionError(
            "_resolve_container_ip called in replay mode — "
            "replay path should short-circuit before this"
        )
    )):
        session = await codex_mod._get_or_open_session(workspace_id, _FakeSettings())
        assert session.ws_url == "replay://"


@pytest.mark.asyncio
async def test_replay_session_has_dynamic_tools_registered(monkeypatch):
    """Even in replay, the session config must list set_project_root
    and focus_browser as dynamic tools — the recorded codex stream
    contains item/tool/call requests for these and the worker's
    handler needs the names to dispatch."""
    if not _GOLF_FIXTURE.exists():
        pytest.skip(f"golf fixture missing at {_GOLF_FIXTURE}")
    monkeypatch.setenv("POLARIS_REPLAY", str(_GOLF_FIXTURE))

    workspace_id = uuid4()
    session = await codex_mod._get_or_open_session(workspace_id, _FakeSettings())
    tool_names = {t.get("name") for t in session._config.dynamic_tools}
    assert "set_project_root" in tool_names
    assert "focus_browser" in tool_names

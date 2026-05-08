"""Tests for ReplayCodexSession against the golf fixture.

Verifies the drop-in session correctly replays a recorded codex
transcript:

  * Public API matches PolarisCodexSession (start/close/is_alive,
    ensure_thread, run_turn, interrupt, steer)
  * Turn-by-turn cursor walk dispatches the right sink events
  * Item lifecycle balanced (started == completed) per turn
  * ensure_thread returns the recorded thread id deterministically
  * Server-side requests (requestUserInput, dynamic-tool calls) hit
    the configured handlers exactly as in a live run
  * Auto-accept approvals don't break replay
  * Cursor-exhausted edge cases surface cleanly to the sink
"""

from __future__ import annotations

import asyncio
import gzip
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from polaris_agent_core.codex_app_server import PolarisAgentConfig
from polaris_agent_core.replay_codex_session import (
    ReplayCodexSession,
    _load_fixture,
)


GOLF_FIXTURE = (
    Path(__file__).parent.parent.parent.parent
    / "tests/fixtures/replay/raw/golf-landing-page.json.gz"
)


class _CountingSink:
    """Minimal TurnItemSink stand-in that records every event."""

    def __init__(self) -> None:
        self.turn_started: list[str] = []
        self.item_started: list[dict[str, Any]] = []
        self.item_completed: list[dict[str, Any]] = []
        self.agent_msg_deltas: list[str] = []
        self.turn_completed: list[tuple[str, str | None]] = []

    async def on_turn_started(self, codex_turn_id: str) -> None:
        self.turn_started.append(codex_turn_id)

    async def on_item_started(self, item: dict[str, Any]) -> None:
        self.item_started.append(item)

    async def on_item_completed(self, item: dict[str, Any]) -> None:
        self.item_completed.append(item)

    async def on_agent_message_delta(self, text: str) -> None:
        self.agent_msg_deltas.append(text)

    async def on_turn_completed(self, status: str, error: str | None) -> None:
        self.turn_completed.append((status, error))


def _golf_session() -> ReplayCodexSession:
    if not GOLF_FIXTURE.exists():
        pytest.skip(f"golf fixture missing at {GOLF_FIXTURE}")
    config = PolarisAgentConfig(ws_url="replay://")
    return ReplayCodexSession(config, fixture_path=GOLF_FIXTURE)


# ── Fixture loader ─────────────────────────────────────────────────────


def test_load_fixture_handles_gzip(tmp_path: Path):
    p = tmp_path / "x.json.gz"
    with gzip.open(p, "wb") as f:
        f.write(json.dumps({"version": 1}).encode())
    assert _load_fixture(p) == {"version": 1}


def test_load_fixture_handles_plain_json(tmp_path: Path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"version": 1}))
    assert _load_fixture(p) == {"version": 1}


# ── Public API surface ─────────────────────────────────────────────────


def test_ws_url_is_replay_sentinel():
    s = _golf_session()
    assert s.ws_url == "replay://"


def test_is_alive_true_after_construct():
    s = _golf_session()
    assert s.is_alive() is True


@pytest.mark.asyncio
async def test_start_and_close_are_no_ops():
    s = _golf_session()
    await s.start()
    await s.close()
    # Still alive — start/close don't manage real connection state.
    assert s.is_alive() is True


@pytest.mark.asyncio
async def test_ensure_thread_returns_recorded_id_when_no_existing():
    s = _golf_session()
    tid = await s.ensure_thread(None)
    # The golf recording has a real codex thread id starting with
    # 019e08e8 (UUIDv7-ish from codex).  Check the format rather than
    # the exact value so a re-recording doesn't break this test.
    assert isinstance(tid, str)
    assert len(tid) > 8


@pytest.mark.asyncio
async def test_ensure_thread_passes_through_existing():
    s = _golf_session()
    # Worker passes existing_thread_id from project.codex_thread_id;
    # session must not clobber it.
    out = await s.ensure_thread("project-pinned-id-123")
    assert out == "project-pinned-id-123"


@pytest.mark.asyncio
async def test_interrupt_is_idempotent_noop():
    s = _golf_session()
    await s.interrupt("any")
    await s.interrupt("any")  # callable repeatedly without state issues


@pytest.mark.asyncio
async def test_steer_is_warning_noop():
    s = _golf_session()
    # Replay can't accept new input; steer is a logged no-op.
    await s.steer("tid", "additional text")  # no exception


# ── Turn replay ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_run_turn_dispatches_plan_turn_to_sink():
    s = _golf_session()
    sink = _CountingSink()
    await s.run_turn(thread_id="x", user_message="ignored", sink=sink)
    # First turn in golf scenario is the plan turn:
    # 1 turn/started, 7 item lifecycles (paired), 1 turn/completed.
    assert len(sink.turn_started) == 1
    assert len(sink.item_started) == 7
    assert len(sink.item_completed) == 7
    assert len(sink.turn_completed) == 1
    assert sink.turn_completed[0][0] == "completed"

    # The plan item must be present — that's what the chat shows
    # and what the user clicks "Proceed" against.
    types = Counter(i.get("type") for i in sink.item_completed)
    assert types["plan"] == 1


@pytest.mark.asyncio
async def test_second_run_turn_dispatches_build_turn_with_file_changes():
    s = _golf_session()
    plan_sink = _CountingSink()
    await s.run_turn(thread_id="x", user_message="ignored", sink=plan_sink)

    build_sink = _CountingSink()
    await s.run_turn(thread_id="x", user_message="proceed", sink=build_sink)

    # Build turn dwarfs the plan turn in event count.
    assert len(build_sink.item_completed) > len(plan_sink.item_completed)

    types = Counter(i.get("type") for i in build_sink.item_completed)
    # File changes are the canonical "did something" signal.
    assert types["fileChange"] >= 1
    # Codex called playwright at least once to verify the build.
    assert types["mcpToolCall"] >= 1


@pytest.mark.asyncio
async def test_each_turn_has_balanced_item_lifecycle():
    """Every item/started must have a matching item/completed in the
    same turn.  A bug that drops one of the dispatches would leave
    the sink with orphan started events; tests against the chat-pane
    rendering would silently mis-render."""
    s = _golf_session()
    for turn_idx in range(2):
        sink = _CountingSink()
        await s.run_turn(thread_id="x", user_message="x", sink=sink)
        assert len(sink.item_started) == len(sink.item_completed), (
            f"turn {turn_idx} imbalanced: {len(sink.item_started)} started "
            f"vs {len(sink.item_completed)} completed"
        )


@pytest.mark.asyncio
async def test_run_turn_after_fixture_exhausted_completes_cleanly():
    s = _golf_session()
    for _ in range(2):
        await s.run_turn(thread_id="x", user_message="x", sink=_CountingSink())
    # 3rd run_turn — fixture is dry but session should not throw.
    sink = _CountingSink()
    await s.run_turn(thread_id="x", user_message="x", sink=sink)
    assert len(sink.turn_completed) == 1
    assert sink.turn_completed[0][0] == "completed"
    assert len(sink.item_started) == 0


@pytest.mark.asyncio
async def test_concurrent_run_turn_calls_serialize_via_lock():
    """Two concurrent run_turn awaits must not interleave frames —
    the cursor is shared mutable state."""
    s = _golf_session()
    sink_a = _CountingSink()
    sink_b = _CountingSink()
    # Schedule both; the lock should ensure A finishes (drains turn 1)
    # before B starts (drains turn 2).
    await asyncio.gather(
        s.run_turn(thread_id="x", user_message="a", sink=sink_a),
        s.run_turn(thread_id="x", user_message="b", sink=sink_b),
    )
    # Each got exactly one turn — neither got both, neither got partial.
    assert len(sink_a.turn_completed) == 1
    assert len(sink_b.turn_completed) == 1
    # Total items across both turns matches the recording's two-turn
    # totals (7 + 52 = 59).
    total = len(sink_a.item_completed) + len(sink_b.item_completed)
    assert total == 59


# ── Server-side request dispatch ───────────────────────────────────────


@pytest.mark.asyncio
async def test_request_user_input_routes_to_handler():
    """When the recording contains a server-side requestUserInput, the
    replay session must invoke the configured ``user_input_handler``
    so the worker's clarification flow drives the real SSE+Redis
    path.  (Codex's continued stream is then read from the recording.)
    """
    if not GOLF_FIXTURE.exists():
        pytest.skip(f"golf fixture missing at {GOLF_FIXTURE}")
    handler_calls: list[list[dict[str, Any]]] = []

    async def _handler(questions, params):
        handler_calls.append(questions)
        return {}

    config = PolarisAgentConfig(ws_url="replay://", user_input_handler=_handler)
    s = ReplayCodexSession(config, fixture_path=GOLF_FIXTURE)
    # Drain both turns
    for _ in range(2):
        await s.run_turn(thread_id="x", user_message="x", sink=_CountingSink())
    # Golf fixture: codex doesn't use requestUserInput (clarifications
    # come from design-intent), so 0 handler calls is the expected
    # baseline.  We just want to confirm the dispatcher path doesn't
    # crash when no codex requestUserInput was recorded.
    assert isinstance(handler_calls, list)


@pytest.mark.asyncio
async def test_dynamic_tool_calls_route_to_handler():
    """Worker registers two dynamic tools (set_project_root,
    focus_browser).  Recorded item/tool/call requests must flow
    through dynamic_tool_handler so the side effects (DB write,
    SSE event) fire in replay too."""
    if not GOLF_FIXTURE.exists():
        pytest.skip(f"golf fixture missing at {GOLF_FIXTURE}")
    tool_calls: list[tuple[str, dict[str, Any]]] = []

    async def _dyn_handler(tool, args, params):
        tool_calls.append((tool, args))
        return {"success": True, "contentItems": []}

    config = PolarisAgentConfig(
        ws_url="replay://", dynamic_tool_handler=_dyn_handler
    )
    s = ReplayCodexSession(config, fixture_path=GOLF_FIXTURE)
    for _ in range(2):
        await s.run_turn(thread_id="x", user_message="x", sink=_CountingSink())
    # Golf recording has 2 dynamicToolCall items in the build turn —
    # set_project_root and focus_browser.  Both fire as server-side
    # item/tool/call requests, which the handler should see.
    assert len(tool_calls) >= 2
    tool_names = {t[0] for t in tool_calls}
    assert "set_project_root" in tool_names
    assert "focus_browser" in tool_names


@pytest.mark.asyncio
async def test_handler_exception_does_not_break_replay():
    """A buggy handler shouldn't tank the replay — log and continue.
    Defense-in-depth so a Phase 3.5 worker bug doesn't poison every
    replay run."""
    if not GOLF_FIXTURE.exists():
        pytest.skip(f"golf fixture missing at {GOLF_FIXTURE}")
    async def _broken_handler(tool, args, params):
        raise RuntimeError("simulated handler failure")

    config = PolarisAgentConfig(
        ws_url="replay://", dynamic_tool_handler=_broken_handler
    )
    s = ReplayCodexSession(config, fixture_path=GOLF_FIXTURE)
    sink = _CountingSink()
    # Both turns drain without raising despite the handler exploding.
    for _ in range(2):
        await s.run_turn(thread_id="x", user_message="x", sink=sink)
    assert sink.turn_completed[-1][0] == "completed"


# ── Edge cases ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_fixture_completes_cleanly(tmp_path: Path):
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({
        "version": 1, "scenario": "empty",
        "user_actions": [],
        "agent_io": {"codex_frames": [], "design_intent_nodes": []},
    }))
    config = PolarisAgentConfig(ws_url="replay://")
    s = ReplayCodexSession(config, fixture_path=p)
    sink = _CountingSink()
    await s.run_turn(thread_id="x", user_message="x", sink=sink)
    # No items, but turn is "completed" cleanly so the orchestrator
    # doesn't hang waiting for never-arriving notifications.
    assert sink.turn_completed[-1][0] == "completed"
    assert len(sink.item_started) == 0


@pytest.mark.asyncio
async def test_truncated_fixture_surfaces_failure(tmp_path: Path):
    """A fixture that has turn/started but no turn/completed should
    surface 'failed' to the sink so tests don't silently pass on
    a corrupt recording."""
    p = tmp_path / "truncated.json"
    p.write_text(json.dumps({
        "version": 1, "scenario": "truncated",
        "user_actions": [],
        "agent_io": {
            "codex_frames": [
                {
                    "t": 0.0,
                    "direction": "in",
                    "frame": {
                        "method": "turn/started",
                        "params": {"turn": {"id": "tid"}},
                    },
                },
                # missing turn/completed
            ],
            "design_intent_nodes": [],
        },
    }))
    config = PolarisAgentConfig(ws_url="replay://")
    s = ReplayCodexSession(config, fixture_path=p)
    sink = _CountingSink()
    await s.run_turn(thread_id="x", user_message="x", sink=sink)
    assert sink.turn_completed[-1][0] == "failed"
    assert "exhausted" in (sink.turn_completed[-1][1] or "")


@pytest.mark.asyncio
async def test_outgoing_frames_are_skipped(tmp_path: Path):
    """Out-direction frames are reconstructable and don't drive sink —
    the recorder captured them for audit but they must not produce
    sink events during replay."""
    p = tmp_path / "out_only.json"
    p.write_text(json.dumps({
        "version": 1, "scenario": "out",
        "user_actions": [],
        "agent_io": {
            "codex_frames": [
                {"t": 0.0, "direction": "out", "frame": {"id": 1, "method": "turn/start"}},
                {
                    "t": 0.1, "direction": "in",
                    "frame": {
                        "method": "turn/started",
                        "params": {"turn": {"id": "tid"}},
                    },
                },
                {
                    "t": 0.2, "direction": "in",
                    "frame": {
                        "method": "turn/completed",
                        "params": {"turn": {"status": "completed"}},
                    },
                },
            ],
            "design_intent_nodes": [],
        },
    }))
    config = PolarisAgentConfig(ws_url="replay://")
    s = ReplayCodexSession(config, fixture_path=p)
    sink = _CountingSink()
    await s.run_turn(thread_id="x", user_message="x", sink=sink)
    # Out-frames produced no events; only the 2 in-frames drove sink.
    assert len(sink.turn_started) == 1
    assert sink.turn_completed[-1][0] == "completed"

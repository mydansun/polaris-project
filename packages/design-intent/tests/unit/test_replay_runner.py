"""Tests for the replay-mode design-intent runner.

Validates the runner against the canonical golf-landing-page fixture:

  * walks all recorded clarifier_ask interrupts and forwards their
    questions to the supplied user_input_fn (so the worker's
    SSE/Redis clarification flow drives the UI just like a live run)
  * reconstructs a fully-populated CompiledBrief from the recorded
    compiler output (intent + brief + pinterest_refs + queries +
    mood_board_b64) so downstream worker code can treat it
    indistinguishably from a real result
  * fires LangChain on_chain_start callbacks per node so the worker's
    progress handler emits the same chat-pane bubbles
  * raises cleanly when the fixture has no design_intent_nodes (a
    misconfigured / corrupt recording)
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from polaris_design_intent.replay_runner import (
    _compiled_brief_from_recording,
    _questions_from_clarifier_ask,
    load_raw_fixture,
    replay_run_design_intent,
)

GOLF_FIXTURE = Path(__file__).parent.parent.parent.parent.parent / (
    "tests/fixtures/replay/raw/golf-landing-page.json.gz"
)


@pytest.fixture
def golf_fixture_data() -> dict[str, Any]:
    if not GOLF_FIXTURE.exists():
        pytest.skip(f"golf fixture missing at {GOLF_FIXTURE}")
    return load_raw_fixture(GOLF_FIXTURE)


# ── load_raw_fixture ───────────────────────────────────────────────────


def test_load_raw_handles_gzip(tmp_path: Path):
    src = {"version": 1, "scenario": "x"}
    p = tmp_path / "x.json.gz"
    with gzip.open(p, "wb") as f:
        f.write(json.dumps(src).encode())
    assert load_raw_fixture(p) == src


def test_load_raw_handles_plain_json(tmp_path: Path):
    src = {"version": 1, "scenario": "x"}
    p = tmp_path / "x.json"
    p.write_text(json.dumps(src))
    assert load_raw_fixture(p) == src


# ── question extraction ────────────────────────────────────────────────


def test_questions_from_clarifier_ask_pulls_structured_payload(golf_fixture_data):
    nodes = golf_fixture_data["agent_io"]["design_intent_nodes"]
    asks = [n for n in nodes if n["node"] == "clarifier_ask"]
    assert len(asks) == 3, "golf scenario should have 3 clarification interrupts"
    for ask in asks:
        qs = _questions_from_clarifier_ask(ask["output"])
        assert len(qs) >= 1, "every clarifier_ask must yield at least 1 question"
        for q in qs:
            assert "id" in q and "title" in q and "choices" in q


def test_questions_from_clarifier_ask_returns_empty_on_missing_messages():
    assert _questions_from_clarifier_ask({}) == []
    assert _questions_from_clarifier_ask({"messages": []}) == []


def test_questions_from_clarifier_ask_returns_empty_when_no_ask_tool_call():
    output = {
        "messages": [
            {
                "_message_type": "ai",
                "content": "no tool calls here",
                "tool_calls": [
                    {"name": "some_other_tool", "args": {}}
                ],
            }
        ]
    }
    assert _questions_from_clarifier_ask(output) == []


# ── CompiledBrief reconstruction ───────────────────────────────────────


def test_compiled_brief_from_recording_populates_all_fields(golf_fixture_data):
    nodes = golf_fixture_data["agent_io"]["design_intent_nodes"]
    brief = _compiled_brief_from_recording(nodes)
    # Intent fields
    assert brief.intent.pageType, "pageType should land in the reconstructed brief"
    assert brief.intent.audience
    assert brief.intent.visualDirection
    assert brief.intent.accentColorHex
    # Brief text
    assert brief.brief, "brief text was lost in reconstruction"
    assert len(brief.brief) > 200, "brief looks truncated"
    # Pinterest refs (we requested 6 and recorded 6)
    assert len(brief.pinterest_refs) == 6
    # Mood board PNG
    assert brief.mood_board_b64 and len(brief.mood_board_b64) > 1_000_000


def test_compiled_brief_from_recording_handles_empty_compiler():
    # Recorder may capture intermediate state where compiler hasn't
    # run yet (interrupted recording).  Reconstruction must not crash
    # — caller (worker) sees an empty brief and surfaces the failure
    # at the orchestration layer instead.
    brief = _compiled_brief_from_recording([])
    assert brief.brief == ""
    assert brief.pinterest_refs == []
    assert brief.mood_board_b64 is None


# ── End-to-end replay ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_replay_calls_user_input_fn_once_per_ask_round(golf_fixture_data, tmp_path: Path):
    # The 3 recorded clarifier_ask interrupts each map to one
    # user_input_fn call.  Drive a fake user_input that records
    # every invocation so we can count and inspect rounds.
    rounds: list[list[dict[str, Any]]] = []

    async def _ui(qs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rounds.append(qs)
        # Real flow returns answers; replay runner discards the value
        # (real user clicks supply answers via SSE/Redis).  Return
        # empty.
        return []

    brief = await replay_run_design_intent(
        fixture_path=GOLF_FIXTURE, user_input_fn=_ui, callbacks=[]
    )
    assert len(rounds) == 3
    # Every round produced ≥1 structured question
    for r in rounds:
        assert len(r) >= 1
        for q in r:
            assert "id" in q
    # Brief is populated
    assert brief.intent.pageType
    assert brief.brief


@pytest.mark.asyncio
async def test_replay_fires_callbacks_for_each_node(golf_fixture_data):
    """Worker's progress handler subclasses LangChain AsyncCallbackHandler;
    we want chain_start to fire for every recorded node so SSE bubbles
    appear identically to a live run."""
    seen_starts: list[str] = []
    seen_ends: list[str] = []

    class _RecorderCallback:
        async def on_chain_start(self, *, name=None, **_kwargs):
            seen_starts.append(name or "?")

        async def on_chain_end(self, _outputs, *, name=None, **_kwargs):
            seen_ends.append(name or "?")

    async def _ui(qs):
        return []

    await replay_run_design_intent(
        fixture_path=GOLF_FIXTURE,
        user_input_fn=_ui,
        callbacks=[_RecorderCallback()],
    )
    # 16 recorded nodes → 16 chain_start
    assert len(seen_starts) == 16
    # The terminal node fires chain_end (mood_board_step in this fixture)
    assert seen_ends == ["mood_board_step"]


@pytest.mark.asyncio
async def test_replay_swallows_user_input_fn_errors(golf_fixture_data, caplog):
    """A buggy user_input_fn shouldn't tank the entire replay — log
    the failure and continue.  This protects the test harness from
    cascading mid-run failures when one round's UI selector breaks."""

    call_count = 0

    async def _ui_that_fails_once(qs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated UI failure")
        return []

    brief = await replay_run_design_intent(
        fixture_path=GOLF_FIXTURE, user_input_fn=_ui_that_fails_once
    )
    # All 3 rounds attempted despite first one raising
    assert call_count == 3
    # Brief still reconstructed
    assert brief.intent.pageType


@pytest.mark.asyncio
async def test_replay_raises_on_empty_fixture(tmp_path: Path):
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({
        "version": 1,
        "scenario": "empty",
        "user_actions": [],
        "agent_io": {"codex_frames": [], "design_intent_nodes": []},
    }))
    async def _ui(qs):
        return []
    with pytest.raises(RuntimeError, match="design_intent_nodes"):
        await replay_run_design_intent(fixture_path=p, user_input_fn=_ui)

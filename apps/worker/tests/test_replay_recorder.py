"""Unit tests for the replay recorder + cross-process merge.

These exercise the JsonFileRecorder against a temp dir, verify each
tap appends to its own JSONL file, then run ``merge_staging`` and
parse the result via the actual ``RawFixture`` schema so we know the
recorder + merger together produce a valid fixture.

No Redis, no asyncpg, no codex — pure file IO.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from polaris_worker.replay.recorder import (
    JsonFileRecorder,
    UserActionEvent,
    _NoopRecorder,
    _make_jsonable,
    _scenario_from_fixture_path,
    install_recorder,
    load_raw_fixture,
    merge_staging,
    staging_dir_for,
)
import polaris_worker.replay.recorder as recorder_module

# Pull in the schema validator so we test recorder output against the
# same shape the integrity test enforces.
_FIXTURES_DIR = Path(__file__).parent.parent.parent.parent / "tests" / "fixtures" / "replay"
sys.path.insert(0, str(_FIXTURES_DIR))
from _schema import RawFixture, compute_raw_hash  # noqa: E402


# ── staging_dir_for ────────────────────────────────────────────────────


def test_staging_dir_is_sibling_of_fixture():
    fixture = Path("/x/y/raw/golf.json")
    assert staging_dir_for(fixture) == Path("/x/y/raw/.staging-golf")


def test_staging_dir_uses_stem_not_full_filename():
    fixture = Path("/x/raw/foo.bar.json")
    # Path.stem strips one extension — keep the rest for scenarios with
    # versioned names like "golf.v2.json".
    assert staging_dir_for(fixture).name == ".staging-foo.bar"


def test_staging_dir_strips_double_extension_for_gzipped():
    # The `.json.gz` form is canonical for committed fixtures;
    # Path.stem only strips one extension, so a naive impl would
    # produce ``.staging-golf.json`` and desync from the api side
    # (which derives the same path from the same env var).  Pin
    # the double-strip so worker + api converge on the same dir.
    fixture = Path("/x/raw/golf.json.gz")
    assert staging_dir_for(fixture).name == ".staging-golf"


def test_scenario_stem_handles_both_extensions():
    # Used inside merge_staging to write the in-fixture scenario
    # field — must give "golf" regardless of the file extension
    # so the dummy + gzipped fixtures have identical scenario
    # values for cross-checking.
    assert _scenario_from_fixture_path(Path("/x/golf.json")) == "golf"
    assert _scenario_from_fixture_path(Path("/x/golf.json.gz")) == "golf"
    assert _scenario_from_fixture_path(Path("/x/golf.v2.json")) == "golf.v2"


# ── JsonFileRecorder ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recorder_creates_staging_dir_and_marker(tmp_path):
    fixture = tmp_path / "raw" / "test.json"
    rec = JsonFileRecorder(fixture)
    await rec.start()
    staging = staging_dir_for(fixture)
    assert staging.is_dir()
    marker = staging / "MARKER"
    assert marker.exists()
    text = marker.read_text()
    assert "recording: test" in text
    assert "started_at:" in text


@pytest.mark.asyncio
async def test_codex_frames_each_become_one_jsonl_line(tmp_path):
    fixture = tmp_path / "raw" / "test.json"
    rec = JsonFileRecorder(fixture)
    await rec.on_codex_frame("out", {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    await rec.on_codex_frame("in", {"jsonrpc": "2.0", "id": 1, "result": {}})
    await rec.on_codex_frame("in", {"jsonrpc": "2.0", "method": "turn/started"})
    lines = (staging_dir_for(fixture) / "codex-frames.jsonl").read_text().splitlines()
    assert len(lines) == 3
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["direction"] == "out"
    assert parsed[0]["frame"]["method"] == "initialize"
    assert parsed[2]["frame"]["method"] == "turn/started"


@pytest.mark.asyncio
async def test_design_intent_node_outputs_persist_pydantic_models(tmp_path):
    # Real graph nodes return dicts that often contain pydantic models
    # (DesignIntent, CompiledBrief).  The recorder must serialize them
    # via .model_dump() rather than crash.
    from pydantic import BaseModel

    class FakeBrief(BaseModel):
        text: str
        score: float

    fixture = tmp_path / "raw" / "test.json"
    rec = JsonFileRecorder(fixture)
    await rec.on_design_intent_node(
        "compiler", {"brief": FakeBrief(text="hi", score=0.9)}
    )
    line = (staging_dir_for(fixture) / "design-intent-nodes.jsonl").read_text().strip()
    parsed = json.loads(line)
    assert parsed["node"] == "compiler"
    assert parsed["output"]["brief"] == {"text": "hi", "score": 0.9}


@pytest.mark.asyncio
async def test_user_action_persists_full_dataclass(tmp_path):
    fixture = tmp_path / "raw" / "test.json"
    rec = JsonFileRecorder(fixture)
    event = UserActionEvent(
        t=1.5,
        kind="click",
        concrete={"selector": "[data-testid=foo]", "viewport_xy": [10, 20]},
        a11y_snapshot={"target": {"role": "button", "name": "Hi"}},
    )
    await rec.on_user_action(event)
    line = (staging_dir_for(fixture) / "user-actions.jsonl").read_text().strip()
    parsed = json.loads(line)
    assert parsed["t"] == 1.5
    assert parsed["kind"] == "click"
    assert parsed["concrete"]["selector"] == "[data-testid=foo]"
    assert parsed["a11y_snapshot"]["target"]["name"] == "Hi"


@pytest.mark.asyncio
async def test_recorder_failures_dont_propagate(tmp_path, monkeypatch):
    # Simulate a disk error in the append path; the recorder must
    # swallow it so the live turn isn't disturbed.  Earlier bugs had
    # us bubble exceptions up to the orchestrator.
    fixture = tmp_path / "raw" / "test.json"
    rec = JsonFileRecorder(fixture)

    def boom(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(recorder_module, "_sync_append", boom)
    # Should not raise.
    await rec.on_codex_frame("in", {"x": 1})
    await rec.on_design_intent_node("foo", {"y": 2})
    await rec.on_user_action(UserActionEvent(t=0.0, kind="click"))


@pytest.mark.asyncio
async def test_recorder_handles_concurrent_writes(tmp_path):
    fixture = tmp_path / "raw" / "test.json"
    rec = JsonFileRecorder(fixture)
    # Fan out 50 codex frames concurrently — the lock + asyncio.to_thread
    # combination must not interleave bytes within a single line.
    await asyncio.gather(
        *(rec.on_codex_frame("in", {"i": i, "method": "x"}) for i in range(50))
    )
    raw_text = (staging_dir_for(fixture) / "codex-frames.jsonl").read_text()
    lines = raw_text.splitlines()
    assert len(lines) == 50
    parsed = [json.loads(line) for line in lines]
    seen = {p["frame"]["i"] for p in parsed}
    assert seen == set(range(50))


# ── _make_jsonable ─────────────────────────────────────────────────────


def test_make_jsonable_passes_primitives_through():
    assert _make_jsonable(None) is None
    assert _make_jsonable(1) == 1
    assert _make_jsonable("hi") == "hi"
    assert _make_jsonable(True) is True


def test_make_jsonable_recurses_into_dicts_and_lists():
    out = _make_jsonable({"a": [1, {"b": "c"}], "d": (4, 5)})
    assert out == {"a": [1, {"b": "c"}], "d": [4, 5]}


def test_make_jsonable_serializes_pydantic_models():
    from pydantic import BaseModel

    class M(BaseModel):
        a: int
        b: str

    assert _make_jsonable(M(a=1, b="x")) == {"a": 1, "b": "x"}


def test_make_jsonable_falls_back_to_repr_for_unknown():
    class Weird:
        def __repr__(self) -> str:
            return "Weird()"

    out = _make_jsonable(Weird())
    assert out == {"_repr": "Weird()"}


def test_make_jsonable_truncates_long_repr():
    class HugeRepr:
        def __repr__(self) -> str:
            return "x" * 1000

    out = _make_jsonable(HugeRepr())
    assert len(out["_repr"]) <= 500


# ── merge_staging ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_merge_produces_schema_valid_raw_fixture(tmp_path):
    fixture = tmp_path / "raw" / "scenario.json"
    rec = JsonFileRecorder(fixture)
    await rec.on_codex_frame("out", {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    await rec.on_design_intent_node("clarifier_step", {"messages": []})
    await rec.on_user_action(
        UserActionEvent(t=0.0, kind="navigate", concrete={"path": "/"})
    )
    out = merge_staging(fixture)
    assert out == fixture
    data = json.loads(out.read_text())
    parsed = RawFixture(**data)
    assert parsed.scenario == "scenario"
    assert len(parsed.user_actions) == 1
    assert len(parsed.agent_io.codex_frames) == 1
    assert len(parsed.agent_io.design_intent_nodes) == 1


@pytest.mark.asyncio
async def test_merge_rebases_timestamps_to_t0(tmp_path):
    # Earliest event becomes t=0; everything else is positive offset.
    fixture = tmp_path / "raw" / "scenario.json"
    rec = JsonFileRecorder(fixture)
    await rec.on_codex_frame("out", {"method": "first"})
    await asyncio.sleep(0.05)
    await rec.on_user_action(UserActionEvent(t=99.0, kind="click"))
    await asyncio.sleep(0.05)
    await rec.on_codex_frame("in", {"method": "later"})
    merge_staging(fixture)
    data = json.loads(fixture.read_text())
    frame_times = [f["t"] for f in data["agent_io"]["codex_frames"]]
    user_times = [u["t"] for u in data["user_actions"]]
    assert frame_times[0] == 0.0  # earliest event in any stream rebases to 0
    assert frame_times[1] >= 0.05
    # user_actions[].t in merged output is the post-rebase server-side
    # timestamp, not the web's relative t the dataclass carried.
    assert user_times[0] >= 0.0


@pytest.mark.asyncio
async def test_merge_sorts_within_each_stream_by_t(tmp_path):
    fixture = tmp_path / "raw" / "scenario.json"
    rec = JsonFileRecorder(fixture)
    # Out-of-order writes shouldn't matter — merge sorts on the recorded
    # ts, which is monotonic with our awaits.
    await rec.on_codex_frame("out", {"method": "a"})
    await asyncio.sleep(0.02)
    await rec.on_codex_frame("in", {"method": "b"})
    await asyncio.sleep(0.02)
    await rec.on_codex_frame("out", {"method": "c"})
    merge_staging(fixture)
    data = json.loads(fixture.read_text())
    methods = [f["frame"]["method"] for f in data["agent_io"]["codex_frames"]]
    times = [f["t"] for f in data["agent_io"]["codex_frames"]]
    assert methods == ["a", "b", "c"]
    assert times == sorted(times)


@pytest.mark.asyncio
async def test_merge_skips_malformed_jsonl_lines(tmp_path):
    fixture = tmp_path / "raw" / "scenario.json"
    rec = JsonFileRecorder(fixture)
    await rec.on_codex_frame("out", {"method": "ok"})
    # Inject a bad line directly — simulating a partial write from a
    # crashed worker that didn't fsync the trailing newline cleanly.
    bad_path = staging_dir_for(fixture) / "codex-frames.jsonl"
    with bad_path.open("a", encoding="utf-8") as f:
        f.write("not-json{}\n")
    await rec.on_codex_frame("in", {"method": "still-ok"})
    merge_staging(fixture)
    data = json.loads(fixture.read_text())
    methods = [f["frame"]["method"] for f in data["agent_io"]["codex_frames"]]
    assert methods == ["ok", "still-ok"]


@pytest.mark.asyncio
async def test_merge_cleans_up_staging_by_default(tmp_path):
    fixture = tmp_path / "raw" / "scenario.json"
    rec = JsonFileRecorder(fixture)
    await rec.on_codex_frame("out", {"method": "x"})
    staging = staging_dir_for(fixture)
    assert staging.exists()
    merge_staging(fixture)
    assert not staging.exists()


@pytest.mark.asyncio
async def test_merge_keeps_staging_when_cleanup_disabled(tmp_path):
    fixture = tmp_path / "raw" / "scenario.json"
    rec = JsonFileRecorder(fixture)
    await rec.on_codex_frame("out", {"method": "x"})
    merge_staging(fixture, cleanup=False)
    assert staging_dir_for(fixture).exists()


def test_merge_raises_when_no_staging_dir(tmp_path):
    fixture = tmp_path / "raw" / "ghost.json"
    with pytest.raises(FileNotFoundError):
        merge_staging(fixture)


@pytest.mark.asyncio
async def test_merged_fixture_passes_compute_raw_hash_round_trip(tmp_path):
    # Sanity: the recorder's output is well-formed enough that the
    # canonical hash (which annotation pins) computes without error.
    fixture = tmp_path / "raw" / "scenario.json"
    rec = JsonFileRecorder(fixture)
    await rec.on_codex_frame("out", {"method": "x"})
    await rec.on_user_action(UserActionEvent(t=0.0, kind="click"))
    merge_staging(fixture)
    parsed = RawFixture(**json.loads(fixture.read_text()))
    h1 = compute_raw_hash(parsed)
    h2 = compute_raw_hash(parsed)
    assert h1 == h2
    assert len(h1) == 64


# ── Singleton swap ─────────────────────────────────────────────────────


def test_install_recorder_swaps_module_singleton(tmp_path):
    fixture = tmp_path / "raw" / "x.json"
    real = JsonFileRecorder(fixture)
    install_recorder(real)
    try:
        from polaris_worker.replay import recorder as r

        assert r.Recorder is real
    finally:
        install_recorder(_NoopRecorder())


def test_get_recorder_returns_current_binding(tmp_path):
    # Imported once at module load, get_recorder() must still see swaps.
    from polaris_worker.replay import get_recorder

    fixture = tmp_path / "raw" / "x.json"
    real = JsonFileRecorder(fixture)
    install_recorder(real)
    try:
        assert get_recorder() is real
    finally:
        install_recorder(_NoopRecorder())

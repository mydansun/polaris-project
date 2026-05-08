"""Replay fixture integrity guard.

Two checks per fixture pair:

  1. **Parse validity.** Every ``raw/<scenario>.json`` must conform to
     ``RawFixture`` and every ``annotated/<scenario>.json`` to
     ``AnnotatedFixture``.  A pydantic validation error here means the
     recording or annotation is broken.

  2. **Hash linkage.** Every annotated file's ``raw_hash`` must match the
     SHA256 of its companion raw file's events array.  A drift here
     means raw was re-recorded but annotation wasn't refreshed; the
     replay would silently use stale semantic info.

The tests parametrize over every scenario in the directory, so dropping
a new fixture pair extends coverage automatically.  The shipped
``_dummy`` pair keeps the validator alive even on a fresh checkout.
"""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import pytest


_HERE = Path(__file__).parent

# Make the schema module importable as ``_schema`` without packaging
# it as a Python module — the fixture directory is path-only so test
# collection from anywhere in the repo still works.
sys.path.insert(0, str(_HERE))
from _schema import (  # noqa: E402  (intentional after sys.path tweak)
    AnnotatedFixture,
    RawFixture,
    compute_raw_hash,
)


def _scenario_name(path: Path) -> str:
    """Strip ``.json`` and ``.json.gz`` from a fixture filename to get the
    scenario name.  Mirrors the worker/api helpers."""
    name = path.name
    for suffix in (".json.gz", ".json"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def _load_raw(path: Path) -> dict:
    """Read a raw fixture, transparently un-gzipping when needed."""
    if path.suffix == ".gz":
        with gzip.open(path, "rb") as f:
            return json.loads(f.read().decode("utf-8"))
    return json.loads(path.read_text(encoding="utf-8"))


def _raw_path_for(scenario: str) -> Path | None:
    """Return whichever extension exists for this scenario, or None."""
    for ext in (".json.gz", ".json"):
        candidate = _HERE / "raw" / f"{scenario}{ext}"
        if candidate.exists():
            return candidate
    return None


def _scenarios() -> list[str]:
    """Return scenario names shared between raw/ and annotated/."""
    raw_dir = _HERE / "raw"
    annotated_dir = _HERE / "annotated"
    raw_names = {_scenario_name(p) for p in raw_dir.glob("*.json*")}
    annotated_names = {_scenario_name(p) for p in annotated_dir.glob("*.json")}
    # Scenarios may exist as raw without annotation yet (recently
    # recorded, awaiting annotation).  Tests cover both arms.
    return sorted(raw_names | annotated_names)


SCENARIOS = _scenarios()


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_raw_fixture_parses(scenario: str) -> None:
    raw_path = _raw_path_for(scenario)
    if raw_path is None:
        pytest.skip(f"no raw/{scenario}.json* — annotated-only entry")
    # Pydantic raises ValidationError on shape problems; pytest renders
    # it usefully without our help.
    RawFixture(**_load_raw(raw_path))


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_annotated_fixture_parses(scenario: str) -> None:
    annotated_path = _HERE / "annotated" / f"{scenario}.json"
    if not annotated_path.exists():
        pytest.skip(f"no annotated/{scenario}.json — raw-only entry")
    data = json.loads(annotated_path.read_text())
    AnnotatedFixture(**data)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_annotation_hash_matches_raw(scenario: str) -> None:
    raw_path = _raw_path_for(scenario)
    annotated_path = _HERE / "annotated" / f"{scenario}.json"
    if raw_path is None or not annotated_path.exists():
        pytest.skip(f"need both raw/ and annotated/ for {scenario}")
    raw = RawFixture(**_load_raw(raw_path))
    annotated = AnnotatedFixture(**json.loads(annotated_path.read_text()))
    expected = compute_raw_hash(raw)
    assert annotated.raw_hash == expected, (
        f"raw_hash mismatch for {scenario!r}: annotation says "
        f"{annotated.raw_hash[:12]}…, raw computes to {expected[:12]}… — "
        "raw was re-recorded; re-run scripts/replay_annotate.py "
        f"on raw/{scenario}.json to refresh"
    )


# ── Spot-checks that the schema actually rejects bad fixtures ──────────


def test_raw_fixture_rejects_unknown_version(tmp_path: Path) -> None:
    bad = {
        "version": 99,
        "scenario": "x",
        "recorded_at": "2026-05-08T00:00:00Z",
        "user_actions": [],
        "agent_io": {"codex_frames": [], "design_intent_nodes": []},
    }
    with pytest.raises(Exception):
        RawFixture(**bad)


def test_annotated_fixture_rejects_short_narrative() -> None:
    base = {
        "version": 1,
        "scenario": "x",
        "raw_hash": "0" * 64,
        "narrative": ["too short", "also short", "still short"],
        "key_invariants": ["a", "b", "c"],
        "actions": [],
    }
    with pytest.raises(Exception):
        AnnotatedFixture(**base)


def test_annotated_fixture_rejects_under_three_narrative_lines() -> None:
    base = {
        "version": 1,
        "scenario": "x",
        "raw_hash": "0" * 64,
        "narrative": ["only one really long enough line of narrative here"],
        "key_invariants": ["a", "b", "c"],
        "actions": [],
    }
    with pytest.raises(Exception):
        AnnotatedFixture(**base)


def test_compute_raw_hash_is_stable(tmp_path: Path) -> None:
    # Re-parsing + hashing the same payload twice yields the same digest.
    raw_path = _HERE / "raw" / "_dummy.json"
    if not raw_path.exists():
        pytest.skip("dummy raw missing")
    payload = json.loads(raw_path.read_text())
    a = compute_raw_hash(RawFixture(**payload))
    b = compute_raw_hash(RawFixture(**payload))
    assert a == b


def test_compute_raw_hash_is_independent_of_recorded_at() -> None:
    # ``recorded_at`` is cosmetic — re-running the same scenario shouldn't
    # invalidate the annotation just because the wall-clock advanced.
    raw_path = _HERE / "raw" / "_dummy.json"
    if not raw_path.exists():
        pytest.skip("dummy raw missing")
    payload = json.loads(raw_path.read_text())
    h1 = compute_raw_hash(RawFixture(**payload))
    payload["recorded_at"] = "2099-12-31T23:59:59Z"
    h2 = compute_raw_hash(RawFixture(**payload))
    assert h1 == h2

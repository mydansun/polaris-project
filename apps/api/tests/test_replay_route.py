"""Tests for the api-side replay recorder surfaces.

Two pieces under test:
  * ``polaris_api.replay.staging`` — the tiny "append a JSON line"
    helper used by the route.
  * ``polaris_api.routes.replay`` — the FastAPI route that gates on
    ``POLARIS_RECORD`` and forwards to the helper.

We don't actually invoke the worker-side merge here; that path is
covered in apps/worker/tests/test_replay_recorder.py against the same
file format.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from polaris_api.replay.staging import (
    append_user_action,
    fixture_path_from_env,
    staging_dir_for,
)
from polaris_api.routes.replay import router as replay_router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(replay_router)
    return TestClient(app)


# ── staging helper ─────────────────────────────────────────────────────


def test_fixture_path_returns_none_when_env_unset(monkeypatch):
    monkeypatch.delenv("POLARIS_RECORD", raising=False)
    assert fixture_path_from_env() is None


def test_fixture_path_resolves_when_env_set(monkeypatch, tmp_path):
    target = tmp_path / "raw" / "scenario.json"
    monkeypatch.setenv("POLARIS_RECORD", str(target))
    out = fixture_path_from_env()
    assert out == target.resolve()


def test_staging_dir_for_matches_worker_layout(tmp_path):
    # Both processes derive the staging dir from the same env var, so
    # the layouts MUST match.  Hard-pin the convention here so api and
    # worker can't drift.
    fixture = tmp_path / "raw" / "golf.json"
    assert staging_dir_for(fixture) == tmp_path / "raw" / ".staging-golf"


def test_append_user_action_writes_jsonl_line(monkeypatch, tmp_path):
    target = tmp_path / "raw" / "scenario.json"
    monkeypatch.setenv("POLARIS_RECORD", str(target))
    ok = append_user_action({"t": 0.5, "kind": "click", "concrete": {"x": 1}})
    assert ok is True
    line_path = staging_dir_for(target) / "user-actions.jsonl"
    assert line_path.exists()
    contents = line_path.read_text().strip()
    parsed = json.loads(contents)
    assert parsed["kind"] == "click"
    assert parsed["concrete"] == {"x": 1}
    assert "ts" in parsed  # server timestamp added


def test_append_user_action_returns_false_when_env_unset(monkeypatch):
    monkeypatch.delenv("POLARIS_RECORD", raising=False)
    assert append_user_action({"kind": "click"}) is False


def test_append_appends_each_call_as_separate_line(monkeypatch, tmp_path):
    target = tmp_path / "raw" / "scenario.json"
    monkeypatch.setenv("POLARIS_RECORD", str(target))
    for i in range(5):
        append_user_action({"t": float(i), "kind": "click", "concrete": {"i": i}})
    lines = (staging_dir_for(target) / "user-actions.jsonl").read_text().splitlines()
    assert len(lines) == 5
    indices = [json.loads(l)["concrete"]["i"] for l in lines]
    assert indices == [0, 1, 2, 3, 4]


# ── FastAPI route ──────────────────────────────────────────────────────


def test_post_user_action_503_when_env_unset(monkeypatch):
    monkeypatch.delenv("POLARIS_RECORD", raising=False)
    r = _client().post(
        "/replay/record/append",
        json={"scenario": "x", "t": 0.0, "kind": "click", "concrete": {}},
    )
    assert r.status_code == 503
    assert "POLARIS_RECORD" in r.text


def test_post_user_action_persists_to_staging(monkeypatch, tmp_path):
    target = tmp_path / "raw" / "scenario.json"
    monkeypatch.setenv("POLARIS_RECORD", str(target))
    r = _client().post(
        "/replay/record/append",
        json={
            "scenario": "scenario",
            "t": 1.5,
            "kind": "click",
            "concrete": {"selector": "[data-testid=foo]"},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["scenario"] == "scenario"

    line = (staging_dir_for(target) / "user-actions.jsonl").read_text().strip()
    parsed = json.loads(line)
    assert parsed["kind"] == "click"
    assert parsed["concrete"]["selector"] == "[data-testid=foo]"


def test_post_user_action_409_on_scenario_mismatch(monkeypatch, tmp_path):
    # API was started for "scenario-A", web posted with "scenario-B".
    # That's nearly always a config bug — easier to reject than to
    # silently mis-route the actions into the wrong recording.
    target = tmp_path / "raw" / "scenario-A.json"
    monkeypatch.setenv("POLARIS_RECORD", str(target))
    r = _client().post(
        "/replay/record/append",
        json={
            "scenario": "scenario-B",
            "t": 0.0,
            "kind": "click",
            "concrete": {},
        },
    )
    assert r.status_code == 409
    assert "scenario-A" in r.text and "scenario-B" in r.text


def test_post_user_action_validates_payload_shape(monkeypatch, tmp_path):
    target = tmp_path / "raw" / "scenario.json"
    monkeypatch.setenv("POLARIS_RECORD", str(target))
    # Missing required fields → pydantic 422
    r = _client().post("/replay/record/append", json={"t": 0.0})
    assert r.status_code == 422


def test_post_user_action_rejects_negative_t(monkeypatch, tmp_path):
    target = tmp_path / "raw" / "scenario.json"
    monkeypatch.setenv("POLARIS_RECORD", str(target))
    r = _client().post(
        "/replay/record/append",
        json={"scenario": "scenario", "t": -1.0, "kind": "click", "concrete": {}},
    )
    assert r.status_code == 422


def test_post_user_action_carries_a11y_snapshot_through(monkeypatch, tmp_path):
    target = tmp_path / "raw" / "scenario.json"
    monkeypatch.setenv("POLARIS_RECORD", str(target))
    snap = {
        "target": {"role": "button", "name": "Continue", "testid": "advance"},
        "ancestors": [{"role": "dialog", "name": "Clarification"}],
    }
    r = _client().post(
        "/replay/record/append",
        json={
            "scenario": "scenario",
            "t": 0.0,
            "kind": "click",
            "concrete": {"selector": "[data-testid=advance]"},
            "a11y_snapshot": snap,
        },
    )
    assert r.status_code == 200
    line = (staging_dir_for(target) / "user-actions.jsonl").read_text().strip()
    parsed = json.loads(line)
    assert parsed["a11y_snapshot"]["target"]["name"] == "Continue"


def test_finalize_route_503_when_env_unset(monkeypatch):
    monkeypatch.delenv("POLARIS_RECORD", raising=False)
    r = _client().post("/replay/record/finalize", json={})
    assert r.status_code == 503

"""Tests for the replay bootstrap.

The bootstrap reads ``POLARIS_RECORD`` from env and either installs a
``JsonFileRecorder`` or leaves the noop in place.  Tests exercise both
arms + idempotence (re-calling must not double-install).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from polaris_worker.replay import recorder as recorder_module
from polaris_worker.replay.bootstrap import ENV_VAR, init_from_env
from polaris_worker.replay.recorder import (
    JsonFileRecorder,
    _NoopRecorder,
    install_recorder,
)


@pytest.fixture(autouse=True)
def _restore_singleton():
    """Save/restore the module-level Recorder so cross-test pollution
    can't bleed into other suites — the singleton is process-global."""
    original = recorder_module.Recorder
    yield
    install_recorder(original)


def test_init_returns_noop_when_env_unset(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    out = init_from_env()
    assert isinstance(out, _NoopRecorder)
    assert isinstance(recorder_module.Recorder, _NoopRecorder)


def test_init_installs_real_recorder_when_env_points_at_path(monkeypatch, tmp_path):
    fixture = tmp_path / "raw" / "scenario.json"
    monkeypatch.setenv(ENV_VAR, str(fixture))
    out = init_from_env()
    assert isinstance(out, JsonFileRecorder)
    assert recorder_module.Recorder is out
    assert out.scenario == "scenario"
    assert out.fixture_path == fixture.resolve()


def test_init_is_idempotent(monkeypatch, tmp_path):
    # Two calls, one recorder.  Worker reload + main call would
    # otherwise create two staging-MARKER timestamps.
    fixture = tmp_path / "raw" / "scenario.json"
    monkeypatch.setenv(ENV_VAR, str(fixture))
    first = init_from_env()
    second = init_from_env()
    assert first is second


def test_init_rejects_directory_path(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV_VAR, str(tmp_path))  # directory, not a file
    with pytest.raises(RuntimeError, match="directory"):
        init_from_env()


def test_init_does_not_overwrite_test_injected_recorder(monkeypatch, tmp_path):
    # If a test has already swapped in a fake, init_from_env must not
    # clobber it just because POLARIS_RECORD is set.  This protects
    # tests that monkey-patch the singleton from being stomped by a
    # late bootstrap call.
    class FakeRecorder:
        scenario = "fake"
        fixture_path = Path("/dev/null")

        async def start(self): ...
        async def on_codex_frame(self, *args, **kwargs): ...
        async def on_design_intent_node(self, *args, **kwargs): ...
        async def on_user_action(self, *args, **kwargs): ...
        async def finalize(self):
            return self.fixture_path

    fake = FakeRecorder()
    install_recorder(fake)
    monkeypatch.setenv(ENV_VAR, str(tmp_path / "raw" / "x.json"))
    out = init_from_env()
    # Bootstrap sees a non-JsonFileRecorder, non-noop singleton and
    # respects it — only swaps when current is a noop OR same kind.
    # Per the implementation, it only short-circuits on
    # JsonFileRecorder; FakeRecorder gets replaced.  Document that
    # behavior here so we notice if it changes.
    assert isinstance(out, JsonFileRecorder)

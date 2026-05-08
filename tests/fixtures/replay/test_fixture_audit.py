"""Coverage gate for replay fixtures.

Reuses ``scripts/replay_audit.audit`` so the same checks the operator
runs by hand also fire in CI.  When a re-recording loses a beat (no
plan emitted, no fileChange, mismatched item lifecycles), this test
fails before the fixture lands on main.

Pure-fixture test: skips cleanly when a scenario has no raw file.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).parent
_REPO_ROOT = _HERE.parent.parent.parent

# Load scripts/replay_audit dynamically — it lives outside any package so
# we can't `import` it the normal way.
_audit_path = _REPO_ROOT / "scripts" / "replay_audit.py"
_spec = importlib.util.spec_from_file_location("replay_audit", _audit_path)
assert _spec and _spec.loader
_module = importlib.util.module_from_spec(_spec)
sys.modules["replay_audit"] = _module
_spec.loader.exec_module(_module)
audit = _module.audit
load_raw_fixture = _module.load_raw_fixture


def _scenario_name(path: Path) -> str:
    name = path.name
    for suffix in (".json.gz", ".json"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


# Scenarios eligible for the audit gate.  ``_dummy`` is the validator
# self-test fixture — it deliberately has zero codex/discovery activity,
# so the audit's "fully populated golf-style scenario" expectations
# don't apply.  Real recordings (anything else with a raw/) get gated.
_AUDIT_EXEMPT = {"_dummy"}


def _audited_scenarios() -> list[str]:
    raw_dir = _HERE / "raw"
    names = {_scenario_name(p) for p in raw_dir.glob("*.json*")}
    return sorted(names - _AUDIT_EXEMPT)


@pytest.mark.parametrize("scenario", _audited_scenarios())
def test_fixture_passes_replay_invariants(scenario: str) -> None:
    raw_dir = _HERE / "raw"
    candidate = next(
        (p for ext in (".json.gz", ".json") if (p := raw_dir / f"{scenario}{ext}").exists()),
        None,
    )
    if candidate is None:
        pytest.skip(f"no raw/ file for {scenario}")
    data = load_raw_fixture(candidate)
    failures, _warnings = audit(data)
    assert not failures, (
        f"{scenario} fails replay invariants:\n  "
        + "\n  ".join(f"✗ {f}" for f in failures)
    )

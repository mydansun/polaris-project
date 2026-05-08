"""API-side staging helpers for the replay recorder.

The api process only writes one stream (``user-actions.jsonl``) — codex
frames and design-intent node outputs are emitted from the worker.  We
keep this module deliberately tiny: derive the staging dir from
``POLARIS_RECORD``, append one JSON line per call, period.

Mirrors the layout in ``polaris_worker.replay.recorder.staging_dir_for``;
the two processes converge on the same dir without IPC because both
read ``POLARIS_RECORD`` and apply the same transform.

When ``POLARIS_RECORD`` is unset, callers should not import this — the
route checks the env var before reaching us.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ENV_VAR = "POLARIS_RECORD"


def fixture_path_from_env() -> Path | None:
    raw = os.environ.get(ENV_VAR)
    if not raw:
        return None
    return Path(raw).resolve()


def _scenario_stem(fixture_path: Path) -> str:
    """Strip both ``.json`` and ``.json.gz`` so the scenario name doesn't
    end up as ``<scenario>.json`` for gzipped fixtures.  Mirrors the
    worker-side helper of the same name."""
    name = fixture_path.name
    for suffix in (".json.gz", ".json"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return fixture_path.stem


def staging_dir_for(fixture_path: Path) -> Path:
    """Same derivation worker-side uses; kept duplicated rather than
    cross-imported because the api package can't import worker.

    If the schema version changes, both implementations need to update
    in lockstep.  See tests/fixtures/replay/_schema.SCHEMA_VERSION.
    """
    return fixture_path.parent / f".staging-{_scenario_stem(fixture_path)}"


def append_user_action(payload: dict[str, Any]) -> bool:
    """Append one JSON line to the recording's ``user-actions.jsonl``.

    Returns False when ``POLARIS_RECORD`` is unset (caller should already
    have 503'd in that case; this is a belt-and-suspenders check).

    Atomicity: we ``open(..., 'a')`` which sets ``O_APPEND``; on Linux,
    line-sized writes under PIPE_BUF (4096) are atomic with respect to
    other appenders, including the worker process.  Our payloads stay
    well under that.
    """
    fixture_path = fixture_path_from_env()
    if fixture_path is None:
        return False
    staging = staging_dir_for(fixture_path)
    staging.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": time.time(), **payload}, default=str)
    target = staging / "user-actions.jsonl"
    try:
        with target.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as exc:
        logger.warning("replay user-action append failed at %s: %s", target, exc)
        return False
    return True

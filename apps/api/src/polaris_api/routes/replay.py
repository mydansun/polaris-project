"""Replay recorder receiver — accepts user actions from the web app
during a recording session.

The web side maintains its own action timeline (clicks, clarification
answers, waits, navigations) and POSTs each one here in real time.
This route persists nothing to the DB — it forwards to the worker's
recorder via the shared fixture file.

Phase 0 ships only the route shape with a no-op handler; Phase 1
swaps in actual forwarding once the recorder writes are implemented.
The route is mounted unconditionally — callers detect "recording is
not enabled" via the 503 response when ``POLARIS_RECORD`` is unset.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from polaris_api.replay.staging import (
    _scenario_stem,
    append_user_action,
    fixture_path_from_env,
)

router = APIRouter(prefix="/replay", tags=["replay"])


class RecordAppendBody(BaseModel):
    """One user action observed by the web recorder.

    ``t`` is seconds since the recording started, set by the web side so
    timeline ordering survives any backend coalescing.  ``concrete`` is
    free-form on purpose — the schema for each action kind is pinned in
    ``tests/fixtures/replay/_schema.py``, not here, so we can add new
    kinds without touching this route.
    """

    scenario: str = Field(min_length=1, max_length=80)
    t: float = Field(ge=0)
    kind: str = Field(min_length=1, max_length=40)
    concrete: dict[str, Any] = Field(default_factory=dict)
    a11y_snapshot: dict[str, Any] | None = None


def _recording_enabled() -> bool:
    """``POLARIS_RECORD`` must be set on the api process for record-mode
    to be live.  Used by the route to 503 cleanly when the web side
    starts a recording but the backend wasn't launched in record mode."""
    return bool(os.environ.get("POLARIS_RECORD"))


@router.post("/record/append")
async def post_user_action(body: RecordAppendBody) -> dict:
    """Forward a recorded user action to the recording's staging dir.

    Writes one JSON line to ``<staging>/user-actions.jsonl``.  Worker
    appends its own streams alongside (codex frames, design-intent node
    outputs); ``merge_staging`` combines all three into the final raw
    fixture later.

    Validates that the body's ``scenario`` matches whatever scenario the
    api was started with via ``POLARIS_RECORD`` — accidentally posting
    actions tagged for one scenario into another's recording would be
    a confusing-to-debug data corruption.
    """
    if not _recording_enabled():
        raise HTTPException(
            status_code=503,
            detail=(
                "Recording is not enabled on this api process — start "
                "the stack with POLARIS_RECORD=<fixture-path> to enable "
                "the /replay/record/append route."
            ),
        )

    fixture_path = fixture_path_from_env()
    expected_scenario = _scenario_stem(fixture_path) if fixture_path else None
    if expected_scenario and body.scenario != expected_scenario:
        raise HTTPException(
            status_code=409,
            detail=(
                f"scenario mismatch: api is recording {expected_scenario!r}, "
                f"but body says {body.scenario!r}.  Restart api with "
                f"POLARIS_RECORD pointing at {body.scenario!r} or update "
                "the web recorder's scenario tag."
            ),
        )

    persisted = append_user_action(
        {
            "t": body.t,
            "kind": body.kind,
            "concrete": body.concrete,
            "a11y_snapshot": body.a11y_snapshot,
        }
    )
    if not persisted:
        # The env was set when the request started but staging append
        # failed (disk full, permission, race with finalize).  Return
        # 500 so the web recorder retries / surfaces the issue rather
        # than silently dropping events.
        raise HTTPException(
            status_code=500,
            detail="failed to append to user-actions.jsonl — check api logs",
        )

    return {"ok": True, "scenario": body.scenario, "t": body.t, "kind": body.kind}


class RecordFinalizeBody(BaseModel):
    """Body for ``/replay/record/finalize``.

    Currently the only knob is ``cleanup`` — when true (default), the
    staging dir is rmtree'd after a successful merge; when false, the
    raw fixture is written and staging stays for inspection.
    """

    cleanup: bool = True


@router.post("/record/finalize")
async def finalize_recording(body: RecordFinalizeBody) -> dict:
    """Merge staging JSONLs into the final raw fixture.

    Only callable when the api was launched with ``POLARIS_RECORD``.
    The merge logic itself lives in the worker package (where the
    recorder also lives), which the api can't import directly — we
    shell out to the worker CLI to keep the boundary clean.

    Returns the absolute fixture path on success so the operator can
    git-add it without searching.
    """
    if not _recording_enabled():
        raise HTTPException(status_code=503, detail="recording not enabled")
    fixture_path = fixture_path_from_env()
    if fixture_path is None:
        raise HTTPException(status_code=503, detail="POLARIS_RECORD unset")

    import asyncio

    # Worker package isn't on the api's sys.path, but the merge utility
    # is dependency-free Python — we invoke it via the worker container's
    # shared volume.  Phase 1.5 may move the merge into agent-core to
    # eliminate the docker hop; for now this stays explicit.
    cmd = [
        "docker",
        "exec",
        "polaris-worker-1",
        "python",
        "-m",
        "polaris_worker.replay.merge_cli",
        "--fixture",
        str(fixture_path),
    ]
    if not body.cleanup:
        cmd.append("--no-cleanup")
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=(
                "merge_cli failed: "
                + (stderr.decode(errors="replace") or stdout.decode(errors="replace"))
            ),
        )
    return {
        "ok": True,
        "fixture": str(fixture_path),
        "log": stdout.decode(errors="replace").strip(),
    }

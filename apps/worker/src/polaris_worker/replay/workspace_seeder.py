"""Seed a replay-mode workspace container with the recording's
post-build /workspace tarball.

Phase 3.5 trade-off: the recorded codex transcript carries fileChange
items as unified diffs, which require the *base* file to exist in the
right state.  In replay we don't actually run the npm/scaffold
commands codex emitted, so the base files aren't there — diffs would
fail to apply.

Workaround: at recording time, snapshot the post-turn /workspace into
a tarball alongside the JSON fixture; at replay start, untar it into
the live workspace container.  The IDE iframe + browser preview see
the same final state codex produced.  Recorded fileChange items still
dispatch to the chat-pane sink (so the StatusBar "files changed"
counter ticks correctly) — they just don't perform the file write
themselves.

Convention:

    tests/fixtures/replay/raw/<scenario>.json.gz       fixture
    tests/fixtures/replay/assets/<scenario>-workspace.tar.gz   seed

Tarball is created out-of-band by the operator after a successful
recording (see scripts/replay_capture_workspace.sh — Phase 3.5+ may
fold this into merge_staging).  Filtered to source files only:
node_modules / dist / .git / playwright artifacts excluded so it
stays small (golf scenario: 30 KB / 20 files).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from uuid import UUID

logger = logging.getLogger(__name__)


SEED_MARKER_PATH = "/workspace/.polaris-replay-seeded"


def workspace_tarball_path(fixture_path: Path) -> Path:
    """Derive the seed tarball path from the fixture path.

    Strips both ``.json`` and ``.json.gz`` extensions so a gzipped
    fixture and a plain one resolve to the same scenario stem.
    """
    name = fixture_path.name
    for suffix in (".json.gz", ".json"):
        if name.endswith(suffix):
            scenario = name[: -len(suffix)]
            break
    else:
        scenario = fixture_path.stem
    return (
        fixture_path.parent.parent / "assets" / f"{scenario}-workspace.tar.gz"
    )


def _container_name(workspace_id: UUID) -> str:
    """Same naming the rest of the worker uses (matches
    ``agents/codex._workspace_container_name``).  Duplicated to avoid
    a circular import at module-load time."""
    hash_id = str(workspace_id).replace("-", "")[:24]
    return f"polaris-ws-{hash_id}"


async def _container_running(container: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "inspect",
        "--format",
        "{{.State.Running}}",
        container,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode == 0 and stdout.decode().strip() == "true"


async def _is_already_seeded(container: str) -> bool:
    """Idempotency probe: marker file written at end of first seed."""
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "exec",
        container,
        "test",
        "-f",
        SEED_MARKER_PATH,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    return proc.returncode == 0


async def seed_workspace(
    workspace_id: UUID, tarball_path: Path
) -> bool:
    """Untar ``tarball_path`` into the workspace container's /workspace.

    Returns True when seeding ran (or was already complete), False on
    failure.  Best-effort: failures are logged but never raised — a
    missing tarball or inaccessible container just leaves the
    workspace empty, and the chat replay still works.

    Idempotent: subsequent calls for the same workspace are no-ops
    after the first successful seed (detected via the marker file).
    """
    if not tarball_path.exists():
        logger.info(
            "replay seed: no tarball at %s — skipping; IDE iframe will "
            "show empty workspace",
            tarball_path,
        )
        return False

    container = _container_name(workspace_id)
    if not await _container_running(container):
        logger.warning(
            "replay seed: workspace container %s not running — cannot seed",
            container,
        )
        return False

    if await _is_already_seeded(container):
        logger.debug("replay seed: %s already seeded; skipping", container)
        return True

    # Stream the tarball into the container via stdin and untar in /workspace.
    # `docker cp` would also work but requires writing the tarball to a
    # temp path inside the container; piping is one fewer intermediate.
    cmd = [
        "docker",
        "exec",
        "-i",
        container,
        "sh",
        "-c",
        # The marker write happens after a successful untar so a partial
        # extract leaves no marker → next call retries.
        f"tar -xzf - -C /workspace && touch {SEED_MARKER_PATH} && "
        # Match the workspace's uid 1000 ownership convention so the
        # in-container codex can read/write what we just dropped.
        "chown -R 1000:1000 /workspace 2>/dev/null || true",
    ]
    try:
        with tarball_path.open("rb") as f:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=f,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _stdout, stderr = await proc.communicate()
    except OSError as exc:
        logger.warning("replay seed: subprocess error: %s", exc)
        return False

    if proc.returncode != 0:
        logger.warning(
            "replay seed: untar into %s failed (exit=%d): %s",
            container,
            proc.returncode,
            stderr.decode(errors="replace")[:200],
        )
        return False

    logger.info(
        "replay seed: untarred %s into %s (%d bytes)",
        tarball_path.name,
        container,
        tarball_path.stat().st_size,
    )
    return True


async def seed_workspace_from_fixture(
    workspace_id: UUID, fixture_path: Path
) -> bool:
    """Convenience wrapper: derive tarball path from fixture path, seed."""
    return await seed_workspace(workspace_id, workspace_tarball_path(fixture_path))

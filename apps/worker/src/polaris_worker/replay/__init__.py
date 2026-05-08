"""Recording / replay scaffolding for the worker side.

Phase 0 deliverable: typed contracts only.  The actual recorder writes
land in Phase 1, the replay shims in Phase 3.  Importing this module is
free — nothing here touches Redis, the DB, or the codex session unless
a caller explicitly opts in via ``Recorder.start``.

Wiring map (informational, not yet implemented):

    PolarisCodexSession.event_tap   ──► Recorder.on_codex_frame
    polaris_design_intent.build_graph(node_tap=...) ──► Recorder.on_design_intent_node
    apps/api/src/polaris_api/replay/* (POST /replay/record/append) ──► Recorder.on_user_action
    finalize() flushes the assembled JSON to ``raw/<scenario>.json``

The annotated layer is generated separately by ``scripts/replay_annotate.py``
from the raw JSON; recorder doesn't know about annotation.
"""

from polaris_worker.replay.bootstrap import init_from_env
from polaris_worker.replay.recorder import (
    JsonFileRecorder,
    Recorder,
    RecorderProtocol,
    UserActionEvent,
    install_recorder,
    merge_staging,
    staging_dir_for,
)

__all__ = [
    "JsonFileRecorder",
    "Recorder",
    "RecorderProtocol",
    "UserActionEvent",
    "init_from_env",
    "install_recorder",
    "merge_staging",
    "staging_dir_for",
]


def get_recorder() -> RecorderProtocol:
    """Live accessor for the module-level recorder singleton.

    Use this in code paths that latch the recorder at call time —
    importing ``Recorder`` directly captures whatever was installed at
    that import moment (noop on cold start), while ``get_recorder()``
    always returns the current binding.  Critical for code that runs
    before ``init_from_env`` (it shouldn't, but defenses are cheap).
    """
    from polaris_worker.replay import recorder as _r

    return _r.Recorder

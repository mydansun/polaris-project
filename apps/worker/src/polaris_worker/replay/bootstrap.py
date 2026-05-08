"""Bootstrap the recorder from environment.

Both worker and api processes call ``init_from_env()`` at startup.  When
``POLARIS_RECORD=<path/to/raw/scenario.json>`` is set, a ``JsonFileRecorder``
replaces the noop singleton and a banner is logged so it's obvious
recording is live.  When unset, the singleton stays noop and zero
overhead is incurred.

Why a separate bootstrap module:
  * Keeps ``recorder.py`` import-safe — importing it must never read
    env or touch disk.
  * Lets tests instantiate ``JsonFileRecorder`` directly without going
    through env.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from polaris_worker.replay import recorder as _recorder_mod
from polaris_worker.replay.recorder import (
    JsonFileRecorder,
    RecorderProtocol,
    install_recorder,
)

logger = logging.getLogger(__name__)

ENV_VAR = "POLARIS_RECORD"


def init_from_env() -> RecorderProtocol:
    """Idempotent bootstrap.

    First call with ``POLARIS_RECORD`` set installs a JsonFileRecorder.
    Subsequent calls return the already-installed recorder unchanged
    (so worker reload + main both calling this is fine).  Returns the
    active recorder either way (noop or real).
    """
    raw_path = os.environ.get(ENV_VAR)
    if not raw_path:
        return _recorder_mod.Recorder

    # Already installed?  isinstance check rather than identity so a
    # test-injected fake recorder isn't clobbered.  We have to read from
    # the live module attribute (not a top-level import) because
    # ``install_recorder`` rebinds ``recorder.Recorder`` and any frozen
    # ``from … import Recorder`` here would see the stale noop.
    if isinstance(_recorder_mod.Recorder, JsonFileRecorder):
        return _recorder_mod.Recorder

    fixture_path = Path(raw_path).resolve()
    if fixture_path.is_dir():
        raise RuntimeError(
            f"{ENV_VAR}={raw_path} points at a directory; expected a "
            "path like raw/<scenario>.json"
        )

    recorder = JsonFileRecorder(fixture_path)
    install_recorder(recorder)
    logger.warning(
        "REPLAY RECORDING ACTIVE — scenario=%s output=%s. Disable by "
        "unsetting %s.",
        recorder.scenario,
        recorder.fixture_path,
        ENV_VAR,
    )
    return recorder

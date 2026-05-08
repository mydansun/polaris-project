"""Replay mode network kill-switch.

When ``POLARIS_REPLAY`` is set, the worker is supposed to satisfy turns
out of a recorded fixture instead of hitting OpenAI / Anthropic /
Pinterest.  But replay shims are rich and easy to leak — a dormant code
path that still calls the real LLM would silently burn $ and corrupt
the deterministic-replay invariant.

This module is the safety net.  Every external-API entry point on the
worker / api / design-intent side calls :func:`check_network(label)`
before initializing its client.  When the env var is set, the call
raises ``ReplayModeNetworkBlocked`` with the label so test failures
immediately point at which client tried to reach out.

Cheap, explicit, and easy to grep for: 13 callsites covers every
known external surface; new external clients should be one-liner to
add.  Local services (MinIO/S3, Redis, Postgres, codex app-server in
the workspace) deliberately bypass this — they don't cost money and
the replay needs them to render the same UI it always renders.
"""

from __future__ import annotations

import os


_RECORD_VAR = "POLARIS_RECORD"
_REPLAY_VAR = "POLARIS_REPLAY"


class ReplayModeNetworkBlocked(RuntimeError):
    """Raised when a replay-mode process tries to call an external API.

    Carries the label of the blocked target so tests can assert which
    surface tripped (a ReplayCodexSession bug would block on
    ``label='openai'``, a missing Pinterest stub on ``label='pinterest'``).
    """

    def __init__(self, label: str) -> None:
        self.label = label
        super().__init__(
            f"replay-mode network call blocked (label={label!r}). "
            f"Either the replay shim for this surface is missing or a "
            f"code path leaked through to the real client.  Unset "
            f"{_REPLAY_VAR} to disable the guard, or add the missing "
            f"replay shim."
        )


def is_replay_mode() -> bool:
    """True when this process is running a replay.

    Read at every guard call rather than cached so tests can flip the
    env mid-session without restarting (monkeypatch.setenv works).
    """
    return bool(os.environ.get(_REPLAY_VAR))


def is_record_mode() -> bool:
    return bool(os.environ.get(_RECORD_VAR))


def check_network(label: str) -> None:
    """Raise ``ReplayModeNetworkBlocked`` if we're in replay mode.

    No-op when replay is off — production / dev / record modes pass
    through with zero overhead.

    ``label`` is a short identifier (``"openai"``, ``"pinterest"``,
    ``"unsplash"``, ``"openai-images"``) used in error messages and
    test assertions.  Use the same label string at every site for
    one external API so error messages are consistent.
    """
    if is_replay_mode():
        raise ReplayModeNetworkBlocked(label)


def assert_modes_not_both() -> None:
    """Reject the misconfiguration where both POLARIS_RECORD and
    POLARIS_REPLAY are set.  Recording from a replay would either
    silently produce an identity copy of the source fixture (best
    case) or write garbage if anything diverges (worst case).  Bail
    early so the operator notices."""
    if is_record_mode() and is_replay_mode():
        raise RuntimeError(
            f"both {_RECORD_VAR} and {_REPLAY_VAR} are set — these "
            "modes are mutually exclusive.  Pick one."
        )

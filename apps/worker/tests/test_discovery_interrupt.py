"""Unit tests for DiscoveryAgent cooperative cancellation.

When the user hits the Stop button, the orchestrator's session control
consumer calls ``DiscoveryAgent.handle_control({"kind": "interrupt"})``
which cancels the in-flight ``run_design_intent`` task.  The
``CancelledError`` propagates out through ``DiscoveryAgent.run`` and is
mapped to ``RunOutcome(status="interrupted")``.

We mock ``run_design_intent`` with an ``asyncio.sleep`` so the test
doesn't need LangGraph / OpenAI / Pinterest.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest

from polaris_worker.agents import discovery as discovery_mod
from polaris_worker.agents.base import RunContext


@dataclass
class _StubSession:
    session_id: UUID
    project_id: UUID
    workspace_id: UUID
    redis: Any = None
    conn: Any = None
    conn_lock: Any = None
    settings: Any = None


class _NoopSink:
    """No-op EventSink.  DiscoveryAgent's finalize_all path over an
    empty started-set doesn't emit anything, so these are placeholders
    for the protocol."""

    async def emit_event_started(self, **_kwargs: Any) -> UUID:
        return uuid4()

    async def emit_event_completed(self, **_kwargs: Any) -> None:
        return None

    async def emit_message_delta(self, **_kwargs: Any) -> None:
        return None

    async def bump_file_delta(self, delta: int = 1) -> None:
        return None

    async def bump_playwright_delta(self, delta: int = 1) -> None:
        return None

    async def finalize_stats(self) -> None:
        return None


async def _never_resolves(**_kwargs: Any) -> Any:
    """Stands in for ``run_design_intent`` — sleeps long enough that
    the test's cancel is the only thing that ends it."""
    await asyncio.sleep(10)
    raise AssertionError("run_design_intent should have been cancelled")


@pytest.mark.asyncio
async def test_discovery_handle_control_cancels_and_returns_interrupted(
    monkeypatch,
):
    monkeypatch.setattr(discovery_mod, "run_design_intent", _never_resolves)

    agent = discovery_mod.DiscoveryAgent()
    session = _StubSession(
        session_id=uuid4(), project_id=uuid4(), workspace_id=uuid4()
    )
    run = RunContext(
        run_id=uuid4(),
        sequence=1,
        input={"user_message": "build me a site", "seed_intent": None},
        user_input_fn=lambda _q: asyncio.sleep(0, result=[]),  # unused
    )

    run_task = asyncio.create_task(agent.run(session, run, _NoopSink()))

    # Let the task hit its `await self._active_task` point.
    await asyncio.sleep(0.05)
    assert agent._active_task is not None
    assert not agent._active_task.done()

    await agent.handle_control({"kind": "interrupt", "session_id": "x"})

    # Cancellation propagates through run_design_intent → back to run()
    # → returns RunOutcome(status="interrupted").  Give it a generous
    # ceiling; the real coroutine exits on the next await point which is
    # the one inside _never_resolves's asyncio.sleep.
    outcome = await asyncio.wait_for(run_task, timeout=2.0)

    assert outcome.status == "interrupted"
    assert outcome.error is None
    # Task reference cleared on return (see the finally block).
    assert agent._active_task is None


@pytest.mark.asyncio
async def test_discovery_handle_control_ignores_non_interrupt_events():
    """Control messages with other `kind` values (e.g. `steer`) must not
    cancel the discovery task — that would be a silent drop."""
    agent = discovery_mod.DiscoveryAgent()

    async def _long_sleep() -> int:
        await asyncio.sleep(1.0)
        return 42

    agent._active_task = asyncio.create_task(_long_sleep())
    await agent.handle_control({"kind": "steer", "message": "go left"})

    # Steer is a no-op here; task still alive.
    assert not agent._active_task.done()
    # Clean up.
    agent._active_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await agent._active_task


@pytest.mark.asyncio
async def test_discovery_handle_control_when_no_active_task_is_noop():
    """Racing a Stop click against a completed run must not raise."""
    agent = discovery_mod.DiscoveryAgent()
    # No `_active_task` → handle_control should just return.
    await agent.handle_control({"kind": "interrupt"})
    assert agent._active_task is None

"""Orchestrator unit tests — mode→agent routing + threading_forward."""

from __future__ import annotations

from polaris_worker.agents.base import AgentKind, RunOutcome
from polaris_worker.orchestrator import (
    AGENTS_BY_MODE,
    _CODEX_MODE_BY_SESSION_MODE,
    _initial_input,
    threading_forward,
)


def test_agents_by_mode_is_complete():
    """Every documented SessionMode has an agent chain."""
    assert set(AGENTS_BY_MODE.keys()) == {
        "build_planned",
        "build_direct",
        "discover_then_build",
    }
    assert AGENTS_BY_MODE["build_planned"] == [AgentKind.codex]
    assert AGENTS_BY_MODE["build_direct"] == [AgentKind.codex]
    assert AGENTS_BY_MODE["discover_then_build"] == [
        AgentKind.discovery,
        AgentKind.codex,
    ]


def test_codex_mode_translation():
    """Session mode -> Codex's internal mode (plan / default)."""
    assert _CODEX_MODE_BY_SESSION_MODE["build_planned"] == "plan"
    assert _CODEX_MODE_BY_SESSION_MODE["build_direct"] == "default"
    # After discovery, codex runs with a plan round so the user can still
    # see/approve an implementation plan on top of the compiled brief.
    assert _CODEX_MODE_BY_SESSION_MODE["discover_then_build"] == "plan"


def test_initial_input_codex_carries_mode():
    inp = _initial_input(
        agent_kind=AgentKind.codex,
        session_mode="build_planned",
        user_message="hi",
        seed_intent=None,
    )
    assert inp == {"user_message": "hi", "codex_mode": "plan"}

    direct = _initial_input(
        agent_kind=AgentKind.codex,
        session_mode="build_direct",
        user_message="just do it",
        seed_intent=None,
    )
    assert direct["codex_mode"] == "default"


def test_initial_input_discovery_carries_seed():
    seed = {"pageType": "landing page"}
    inp = _initial_input(
        agent_kind=AgentKind.discovery,
        session_mode="discover_then_build",
        user_message="update colors",
        seed_intent=seed,
    )
    assert inp == {"user_message": "update colors", "seed_intent": seed}


def test_threading_forward_promotes_brief_to_user_message():
    """After a discovery run, the codex run's user_message becomes the
    compiled brief so the Codex plan round sees it directly."""
    prev = RunOutcome(
        status="completed",
        output={
            "brief": "Build a spacious editorial estate landing page.",
            "intent": {"pageType": "landing page"},
        },
    )
    new_input = threading_forward(
        next_kind=AgentKind.codex,
        prev_outcome=prev,
        session_mode="discover_then_build",
        base_input={"user_message": "original vague request", "codex_mode": "plan"},
    )
    assert new_input["user_message"] == "Build a spacious editorial estate landing page."
    assert new_input["codex_mode"] == "plan"  # preserved


def test_threading_forward_is_noop_without_brief():
    prev = RunOutcome(status="completed", output={})
    new_input = threading_forward(
        next_kind=AgentKind.codex,
        prev_outcome=prev,
        session_mode="build_planned",
        base_input={"user_message": "unchanged", "codex_mode": "plan"},
    )
    assert new_input["user_message"] == "unchanged"

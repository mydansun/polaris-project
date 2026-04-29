"""Integration test: one-shot emit on the first clarifier turn."""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage

from polaris_design_intent.graph import run_design_intent
from polaris_design_intent.models import DesignIntent
from polaris_design_intent.nodes import clarifier as clarifier_mod
from polaris_design_intent.nodes import compiler as compiler_mod
from tests.fixtures.intents import GOLDEN_INTENT


@pytest.mark.integration
@pytest.mark.asyncio
async def test_happy_path_one_shot_emit(
    monkeypatch, settings, mock_pinterest_transport, stub_user_input_fn
):
    # Clarifier's LLM: emit on first call.
    emit_msg = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "emit_design_intent",
                "args": {
                    "intent": GOLDEN_INTENT,
                    "pinterest_queries": ["real estate landing page white"],
                },
                "id": "call_emit",
            }
        ],
    )

    class _ClarifierFake:
        def __init__(self, *_args, **_kwargs):
            self._sent = False

        def bind_tools(self, *_a, **_kw):
            return self

        async def ainvoke(self, _messages):
            self._sent = True
            return emit_msg

    monkeypatch.setattr(clarifier_mod, "ChatOpenAI", _ClarifierFake)

    # Compiler's LLM: return a structured brief.
    class _CompilerFake:
        def __init__(self, *_args, **_kwargs):
            pass

        def with_structured_output(self, _schema, **_kwargs):
            compiled = compiler_mod.CompiledBriefSchema(
                intent=DesignIntent.model_validate(GOLDEN_INTENT),
                brief="A spacious, editorial landing page...",
            )

            class _Inner:
                async def ainvoke(self, _m):
                    return compiled

            return _Inner()

    monkeypatch.setattr(compiler_mod, "ChatOpenAI", _CompilerFake)

    brief = await run_design_intent(
        project_id="proj-1",
        turn_id="turn-1",
        user_message="a landing page for my estate business",
        user_input_fn=stub_user_input_fn([]),  # no answers needed on happy path
        settings=settings,
    )

    assert brief.intent.audience == "HNW real-estate buyers"
    assert "editorial landing page" in brief.brief
    assert brief.pinterest_queries == ["real estate landing page white"]
    # Pinterest refs are populated from the mocked transport.
    assert {r.id for r in brief.pinterest_refs} >= {"pin-001", "pin-002"}
    # Base64 bytes are stripped from the returned refs (internal only).
    assert all(r.image_b64 is None for r in brief.pinterest_refs)

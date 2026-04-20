"""Integration test: two rounds of clarification, then emit."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from polaris_design_intent.graph import run_design_intent
from polaris_design_intent.models import DesignIntent
from polaris_design_intent.nodes import clarifier as clarifier_mod
from polaris_design_intent.nodes import compiler as compiler_mod
from tests.fixtures.intents import GOLDEN_INTENT


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_rounds_then_emit(
    monkeypatch, settings, mock_pinterest_transport, stub_user_input_fn
):
    ask_1 = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "ask_questions",
                "args": {
                    "questions": [
                        {
                            "id": "industry",
                            "title": "What industry is this site for?",
                            "choices": ["real estate", "saas", "dental", "portfolio"],
                            "required": True,
                        }
                    ]
                },
                "id": "call_1",
            }
        ],
    )
    ask_2 = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "ask_questions",
                "args": {
                    "questions": [
                        {
                            "id": "primary_color",
                            "title": "Primary color name?",
                            "choices": ["white", "beige", "navy", "charcoal"],
                            "required": True,
                        }
                    ]
                },
                "id": "call_2",
            }
        ],
    )
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

    scripted = iter([ask_1, ask_2, emit_msg])

    class _ClarifierFake:
        def __init__(self, *_a, **_kw):
            pass

        def bind_tools(self, *_a, **_kw):
            return self

        async def ainvoke(self, _messages):
            return next(scripted)

    class _CompilerFake:
        def __init__(self, *_a, **_kw):
            pass

        def with_structured_output(self, _schema, **_kwargs):
            class _Inner:
                async def ainvoke(self, _m):
                    return compiler_mod.CompiledBriefSchema(
                        intent=DesignIntent.model_validate(GOLDEN_INTENT),
                        brief="(final brief)",
                    )

            return _Inner()

    monkeypatch.setattr(clarifier_mod, "ChatOpenAI", _ClarifierFake)
    monkeypatch.setattr(compiler_mod, "ChatOpenAI", _CompilerFake)

    uif = stub_user_input_fn(
        [
            [{"question_id": "industry", "answer": "real estate"}],
            [{"question_id": "primary_color", "answer": "white"}],
        ]
    )

    brief = await run_design_intent(
        project_id="proj-2",
        turn_id="turn-2",
        user_message="make me a site",
        user_input_fn=uif,
        settings=settings,
    )
    assert brief.intent.audience == "HNW real-estate buyers"
    assert brief.brief == "(final brief)"

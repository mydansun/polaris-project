"""Integration test: review_step rejects the clarifier's first emit, which
must bounce back to clarifier_step for another round of targeted questions,
then pass on the second emit."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from polaris_design_intent.graph import run_design_intent
from polaris_design_intent.models import DesignIntent
from polaris_design_intent.nodes import clarifier as clarifier_mod
from polaris_design_intent.nodes import compiler as compiler_mod
from polaris_design_intent.nodes import review as review_mod
from tests.fixtures.intents import GOLDEN_INTENT


@pytest.mark.integration
@pytest.mark.asyncio
async def test_review_rejects_then_passes(
    monkeypatch, settings, mock_pinterest_transport, stub_user_input_fn
):
    """Flow:
      1. Clarifier emits a first (weak) design_intent after one ask round.
      2. Review rejects it (ok=False, gaps=[audience]).
      3. Graph bounces back to clarifier_step, which triggers another
         ask_questions round → user answers → clarifier emits a stronger
         intent.
      4. Review passes this time.
      5. Pinterest + compiler run normally.
    """

    # ── Clarifier: three scripted AIMessages ────────────────────────────
    ask_1 = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "ask_questions",
                "args": {
                    "questions": [
                        {"id": "industry", "title": "Industry?",
                         "choices": ["real estate", "saas"], "required": True},
                    ]
                },
                "id": "call_ask1",
            }
        ],
    )
    weak_emit = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "emit_design_intent",
                "args": {
                    "intent": {
                        **GOLDEN_INTENT,
                        # Weak audience — review should reject this.
                        "audience": "general audience",
                    },
                    "pinterest_queries": ["real estate landing page white"],
                },
                "id": "call_emit_weak",
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
                        {"id": "audience_specific", "title": "Who specifically?",
                         "choices": ["HNW buyers", "first-time buyers"],
                         "required": True},
                    ]
                },
                "id": "call_ask2",
            }
        ],
    )
    strong_emit = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "emit_design_intent",
                "args": {
                    "intent": GOLDEN_INTENT,  # full golden — review should pass
                    "pinterest_queries": ["real estate landing page white"],
                },
                "id": "call_emit_strong",
            }
        ],
    )
    scripted = iter([ask_1, weak_emit, ask_2, strong_emit])

    class _ClarifierFake:
        def __init__(self, *_a, **_kw):
            pass

        def bind_tools(self, *_a, **_kw):
            return self

        async def ainvoke(self, _messages):
            return next(scripted)

    # ── Review: scripted verdicts ───────────────────────────────────────
    review_verdicts = iter([
        review_mod.ReviewVerdict(
            ok=False,
            gaps=["audience"],
            reasons="Audience 'general audience' is too vague — ask who specifically.",
        ),
        review_mod.ReviewVerdict(ok=True, gaps=[], reasons=""),
    ])

    class _ReviewFake:
        def __init__(self, *_a, **_kw):
            pass

        def with_structured_output(self, _schema, **_kwargs):
            class _Inner:
                async def ainvoke(self, _m):
                    return next(review_verdicts)

            return _Inner()

    # ── Compiler: fixed brief ──────────────────────────────────────────
    class _CompilerFake:
        def __init__(self, *_a, **_kw):
            pass

        def with_structured_output(self, _schema, **_kwargs):
            class _Inner:
                async def ainvoke(self, _m):
                    return compiler_mod.CompiledBriefSchema(
                        intent=DesignIntent.model_validate(GOLDEN_INTENT),
                        brief="(final brief after review pass)",
                    )

            return _Inner()

    monkeypatch.setattr(clarifier_mod, "ChatOpenAI", _ClarifierFake)
    monkeypatch.setattr(review_mod, "ChatOpenAI", _ReviewFake)
    monkeypatch.setattr(compiler_mod, "ChatOpenAI", _CompilerFake)

    uif = stub_user_input_fn([
        [{"question_id": "industry", "answer": "real estate"}],
        [{"question_id": "audience_specific", "answer": "HNW buyers"}],
    ])

    brief = await run_design_intent(
        project_id="proj-r",
        turn_id="turn-r",
        user_message="make me a site",
        user_input_fn=uif,
        settings=settings,
    )

    # Final compiled brief came from the strong emit, so audience is the
    # golden value ("HNW real-estate buyers") not the rejected one.
    assert brief.intent.audience == "HNW real-estate buyers"
    assert brief.brief == "(final brief after review pass)"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_review_rejection_cap_lets_intent_through(
    monkeypatch, settings, mock_pinterest_transport, stub_user_input_fn
):
    """When the clarifier keeps emitting weak intents past the cap
    (``MAX_REVIEW_REJECTIONS``), review stops sending users back for more
    questions and passes the latest intent through with a note."""

    weak_intent = {
        **GOLDEN_INTENT,
        "audience": "general audience",  # always weak, always rejected
    }

    ask = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "ask_questions",
                "args": {
                    "questions": [{
                        "id": "q", "title": "q", "choices": ["a"], "required": True,
                    }],
                },
                "id": f"call_ask_{id(object())}",
            }
        ],
    )
    emit = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "emit_design_intent",
                "args": {
                    "intent": weak_intent,
                    "pinterest_queries": ["real estate landing page white"],
                },
                "id": f"call_emit_{id(object())}",
            }
        ],
    )
    # ask → emit → (reject) → ask → emit → (reject) → ask → emit → (cap hit, passes)
    scripted = iter([ask, emit, ask, emit, ask, emit])

    class _ClarifierFake:
        def __init__(self, *_a, **_kw):
            pass

        def bind_tools(self, *_a, **_kw):
            return self

        async def ainvoke(self, _messages):
            return next(scripted)

    class _ReviewFake:
        def __init__(self, *_a, **_kw):
            pass

        def with_structured_output(self, _schema, **_kwargs):
            class _Inner:
                async def ainvoke(self, _m):
                    return review_mod.ReviewVerdict(
                        ok=False, gaps=["audience"], reasons="too vague",
                    )

            return _Inner()

    class _CompilerFake:
        def __init__(self, *_a, **_kw):
            pass

        def with_structured_output(self, _schema, **_kwargs):
            class _Inner:
                async def ainvoke(self, _m):
                    return compiler_mod.CompiledBriefSchema(
                        intent=DesignIntent.model_validate(weak_intent),
                        brief="(final brief despite weak intent)",
                    )

            return _Inner()

    monkeypatch.setattr(clarifier_mod, "ChatOpenAI", _ClarifierFake)
    monkeypatch.setattr(review_mod, "ChatOpenAI", _ReviewFake)
    monkeypatch.setattr(compiler_mod, "ChatOpenAI", _CompilerFake)

    uif = stub_user_input_fn([
        [{"question_id": "q", "answer": "a"}],
        [{"question_id": "q", "answer": "a"}],
        [{"question_id": "q", "answer": "a"}],
    ])

    brief = await run_design_intent(
        project_id="proj-r2",
        turn_id="turn-r2",
        user_message="make me a site",
        user_input_fn=uif,
        settings=settings,
    )

    # Cap was hit — intent let through despite reviewer still saying no.
    # The graph completed without throwing.
    assert brief.brief == "(final brief despite weak intent)"

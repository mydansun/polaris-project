"""Exercise the compiler node with a stubbed structured-output chat model."""

import pytest
from langchain_core.messages import AIMessage

from polaris_design_intent.models import DesignIntent
from polaris_design_intent.nodes import compiler as compiler_mod


@pytest.mark.asyncio
async def test_compiler_returns_intent_and_brief(monkeypatch, settings):
    golden_intent = {
        "pageType": "landing page",
        "themeMode": "light",
        "brandName": None,
        "productName": None,
        "audience": "HNW real-estate buyers",
        "primaryGoal": "book a private tour",
        "coreUseCase": None,
        "visualDirection": "Editorial, minimal, airy; primary color: white.",
        "contentStructure": "1. Hero  2. Trust  3. Lifestyle  4. Showcase  5. CTA",
        "narrative": "airy, restrained, premium",
        "designSystem": None,
        "interactionStyle": None,
        "hardConstraints": [],
        "avoidPatterns": ["generic card-grid-first layouts"],
        "motionGuidance": None,
        "imageryGuidance": None,
        "implementationRequirements": None,
        "notes": None,
    }
    golden_brief = "Build a single-column landing page..."

    # Patch ChatOpenAI to avoid any network path and hand-deliver structured output.
    class _FakeStructured:
        async def ainvoke(self, _messages):
            return compiler_mod.CompiledBriefSchema(
                intent=DesignIntent.model_validate(golden_intent),
                brief=golden_brief,
            )

    class _FakeChat:
        def __init__(self, *_args, **_kwargs):
            pass

        def with_structured_output(self, _schema, **_kwargs):
            return _FakeStructured()

    monkeypatch.setattr(compiler_mod, "ChatOpenAI", _FakeChat)

    state = {"design_intent": golden_intent, "pinterest_refs": []}
    result = await compiler_mod.compiler_node(state, settings)

    # All caller-supplied keys are preserved verbatim; the full dump also
    # includes the frontend-craft token fields (typography*, accentColorHex,
    # heroLayout, cardPolicy, motionPlan) as their defaults.
    assert set(golden_intent.keys()).issubset(result["compiled_brief_json"].keys())
    assert result["compiled_brief_json"]["pageType"] == "landing page"
    assert result["compiled_brief_prompt"] == golden_brief


@pytest.mark.asyncio
async def test_compiler_builds_multimodal_blocks_when_images_present(settings, monkeypatch):
    captured = {}

    class _FakeStructured:
        async def ainvoke(self, messages):
            captured["messages"] = messages
            return compiler_mod.CompiledBriefSchema(
                intent=DesignIntent(), brief="x"
            )

    class _FakeChat:
        def __init__(self, *_args, **_kwargs):
            pass

        def with_structured_output(self, _schema, **_kwargs):
            return _FakeStructured()

    monkeypatch.setattr(compiler_mod, "ChatOpenAI", _FakeChat)

    refs = [
        {
            "id": "p1", "title": "t1",
            "max": "u1", "normal": "n1",
            "mime_type": "image/png", "image_b64": "AAA=",
        },
        {
            "id": "p2", "title": "t2",
            "max": "u2", "normal": "n2",
            "mime_type": None, "image_b64": None,  # won't be attached
        },
    ]
    state = {"design_intent": {"pageType": "landing page"}, "pinterest_refs": refs}
    await compiler_mod.compiler_node(state, settings)

    human_msg = captured["messages"][1]
    content = human_msg.content
    # Text block + one image_url block (ref p2 was skipped — no b64)
    kinds = [c.get("type") for c in content]
    assert kinds == ["text", "image_url"]
    assert content[1]["image_url"]["url"] == "data:image/png;base64,AAA="

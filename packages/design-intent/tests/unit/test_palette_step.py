"""Unit tests for the palette_step node + palette parsing helpers.

Strategy: stub `langchain_openai.ChatOpenAI` in the clarifier module so
no real API is hit.  We exercise:
- Happy path: LLM returns a valid 5-item JSON array → that array passes
  through as the ToolMessage content
- Code-fence wrapped JSON (some models add ```json``` despite instructions)
  → stripped + accepted
- Wrong length (4 items) → falls back to the neutral default
- Invalid hex ("red") → falls back
- LLM call raises → falls back
- Routing: when the clarifier's last tool call is `propose_color_palette`,
  `route_after_step` returns ROUTE_PALETTE
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage

from polaris_design_intent.nodes import clarifier as clarifier_mod
from polaris_design_intent.nodes.clarifier import (
    ROUTE_PALETTE,
    _FALLBACK_PALETTE,
    _parse_and_validate_palette,
    palette_step,
    route_after_step,
)


# ── Direct parser tests (no LLM involved) ─────────────────────────────────


_VALID_PALETTE = [
    {"id": "sand", "label": "Sand", "swatch": "#E8D9C0"},
    {"id": "sage", "label": "Sage", "swatch": "#A6B89F"},
    {"id": "clay", "label": "Clay", "swatch": "#B27A5C"},
    {"id": "ink", "label": "Ink", "swatch": "#1E2A3A"},
    {"id": "paper", "label": "Paper", "swatch": "#FBF7F0"},
]


def test_parse_plain_json_array():
    raw = json.dumps(_VALID_PALETTE)
    assert _parse_and_validate_palette(raw) == _VALID_PALETTE


def test_parse_strips_json_code_fence():
    raw = "```json\n" + json.dumps(_VALID_PALETTE) + "\n```"
    assert _parse_and_validate_palette(raw) == _VALID_PALETTE


def test_parse_strips_generic_code_fence():
    raw = "```\n" + json.dumps(_VALID_PALETTE) + "\n```"
    assert _parse_and_validate_palette(raw) == _VALID_PALETTE


def test_parse_rejects_wrong_length():
    raw = json.dumps(_VALID_PALETTE[:4])  # only 4 items
    assert _parse_and_validate_palette(raw) == _FALLBACK_PALETTE


def test_parse_rejects_bad_hex():
    bad = [
        {"id": "red", "label": "Red", "swatch": "red"},  # not a hex
        {"id": "sage", "label": "Sage", "swatch": "#A6B89F"},
        {"id": "clay", "label": "Clay", "swatch": "#B27A5C"},
        {"id": "ink", "label": "Ink", "swatch": "#1E2A3A"},
        {"id": "paper", "label": "Paper", "swatch": "#FBF7F0"},
    ]
    assert _parse_and_validate_palette(json.dumps(bad)) == _FALLBACK_PALETTE


def test_parse_rejects_missing_fields():
    bad = [
        {"label": "Missing ID", "swatch": "#E8D9C0"},  # no id
        {"id": "sage", "label": "Sage", "swatch": "#A6B89F"},
        {"id": "clay", "label": "Clay", "swatch": "#B27A5C"},
        {"id": "ink", "label": "Ink", "swatch": "#1E2A3A"},
        {"id": "paper", "label": "Paper", "swatch": "#FBF7F0"},
    ]
    assert _parse_and_validate_palette(json.dumps(bad)) == _FALLBACK_PALETTE


def test_parse_rejects_garbage():
    assert _parse_and_validate_palette("not json at all") == _FALLBACK_PALETTE


# ── palette_step node tests (stubbed ChatOpenAI) ──────────────────────────


class _FakeResponse:
    def __init__(self, content: str):
        self.content = content


def _install_fake_chat(monkeypatch, *, content: str | None = None, raises: bool = False):
    """Patch `ChatOpenAI` inside the clarifier module to produce a scripted
    response.  Avoids network + real API keys."""

    class _FakeChat:
        def __init__(self, *args, **kwargs):  # signature-compatible stub
            pass

        async def ainvoke(self, _messages):
            if raises:
                raise RuntimeError("stub: simulated LLM failure")
            return _FakeResponse(content or "")

    monkeypatch.setattr(clarifier_mod, "ChatOpenAI", _FakeChat)


def _state_with_palette_tool_call(args: dict) -> dict:
    """Build a minimal DesignIntentState whose last message is an AI
    message calling `propose_color_palette` — palette_step reads its args
    from there."""
    ai = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "propose_color_palette",
                "args": args,
                "id": "call_42",
                "type": "tool_call",
            }
        ],
    )
    return {"messages": [ai]}


@pytest.mark.asyncio
async def test_palette_step_happy_path(settings, monkeypatch):
    _install_fake_chat(monkeypatch, content=json.dumps(_VALID_PALETTE))
    state = _state_with_palette_tool_call(
        {
            "industry": "luxury real estate",
            "visual_direction": "architectural cinematic",
            "audience": "high net worth",
            "language": "en",
        }
    )
    result = await palette_step(state, settings)

    messages = result["messages"]
    assert len(messages) == 2  # original AI + new ToolMessage
    tool_msg = messages[-1]
    assert tool_msg.tool_call_id == "call_42"
    parsed = json.loads(tool_msg.content)
    assert parsed == _VALID_PALETTE


@pytest.mark.asyncio
async def test_palette_step_falls_back_when_llm_output_invalid(settings, monkeypatch):
    # Missing one item → parser rejects → fallback.
    _install_fake_chat(monkeypatch, content=json.dumps(_VALID_PALETTE[:4]))
    state = _state_with_palette_tool_call(
        {"industry": "saas", "visual_direction": "minimal", "audience": "pm", "language": "en"}
    )
    result = await palette_step(state, settings)

    tool_msg = result["messages"][-1]
    assert json.loads(tool_msg.content) == _FALLBACK_PALETTE


@pytest.mark.asyncio
async def test_palette_step_falls_back_on_llm_exception(settings, monkeypatch):
    _install_fake_chat(monkeypatch, raises=True)
    state = _state_with_palette_tool_call(
        {"industry": "blog", "visual_direction": "clean", "audience": "reader", "language": "en"}
    )
    result = await palette_step(state, settings)

    tool_msg = result["messages"][-1]
    assert json.loads(tool_msg.content) == _FALLBACK_PALETTE


# ── Routing test ──────────────────────────────────────────────────────────


def test_route_after_step_picks_palette_when_tool_is_propose_color_palette():
    ai = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "propose_color_palette",
                "args": {"industry": "x", "visual_direction": "y", "audience": "z", "language": "en"},
                "id": "call_1",
                "type": "tool_call",
            }
        ],
    )
    state = {"messages": [ai]}
    assert route_after_step(state) == ROUTE_PALETTE

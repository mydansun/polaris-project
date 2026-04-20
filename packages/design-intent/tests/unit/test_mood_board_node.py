"""Unit tests for the mood_board_step node.

Strategy: stub the OpenAI SDK so no real API is hit.  We exercise:
- Happy path (image generation succeeds, b64 stashed in state)
- Pinterest ref with no image_b64 → short-circuit, no API call
- Image generation fails → graceful None
- Intent field fallback chain (compiled_brief_json → design_intent)
"""

from __future__ import annotations

import base64

import pytest

from polaris_design_intent.nodes import mood_board as mood_board_mod
from polaris_design_intent.nodes.mood_board import mood_board_node


class _FakeResponse:
    def __init__(self, b64: str | None):
        self.data = [_FakeDatum(b64)] if b64 is not None else []


class _FakeDatum:
    def __init__(self, b64: str | None):
        self.b64_json = b64


def _install_fake_openai(monkeypatch, *, b64: str | None = None, fail: bool = False):
    """Patch AsyncOpenAI so images.edit() returns the configured b64
    or raises.  Returns a dict that captures the last call args."""
    call: dict[str, object] = {}

    class _FakeImages:
        async def edit(self, **kwargs):
            call["args"] = kwargs
            if fail:
                raise RuntimeError("safety filter tripped")
            return _FakeResponse(b64)

    class _FakeClient:
        def __init__(self, **_kw):
            self.images = _FakeImages()

    monkeypatch.setattr(mood_board_mod, "AsyncOpenAI", _FakeClient)
    return call


@pytest.mark.asyncio
async def test_mood_board_happy_path(settings, monkeypatch):
    fake_png_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode("ascii")
    call = _install_fake_openai(monkeypatch, b64=fake_png_b64)

    state = {
        "design_intent": {
            "pageType": "dental clinic",
            "audience": "local families",
            "visualDirection": "warm, approachable",
            "accentColorHex": "#A6B89F",
            "typographyPrimary": "Fraunces",
        },
        "pinterest_refs": [
            {"id": "pin-001", "image_b64": "UklGRmZmZmZmZg==", "mime_type": "image/jpeg"}
        ],
    }
    result = await mood_board_node(state, settings)

    assert result == {"mood_board_b64": fake_png_b64}
    # Verify the reference + prompt were sent
    assert call["args"]["model"] == settings.mood_board_image_model
    assert call["args"]["size"] == settings.mood_board_image_size
    assert call["args"]["n"] == 1
    prompt_text = call["args"]["prompt"]
    assert "dental clinic" in prompt_text
    assert "Fraunces" in prompt_text
    assert "#A6B89F" in prompt_text


@pytest.mark.asyncio
async def test_mood_board_no_ref_with_image_short_circuits(settings, monkeypatch):
    # Install an exploding fake — if called we'll see the error; our test
    # asserts it is NOT called because we short-circuit first.
    call = _install_fake_openai(monkeypatch, fail=True)
    state = {
        "design_intent": {"pageType": "x"},
        "pinterest_refs": [
            {"id": "pin-001", "image_b64": None},
        ],
    }
    result = await mood_board_node(state, settings)
    assert result == {"mood_board_b64": None}
    assert "args" not in call  # never reached images.edit


@pytest.mark.asyncio
async def test_mood_board_openai_failure_returns_none(settings, monkeypatch):
    _install_fake_openai(monkeypatch, fail=True)
    state = {
        "design_intent": {"pageType": "x"},
        "pinterest_refs": [
            {"id": "pin-001", "image_b64": "UklGRg==", "mime_type": "image/jpeg"}
        ],
    }
    result = await mood_board_node(state, settings)
    assert result == {"mood_board_b64": None}


@pytest.mark.asyncio
async def test_mood_board_prefers_compiled_intent_over_raw(settings, monkeypatch):
    fake_png_b64 = base64.b64encode(b"ok").decode("ascii")
    call = _install_fake_openai(monkeypatch, b64=fake_png_b64)

    state = {
        # Raw clarifier intent (would give "modern sans")
        "design_intent": {"typographyPrimary": "modern sans", "pageType": "raw"},
        # Compiler-enriched intent (should win)
        "compiled_brief_json": {"typographyPrimary": "Cormorant Garamond", "pageType": "enriched"},
        "pinterest_refs": [
            {"id": "pin-001", "image_b64": "UklGRg==", "mime_type": "image/jpeg"}
        ],
    }
    result = await mood_board_node(state, settings)
    assert result == {"mood_board_b64": fake_png_b64}
    prompt = call["args"]["prompt"]
    assert "Cormorant Garamond" in prompt
    assert "enriched" in prompt
    assert "modern sans" not in prompt


@pytest.mark.asyncio
async def test_mood_board_empty_response_returns_none(settings, monkeypatch):
    _install_fake_openai(monkeypatch, b64=None)
    state = {
        "design_intent": {"pageType": "x"},
        "pinterest_refs": [
            {"id": "pin-001", "image_b64": "UklGRg==", "mime_type": "image/jpeg"}
        ],
    }
    result = await mood_board_node(state, settings)
    assert result == {"mood_board_b64": None}

"""Tests for codex_agents_md render + docker exec write."""

from __future__ import annotations

import asyncio
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from polaris_worker.codex_agents_md import (
    BEGIN_MARKER,
    END_MARKER,
    render_design_intent_markdown,
    write_codex_home_agents_md,
)


SAMPLE_BRIEF = {
    "intent": {
        "pageType": "landing page",
        "audience": "HNW real-estate buyers",
        "visualDirection": {"style": "editorial", "primaryColor": "white"},
        "hardConstraints": [],
    },
    "brief": "Build a spacious, editorial landing page.",
    "pinterest_refs": [
        {
            "id": "pin-001",
            "title": "Airy whites",
            "max": "https://example.com/max/1.jpg",
            "normal": "https://example.com/normal/1.jpg",
        }
    ],
    "pinterest_queries": ["real estate landing page white luxury"],
}


def test_render_markdown_wraps_in_markers():
    md = render_design_intent_markdown(SAMPLE_BRIEF)
    assert md.startswith(BEGIN_MARKER)
    assert md.rstrip().endswith(END_MARKER)
    assert "HNW real-estate buyers" in md
    assert "real estate landing page white luxury" in md
    assert "Build a spacious, editorial landing page." in md


def test_render_markdown_never_embeds_image_urls_or_base64():
    """Product rule: AGENTS.md is text-only.  No image URLs, no base64 image
    payload, no reference-image section — visual cues are already baked into
    the compiled brief's prose by the multimodal compiler node."""
    md = render_design_intent_markdown(SAMPLE_BRIEF)
    assert "https://example.com/max/1.jpg" not in md
    assert "https://example.com/normal/1.jpg" not in md
    assert "[Airy whites]" not in md
    assert "## Reference images" not in md
    assert "data:image/" not in md  # base64 data URLs never leak either


def test_render_markdown_handles_missing_fields():
    md = render_design_intent_markdown(
        {"intent": {}, "brief": "", "pinterest_refs": [], "pinterest_queries": []}
    )
    assert BEGIN_MARKER in md
    assert END_MARKER in md
    assert "_(none)_" in md  # empty fields placeholder


@pytest.mark.asyncio
async def test_write_codex_home_agents_md_invokes_docker_exec(monkeypatch):
    captured: dict = {}

    async def _fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

        class _FakeProc:
            returncode = 0

            async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
                captured["stdin"] = input
                return (b"", b"")

        return _FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _fake_create_subprocess_exec
    )
    await write_codex_home_agents_md(container="polaris-ws-abc123", content="hello")

    # args should be: docker, exec, -i, <container>, sh, -c, <cmd>
    assert captured["args"][0] == "docker"
    assert captured["args"][1] == "exec"
    assert captured["args"][2] == "-i"
    assert captured["args"][3] == "polaris-ws-abc123"
    assert captured["args"][4] == "sh"
    assert captured["args"][5] == "-c"
    assert "/home/workspace/.codex/AGENTS.md" in captured["args"][6]
    assert captured["stdin"] == b"hello"


@pytest.mark.asyncio
async def test_write_codex_home_agents_md_propagates_non_zero_exit(monkeypatch):
    async def _fake_create_subprocess_exec(*_args, **_kwargs):
        class _FakeProc:
            returncode = 1

            async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
                return (b"", b"boom")

        return _FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _fake_create_subprocess_exec
    )
    with pytest.raises(RuntimeError, match="boom"):
        await write_codex_home_agents_md(container="polaris-ws-abc123", content="x")

"""Tests for the batched image scorer.

The scorer's job is to take refs with base64 images, hand them to a
multimodal LLM in a single call, and write per-image ``score`` /
``score_reason`` back.  We exercise the wiring (stub the LLM) and the
edge-cases (missing score, LLM failure, refs without images)."""

from __future__ import annotations

import pytest

from polaris_design_intent.models import PinterestRef
from polaris_design_intent.nodes import image_scorer as scorer_mod
from polaris_design_intent.nodes.image_scorer import (
    ImageScore,
    ImageScoringBatch,
    score_images_batched,
)


def _ref(i: int, *, encoded: bool = True) -> PinterestRef:
    return PinterestRef(
        id=f"pin-{i:03d}",
        title=f"pin {i}",
        max=f"https://example.com/max/{i}.jpg",
        normal=f"https://example.com/normal/{i}.jpg",
        mime_type="image/jpeg" if encoded else None,
        image_b64="FAKE_B64" if encoded else None,
    )


class _FakeModel:
    """Stand-in for ``ChatOpenAI(...).with_structured_output(...)``."""

    def __init__(self, batch: ImageScoringBatch):
        self._batch = batch

    async def ainvoke(self, _messages):
        return self._batch


def _patch_chatopenai(monkeypatch, batch):
    """Replace ChatOpenAI so it returns a stub when .with_structured_output is called."""

    class _Builder:
        def __init__(self, *_a, **_kw):
            pass

        def with_structured_output(self, _schema, **_kw):
            return _FakeModel(batch)

    monkeypatch.setattr(scorer_mod, "ChatOpenAI", _Builder)


@pytest.mark.asyncio
async def test_score_applies_llm_scores_to_encoded_refs(settings, monkeypatch):
    refs = [_ref(1), _ref(2), _ref(3)]
    batch = ImageScoringBatch(
        scores=[
            ImageScore(index=0, score=4.5, reason="tight"),
            ImageScore(index=1, score=3.0, reason="mid"),
            ImageScore(index=2, score=5.0, reason="best"),
        ]
    )
    _patch_chatopenai(monkeypatch, batch)

    out = await score_images_batched(refs=refs, queries=["q"], settings=settings)
    assert [(r.id, r.score) for r in out] == [
        ("pin-001", 4.5),
        ("pin-002", 3.0),
        ("pin-003", 5.0),
    ]
    assert out[0].score_reason == "tight"


@pytest.mark.asyncio
async def test_score_preserves_non_encoded_refs_untouched(settings, monkeypatch):
    refs = [_ref(1, encoded=False), _ref(2), _ref(3, encoded=False)]
    batch = ImageScoringBatch(
        scores=[ImageScore(index=0, score=4.2, reason="ok")]
    )
    _patch_chatopenai(monkeypatch, batch)

    out = await score_images_batched(refs=refs, queries=["q"], settings=settings)
    # Refs 1 and 3 had no image — score stays None
    assert out[0].score is None
    assert out[2].score is None
    # Ref 2 was the only encoded one → gets the single score at index=0
    assert out[1].score == 4.2


@pytest.mark.asyncio
async def test_score_missing_returns_zero(settings, monkeypatch):
    refs = [_ref(1), _ref(2)]
    batch = ImageScoringBatch(
        scores=[ImageScore(index=0, score=4.0, reason="ok")]
        # index=1 deliberately missing
    )
    _patch_chatopenai(monkeypatch, batch)

    out = await score_images_batched(refs=refs, queries=["q"], settings=settings)
    assert out[0].score == 4.0
    assert out[1].score == 0.0
    assert out[1].score_reason == "(no score)"


@pytest.mark.asyncio
async def test_score_no_encoded_refs_short_circuits(settings, monkeypatch):
    """If every ref lacks image_b64, return list unchanged without LLM call."""
    called = {"n": 0}

    class _Builder:
        def __init__(self, *_a, **_kw):
            called["n"] += 1

        def with_structured_output(self, *_a, **_kw):
            raise AssertionError("Should not be invoked when no images to score")

    monkeypatch.setattr(scorer_mod, "ChatOpenAI", _Builder)

    refs = [_ref(1, encoded=False), _ref(2, encoded=False)]
    out = await score_images_batched(refs=refs, queries=["q"], settings=settings)
    assert out == refs
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_score_llm_failure_returns_unscored_refs(settings, monkeypatch):
    """LLM exception path: return refs untouched, leave graph to use fallback."""

    class _ExplodingModel:
        async def ainvoke(self, _messages):
            raise RuntimeError("api down")

    class _Builder:
        def __init__(self, *_a, **_kw):
            pass

        def with_structured_output(self, *_a, **_kw):
            return _ExplodingModel()

    monkeypatch.setattr(scorer_mod, "ChatOpenAI", _Builder)

    refs = [_ref(1), _ref(2)]
    out = await score_images_batched(refs=refs, queries=["q"], settings=settings)
    assert all(r.score is None for r in out)
    assert [r.id for r in out] == ["pin-001", "pin-002"]

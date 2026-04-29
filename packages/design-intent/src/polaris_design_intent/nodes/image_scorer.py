"""Batched image scoring — sends every encoded Pinterest image into one
multimodal LLM call and returns per-image 0–5 match scores against the
original Pinterest queries.

Running 6 images through a single structured-output call is ~1 API round
trip and O(1) latency; looping one call per image would 6× both.  The LLM
identifies each image by its position in the user message (``index``,
zero-based over images it actually sees — i.e. only refs that were
successfully downloaded and encoded).
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field

from polaris_design_intent.config import Settings
from polaris_design_intent.models import PinterestRef

logger = logging.getLogger(__name__)


class ImageScore(BaseModel):
    """One image's verdict.  ``index`` refers to its position in the
    scorer's input (NOT the caller's PinterestRef list)."""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(
        description=(
            "Position of the image in the input array, zero-based.  Must "
            "exactly match the ``index=N`` header that preceded each image."
        )
    )
    score: float = Field(ge=0, le=5, description="0=unrelated, 5=tight match.")
    reason: str = Field(description="One-sentence justification.")


class ImageScoringBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scores: list[ImageScore]


_SYSTEM_PROMPT = """You rate how well each reference image matches the user's
Pinterest search queries.

Scoring rubric (0–5):
  0 = unrelated / wrong subject
  2 = vaguely on-topic but weak match
  3 = roughly on-topic, workable
  4 = strong match in subject AND composition/style
  5 = tight match on subject, composition, color, AND atmosphere

Return exactly one score per image, referenced by `index` (the zero-based
position in the input, as announced in each `=== image index=N ===` header).
Return scores in the same order as the images; do not skip or duplicate
indices."""


async def score_images_batched(
    *,
    refs: list[PinterestRef],
    queries: list[str],
    settings: Settings,
) -> list[PinterestRef]:
    """Return a new list of refs with ``score`` / ``score_reason`` populated
    for every ref that had an ``image_b64`` at input.  Refs without an
    image stay unchanged.

    Order of the returned list matches the input order.  The scorer's
    ``index`` maps to the position in ``encoded`` (the sub-list of refs
    that carried images).
    """
    encoded = [(i, r) for i, r in enumerate(refs) if r.image_b64]
    if not encoded:
        logger.info("image_scorer: no encoded images to score")
        return list(refs)

    logger.info(
        "image_scorer: scoring %d images via %s (queries=%s)",
        len(encoded),
        settings.scorer_model,
        queries,
    )

    model = ChatOpenAI(
        model=settings.scorer_model,
        api_key=settings.openai_api_key,
        temperature=0,
    ).with_structured_output(ImageScoringBatch)

    blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "Queries:\n"
                + "\n".join(f"  - {q}" for q in queries)
                + f"\n\nScore these {len(encoded)} images by index "
                "(0-based, one score per image)."
            ),
        }
    ]
    for pos, (_, ref) in enumerate(encoded):
        mime = ref.mime_type or "image/jpeg"
        blocks.append({"type": "text", "text": f"\n=== image index={pos} ==="})
        blocks.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{ref.image_b64}"},
            }
        )

    try:
        result: ImageScoringBatch = await model.ainvoke(
            [SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=blocks)]
        )  # type: ignore[assignment]
    except Exception:  # noqa: BLE001
        logger.warning(
            "image_scorer: LLM call failed, returning unscored refs", exc_info=True
        )
        return list(refs)

    by_pos: dict[int, ImageScore] = {s.index: s for s in result.scores}

    # Apply scores back to the original-order refs.  Refs that weren't
    # encoded get score=None untouched; any encoded ref whose index the
    # scorer forgot to rate gets score=0 (treated as worst).
    out: list[PinterestRef] = []
    enc_pos_by_orig: dict[int, int] = {orig_i: p for p, (orig_i, _) in enumerate(encoded)}
    for orig_i, ref in enumerate(refs):
        pos = enc_pos_by_orig.get(orig_i)
        if pos is None:
            out.append(ref)
            continue
        s = by_pos.get(pos)
        if s is None:
            logger.warning(
                "image_scorer: no score returned for pos=%d (ref id=%s); defaulting to 0",
                pos,
                ref.id,
            )
            out.append(ref.model_copy(update={"score": 0.0, "score_reason": "(no score)"}))
            continue
        out.append(
            ref.model_copy(
                update={"score": float(s.score), "score_reason": s.reason},
            )
        )

    logger.info(
        "image_scorer: scored %d images: %s",
        len(encoded),
        [
            (r.id[:8], r.score)
            for r in out
            if r.image_b64 and r.score is not None
        ],
    )
    return out

from __future__ import annotations

import base64
import logging
import random
from typing import Any

from polaris_design_intent.config import Settings
from polaris_design_intent.models import PinterestRef
from polaris_design_intent.nodes.image_scorer import score_images_batched
from polaris_design_intent.state import DesignIntentState
from polaris_design_intent.tools.pinterest_client import PinterestClient

logger = logging.getLogger(__name__)


# Appended to every Pinterest query to bias results toward website /
# UI design references.  Without this, Pinterest tends to surface
# real-world photography (actual real-estate listings, interior shots,
# lifestyle imagery) rather than the landing-page / dribbble-style
# layouts the compiler actually wants.  Module-level constant so tests
# can patch it to empty string for deterministic fixture matching.
_QUERY_SUFFIX = "web design"


def _enrich_query(q: str) -> str:
    """Append ``_QUERY_SUFFIX`` to ``q`` unless the string already
    contains it (case-insensitive).  Empty / whitespace-only queries
    return unchanged."""
    q = q.strip()
    if not q or not _QUERY_SUFFIX:
        return q
    if _QUERY_SUFFIX in q.lower():
        return q
    return f"{q} {_QUERY_SUFFIX}"


async def pinterest_node(state: DesignIntentState, settings: Settings) -> dict[str, Any]:
    """Fetch references, score them, and hand ONE image to the compiler.

    Flow:
      1. Hit the Pinterest HTTP tool for each query (capped at 3 queries,
         `max_refs` results total after dedupe).
      2. Base64-encode every ref that has a ``max`` URL.
      3. Shuffle so the scorer can't lean on ranking / position bias.
      4. Batched multimodal LLM call (gpt-5.4-mini) scores each image 0-5
         against the queries.
      5. Pick the first ref with score >= ``image_score_threshold``; if
         none qualify, pick the single highest-scored.
      6. Strip ``image_b64`` from every non-chosen ref so only one image
         gets fed to the compiler (saves input tokens + avoids diluting
         the visual anchor).

    Per-query / per-image failures are logged and skipped — the graph
    proceeds with whatever succeeded, including the empty set.
    """
    queries = (state.get("pinterest_queries") or [])[:3]
    logger.info("pinterest: enter with %d raw queries: %s", len(queries), queries)
    if not queries:
        logger.info("pinterest: no queries, skipping")
        return {"pinterest_refs": []}
    queries = [_enrich_query(q) for q in queries]
    logger.info("pinterest: enriched queries: %s", queries)

    refs: list[PinterestRef] = []
    seen_ids: set[str] = set()

    async with PinterestClient(settings.pinterest_base_url) as client:
        for query in queries:
            try:
                results = await client.query(query, hops=settings.pinterest_hops)
            except Exception:
                logger.warning("Pinterest query failed: %s", query, exc_info=True)
                continue
            for item in results:
                ref_id = str(item.get("id") or "")
                if not ref_id or ref_id in seen_ids:
                    continue
                seen_ids.add(ref_id)
                refs.append(
                    PinterestRef(
                        id=ref_id,
                        title=str(item.get("title") or ""),
                        max=str(item.get("max") or ""),
                        normal=str(item.get("normal") or ""),
                    )
                )
                if len(refs) >= settings.max_refs:
                    break
            if len(refs) >= settings.max_refs:
                break

        # Base64-encode everything that has a `max` URL.  Scorer needs the
        # bytes on every candidate, not just the top-N of the old pipeline.
        for ref in refs:
            if not ref.max:
                continue
            try:
                data, mime = await client.download_image(ref.max)
            except Exception:
                logger.warning("Pinterest image download failed: %s", ref.max, exc_info=True)
                continue
            ref.mime_type = mime
            ref.image_b64 = base64.b64encode(data).decode("ascii")

    encoded = sum(1 for r in refs if r.image_b64)
    logger.info("pinterest: fetched %d refs, encoded %d images", len(refs), encoded)

    if encoded == 0:
        logger.info("pinterest: no encoded images, returning refs unscored")
        return {"pinterest_refs": [r.model_dump() for r in refs]}

    # Shuffle to neutralize position-bias in the scorer.  Using index in the
    # user message (not the original list order) as the reference key.
    random.shuffle(refs)

    scored = await score_images_batched(
        refs=refs, queries=list(queries), settings=settings
    )

    chosen = _pick_best(scored, threshold=settings.image_score_threshold)
    if chosen is None:
        logger.warning("pinterest: _pick_best returned None; falling back to first encoded ref")
        chosen = next((r for r in scored if r.image_b64), scored[0] if scored else None)

    if chosen is not None:
        # Log the exact URL that fed the scorer + compiler (i.e. max-size).
        logger.info("pinterest choose img: %s", chosen.max or chosen.normal or "?")
        # Strip image_b64 from every ref except the chosen one.
        for ref in scored:
            if chosen is not None and ref.id != chosen.id:
                ref.image_b64 = None

    return {"pinterest_refs": [r.model_dump() for r in scored]}


def _pick_best(
    refs: list[PinterestRef], *, threshold: float
) -> PinterestRef | None:
    """Prefer the first ref with ``score >= threshold`` (order is whatever
    was shuffled in); otherwise return the single max-scored ref.  Returns
    None only if there are no scored refs with images at all.
    """
    scored = [r for r in refs if r.image_b64 and r.score is not None]
    if not scored:
        return None
    for r in scored:
        if r.score is not None and r.score >= threshold:
            return r
    return max(scored, key=lambda r: r.score or 0.0)

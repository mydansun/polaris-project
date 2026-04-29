"""Pinterest reference fetch + scoring, split across two graph nodes.

The split is purely a UX-driven decision: the chat needs to render a
gallery of blurred thumbnails AS SOON AS the search lands, then keep
the same gallery visible (with per-tile spinner overlays) while the
batched scorer call grinds for ~12 s on the multimodal LLM.  A single
fused node would only emit one ``discovery:references`` lifecycle
event after both phases complete — the user would stare at a blank
chat for the entire 30-s window.

Phase split:
  pinterest_search_node — fetch refs, download bytes, gaussian-blur +
      thumbnail, upload blurred PNGs to S3, return refs with both
      ``image_b64`` (for the scorer) and ``blurred_url`` (for the
      chat) populated.
  pinterest_score_node  — take refs from state, batched-score against
      queries, pick the best, strip ``image_b64`` from non-chosen
      refs (the compiler only needs ONE image; carrying the others'
      bytes through the rest of the graph is dead weight).

The progress handler in ``polaris_worker.agents.discovery`` watches
for the boundary between these two nodes to emit a payload-delta
event onto the still-open ``discovery:references`` bubble — the
frontend uses that delta to swap from a "searching…" placeholder to
the gallery + spinner overlays.
"""
from __future__ import annotations

import base64
import io
import logging
import random
import secrets
from typing import Any

from PIL import Image, ImageFilter

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

# Blur tuning.  20px radius on a 480-px-wide thumbnail is "shapes and
# colors only, no readable detail" — visually rich enough that the
# user perceives a meaningful preview, abstract enough that the
# original Pinterest content isn't recoverable.  JPEG quality 70
# keeps a typical thumbnail at 15-30 KB.
_BLUR_RADIUS = 20
_THUMBNAIL_MAX = 480
_JPEG_QUALITY = 70
_S3_KEY_TEMPLATE = "static/images/pinterest-thumbs/{ref_id}-{salt}.jpg"


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


def _blur_thumbnail(raw: bytes) -> bytes:
    """Decode ``raw``, downscale to ``_THUMBNAIL_MAX`` on the long
    edge, gaussian-blur, re-encode as JPEG.  Returns the JPEG bytes.
    Raises whatever PIL raises on bad input — caller should swallow."""
    img = Image.open(io.BytesIO(raw))
    img = img.convert("RGB")
    img.thumbnail((_THUMBNAIL_MAX, _THUMBNAIL_MAX))
    img = img.filter(ImageFilter.GaussianBlur(radius=_BLUR_RADIUS))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
    return out.getvalue()


async def _upload_blurred_thumbnail(
    *, raw_bytes: bytes, ref_id: str, settings: Settings
) -> str | None:
    """Blur + upload to S3.  Returns the public URL or ``None`` on any
    failure (decode error, network blip, etc.) — caller logs and the
    ref's ``blurred_url`` stays None.  The chat hides any tile whose
    ``blurred_url`` is missing rather than falling back to the
    original Pinterest URL (which would defeat the whole point of
    blurring).

    Note: the ``settings`` arg is the design-intent's own Settings
    (used for everything Pinterest-related); S3 upload needs the
    api-side Settings (s3_endpoint, etc.) which we fetch lazily
    here.  Same pattern as ``_upload_mood_board_to_s3`` in the
    worker's discovery agent.
    """
    del settings  # only used for signature parity; s3 needs api settings
    try:
        from polaris_api.config import get_settings as _api_get_settings
        from polaris_api.services.s3 import (
            public_url as _s3_public_url,
            upload_bytes as _s3_upload_bytes,
        )
    except ImportError as exc:
        logger.warning("pinterest_search: s3 helper unavailable, skipping blur upload: %s", exc)
        return None
    try:
        blurred = _blur_thumbnail(raw_bytes)
    except Exception:  # noqa: BLE001
        logger.warning("pinterest_search: blur failed for ref %s", ref_id, exc_info=True)
        return None
    salt = secrets.token_hex(4)
    key = _S3_KEY_TEMPLATE.format(ref_id=ref_id, salt=salt)
    api_settings = _api_get_settings()
    try:
        await _s3_upload_bytes(
            key=key,
            data=blurred,
            content_type="image/jpeg",
            settings=api_settings,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "pinterest_search: s3 upload failed for ref %s", ref_id, exc_info=True
        )
        return None
    return _s3_public_url(key=key, settings=api_settings)


async def pinterest_search_node(
    state: DesignIntentState, settings: Settings
) -> dict[str, Any]:
    """Fetch refs, download images, blur thumbnails, upload to S3.

    Returns ``{"pinterest_refs": [...]}`` with each ref carrying:
      * ``image_b64`` / ``mime_type``  — original bytes for the scorer
      * ``blurred_url``                — public S3 URL (chat-safe)

    The scorer step (next node) reads ``image_b64`` and writes
    ``score`` / ``score_reason`` per ref, then strips ``image_b64``
    from non-chosen refs.  Failures (per-query / per-image) are
    logged-and-skipped — the graph proceeds with whatever subset
    succeeded, including the empty set.
    """
    queries = (state.get("pinterest_queries") or [])[:3]
    logger.info("pinterest_search: enter with %d raw queries: %s", len(queries), queries)
    if not queries:
        logger.info("pinterest_search: no queries, skipping")
        return {"pinterest_refs": []}
    queries = [_enrich_query(q) for q in queries]
    logger.info("pinterest_search: enriched queries: %s", queries)

    refs: list[PinterestRef] = []
    seen_ids: set[str] = set()

    async with PinterestClient(
        settings.pinterest_base_url, api_key=settings.pinterest_api_key
    ) as client:
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

        # Download originals for the scorer (image_b64) AND derive a
        # blurred thumbnail uploaded to S3 (blurred_url) — both happen
        # per-ref, both fail-soft.
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
            ref.blurred_url = await _upload_blurred_thumbnail(
                raw_bytes=data, ref_id=ref.id, settings=settings
            )

    encoded = sum(1 for r in refs if r.image_b64)
    blurred = sum(1 for r in refs if r.blurred_url)
    logger.info(
        "pinterest_search: fetched %d refs, encoded %d images, uploaded %d blurred thumbs",
        len(refs),
        encoded,
        blurred,
    )

    return {"pinterest_refs": [r.model_dump() for r in refs]}


async def pinterest_score_node(
    state: DesignIntentState, settings: Settings
) -> dict[str, Any]:
    """Score the refs that ``pinterest_search_node`` produced.

    No-op when there are no encoded images.  After scoring, picks the
    chosen ref and strips ``image_b64`` from every other one — the
    compiler downstream only consumes the chosen ref's bytes, and
    carrying the others through state would inflate the LangGraph
    checkpoint payload.
    """
    raw = state.get("pinterest_refs") or []
    refs = [PinterestRef.model_validate(r) for r in raw]
    if not refs:
        return {"pinterest_refs": []}

    encoded = sum(1 for r in refs if r.image_b64)
    if encoded == 0:
        logger.info("pinterest_score: no encoded images, returning refs unscored")
        return {"pinterest_refs": [r.model_dump() for r in refs]}

    # Shuffle to neutralize position-bias in the scorer.  The
    # ``index`` the scorer assigns refers to the position in the
    # encoded sub-list; the helper handles the mapping back.
    queries = [_enrich_query(q) for q in (state.get("pinterest_queries") or [])[:3]]
    random.shuffle(refs)
    scored = await score_images_batched(
        refs=refs, queries=list(queries), settings=settings
    )

    chosen = _pick_best(scored, threshold=settings.image_score_threshold)
    if chosen is None:
        logger.warning(
            "pinterest_score: _pick_best returned None; falling back to first encoded ref"
        )
        chosen = next((r for r in scored if r.image_b64), scored[0] if scored else None)
    if chosen is not None:
        logger.info("pinterest choose img: %s", chosen.max or chosen.normal or "?")
        for ref in scored:
            if chosen is not None and ref.id != chosen.id:
                ref.image_b64 = None

    return {"pinterest_refs": [r.model_dump() for r in scored]}


def _pick_best(
    refs: list[PinterestRef], *, threshold: float
) -> PinterestRef | None:
    """Prefer the first ref with ``score >= threshold`` (order is
    whatever was shuffled in); otherwise return the single max-scored
    ref.  Returns None only if there are no scored refs with images
    at all."""
    scored = [r for r in refs if r.image_b64 and r.score is not None]
    if not scored:
        return None
    for r in scored:
        if r.score is not None and r.score >= threshold:
            return r
    return max(scored, key=lambda r: r.score or 0.0)


# ── Back-compat shim for the unit tests that drove the old fused
# ── ``pinterest_node`` directly.  Callers in the production graph
# ── now use the split pair above.
async def pinterest_node(
    state: DesignIntentState, settings: Settings
) -> dict[str, Any]:
    """Deprecated single-step variant.  Composes search + score so
    legacy tests keep passing without forcing them to drive both
    nodes manually."""
    after_search = await pinterest_search_node(state, settings)
    return await pinterest_score_node(
        {**state, "pinterest_refs": after_search.get("pinterest_refs", [])},
        settings,
    )

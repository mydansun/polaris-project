"""Unsplash search with transparent S3 re-hosting + dedupe.

The workspace-side MCP proxies to :func:`search_and_cache` via the
``/workspace/unsplash/search`` route.  The API key and S3 credentials
stay on the platform — the workspace container never sees them.

Behavior:
  1. Call Unsplash ``/search/photos``.
  2. For every result, for each of ``regular`` + ``small``:
     - Look up ``unsplash_images(photo_id, size)``.  Hit → reuse the
       existing ``s3_key``.  Miss → download from Unsplash CDN, upload
       to S3 under ``static/images/up/<uuid4>.<ext>``, insert a row.
  3. Per Unsplash API guidelines, fire-and-forget ``/photos/{id}/download``
     exactly once the FIRST time we cache any size of a photo.
  4. Return a list of :class:`StoredPhoto` dicts where ``urls.regular`` /
     ``urls.small`` are anonymous-public S3 URLs built from ``S3_URL_BASE``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polaris_api.config import Settings
from polaris_api.models import UnsplashImage
from polaris_api.services.s3 import public_url, upload_bytes

logger = logging.getLogger(__name__)


UNSPLASH_API_BASE = "https://api.unsplash.com"
CACHED_SIZES = ("regular", "small")

# Every Unsplash URL/link we send back must include UTM params per the
# attribution guidelines.  Centralized so we never forget.
_UTM = "utm_source=polaris&utm_medium=referral"


def _auth_headers(settings: Settings) -> dict[str, str]:
    if not settings.unsplash_access_key:
        raise RuntimeError(
            "UNSPLASH_ACCESS_KEY not configured — refusing to call Unsplash"
        )
    return {
        "Accept-Version": "v1",
        "Authorization": f"Client-ID {settings.unsplash_access_key}",
    }


def _extension_from_content_type(content_type: str) -> str:
    """Map the Unsplash CDN Content-Type to a file extension.  Unsplash
    defaults to JPEG; anything else is rare but we handle PNG/WebP too."""
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    return {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
    }.get(ct, "jpg")


async def _download(url: str, client: httpx.AsyncClient) -> tuple[bytes, str]:
    resp = await client.get(url, timeout=30.0, follow_redirects=True)
    resp.raise_for_status()
    return resp.content, resp.headers.get("content-type", "image/jpeg")


async def _fetch_existing(
    session: AsyncSession, photo_id: str
) -> dict[str, UnsplashImage]:
    """Return ``{size: UnsplashImage}`` for any cached rows of this photo."""
    stmt = select(UnsplashImage).where(
        UnsplashImage.photo_id == photo_id,
        UnsplashImage.size.in_(list(CACHED_SIZES)),
    )
    rows = (await session.execute(stmt)).scalars().all()
    return {row.size: row for row in rows}


async def _ensure_size(
    *,
    photo_id: str,
    size: str,
    unsplash_url: str,
    existing: dict[str, UnsplashImage],
    session: AsyncSession,
    http: httpx.AsyncClient,
    settings: Settings,
) -> str:
    """Return the ``s3_key`` for the (photo_id, size) pair; upload if missing."""
    cached = existing.get(size)
    if cached is not None:
        return cached.s3_key

    data, content_type = await _download(unsplash_url, http)
    ext = _extension_from_content_type(content_type)
    s3_key = f"static/images/up/{uuid4()}.{ext}"
    await upload_bytes(
        key=s3_key,
        data=data,
        content_type=content_type,
        settings=settings,
    )

    # INSERT; on race with another request for the same (photo_id, size)
    # the unique constraint will fire and we fall back to the winning row.
    try:
        session.add(
            UnsplashImage(
                photo_id=photo_id,
                size=size,
                s3_key=s3_key,
                content_type=content_type,
                bytes=len(data),
            )
        )
        await session.flush()
    except Exception:  # noqa: BLE001
        await session.rollback()
        logger.warning(
            "unsplash: insert race for photo_id=%s size=%s — re-reading", photo_id, size
        )
        row = (
            await session.execute(
                select(UnsplashImage).where(
                    UnsplashImage.photo_id == photo_id,
                    UnsplashImage.size == size,
                )
            )
        ).scalar_one()
        return row.s3_key

    logger.info(
        "unsplash: cached photo=%s size=%s key=%s (%d bytes)",
        photo_id,
        size,
        s3_key,
        len(data),
    )
    return s3_key


async def _track_download(photo_id: str, settings: Settings) -> None:
    """Fire-and-forget: hit ``/photos/{id}/download`` to tell Unsplash this
    image was "downloaded".  Required by their API terms when we actually
    use the image.  Logs and swallows any failure — bookkeeping only."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{UNSPLASH_API_BASE}/photos/{photo_id}/download",
                headers=_auth_headers(settings),
            )
            if resp.status_code >= 400:
                logger.warning(
                    "unsplash: track_download %s returned %s",
                    photo_id,
                    resp.status_code,
                )
            else:
                logger.info("unsplash: track_download ok for %s", photo_id)
    except Exception:  # noqa: BLE001
        logger.warning(
            "unsplash: track_download failed for %s — continuing",
            photo_id,
            exc_info=True,
        )


def _photo_to_stored(
    photo: dict[str, Any], *, s3_urls: dict[str, str]
) -> dict[str, Any]:
    user = photo.get("user") or {}
    username = user.get("username") or ""
    display_name = user.get("name") or username
    photographer_url = (
        f"https://unsplash.com/@{username}?{_UTM}" if username else ""
    )
    html_links = (photo.get("links") or {}).get("html") or ""
    photo_url = f"{html_links}?{_UTM}" if html_links else ""
    attribution_text = f"Photo by {display_name} on Unsplash"
    attribution_html = (
        f'Photo by <a href="{photographer_url}">{display_name}</a> '
        f'on <a href="https://unsplash.com/?{_UTM}">Unsplash</a>'
    )
    return {
        "photo_id": photo["id"],
        "description": photo.get("description"),
        "alt_description": photo.get("alt_description"),
        "urls": s3_urls,
        "width": int(photo.get("width") or 0),
        "height": int(photo.get("height") or 0),
        "color": photo.get("color") or "#000000",
        "blur_hash": photo.get("blur_hash"),
        "photographer_name": display_name,
        "photographer_username": username,
        "photographer_url": photographer_url,
        "photo_url": photo_url,
        "attribution_text": attribution_text,
        "attribution_html": attribution_html,
    }


async def search_and_cache(
    *,
    query: str,
    per_page: int,
    orientation: str | None,
    color: str | None,
    content_filter: str,
    session: AsyncSession,
    settings: Settings,
) -> list[dict[str, Any]]:
    """Query Unsplash, cache any missing sizes to S3, return a list of
    fully-materialized photo records with S3-backed URLs."""
    params: dict[str, Any] = {
        "query": query,
        "per_page": max(1, min(30, int(per_page))),
        "content_filter": content_filter,
    }
    if orientation:
        params["orientation"] = orientation
    if color:
        params["color"] = color

    logger.info(
        "unsplash: search query=%r per_page=%d orientation=%s color=%s",
        query,
        params["per_page"],
        orientation,
        color,
    )

    async with httpx.AsyncClient(timeout=30.0) as http:
        resp = await http.get(
            f"{UNSPLASH_API_BASE}/search/photos",
            params=params,
            headers=_auth_headers(settings),
        )
        resp.raise_for_status()
        data = resp.json()
        photos = data.get("results") or []

        results: list[dict[str, Any]] = []
        for photo in photos:
            photo_id = photo.get("id")
            urls = photo.get("urls") or {}
            if not photo_id or not isinstance(urls, dict):
                continue

            existing = await _fetch_existing(session, photo_id)
            was_new_photo = len(existing) == 0

            s3_keys: dict[str, str] = {}
            try:
                for size in CACHED_SIZES:
                    src = urls.get(size)
                    if not src:
                        continue
                    s3_keys[size] = await _ensure_size(
                        photo_id=photo_id,
                        size=size,
                        unsplash_url=src,
                        existing=existing,
                        session=session,
                        http=http,
                        settings=settings,
                    )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "unsplash: failed to cache photo %s, skipping", photo_id, exc_info=True
                )
                continue

            if not s3_keys:
                continue

            # Commit per-photo so partial failures don't lose earlier work.
            await session.commit()

            if was_new_photo:
                # Fire-and-forget — don't await.
                asyncio.create_task(_track_download(photo_id, settings))

            s3_urls = {
                size: public_url(key=key, settings=settings)
                for size, key in s3_keys.items()
            }
            results.append(_photo_to_stored(photo, s3_urls=s3_urls))

    logger.info("unsplash: returning %d photos for query=%r", len(results), query)
    return results

"""Tests for the Unsplash search + S3 re-hosting service.

Strategy: stub both sides — respx for Unsplash API + image CDN, monkeypatch
for the S3 uploader — so the test is offline + deterministic.  The DB
side uses a real in-memory SQLite session (async) so dedupe queries
actually exercise the UNIQUE constraint.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from polaris_api.config import Settings
from polaris_api.models import UnsplashImage
from polaris_api.services import s3 as s3_mod
from polaris_api.services import unsplash as unsplash_mod
from polaris_api.services.unsplash import search_and_cache


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def settings() -> Settings:
    s = Settings(
        UNSPLASH_ACCESS_KEY="test-key",  # type: ignore[call-arg]
        S3_ACCESS_KEY_ID="test",  # type: ignore[call-arg]
        S3_SECRET_ACCESS_KEY="test",  # type: ignore[call-arg]
        S3_ENDPOINT="https://s3.test",  # type: ignore[call-arg]
        S3_BUCKET="polaris",  # type: ignore[call-arg]
        S3_URL_BASE="https://polaris.s3.test",  # type: ignore[call-arg]
    )
    return s


@pytest_asyncio.fixture
async def db_session():
    """In-memory async-SQLite for exercising the UnsplashImage ORM +
    UNIQUE constraint.  Each test gets a fresh DB."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    # Only create the table we exercise — most other tables in the schema
    # use Postgres-specific JSONB columns SQLite can't render.
    async with engine.begin() as conn:
        await conn.run_sync(UnsplashImage.__table__.create)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest.fixture(autouse=True)
def _stub_s3(monkeypatch):
    """Record every S3 upload without actually calling MinIO."""
    uploads: list[dict[str, Any]] = []

    async def fake_upload(*, key, data, content_type, settings):
        uploads.append(
            {"key": key, "bytes": len(data), "content_type": content_type}
        )

    monkeypatch.setattr(s3_mod, "upload_bytes", fake_upload)
    # `unsplash.search_and_cache` imports `upload_bytes` at module load, so
    # we also patch it at that reference.
    monkeypatch.setattr(unsplash_mod, "upload_bytes", fake_upload)
    return uploads


@pytest.fixture(autouse=True)
def _no_track_download(monkeypatch):
    """Record track_download calls without firing real HTTP."""
    calls: list[str] = []

    async def fake_track(photo_id, settings):
        calls.append(photo_id)

    monkeypatch.setattr(unsplash_mod, "_track_download", fake_track)
    return calls


def _sample_photo(photo_id: str = "pin-001") -> dict[str, Any]:
    return {
        "id": photo_id,
        "description": "A sample photo",
        "alt_description": "alt",
        "width": 4000,
        "height": 3000,
        "color": "#ABCDEF",
        "blur_hash": "LK123",
        "urls": {
            "raw": f"https://cdn.unsplash.com/raw/{photo_id}.jpg",
            "full": f"https://cdn.unsplash.com/full/{photo_id}.jpg",
            "regular": f"https://cdn.unsplash.com/regular/{photo_id}.jpg",
            "small": f"https://cdn.unsplash.com/small/{photo_id}.jpg",
            "thumb": f"https://cdn.unsplash.com/thumb/{photo_id}.jpg",
        },
        "links": {"html": f"https://unsplash.com/photos/{photo_id}"},
        "user": {"username": "jane", "name": "Jane Doe"},
    }


def _mock_unsplash(router, photos, query_fixture: str = "q") -> None:
    """Mount a respx route for the Unsplash search + each photo's
    regular/small CDN endpoints."""
    router.get("https://api.unsplash.com/search/photos").mock(
        return_value=httpx.Response(200, json={"results": photos})
    )
    for p in photos:
        for size in ("regular", "small"):
            router.get(p["urls"][size]).mock(
                return_value=httpx.Response(
                    200,
                    content=f"FAKE_{p['id']}_{size}".encode(),
                    headers={"content-type": "image/jpeg"},
                )
            )


# ── Tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_uploads_two_sizes_and_returns_s3_urls(
    settings, db_session, _stub_s3, _no_track_download
):
    photos = [_sample_photo("pin-001"), _sample_photo("pin-002")]
    with respx.mock(assert_all_called=False) as router:
        _mock_unsplash(router, photos)
        out = await search_and_cache(
            query="coffee",
            per_page=2,
            orientation=None,
            color=None,
            content_filter="low",
            session=db_session,
            settings=settings,
        )
        # Let fire-and-forget track_download tasks finish.
        await asyncio.sleep(0.05)

    assert len(out) == 2
    for record in out:
        assert record["urls"]["regular"].startswith("https://polaris.s3.test/static/images/")
        assert record["urls"]["small"].startswith("https://polaris.s3.test/static/images/")
        assert record["urls"]["regular"].endswith(".jpg")
        assert record["attribution_text"] == "Photo by Jane Doe on Unsplash"
        assert "utm_source=polaris" in record["photographer_url"]
        assert record["photo_id"] in {"pin-001", "pin-002"}

    # 2 photos × 2 sizes = 4 S3 uploads
    assert len(_stub_s3) == 4
    # 2 new photos → 2 track_download fires
    assert sorted(_no_track_download) == ["pin-001", "pin-002"]

    # 4 rows in unsplash_images
    rows = (await db_session.execute(UnsplashImage.__table__.select())).all()
    assert len(rows) == 4


@pytest.mark.asyncio
async def test_second_search_reuses_cache(
    settings, db_session, _stub_s3, _no_track_download
):
    """Searching twice for the same photo uploads S3 only once."""
    photos = [_sample_photo("pin-xyz")]
    with respx.mock(assert_all_called=False) as router:
        _mock_unsplash(router, photos)
        await search_and_cache(
            query="q1", per_page=1, orientation=None, color=None,
            content_filter="low", session=db_session, settings=settings,
        )
        await asyncio.sleep(0.05)
        first_uploads = len(_stub_s3)
        first_tracks = list(_no_track_download)

        await search_and_cache(
            query="q2", per_page=1, orientation=None, color=None,
            content_filter="low", session=db_session, settings=settings,
        )
        await asyncio.sleep(0.05)

    assert first_uploads == 2  # regular + small on first call
    assert len(_stub_s3) == 2  # still 2 — second call hit cache for both sizes
    # track_download fired exactly once even though the photo was returned twice
    assert first_tracks == ["pin-xyz"]
    assert _no_track_download == ["pin-xyz"]


@pytest.mark.asyncio
async def test_partial_cache_uploads_missing_size_only(
    settings, db_session, _stub_s3, _no_track_download
):
    """Photo with only 'small' cached previously — second search should
    upload 'regular' only, reuse 'small'."""
    # Seed the DB as if an earlier search had stored only the 'small' variant.
    db_session.add(
        UnsplashImage(
            photo_id="pin-partial",
            size="small",
            s3_key="static/images/seed.jpg",
            content_type="image/jpeg",
            bytes=10,
        )
    )
    await db_session.commit()

    photos = [_sample_photo("pin-partial")]
    with respx.mock(assert_all_called=False) as router:
        _mock_unsplash(router, photos)
        out = await search_and_cache(
            query="q", per_page=1, orientation=None, color=None,
            content_filter="low", session=db_session, settings=settings,
        )
        await asyncio.sleep(0.05)

    # Only 'regular' was missing → 1 new upload
    assert len(_stub_s3) == 1
    assert _stub_s3[0]["key"].startswith("static/images/up/")
    # small URL comes from the seed row
    assert out[0]["urls"]["small"] == "https://polaris.s3.test/static/images/seed.jpg"
    # regular URL is newly assigned
    assert out[0]["urls"]["regular"].startswith("https://polaris.s3.test/static/images/")
    assert out[0]["urls"]["regular"] != out[0]["urls"]["small"]

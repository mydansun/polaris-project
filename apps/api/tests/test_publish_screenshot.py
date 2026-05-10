"""Tests for services/publish_screenshot.py.

Strategy:
  * The pure command-construction layer (`_chromium_command`) is unit-
    tested directly — guards the flag set we depend on (no-sandbox,
    headless=new, virtual-time-budget) without touching subprocess.
  * The end-to-end orchestrator (`capture_and_record`) is exercised
    against an in-memory async sqlite + monkeypatched s3 + faked
    chromium subprocess.  Validates: the public URL is composed, the
    DB column is updated, the temp file is cleaned up.
  * Failure modes (timeout, non-zero exit, missing output, S3 raise,
    DB raise) all swallow + return None.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from polaris_api.config import Settings
from polaris_api.models import Deployment, Project, User
from polaris_api.services import publish_screenshot, s3 as s3_mod


# ── Pure helper ──────────────────────────────────────────────────────────


def test_chromium_command_includes_no_sandbox_and_headless_new(tmp_path: Path):
    cmd = publish_screenshot._chromium_command(
        "https://example.com/", tmp_path / "out.png"
    )
    assert cmd[0] == "chromium"
    assert "--headless=new" in cmd
    assert "--no-sandbox" in cmd
    assert any(arg.startswith("--window-size=") for arg in cmd)
    assert any(arg.startswith("--virtual-time-budget=") for arg in cmd)
    assert cmd[-1] == "https://example.com/"
    assert any(arg.startswith("--screenshot=") for arg in cmd)


def test_chromium_command_window_size_matches_constants(tmp_path: Path):
    cmd = publish_screenshot._chromium_command("u", tmp_path / "x.png")
    size = next(a for a in cmd if a.startswith("--window-size="))
    expected = (
        publish_screenshot.VIEWPORT_WIDTH,
        publish_screenshot.VIEWPORT_HEIGHT,
    )
    assert size == f"--window-size={expected[0]},{expected[1]}"


def test_chromium_command_url_is_last_positional(tmp_path: Path):
    """Chromium parses the trailing non-flag arg as the URL.  Order
    matters — if a future change shuffled flags after the URL, chromium
    would silently treat the URL as a flag value and load about:blank."""
    cmd = publish_screenshot._chromium_command(
        "https://e1b1.prod.polaris-dev.xyz/", tmp_path / "shot.png"
    )
    assert cmd[-1].startswith("https://")


# ── orchestrator with stubs ──────────────────────────────────────────────


@pytest.fixture
def settings() -> Settings:
    return Settings(
        POLARIS_DOMAIN="polaris-dev.xyz",  # type: ignore[call-arg]
        S3_ACCESS_KEY_ID="ak",  # type: ignore[call-arg]
        S3_SECRET_ACCESS_KEY="sk",  # type: ignore[call-arg]
        S3_BUCKET="polaris",  # type: ignore[call-arg]
        # s3_url_base auto-derived from POLARIS_DOMAIN by validators
    )


@pytest_asyncio.fixture
async def db_with_deployment():
    """In-memory async-sqlite with the rows the orchestrator touches.
    sqlite for portability — we don't need any postgres-only feature
    here (no DISTINCT ON, no ANY())."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        # Create just users / projects / deployments — same minimal set
        # the test_unsplash fixture uses.  Postgres-only types (UUID,
        # JSONB) round-trip via SQLAlchemy's compat layer.
        await conn.run_sync(User.__table__.create)
        await conn.run_sync(Project.__table__.create)
        await conn.run_sync(Deployment.__table__.create)

    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        user = User(id=uuid4(), email="t@example", name="T")
        session.add(user)
        await session.flush()
        project = Project(
            id=uuid4(),
            user_id=user.id,
            name="x",
            slug="x",
            description=None,
            stack_template="spa",
            status="active",
        )
        session.add(project)
        await session.flush()
        dep = Deployment(
            id=uuid4(),
            project_id=project.id,
            domain="x.prod.polaris-dev.xyz",
            status="ready",
        )
        session.add(dep)
        await session.commit()
        yield session, dep
    await engine.dispose()


def _stub_chromium_writes_a_png(content: bytes = b"\x89PNG\r\n\x1a\n"):
    """Replace _run_chromium with one that writes ``content`` to the
    file path passed in, simulating a successful capture."""

    async def fake(url: str, output: Path) -> None:
        output.write_bytes(content)

    return fake


def _stub_probe_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass the pre-screenshot HTTP probe in orchestrator tests.

    The probe is exercised separately by ``test_wait_until_ready_*``;
    these orchestrator tests want to assert the post-probe path
    (chromium + S3 + DB)."""

    async def fake_ready(*_args, **_kwargs):
        return True

    monkeypatch.setattr(publish_screenshot, "_wait_until_ready", fake_ready)


@pytest.mark.asyncio
async def test_capture_and_record_happy_path(
    settings, db_with_deployment, monkeypatch
):
    session, dep = db_with_deployment

    # Stub chromium subprocess.
    _stub_probe_ok(monkeypatch)
    monkeypatch.setattr(
        publish_screenshot, "_run_chromium", _stub_chromium_writes_a_png()
    )

    # Stub S3 upload — record bytes + key.
    uploaded: dict[str, object] = {}

    async def fake_upload(*, key, data, content_type, settings):
        uploaded["key"] = key
        uploaded["bytes"] = len(data)
        uploaded["content_type"] = content_type

    monkeypatch.setattr(s3_mod, "upload_bytes", fake_upload)

    public_url = await publish_screenshot.capture_and_record(
        deployment_id=dep.id,
        url=f"https://{dep.domain}/",
        db=session,
        settings=settings,
    )

    # Returned URL composed from settings.s3_url_base + the salted key.
    # Each shoot gets a fresh uuid4 suffix so the URL itself changes —
    # bypasses any browser cache that might be holding a previous
    # response for this deployment_id.
    assert public_url is not None
    import re
    key_pattern = (
        rf"static/images/deployments/{re.escape(str(dep.id))}-[0-9a-f]{{32}}\.png"
    )
    assert re.search(key_pattern, public_url), (
        f"public_url {public_url!r} doesn't match {key_pattern!r}"
    )
    assert re.fullmatch(key_pattern, str(uploaded["key"])), (
        f"uploaded key {uploaded['key']!r} doesn't match {key_pattern!r}"
    )
    assert uploaded["content_type"] == "image/png"
    assert uploaded["bytes"] >= 8  # at least the PNG header

    # DB row got the URL written.
    refreshed = (
        await session.execute(
            select(Deployment).where(Deployment.id == dep.id)
        )
    ).scalar_one()
    assert refreshed.screenshot_url == public_url


@pytest.mark.asyncio
async def test_capture_and_record_chromium_timeout_returns_none(
    settings, db_with_deployment, monkeypatch
):
    session, dep = db_with_deployment

    _stub_probe_ok(monkeypatch)

    async def fake_run(url, output):
        raise RuntimeError("chromium timed out")

    monkeypatch.setattr(publish_screenshot, "_run_chromium", fake_run)

    out = await publish_screenshot.capture_and_record(
        deployment_id=dep.id,
        url="https://x/",
        db=session,
        settings=settings,
    )
    assert out is None
    refreshed = (
        await session.execute(
            select(Deployment).where(Deployment.id == dep.id)
        )
    ).scalar_one()
    assert refreshed.screenshot_url is None  # no DB write on capture failure


@pytest.mark.asyncio
async def test_capture_and_record_s3_failure_returns_none_no_db_write(
    settings, db_with_deployment, monkeypatch
):
    session, dep = db_with_deployment
    _stub_probe_ok(monkeypatch)
    monkeypatch.setattr(
        publish_screenshot, "_run_chromium", _stub_chromium_writes_a_png()
    )

    async def fake_upload(**_kwargs):
        raise RuntimeError("MinIO unreachable")

    monkeypatch.setattr(s3_mod, "upload_bytes", fake_upload)

    out = await publish_screenshot.capture_and_record(
        deployment_id=dep.id, url="https://x/", db=session, settings=settings,
    )
    assert out is None
    refreshed = (
        await session.execute(
            select(Deployment).where(Deployment.id == dep.id)
        )
    ).scalar_one()
    assert refreshed.screenshot_url is None


@pytest.mark.asyncio
async def test_capture_and_record_cleans_up_tempfile(
    settings, db_with_deployment, monkeypatch, tmp_path
):
    """The tempfile chromium writes to must be unlinked even when
    upload fails — otherwise long-running api processes leak /tmp."""
    session, dep = db_with_deployment

    _stub_probe_ok(monkeypatch)

    captured_paths: list[Path] = []

    async def fake_run(url, output):
        captured_paths.append(output)
        output.write_bytes(b"\x89PNG\r\n\x1a\n")

    monkeypatch.setattr(publish_screenshot, "_run_chromium", fake_run)

    async def fake_upload(**_kwargs):
        raise RuntimeError("nope")

    monkeypatch.setattr(s3_mod, "upload_bytes", fake_upload)

    await publish_screenshot.capture_and_record(
        deployment_id=dep.id, url="https://x/", db=session, settings=settings,
    )
    # The temp file should NOT linger on disk.
    assert captured_paths
    assert not captured_paths[0].exists()


# ── pre-screenshot HTTP probe ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wait_until_ready_succeeds_on_first_200(respx_mock):
    respx_mock.get("https://x/").respond(status_code=200, text="ok")
    ok = await publish_screenshot._wait_until_ready(
        "https://x/", timeout_s=2.0, interval_s=0.1, request_timeout_s=1.0,
    )
    assert ok is True


@pytest.mark.asyncio
async def test_wait_until_ready_retries_until_200(respx_mock):
    """Connection refused / 503 / 502 are all "not ready yet" — keep polling
    until either the URL flips to 200 or we hit the deadline."""
    route = respx_mock.get("https://x/")
    route.side_effect = [
        httpx.ConnectError("refused"),
        httpx.Response(503),
        httpx.Response(200, text="ok"),
    ]
    ok = await publish_screenshot._wait_until_ready(
        "https://x/", timeout_s=5.0, interval_s=0.05, request_timeout_s=1.0,
    )
    assert ok is True
    assert route.call_count == 3


@pytest.mark.asyncio
async def test_wait_until_ready_times_out_when_never_200(respx_mock):
    """If the URL never flips to 200 within the budget, return False so
    the caller skips chromium altogether."""
    respx_mock.get("https://x/").respond(status_code=503)
    ok = await publish_screenshot._wait_until_ready(
        "https://x/", timeout_s=0.3, interval_s=0.1, request_timeout_s=0.1,
    )
    assert ok is False


@pytest.mark.asyncio
async def test_wait_until_ready_follows_redirects(respx_mock):
    """Apps that redirect / → /home should be detected by the final-page
    status, not by the redirect status of the first hop."""
    respx_mock.get("https://x/").respond(
        status_code=308, headers={"Location": "https://x/home"}
    )
    respx_mock.get("https://x/home").respond(status_code=200, text="ok")
    ok = await publish_screenshot._wait_until_ready(
        "https://x/", timeout_s=2.0, interval_s=0.1, request_timeout_s=1.0,
    )
    assert ok is True


@pytest.mark.asyncio
async def test_capture_and_record_uses_fresh_salt_per_shoot(
    settings, db_with_deployment, monkeypatch
):
    """Re-shooting the same deployment must produce a different S3 key
    so browsers can't serve a stale cached PNG under the same URL.
    Otherwise users hit the May-2026 ``404 in browser cache`` trap
    where the URL was 404 before backfill, browser cached the 404,
    and even after the file existed in S3 the user kept seeing 404."""
    session, dep = db_with_deployment
    _stub_probe_ok(monkeypatch)
    monkeypatch.setattr(
        publish_screenshot, "_run_chromium", _stub_chromium_writes_a_png()
    )

    keys: list[str] = []

    async def fake_upload(*, key, **_):
        keys.append(str(key))

    monkeypatch.setattr(s3_mod, "upload_bytes", fake_upload)

    for _ in range(3):
        await publish_screenshot.capture_and_record(
            deployment_id=dep.id, url="https://x/", db=session, settings=settings,
        )

    assert len(keys) == 3
    assert len(set(keys)) == 3, f"keys must all be distinct, got: {keys}"
    # All three should match the salted shape.
    import re
    pattern = (
        rf"static/images/deployments/{re.escape(str(dep.id))}-[0-9a-f]{{32}}\.png"
    )
    for k in keys:
        assert re.fullmatch(pattern, k), f"{k!r} does not match {pattern!r}"


@pytest.mark.asyncio
async def test_capture_and_record_skips_chromium_when_probe_times_out(
    settings, db_with_deployment, monkeypatch
):
    """If the probe never returns 200, capture_and_record returns None
    WITHOUT spawning chromium — saves the 25s wasted timeout."""
    session, dep = db_with_deployment

    async def fake_ready(*_args, **_kwargs):
        return False

    monkeypatch.setattr(publish_screenshot, "_wait_until_ready", fake_ready)

    chromium_calls = 0

    async def fail_if_called(*_args, **_kwargs):
        nonlocal chromium_calls
        chromium_calls += 1
        raise AssertionError("chromium should not be invoked when probe times out")

    monkeypatch.setattr(publish_screenshot, "_run_chromium", fail_if_called)

    out = await publish_screenshot.capture_and_record(
        deployment_id=dep.id, url="https://x/", db=session, settings=settings,
    )
    assert out is None
    assert chromium_calls == 0
    refreshed = (
        await session.execute(select(Deployment).where(Deployment.id == dep.id))
    ).scalar_one()
    assert refreshed.screenshot_url is None

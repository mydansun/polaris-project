"""Tests for ``GET /projects/{id}/codex-quota``.

The route composes three pieces:
  1. project ownership check (404 on miss)
  2. latest workspace lookup (returns ``available=False`` when none)
  3. ``get_codex_quota`` upstream call (cached, may return None)

We bypass auth + redis + the real upstream WS via dependency
overrides; the DB is a real in-memory async sqlite so SQLAlchemy
joins/scalars actually run.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from polaris_api.db import get_session
from polaris_api.deps import get_current_user
from polaris_api.models import Project, User, Workspace
from polaris_api.redis_client import get_redis
from polaris_api.routes.codex_quota import router as codex_quota_router
from polaris_api.services import codex_quota as codex_quota_svc


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        # Only the tables this route reads — Sessions/Clarifications
        # carry JSONB columns sqlite can't render.
        await conn.run_sync(User.__table__.create)
        await conn.run_sync(Project.__table__.create)
        await conn.run_sync(Workspace.__table__.create)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as session:
        yield session


@pytest_asyncio.fixture
async def seeded_user(db_session) -> User:
    user = User(
        id=uuid4(),
        email="t@example.com",
        name="T",
        avatar_url=None,
    )
    db_session.add(user)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def seeded_project(db_session, seeded_user) -> Project:
    project = Project(
        id=uuid4(),
        user_id=seeded_user.id,
        name="Test Project",
        slug="test-project",
        description=None,
        stack_template="default-stack",
        status="active",
    )
    db_session.add(project)
    await db_session.commit()
    return project


def _client(
    db_session,
    user: User,
    *,
    quota_result: dict | None,
) -> TestClient:
    """Build a tiny FastAPI app mounting just codex_quota_router with
    deps overridden to in-memory DB / fake redis / pinned user / pinned
    upstream result."""
    app = FastAPI()
    app.include_router(codex_quota_router)

    async def _fake_get_session():
        yield db_session

    async def _fake_get_user():
        return user

    class _FakeRedis:
        # The route doesn't call methods on redis directly — it's only
        # forwarded to get_codex_quota, which we replace below.  But
        # the dep returns a Redis instance, so any object satisfies.
        pass

    async def _fake_get_codex_quota(*, workspace_id, redis):
        return quota_result

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_user] = _fake_get_user
    app.dependency_overrides[get_redis] = lambda: _FakeRedis()

    # Patch the function the route imported at module load.
    import polaris_api.routes.codex_quota as route_mod

    route_mod.get_codex_quota = _fake_get_codex_quota  # type: ignore[assignment]

    client = TestClient(app)
    client._restore = lambda: setattr(  # type: ignore[attr-defined]
        route_mod, "get_codex_quota", codex_quota_svc.get_codex_quota
    )
    return client


# ── Tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_returns_404_for_missing_project(
    db_session, seeded_user
):
    client = _client(db_session, seeded_user, quota_result=None)
    try:
        r = client.get(f"/projects/{uuid4()}/codex-quota")
        assert r.status_code == 404
    finally:
        client._restore()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_returns_404_for_other_users_project(
    db_session, seeded_user, seeded_project
):
    # Project belongs to seeded_user; query as a different user.
    other = User(id=uuid4(), email="o@example.com", name="O", avatar_url=None)
    db_session.add(other)
    await db_session.commit()
    client = _client(db_session, other, quota_result=None)
    try:
        r = client.get(f"/projects/{seeded_project.id}/codex-quota")
        assert r.status_code == 404
    finally:
        client._restore()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_returns_unavailable_when_project_has_no_workspace(
    db_session, seeded_user, seeded_project
):
    client = _client(db_session, seeded_user, quota_result=None)
    try:
        r = client.get(f"/projects/{seeded_project.id}/codex-quota")
        assert r.status_code == 200
        assert r.json() == {"available": False}
    finally:
        client._restore()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_returns_unavailable_when_upstream_returns_none(
    db_session, seeded_user, seeded_project
):
    # Workspace exists but codex isn't reachable / hasn't observed
    # any requests yet — upstream returns None.
    workspace = Workspace(
        id=uuid4(),
        project_id=seeded_project.id,
        repo_path="/tmp/x",
        status="ready",
    )
    db_session.add(workspace)
    await db_session.commit()

    client = _client(db_session, seeded_user, quota_result=None)
    try:
        r = client.get(f"/projects/{seeded_project.id}/codex-quota")
        assert r.status_code == 200
        assert r.json() == {"available": False}
    finally:
        client._restore()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_returns_quota_payload_when_upstream_succeeds(
    db_session, seeded_user, seeded_project
):
    workspace = Workspace(
        id=uuid4(),
        project_id=seeded_project.id,
        repo_path="/tmp/x",
        status="ready",
    )
    db_session.add(workspace)
    await db_session.commit()

    quota = {
        "primary": {
            "used_percent": 14.0,
            "window_minutes": 300,
            "resets_at": 1_770_000_000,
        },
        "secondary": None,
        "plan_type": "plus",
    }
    client = _client(db_session, seeded_user, quota_result=quota)
    try:
        r = client.get(f"/projects/{seeded_project.id}/codex-quota")
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is True
        assert body["primary"]["used_percent"] == 14.0
        assert body["primary"]["window_minutes"] == 300
        assert body["secondary"] is None
        assert body["plan_type"] == "plus"
    finally:
        client._restore()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_picks_latest_workspace_when_project_has_multiple(
    db_session, seeded_user, seeded_project
):
    # Two workspaces on one project — the route picks the most-recently
    # created one.  Without this, a project that was reset would keep
    # querying the dead old workspace's container.
    older = Workspace(
        id=uuid4(),
        project_id=seeded_project.id,
        repo_path="/tmp/old",
        status="ready",
        created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    newer = Workspace(
        id=uuid4(),
        project_id=seeded_project.id,
        repo_path="/tmp/new",
        status="ready",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    db_session.add_all([older, newer])
    await db_session.commit()

    seen: list[UUID] = []

    async def _spy_get_codex_quota(*, workspace_id, redis):
        seen.append(workspace_id)
        return None

    import polaris_api.routes.codex_quota as route_mod

    client = _client(db_session, seeded_user, quota_result=None)
    route_mod.get_codex_quota = _spy_get_codex_quota  # type: ignore[assignment]

    try:
        r = client.get(f"/projects/{seeded_project.id}/codex-quota")
        assert r.status_code == 200
        assert seen == [newer.id], seen
    finally:
        client._restore()  # type: ignore[attr-defined]

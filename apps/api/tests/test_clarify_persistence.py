"""Tests for ``POST /projects/{id}/clarify/response`` persistence.

Before today the route only published answers to Redis — the
``Clarification`` row stayed at status='pending' / answers={}, so chat
replay on reload showed nothing.  These tests pin the round-trip:
submit answers → the row gets ``status='answered'``, the answers JSON,
and an ``answered_at`` timestamp.

Strategy: real in-memory async sqlite with JSONB columns hot-swapped
to JSON (sqlite can't render the postgres dialect's JSONB), real
``Clarification`` ORM, fake redis recorder, ``_resolve_project_access``
stubbed so we don't have to mint cookies.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import JSON, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import polaris_api.routes.clarify as clarify_mod
from polaris_api.db import get_session
from polaris_api.models import (
    AgentRun,
    Clarification,
    Project,
    Session as SessionRow,
    User,
    Workspace,
)
from polaris_api.redis_client import get_redis
from polaris_api.routes.clarify import router as clarify_router


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture(scope="module", autouse=True)
def _swap_jsonb_for_sqlite():
    """Hot-swap every JSONB column in the models we exercise to plain
    JSON, so sqlite's DDL rendering doesn't choke.  Restore afterwards
    so other test files that import these models keep their postgres
    types.
    """
    swapped: list[tuple] = []
    for table in (
        SessionRow.__table__,
        AgentRun.__table__,
        Clarification.__table__,
    ):
        for col in table.columns:
            if isinstance(col.type, JSONB):
                swapped.append((col, col.type))
                col.type = JSON()
    yield
    for col, original in swapped:
        col.type = original


@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for model in (User, Project, Workspace, SessionRow, AgentRun, Clarification):
            await conn.run_sync(model.__table__.create)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as session:
        yield session


@pytest_asyncio.fixture
async def seeded(db_session):
    """Seed a complete clarification scenario: user → project →
    workspace → session → run → pending clarification."""
    user = User(id=uuid4(), email="t@example.com", name="T", avatar_url=None)
    project = Project(
        id=uuid4(),
        user_id=user.id,
        name="P",
        slug="p",
        description=None,
        stack_template="default-stack",
        status="active",
    )
    workspace = Workspace(
        id=uuid4(),
        project_id=project.id,
        repo_path="/tmp/x",
        status="ready",
    )
    session_row = SessionRow(
        id=uuid4(),
        project_id=project.id,
        workspace_id=workspace.id,
        sequence=1,
        user_message="hi",
        mode="discover_then_build",
        status="running",
    )
    run = AgentRun(
        id=uuid4(),
        session_id=session_row.id,
        sequence=1,
        agent_kind="discovery",
        status="running",
    )
    clarification = Clarification(
        id=uuid4(),
        request_id="req-001",
        project_id=project.id,
        session_id=session_row.id,
        run_id=run.id,
        agent_kind="discovery",
        status="pending",
        questions_jsonb=[
            {
                "id": "q1",
                "title": "Pick a vibe",
                "choices": [
                    {"id": "calm", "label": "Calm"},
                    {"id": "bold", "label": "Bold"},
                ],
            }
        ],
        answers_jsonb={},
    )
    db_session.add_all([user, project, workspace, session_row, run, clarification])
    await db_session.commit()
    return {
        "user": user,
        "project": project,
        "workspace": workspace,
        "session": session_row,
        "run": run,
        "clarification": clarification,
    }


def _client(db_session, seeded, *, redis_recorder):
    """Build a tiny FastAPI app with clarify_router mounted, deps
    overridden to the in-memory DB + a fake redis.

    The route imports ``get_redis`` and calls it directly (not via
    FastAPI's Depends), so the dependency_overrides hook can't reach
    it — we monkey-patch the function in the route module instead.
    """
    app = FastAPI()
    app.include_router(clarify_router)

    async def _fake_get_session():
        yield db_session

    class _FakeRedis:
        async def publish(self, channel, payload):
            redis_recorder.append({"channel": channel, "payload": payload})

        async def aclose(self):
            return None

    async def _fake_resolve(request, project_id, db, settings, workspace_token):
        # Bypass auth entirely — we trust the test harness.  Returns
        # the seeded Project so the route sees a normal happy-path.
        return seeded["project"]

    app.dependency_overrides[get_session] = _fake_get_session

    original_resolve = clarify_mod._resolve_project_access
    original_redis_factory = clarify_mod.get_redis
    clarify_mod._resolve_project_access = _fake_resolve  # type: ignore[assignment]
    clarify_mod.get_redis = lambda: _FakeRedis()  # type: ignore[assignment]

    def _restore() -> None:
        clarify_mod._resolve_project_access = original_resolve  # type: ignore[assignment]
        clarify_mod.get_redis = original_redis_factory  # type: ignore[assignment]

    client = TestClient(app)
    client._restore = _restore  # type: ignore[attr-defined]
    return client


# ── Tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_persists_answers_and_flips_status(db_session, seeded):
    redis_calls: list[dict] = []
    client = _client(db_session, seeded, redis_recorder=redis_calls)
    try:
        before = datetime.now(timezone.utc)
        r = client.post(
            f"/projects/{seeded['project'].id}/clarify/response",
            json={
                "request_id": "req-001",
                "answers": {
                    "q1": {"selected_choice": "calm", "override_text": None}
                },
                "session_id": str(seeded["session"].id),
                "run_id": str(seeded["run"].id),
            },
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True}

        # Reload the row from the DB — must reflect the persisted state.
        await db_session.refresh(seeded["clarification"])
        row = seeded["clarification"]
        assert row.status == "answered"
        assert row.answers_jsonb == {
            "q1": {"selected_choice": "calm", "override_text": None}
        }
        assert row.answered_at is not None
        # answered_at must be set to "now-ish" — within a couple seconds
        # of when we issued the call.  Sqlite datetimes round-trip
        # naive UTC; normalize for comparison.
        a = row.answered_at
        if a.tzinfo is None:
            a = a.replace(tzinfo=timezone.utc)
        delta = abs((a - before).total_seconds())
        assert delta < 5, f"answered_at drifted by {delta}s"
    finally:
        client._restore()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_submit_publishes_clarification_channel_and_ack(db_session, seeded):
    redis_calls: list[dict] = []
    client = _client(db_session, seeded, redis_recorder=redis_calls)
    try:
        r = client.post(
            f"/projects/{seeded['project'].id}/clarify/response",
            json={
                "request_id": "req-001",
                "answers": {
                    "q1": {"selected_choice": "bold", "override_text": None}
                },
                "session_id": str(seeded["session"].id),
                "run_id": str(seeded["run"].id),
            },
        )
        assert r.status_code == 200

        # Two publishes: one to the worker's per-session clarification
        # channel (for the agent to resume) and one to the per-session
        # events channel (the SSE ack the frontend hears to dismiss
        # the modal).
        channels = [c["channel"] for c in redis_calls]
        assert any("clarification" in ch for ch in channels), channels
        assert any("events" in ch for ch in channels), channels
    finally:
        client._restore()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_submit_with_override_text_persists_free_text(db_session, seeded):
    redis_calls: list[dict] = []
    client = _client(db_session, seeded, redis_recorder=redis_calls)
    try:
        r = client.post(
            f"/projects/{seeded['project'].id}/clarify/response",
            json={
                "request_id": "req-001",
                "answers": {
                    "q1": {
                        "selected_choice": None,
                        "override_text": "something custom",
                    }
                },
                "session_id": str(seeded["session"].id),
                "run_id": str(seeded["run"].id),
            },
        )
        assert r.status_code == 200
        await db_session.refresh(seeded["clarification"])
        # Free-text overrides are persisted verbatim — clarification
        # replay must surface them, not strip to selected_choice only.
        assert seeded["clarification"].answers_jsonb["q1"]["override_text"] == (
            "something custom"
        )
        assert seeded["clarification"].answers_jsonb["q1"]["selected_choice"] is None
    finally:
        client._restore()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_submit_with_unknown_request_id_is_idempotent(db_session, seeded):
    # When the row has already been answered (or never existed), the
    # route should still publish to Redis so the worker resumes — it
    # just skips the DB write.  Belt-and-suspenders for retries.
    redis_calls: list[dict] = []
    client = _client(db_session, seeded, redis_recorder=redis_calls)
    try:
        r = client.post(
            f"/projects/{seeded['project'].id}/clarify/response",
            json={
                "request_id": "req-DOES-NOT-EXIST",
                "answers": {"q1": {"selected_choice": "calm", "override_text": None}},
                "session_id": str(seeded["session"].id),
                "run_id": str(seeded["run"].id),
            },
        )
        assert r.status_code == 200, r.text
        # Row stays untouched.
        await db_session.refresh(seeded["clarification"])
        assert seeded["clarification"].status == "pending"
        assert seeded["clarification"].answers_jsonb == {}
        # Redis still got the publishes (worker-side resume can't be
        # blocked by a missing audit row).
        assert len(redis_calls) == 2
    finally:
        client._restore()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_submit_404_when_session_does_not_exist(db_session, seeded):
    redis_calls: list[dict] = []
    client = _client(db_session, seeded, redis_recorder=redis_calls)
    try:
        r = client.post(
            f"/projects/{seeded['project'].id}/clarify/response",
            json={
                "request_id": "req-001",
                "answers": {"q1": {"selected_choice": "calm", "override_text": None}},
                "session_id": str(uuid4()),  # not in DB
                "run_id": str(seeded["run"].id),
            },
        )
        assert r.status_code == 404
        # Nothing got published — we bailed before the redis stage.
        assert redis_calls == []
        # Row is unchanged.
        await db_session.refresh(seeded["clarification"])
        assert seeded["clarification"].status == "pending"
    finally:
        client._restore()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_submit_404_when_run_belongs_to_different_session(
    db_session, seeded
):
    # Construct a second session+run on the same project; submit with
    # the first session_id but the second run_id — the route must
    # reject (otherwise answers route to the wrong agent).
    other_session = SessionRow(
        id=uuid4(),
        project_id=seeded["project"].id,
        workspace_id=seeded["workspace"].id,
        sequence=2,
        user_message="hi2",
        mode="discover_then_build",
        status="running",
    )
    other_run = AgentRun(
        id=uuid4(),
        session_id=other_session.id,
        sequence=1,
        agent_kind="discovery",
        status="running",
    )
    db_session.add_all([other_session, other_run])
    await db_session.commit()

    redis_calls: list[dict] = []
    client = _client(db_session, seeded, redis_recorder=redis_calls)
    try:
        r = client.post(
            f"/projects/{seeded['project'].id}/clarify/response",
            json={
                "request_id": "req-001",
                "answers": {"q1": {"selected_choice": "calm", "override_text": None}},
                "session_id": str(seeded["session"].id),
                "run_id": str(other_run.id),  # belongs to other_session
            },
        )
        assert r.status_code == 404, r.text
        assert redis_calls == []
    finally:
        client._restore()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_submit_handles_partial_answer_dict(db_session, seeded):
    # Some clarifications have multiple questions; if the user only
    # answered one, the row's answers_jsonb still gets exactly what
    # they sent (no fabrication of None entries).
    redis_calls: list[dict] = []
    client = _client(db_session, seeded, redis_recorder=redis_calls)
    try:
        r = client.post(
            f"/projects/{seeded['project'].id}/clarify/response",
            json={
                "request_id": "req-001",
                "answers": {},  # empty
                "session_id": str(seeded["session"].id),
                "run_id": str(seeded["run"].id),
            },
        )
        assert r.status_code == 200
        await db_session.refresh(seeded["clarification"])
        assert seeded["clarification"].status == "answered"
        # Empty dict is a valid persisted state — replay shows zero
        # answer rows; modal-side logic can decide if that's acceptable.
        assert seeded["clarification"].answers_jsonb == {}
    finally:
        client._restore()  # type: ignore[attr-defined]

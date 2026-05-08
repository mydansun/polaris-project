"""Tests for the SSE clarification snapshot-on-connect.

Bug fix: Redis pubsub is fire-and-forget — a clarification_requested
event published between session-create and EventSource-connect was
lost.  Real-mode hides it (LLM latency widens the gap to seconds);
replay mode made it instant and reproducible.

Fix: ``_build_clarification_snapshot_event`` queries the DB at
connect time and the route emits the result as a synthetic SSE event.
We test the helper directly here — exercising the full SSE generator
hangs in TestClient (the keepalive timeout is hardcoded to 15 s and
TestClient never disconnects mid-stream).  The integration is covered
by the live smoke test instead.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from polaris_api.models import (
    AgentRun,
    Clarification,
    Project,
    Session as SessionRow,
    User,
    Workspace,
)
from polaris_api.routes.sessions import _build_clarification_snapshot_event


@pytest.fixture(scope="module", autouse=True)
def _swap_jsonb_for_sqlite():
    """Hot-swap JSONB → JSON so sqlite can render the schema.  Same
    pattern as test_clarify_persistence.py — see there for rationale."""
    swapped: list[tuple] = []
    for table in (SessionRow.__table__, AgentRun.__table__, Clarification.__table__):
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
    user = User(id=uuid4(), email="t@example.com", name="T", avatar_url=None)
    project = Project(
        id=uuid4(), user_id=user.id, name="P", slug="p", description=None,
        stack_template="default-stack", status="active",
    )
    workspace = Workspace(
        id=uuid4(), project_id=project.id, repo_path="/tmp/x", status="ready"
    )
    session = SessionRow(
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
        session_id=session.id,
        sequence=1,
        agent_kind="discovery",
        status="running",
    )
    db_session.add_all([user, project, workspace, session, run])
    await db_session.commit()
    return {"user": user, "project": project, "session": session, "run": run}


# ── Pending → snapshot event ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_returns_synthetic_event_when_pending_clarification_exists(
    db_session, seeded
):
    clar = Clarification(
        id=uuid4(),
        request_id="req-snap-1",
        project_id=seeded["project"].id,
        session_id=seeded["session"].id,
        run_id=seeded["run"].id,
        agent_kind="discovery",
        status="pending",
        questions_jsonb=[{"id": "q1", "title": "Pick a vibe"}],
        answers_jsonb={},
    )
    db_session.add(clar)
    await db_session.commit()

    out = await _build_clarification_snapshot_event(db_session, seeded["session"].id)
    assert out is not None
    payload = json.loads(out)
    assert payload["kind"] == "clarification_requested"
    assert payload["session_id"] == str(seeded["session"].id)
    assert payload["run_id"] == str(seeded["run"].id)
    assert payload["request"]["request_id"] == "req-snap-1"
    assert payload["request"]["source"] == "discovery"
    assert payload["request"]["questions"] == [{"id": "q1", "title": "Pick a vibe"}]


@pytest.mark.asyncio
async def test_returns_none_when_no_clarifications_exist(db_session, seeded):
    out = await _build_clarification_snapshot_event(db_session, seeded["session"].id)
    assert out is None


@pytest.mark.asyncio
async def test_returns_none_when_clarifications_all_answered(db_session, seeded):
    """An answered clarification must NOT trigger the snapshot —
    otherwise reconnecting clients would re-render the modal for
    a question the user already answered."""
    clar = Clarification(
        id=uuid4(),
        request_id="req-already-answered",
        project_id=seeded["project"].id,
        session_id=seeded["session"].id,
        run_id=seeded["run"].id,
        agent_kind="discovery",
        status="answered",
        questions_jsonb=[{"id": "q1", "title": "x"}],
        answers_jsonb={"q1": {"selected_choice": "calm"}},
        answered_at=datetime.now(timezone.utc),
    )
    db_session.add(clar)
    await db_session.commit()

    out = await _build_clarification_snapshot_event(db_session, seeded["session"].id)
    assert out is None


@pytest.mark.asyncio
async def test_returns_only_most_recent_pending(db_session, seeded):
    """If two pending clarifications somehow coexist (rare race),
    snapshot the most-recent one — older one is presumably abandoned."""
    older = Clarification(
        id=uuid4(),
        request_id="req-older",
        project_id=seeded["project"].id,
        session_id=seeded["session"].id,
        run_id=seeded["run"].id,
        agent_kind="discovery",
        status="pending",
        questions_jsonb=[{"id": "q-old"}],
        answers_jsonb={},
        created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    newer = Clarification(
        id=uuid4(),
        request_id="req-newer",
        project_id=seeded["project"].id,
        session_id=seeded["session"].id,
        run_id=seeded["run"].id,
        agent_kind="discovery",
        status="pending",
        questions_jsonb=[{"id": "q-new"}],
        answers_jsonb={},
        created_at=datetime(2026, 5, 8, tzinfo=timezone.utc),
    )
    db_session.add_all([older, newer])
    await db_session.commit()

    out = await _build_clarification_snapshot_event(db_session, seeded["session"].id)
    assert out is not None
    payload = json.loads(out)
    assert payload["request"]["request_id"] == "req-newer"


@pytest.mark.asyncio
async def test_does_not_leak_pending_from_other_session(db_session, seeded):
    """A pending clarification on a DIFFERENT session must not be
    returned — the snapshot is per-session."""
    other_session = SessionRow(
        id=uuid4(),
        project_id=seeded["project"].id,
        workspace_id=seeded["session"].workspace_id,
        sequence=2,
        user_message="other",
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
    other_clar = Clarification(
        id=uuid4(),
        request_id="req-other-session",
        project_id=seeded["project"].id,
        session_id=other_session.id,
        run_id=other_run.id,
        agent_kind="discovery",
        status="pending",
        questions_jsonb=[],
        answers_jsonb={},
    )
    db_session.add_all([other_session, other_run, other_clar])
    await db_session.commit()

    # Snapshot for our session — must not include other_clar.
    out = await _build_clarification_snapshot_event(db_session, seeded["session"].id)
    assert out is None

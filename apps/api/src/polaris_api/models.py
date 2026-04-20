from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from polaris_api.db import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email"),
    )

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), index=True)
    name: Mapped[str] = mapped_column(String(200))
    avatar_url: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    projects: Mapped[list["Project"]] = relationship(back_populates="user")


class VerificationCode(Base):
    __tablename__ = "verification_codes"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), index=True)
    code: Mapped[str] = mapped_column(String(6))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("user_id", "slug", name="uq_projects_user_slug"),)

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(220))
    description: Mapped[str | None] = mapped_column(Text)
    stack_template: Mapped[str] = mapped_column(String(100), default="default-stack")
    status: Mapped[str] = mapped_column(String(50), default="active")
    # Single Codex app-server thread per project — NULL until the first turn is
    # started. `make clear` resets it back to NULL when wiping codex-home.
    codex_thread_id: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    user: Mapped[User] = relationship(back_populates="projects")
    workspaces: Mapped[list["Workspace"]] = relationship(back_populates="project")
    sessions: Mapped[list["Session"]] = relationship(back_populates="project")
    versions: Mapped[list["ProjectVersion"]] = relationship(back_populates="project")
    browser_sessions: Mapped[list["BrowserSession"]] = relationship(back_populates="project")


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    repo_path: Mapped[str] = mapped_column(String(1000))
    current_branch: Mapped[str] = mapped_column(String(200), default="main")
    current_commit: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(50), default="provisioning")
    compose_profile: Mapped[str] = mapped_column(String(100), default="app-postgres-redis")
    current_browser_session_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    ide_url: Mapped[str | None] = mapped_column(String(1000))
    ide_status: Mapped[str] = mapped_column(String(50), default="not_configured")
    # The directory the IDE should open for this project.  NULL until the
    # Codex agent calls `set_project_root` on the first scaffolding turn
    # (e.g. "/workspace" or "/workspace/my-app"); frontend shows a skeleton
    # while NULL + turn-in-flight, and falls back to "/workspace" otherwise.
    project_root: Mapped[str | None] = mapped_column(String(1000))
    # Shared secret injected as env into the workspace container so the
    # in-container `polaris` CLI can authenticate back to the platform API
    # (publish / rollback / status).  Scoped per workspace; rotates only
    # when the workspace is recreated.
    workspace_token: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    project: Mapped[Project] = relationship(back_populates="workspaces")
    sessions: Mapped[list["Session"]] = relationship(back_populates="workspace")
    browser_sessions: Mapped[list["BrowserSession"]] = relationship(back_populates="workspace")
    dep_services: Mapped[list["WorkspaceDepService"]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )


class Session(Base):
    """One user message → one Polaris Session.

    A Session aggregates every agent run triggered by a single user
    message.  For ``mode='discover_then_build'`` it contains a discovery
    AgentRun followed by a codex AgentRun; for the other modes it
    contains exactly one codex AgentRun.  The column set mirrors the old
    ``turns`` table at the user-facing level; Codex-specific ids
    (codex_turn_id) now live on AgentRun.external_id.
    """

    __tablename__ = "sessions"
    __table_args__ = (
        UniqueConstraint("project_id", "sequence", name="uq_sessions_project_sequence"),
    )

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    user_message: Mapped[str] = mapped_column(Text)
    # build_planned | build_direct | discover_then_build
    mode: Mapped[str] = mapped_column(String(30))
    # queued | running | completed | interrupted | failed
    status: Mapped[str] = mapped_column(String(20), default="queued")
    final_message: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    metadata_jsonb: Mapped[dict] = mapped_column(JSONB, default=dict)
    cost_jsonb: Mapped[dict] = mapped_column(JSONB, default=dict)
    # StatusBar counters (see migration 0019).  Updated with 500ms-coalesced
    # writes by the worker's DbEventSink; also hydrated on GET /sessions.
    file_change_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    playwright_call_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    project: Mapped[Project] = relationship(back_populates="sessions")
    workspace: Mapped[Workspace] = relationship(back_populates="sessions")
    runs: Mapped[list["AgentRun"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="AgentRun.sequence",
    )


class AgentRun(Base):
    """One agent's execution within a Session.

    ``agent_kind`` names the adapter (codex, discovery, ...).  Each agent's
    adapter decides what to put in ``input_jsonb`` / ``output_jsonb``;
    ``external_id`` is reserved for ids owned by the agent's backend
    (``codex_turn_id`` for codex runs, LangGraph thread id for discovery).
    """

    __tablename__ = "agent_runs"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_agent_runs_session_sequence"),
    )

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    # codex | discovery
    agent_kind: Mapped[str] = mapped_column(String(30))
    # queued | running | completed | failed | skipped
    status: Mapped[str] = mapped_column(String(20), default="queued")
    external_id: Mapped[str | None] = mapped_column(String(100))
    input_jsonb: Mapped[dict] = mapped_column(JSONB, default=dict)
    output_jsonb: Mapped[dict] = mapped_column(JSONB, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    session: Mapped[Session] = relationship(back_populates="runs")
    events: Mapped[list["Event"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="Event.sequence",
    )


class Event(Base):
    """An atomic progress item inside an AgentRun.

    Replaces the old ``TurnItem``.  ``kind`` is an agent-prefixed string
    like ``codex:agent_message`` / ``discovery:clarifying`` so the two
    namespaces never collide.  ``external_id`` holds Codex's item id for
    codex runs; discovery emits synthetic ids.
    """

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_events_run_sequence"),
    )

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    external_id: Mapped[str | None] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(80))
    # started | completed | failed
    status: Mapped[str] = mapped_column(String(20), default="started")
    payload_jsonb: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    run: Mapped[AgentRun] = relationship(back_populates="events")


class Clarification(Base):
    """A structured clarification prompt raised by an agent during a run.

    Lives in its own table (not ``events``) so the in-container CLI and the
    worker's event sink don't race on the per-run sequence unique index.
    Bound to both a session and the specific run that asked — this makes
    the ``/clarify/response`` route's channel lookup explicit rather than
    guessing via status='running'.
    """

    __tablename__ = "clarifications"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_clarifications_request_id"),
    )

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    request_id: Mapped[str] = mapped_column(String(100))
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE")
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True
    )
    # codex | discovery — redundant with agent_runs.agent_kind but avoids a
    # join for the frontend "source" label and SSE payload.
    agent_kind: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    questions_jsonb: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    answers_jsonb: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DesignIntent(Base):
    """The discovery agent's output for a project.

    Written by the worker after a ``mode='discover_then_build'`` session
    finishes its discovery AgentRun (see ``polaris_design_intent``).  The
    current row per project is ``status='active'``; re-discover flips the
    prior row to ``'superseded'`` and inserts a new active row atomically.
    """

    __tablename__ = "design_intents"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    intent_jsonb: Mapped[dict] = mapped_column(JSONB)
    compiled_brief: Mapped[str] = mapped_column(Text)
    pinterest_refs_jsonb: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    pinterest_queries_jsonb: Mapped[list[str]] = mapped_column(JSONB, default=list)
    # Public S3 URL of the generated mood board PNG.  Null when mood-board
    # generation failed or was skipped — Codex falls back to text-only input
    # and the frontend hides the mood-board card.
    mood_board_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ProjectVersion(Base):
    __tablename__ = "project_versions"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    git_commit_hash: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    created_by_type: Mapped[str] = mapped_column(String(50), default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    project: Mapped[Project] = relationship(back_populates="versions")


class WorkspaceDepService(Base):
    """A dev-time dependency container (postgres / redis / …) attached to a
    workspace via its per-project docker network.

    This is NOT part of the workspace compose file — it's an independent
    docker container started/stopped by `services/dev_deps.py` and tracked
    here for lifecycle management.  The workspace reaches it by DNS name
    (the `service` column doubles as the network-alias, so Prisma / Next
    / python code just uses `postgres:5432`).
    """

    __tablename__ = "workspace_dep_services"
    __table_args__ = (
        UniqueConstraint("workspace_id", "service", name="uq_wds_workspace_service"),
    )

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    service: Mapped[str] = mapped_column(String(50))
    container_name: Mapped[str] = mapped_column(String(100))
    volume_name: Mapped[str] = mapped_column(String(100))
    image: Mapped[str] = mapped_column(String(200))
    network: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30), default="starting")
    env_jsonb: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    workspace: Mapped[Workspace] = relationship(back_populates="dep_services")


class Deployment(Base):
    """A single publish attempt for a project.

    Each row is immutable once created except for `status`, `error`,
    build/smoke logs, and `ready_at`.  Rollback creates a NEW row; the
    superseded one is marked `rolled_back` via `superseded_by_id`.
    """

    __tablename__ = "deployments"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    project_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("project_versions.id"), nullable=True
    )
    git_commit_hash: Mapped[str | None] = mapped_column(String(100))
    image_tag: Mapped[str | None] = mapped_column(String(300))
    domain: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(30), default="queued")
    build_log: Mapped[str | None] = mapped_column(Text)
    smoke_log: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    # Chain rows: when a rollback creates a new deployment that replaces
    # an older one, the older row's `superseded_by_id` points forward.
    superseded_by_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("deployments.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BrowserSession(Base):
    __tablename__ = "browser_sessions"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    status: Mapped[str] = mapped_column(String(50), default="starting")
    vnc_url: Mapped[str | None] = mapped_column(String(1000))
    context_metadata_jsonb: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    project: Mapped[Project] = relationship(back_populates="browser_sessions")
    workspace: Mapped[Workspace] = relationship(back_populates="browser_sessions")


class UnsplashImage(Base):
    """One re-hosted Unsplash image size.

    We cache two sizes per photo (``regular`` / ``small``) under the S3
    ``static/images/up/*`` prefix so the workspace-side Unsplash MCP can return
    stable S3 URLs instead of fragile Unsplash CDN links.  The unique
    constraint on ``(photo_id, size)`` makes the dedupe lookup cheap.
    """

    __tablename__ = "unsplash_images"
    __table_args__ = (
        UniqueConstraint("photo_id", "size", name="uq_unsplash_photo_size"),
    )

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    photo_id: Mapped[str] = mapped_column(Text, index=True)
    size: Mapped[str] = mapped_column(String(20))
    s3_key: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(Text)
    bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

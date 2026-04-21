from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    service: str
    version: str
    status: str


class ReadyResponse(BaseModel):
    service: str
    database: str
    redis: str


class UserResponse(BaseModel):
    id: UUID
    email: str
    name: str
    avatar_url: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class RequestCodeBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    # When provided, invite_code must be exactly 6 digits.
    invite_code: str | None = Field(
        default=None,
        min_length=6,
        max_length=6,
        pattern=r"^[0-9]{6}$",
    )


class VerifyCodeBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    # Email verification code — 6 digits, matching generate_code() output.
    code: str = Field(
        min_length=6,
        max_length=6,
        pattern=r"^[0-9]{6}$",
    )


class ClarificationAnswerBody(BaseModel):
    selected_choice: str | None = None
    override_text: str | None = None


class ClarificationResponseBody(BaseModel):
    request_id: str
    answers: dict[str, ClarificationAnswerBody]


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    # Empty by default: workspace init leaves /workspace empty so the agent
    # can scaffold into `.`. Kept as a field for future template selection.
    stack_template: str = ""


class WorkspaceResponse(BaseModel):
    id: UUID
    project_id: UUID
    repo_path: str
    current_branch: str
    current_commit: str | None
    status: str
    compose_profile: str
    current_browser_session_id: UUID | None
    ide_url: str | None
    ide_status: str
    project_root: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class WorkspaceIdeSessionResponse(BaseModel):
    workspace_id: UUID
    project_id: UUID
    ide_url: str | None
    ide_status: str


class WorkspaceRuntimeRequest(BaseModel):
    services: list[Literal["postgres", "redis"]] = Field(default_factory=list)


class WorkspaceRuntimeResponse(BaseModel):
    workspace_id: UUID
    project_id: UUID
    status: str
    enabled_services: list[Literal["postgres", "redis"]]
    ide_url: str | None
    browser_url: str | None
    project_root: str | None
    health: dict[str, str] = Field(default_factory=dict)


class BrowserSessionResponse(BaseModel):
    id: UUID
    project_id: UUID
    workspace_id: UUID
    status: str
    vnc_url: str | None
    context_metadata_jsonb: dict
    created_at: datetime
    expires_at: datetime | None

    model_config = {"from_attributes": True}


class WorkspaceFileEntry(BaseModel):
    path: str
    kind: str
    size: int | None


class WorkspaceFileContent(BaseModel):
    path: str
    content: str
    revision: str


class WorkspaceFileWrite(BaseModel):
    path: str = Field(min_length=1)
    content: str
    base_revision: str | None = None


class SnapshotCreate(BaseModel):
    title: str = Field(default="Workspace snapshot", min_length=1, max_length=300)
    description: str | None = None
    created_by_type: str = "user"


class DeploymentSummary(BaseModel):
    """Slim deployment shape served as part of ``ProjectResponse``.

    Only the fields a HomePage card actually shows — status badge,
    public URL link, "live since" / "last attempt" timestamps,
    screenshot.  Token-leak surfaces (build_log / smoke_log / error)
    are deliberately excluded; full Deployment view is a separate
    endpoint."""

    id: UUID
    status: "DeploymentStatus"  # forward-ref; resolved below
    domain: str | None
    created_at: datetime
    ready_at: datetime | None
    screenshot_url: str | None = None

    model_config = {"from_attributes": True}


class ProjectResponse(BaseModel):
    id: UUID
    user_id: UUID
    name: str
    slug: str
    description: str | None
    stack_template: str
    status: str
    codex_thread_id: str | None
    created_at: datetime
    updated_at: datetime
    # Latest deployment by created_at (any status, not just ``ready``).
    # ``None`` when the project has never been published.  Populated by
    # the list endpoint via a single batched query (no N+1).
    latest_deployment: DeploymentSummary | None = None
    # Status of the most recent agent session (``queued`` / ``running`` /
    # ``completed`` / ``failed`` / ``interrupted``).  Lets the project
    # drawer surface a "session failed before publish" red dot — without
    # this, projects that crashed in clarification or codex would render
    # as gray drafts despite clearly being broken.
    latest_session_status: str | None = None
    # True when the project has at least one ``design_intents`` row with
    # ``status='active'``.  The frontend reads this to decide whether
    # the next user message should re-trigger discovery (interrupted /
    # never-completed prior runs leave this false; the next message
    # then routes through ``discover_then_build`` again with all prior
    # user messages prepended, so a user who interrupted clarification
    # mid-flow can resume by sending more text).
    has_active_design_intent: bool = False
    # Mood board PNG URL from the active design_intent, surfaced here so
    # cards can render a thumbnail without a per-project intent fetch.
    mood_board_url: str | None = None

    model_config = {"from_attributes": True}


class ProjectDetailResponse(ProjectResponse):
    workspace: WorkspaceResponse | None


# ── Turn / TurnItem: 1 turn per user message; items aligned to Codex item kinds ──


# ── Session / AgentRun / Event (replaces Turn/TurnItem) ───────────────────

AgentKind = Literal["codex", "discovery"]

SessionStatus = Literal["queued", "running", "completed", "interrupted", "failed"]

RunStatus = Literal["queued", "running", "completed", "failed", "skipped"]

EventStatus = Literal["started", "completed", "failed"]

SessionMode = Literal["build_planned", "build_direct", "discover_then_build"]
"""Which agents run for a Session.
- build_planned:         [codex]          — Codex plans then executes
- build_direct:          [codex]          — Codex executes directly
- discover_then_build:   [discovery, codex] — discovery first, brief drives codex
"""

EventKind = Literal[
    # Codex-emitted events (prefix: codex:*)
    "codex:agent_message",
    "codex:plan",
    "codex:reasoning",
    "codex:command_execution",
    "codex:file_change",
    "codex:mcp_tool_call",
    "codex:dynamic_tool_call",
    "codex:web_search",
    "codex:error",
    "codex:other",
    # Discovery-emitted events (prefix: discovery:*)
    "discovery:clarifying",
    "discovery:references",
    "discovery:compiled",
    "discovery:moodboard",
]


class SessionCreate(BaseModel):
    message: str = Field(min_length=1)
    mode: SessionMode | None = Field(default=None)  # defaults to build_planned


class EventResponse(BaseModel):
    id: UUID
    run_id: UUID
    sequence: int
    external_id: str | None
    kind: EventKind
    status: EventStatus
    payload_jsonb: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AgentRunResponse(BaseModel):
    id: UUID
    session_id: UUID
    sequence: int
    agent_kind: AgentKind
    status: RunStatus
    external_id: str | None
    started_at: datetime | None
    finished_at: datetime | None
    events: list[EventResponse] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class SessionResponse(BaseModel):
    id: UUID
    project_id: UUID
    workspace_id: UUID
    sequence: int
    user_message: str
    mode: SessionMode
    status: SessionStatus
    final_message: str | None
    error_message: str | None
    cost_jsonb: dict
    metadata_jsonb: dict
    file_change_count: int = 0
    playwright_call_count: int = 0
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ClarificationRecord(BaseModel):
    """One round of clarification, with both questions and the user's answers.

    Returned as part of ``SessionDetailResponse`` so the chat replay can
    re-render the question card + the user's selections after a reload —
    otherwise these rounds vanish from the timeline once the modal closes.
    Only ``status='answered'`` rows are surfaced; pending ones flow through
    ``GET /clarify/pending`` instead.
    """

    id: UUID
    request_id: str
    session_id: UUID
    run_id: UUID
    agent_kind: AgentKind
    status: str
    questions: list[dict[str, Any]] = Field(default_factory=list)
    answers: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    answered_at: datetime | None


class SessionDetailResponse(SessionResponse):
    runs: list[AgentRunResponse] = Field(default_factory=list)
    clarifications: list[ClarificationRecord] = Field(default_factory=list)


class SessionSteerRequest(BaseModel):
    message: str = Field(min_length=1)


class ProjectVersionResponse(BaseModel):
    id: UUID
    project_id: UUID
    git_commit_hash: str
    title: str
    description: str | None
    created_by_type: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Deployment ───────────────────────────────────────────────────────────

DeploymentStatus = Literal[
    "queued", "building", "deploying", "ready", "failed", "rolled_back"
]


class PolarisManifestPublish(BaseModel):
    service: str = Field(min_length=1)
    port: int = Field(gt=0, lt=65536)


# ── Prepublish LLM audit ────────────────────────────────────────────────


class AuditRequest(BaseModel):
    """Input to POST /projects/{id}/prepublish-audit.  The workspace CLI
    reads these three files (whatever exists) and uploads their contents
    for the platform-side LLM to review."""

    polaris_yaml: str = ""
    dockerfile: str = ""
    package_json_scripts: dict[str, str] = Field(default_factory=dict)


class AuditIssue(BaseModel):
    severity: Literal["error", "warning"]
    hint: str
    fix: str = ""


class AuditResponse(BaseModel):
    issues: list[AuditIssue] = Field(default_factory=list)


class PolarisManifest(BaseModel):
    """Shape of the user-authored polaris.yaml committed to their repo."""

    version: Literal[1] = 1
    stack: Literal["spa", "node", "python", "static", "custom"]
    build: str = ""
    start: str = ""
    port: int = Field(gt=0, lt=65536)
    deps: list[Literal["postgres", "redis"]] = Field(default_factory=list)
    secrets: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    publish: PolarisManifestPublish


class DeploymentResponse(BaseModel):
    id: UUID
    project_id: UUID
    project_version_id: UUID | None
    git_commit_hash: str | None
    image_tag: str | None
    domain: str | None
    status: DeploymentStatus
    error: str | None
    created_at: datetime
    ready_at: datetime | None
    screenshot_url: str | None = None

    model_config = {"from_attributes": True}


class DeploymentDetailResponse(DeploymentResponse):
    build_log: str | None
    smoke_log: str | None


class DeploymentTriggerRequest(BaseModel):
    dry_run: bool = False


# ─── Dev dep slots ───────────────────────────────────────────────────────


class WorkspaceDepServiceResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    service: str
    container_name: str
    volume_name: str
    image: str
    network: str
    status: str
    env_jsonb: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class DevDepEnsureRequest(BaseModel):
    service: Literal["postgres", "redis"]


# ─── Unsplash MCP proxy ────────────────────────────────────────────────


class UnsplashSearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    per_page: int = Field(default=6, ge=1, le=30)
    orientation: Literal["landscape", "portrait", "squarish"] | None = None
    color: str | None = None
    content_filter: Literal["low", "high"] = "low"


class StoredPhotoResponse(BaseModel):
    """One Unsplash photo re-hosted on our S3.  ``urls.regular`` /
    ``urls.small`` are stable anonymous URLs; ``attribution_*`` MUST be
    displayed when using the image."""

    photo_id: str
    description: str | None
    alt_description: str | None
    urls: dict[str, str]   # {"regular": "<s3>", "small": "<s3>"}
    width: int
    height: int
    color: str
    blur_hash: str | None
    photographer_name: str
    photographer_username: str
    photographer_url: str
    photo_url: str
    attribution_text: str
    attribution_html: str

// ─── Session / AgentRun / Event ───────────────────────────────────────────
// Polaris domain model (replaces the old Turn / TurnItem shape).
//
//   Session ─┬─ AgentRun (agent_kind=discovery) ──► Event Event ...
//            └─ AgentRun (agent_kind=codex)     ──► Event Event ...
//
// A Session is one user message + its entire processing chain.  Each
// AgentRun is one agent's execution inside the session; Events are the
// atomic progress items that agent emits.  Kinds are prefixed with the
// agent namespace (`codex:` / `discovery:`) so the two do not collide.

export type SessionStatus =
  | "queued" | "running" | "completed" | "interrupted" | "failed";

export type RunStatus =
  | "queued" | "running" | "completed" | "failed" | "skipped";

export type EventStatus = "started" | "completed" | "failed";

export type AgentKind = "codex" | "discovery";

/** Which agents the orchestrator will run for this Session.
 *
 * - `build_planned` (default): [codex] — Codex does a plan round first.
 * - `build_direct`:            [codex] — Codex executes directly.
 * - `discover_then_build`:     [discovery, codex] — discovery agent first,
 *    its compiled brief drives the codex run.
 */
export type SessionMode = "build_planned" | "build_direct" | "discover_then_build";

export type EventKind =
  // Codex adapter emits these (projected from Codex's item `type`)
  | "codex:agent_message"
  | "codex:plan"
  | "codex:reasoning"
  | "codex:command_execution"
  | "codex:file_change"
  | "codex:mcp_tool_call"
  | "codex:dynamic_tool_call"
  | "codex:web_search"
  | "codex:error"
  | "codex:other"
  // Discovery adapter emits these (one per LangGraph phase)
  | "discovery:clarifying"
  | "discovery:references"
  | "discovery:compiled"
  | "discovery:moodboard";

export type EventResponse = {
  id: string;
  run_id: string;
  sequence: number;
  external_id: string | null;
  kind: EventKind;
  status: EventStatus;
  payload_jsonb: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type AgentRunResponse = {
  id: string;
  session_id: string;
  sequence: number;
  agent_kind: AgentKind;
  status: RunStatus;
  external_id: string | null;
  started_at: string | null;
  finished_at: string | null;
  events: EventResponse[];
};

export type SessionResponse = {
  id: string;
  project_id: string;
  workspace_id: string;
  sequence: number;
  user_message: string;
  mode: SessionMode;
  status: SessionStatus;
  final_message: string | null;
  error_message: string | null;
  cost_jsonb: Record<string, unknown>;
  metadata_jsonb: Record<string, unknown>;
  /** StatusBar counters — cumulative over the session's lifetime. */
  file_change_count: number;
  playwright_call_count: number;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
};

export type SessionDetailResponse = SessionResponse & {
  runs: AgentRunResponse[];
};

export type SessionCreatePayload = {
  message: string;
  mode?: SessionMode;
};

export type SessionSteerPayload = {
  message: string;
};

// ─── Clarification (structured requirement questions from agent) ──────────

export type ClarificationChoice = {
  id: string;
  label: string;
  summary?: string | null;
  /** CSS hex (e.g. "#F5F1E8").  Present when the choice represents a
   * color — the card renders a real swatch alongside the label. */
  swatch?: string | null;
};

export type ClarificationQuestion = {
  id: string;
  title: string;
  description?: string;
  required: boolean;
  choices: ClarificationChoice[];
  allow_override_text: boolean;
  override_label?: string;
};

/** Which agent raised this clarification — the UI may label the source
 * (e.g. "Design intent" vs. "Code") but the question/answer shape is
 * shared.  Matches the server-side `AgentKind`. */
export type ClarificationSource = AgentKind;

export type ClarificationRequest = {
  request_id: string;
  questions: ClarificationQuestion[];
  source?: ClarificationSource;
};

export type ClarificationAnswer = {
  selected_choice: string | null;
  override_text: string | null;
};

export type ClarificationResponse = {
  request_id: string;
  answers: Record<string, ClarificationAnswer>;
  /** Which Session this clarification belongs to.  The SSE event already
   * carries session_id; the frontend threads it back so the API doesn't
   * have to guess via status='running'.  Optional for the in-container CLI
   * path which falls back to the legacy running-run lookup. */
  session_id?: string;
  /** Which AgentRun asked the question.  Required on the web path to bind
   * the answer Redis channel to exactly one agent's lifecycle.  Optional
   * for the in-container CLI path. */
  run_id?: string;
};

// Events streamed over /sessions/{id}/events SSE.  Redis pubsub JSON body.
export type SessionEvent =
  | { session_id: string; kind: "session_started" }
  | {
      session_id: string;
      run_id: string;
      kind: "run_started";
      agent_kind: AgentKind;
      sequence: number;
    }
  | {
      session_id: string;
      run_id: string;
      kind: "run_completed";
      agent_kind: AgentKind;
      status: RunStatus;
      error: string | null;
      external_id: string | null;
    }
  | {
      session_id: string;
      run_id: string;
      kind: "event_started";
      event_kind: EventKind;
      sequence: number;
      external_id: string | null;
      payload: Record<string, unknown>;
    }
  | {
      session_id: string;
      run_id: string;
      kind: "event_completed";
      event_kind: EventKind;
      external_id: string | null;
      payload: Record<string, unknown>;
    }
  | { session_id: string; run_id: string; kind: "agent_message_delta"; text: string }
  | { session_id: string; kind: "project_root_changed"; path: string }
  | { session_id: string; kind: "browser_focus_requested"; reason?: string }
  | {
      session_id: string;
      run_id?: string;
      kind: "session_stats_updated";
      file_change_count: number;
      playwright_call_count: number;
      file_change_delta: number;
      playwright_call_delta: number;
    }
  | {
      session_id: string;
      run_id: string;
      kind: "clarification_requested";
      request: ClarificationRequest;
    }
  | {
      session_id: string;
      run_id?: string;
      kind: "clarification_answered";
      request_id: string;
    }
  | {
      session_id: string;
      kind: "session_completed";
      status: SessionStatus;
      error: string | null;
      final_message: string | null;
    };

// ─── Application runtime / workspace / project ────────────────────────────

export type AppRuntime =
  | "vite"
  | "next"
  | "node_generic"
  | "uvicorn"
  | "flask"
  | "django"
  | "unknown";

export type BrowserSessionStatus = "starting" | "ready" | "expired" | "failed" | "stopped";

export type BrowserSessionSummary = {
  id: string;
  projectId: string;
  workspaceId: string;
  status: BrowserSessionStatus;
  vncUrl?: string;
  expiresAt?: string;
};

export type BrowserSessionResponse = {
  id: string;
  project_id: string;
  workspace_id: string;
  status: BrowserSessionStatus;
  vnc_url: string | null;
  context_metadata_jsonb: Record<string, unknown>;
  created_at: string;
  expires_at: string | null;
};

export type ReadyResponse = {
  service: string;
  database: string;
  redis: string;
};

export type WorkspaceResponse = {
  id: string;
  project_id: string;
  repo_path: string;
  current_branch: string;
  current_commit: string | null;
  status: string;
  compose_profile: string;
  current_browser_session_id: string | null;
  ide_url: string | null;
  ide_status: string;
  /**
   * Directory the IDE should open for this project.  NULL until the Polaris
   * agent sets it via the `set_project_root` dynamic tool (frontend shows
   * a skeleton during that window when a session is in flight).
   */
  project_root: string | null;
  created_at: string;
  updated_at: string;
};

export type WorkspaceIdeSessionResponse = {
  workspace_id: string;
  project_id: string;
  ide_url: string | null;
  ide_status: string;
};

export type WorkspaceRuntimeRequest = {
  services?: Array<"postgres" | "redis">;
};

export type WorkspaceRuntimeResponse = {
  workspace_id: string;
  project_id: string;
  status: string;
  enabled_services: Array<"postgres" | "redis">;
  ide_url: string | null;
  browser_url: string | null;
  project_root: string | null;
  health: Record<string, string>;
};

export type ProjectResponse = {
  id: string;
  user_id: string;
  name: string;
  slug: string;
  description: string | null;
  stack_template: string;
  status: string;
  codex_thread_id: string | null;
  created_at: string;
  updated_at: string;
};

export type ProjectDetailResponse = ProjectResponse & {
  workspace: WorkspaceResponse | null;
};

export type ProjectCreatePayload = {
  name: string;
  description?: string | null;
  stack_template?: string;
};

export type WorkspaceFileEntry = {
  path: string;
  kind: "file" | "directory";
  size: number | null;
};

export type WorkspaceFileContent = {
  path: string;
  content: string;
  revision: string;
};

export type WorkspaceFileWritePayload = {
  path: string;
  content: string;
  base_revision?: string | null;
};

export type SnapshotCreatePayload = {
  title?: string;
  description?: string | null;
  created_by_type?: string;
};

export type ProjectVersionResponse = {
  id: string;
  project_id: string;
  git_commit_hash: string;
  title: string;
  description: string | null;
  created_by_type: string;
  created_at: string;
};

export type UserResponse = {
  id: string;
  email: string;
  name: string;
  avatar_url: string | null;
  created_at: string;
};

export type DeploymentStatus = "queued" | "building" | "deploying" | "ready" | "failed" | "rolled_back";

// ─── Polaris publish manifest (polaris.yaml) ──────────────────────────────────
// Authored by Polaris Agent / the user via `polaris scaffold-publish`. Platform reads
// this at publish time to decide build/run/route/secrets shape.

export type PolarisStack = "spa" | "node" | "python" | "static" | "custom";

export type PolarisDependency = "postgres" | "redis";

export type PolarisManifest = {
  version: 1;
  stack: PolarisStack;
  build: string;
  start: string;
  port: number;
  deps: PolarisDependency[];
  secrets: string[];
  env: Record<string, string>;
  publish: {
    service: string;
    port: number;
  };
};

// ─── Deployment DTO ───────────────────────────────────────────────────────

export type DeploymentResponse = {
  id: string;
  project_id: string;
  project_version_id: string | null;
  git_commit_hash: string | null;
  image_tag: string | null;
  domain: string | null;
  status: DeploymentStatus;
  error: string | null;
  created_at: string;
  ready_at: string | null;
};

export type DeploymentDetailResponse = DeploymentResponse & {
  build_log: string | null;
  smoke_log: string | null;
};


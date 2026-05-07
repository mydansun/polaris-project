# API Reference

FastAPI at `http://localhost:8000` (behind traefik at `/api/` on `https://polaris-dev.xyz/`).

## Health

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness |
| GET | `/ready` | Readiness (database + redis) |

## Authentication

Email verification code + optional invite code. Session: `polaris_session` HTTP-only JWT cookie.

| Method | Path | Description |
|--------|------|-------------|
| POST | `/auth/request-code` | Send verification code. Body: `{ email, invite_code? }`. Unregistered without invite → `{ ok: false, reason: "invite_required" }`. Rate limit: 5/email/hour. |
| POST | `/auth/verify-code` | Verify + auto-register. Body: `{ email, code }`. Sets cookie. |
| GET  | `/auth/me` | Current user |
| GET  | `/auth/dev-login` | Auto-login as dev user (local dev only) |
| POST | `/auth/logout` | Clear session cookie |

## Projects

| Method | Path | Description |
|--------|------|-------------|
| POST | `/projects` | Create project (auto-provisions workspace) |
| GET  | `/projects` | List user's projects |
| GET  | `/projects/{id}` | Project detail with workspace |

## Sessions

A Session is created per user message.  The orchestrator runs one or more
`AgentRun`s inside each Session (discovery, codex, or both).  See
`docs/ARCHITECTURE.md#sessionagentrunevent-model` for the data model.

| Method | Path | Description |
|--------|------|-------------|
| POST | `/projects/{id}/sessions` | Create session. Body: `{ message, mode? }` where mode is `discover_then_build` (frontend sends this on the first message of a project) \| `build_direct` (frontend default for 2nd+ messages AND for the Proceed-on-plan button) \| `build_planned` (backend default when `mode` is omitted; not currently sent by the frontend — kept for scripted callers that want a plan round). Returns **HTTP 429** with `{detail: {reason: "global_quota" \| "user_quota", limit: N}}` when the concurrency cap is hit. |
| GET  | `/projects/{id}/sessions?limit=N&before_sequence=M` | List sessions (paginated, newest first) |
| GET  | `/sessions/{id}` | Session detail (agent_runs + their events) |
| GET  | `/sessions/{id}/events` | SSE stream |
| POST | `/sessions/{id}/interrupt` | Flip session status to `interrupted`, publish `interrupt` on the control channel, and emit a terminal `session_completed(status=interrupted)` SSE frame so the UI flips immediately (worker catches up and re-finalises; the duplicate terminal frame is idempotent). |
| POST | `/sessions/{id}/steer` | Inject additional user text mid-session |

### SSE events

All envelopes include `session_id` and, where relevant, `run_id`.  (The
old `turn_id` wire alias has been removed — frontend and backend are
now both session-native end-to-end.)

```jsonc
{ "kind": "session_started",   "session_id": "..." }

// agent_runs lifecycle
{ "kind": "run_started",   "run_id": "...", "agent": "discovery" | "codex" }
{ "kind": "run_completed", "run_id": "...", "status": "completed" | "failed" }

// event row lifecycle (one per codex item / discovery node)
{ "kind": "event_started",   "event_kind": "codex:plan", "sequence": N, "external_id": "...", "payload": {...} }
{ "kind": "event_completed", "event_kind": "codex:plan", "external_id": "...", "payload": {...}, "status": "completed" }

// streaming token deltas (codex:agent_message; not persisted)
{ "kind": "agent_message_delta", "text": "..." }

// platform signals
{ "kind": "project_root_changed",    "path": "/workspace/..." }
{ "kind": "browser_focus_requested", "reason": "..." }

// status-bar counters — worker coalesces fs / playwright-call bursts
// into one frame per ~500ms; frontend additionally throttles to ~400ms
// for a single "+N" float animation (see StatusBar.tsx).
{ "kind": "session_stats_updated",
  "file_change_count": N, "playwright_call_count": M,
  "file_change_delta": n, "playwright_call_delta": m }

// clarification round-trip (discovery + codex both use this path)
{ "kind": "clarification_requested", "request": { "request_id": "...", "questions": [...] } }
{ "kind": "clarification_answered",  "request_id": "..." }

{ "kind": "session_completed", "status": "completed"|"failed"|"interrupted", "final_message": "..." }
```

### `event_kind` union

Discovery events are emitted by the LangGraph callback handler;
Codex events mirror Codex's `item.type` stream.

| Group | Kinds |
|---|---|
| Codex | `codex:agent_message`, `codex:plan`, `codex:reasoning`, `codex:command_execution`, `codex:file_change`, `codex:mcp_tool_call`, `codex:dynamic_tool_call`, `codex:web_search`, `codex:error`, `codex:other` |
| Discovery | `discovery:clarifying`, `discovery:references`, `discovery:compiled`, `discovery:moodboard` |

The `discovery:moodboard` event's completion payload carries
`mood_board_url` (S3 URL of the generated PNG) so the frontend renders
a mood-board card inline in chat.

## Clarification

Both discovery and Codex funnel structured-question pop-ups through this
endpoint.

| Method | Path | Description |
|--------|------|-------------|
| POST | `/projects/{id}/clarify/request`  | Worker posts questions (persists + SSE) |
| GET  | `/projects/{id}/clarify/pending`  | Frontend checks for an unanswered clarification on reload |
| GET  | `/projects/{id}/clarify/response?request_id=X` | Worker polls for answers (fallback if missed pubsub) |
| POST | `/projects/{id}/clarify/response` | Frontend submits answers → Redis publish to the specific `(session_id, run_id)` channel |

## Workspace

| Method | Path | Description |
|--------|------|-------------|
| GET / POST / DELETE | `/projects/{id}/workspace/runtime` | Manage workspace compose (GET returns `ide_url` / `browser_url` only when `project_root IS NOT NULL`) |
| POST   | `/projects/{id}/workspace/runtime/restart` | Restart containers |
| GET    | `/projects/{id}/workspace/ide` | Current Theia IDE session (gated on `project_root`) |
| POST / DELETE | `/projects/{id}/workspace/ide/session` | Ensure / stop the IDE workspace session |
| GET    | `/projects/{id}/workspace/files` | List files |
| GET    | `/projects/{id}/workspace/files/content?path=X` | Read one file |
| PUT    | `/projects/{id}/workspace/files/content` | Write one file |
| POST   | `/projects/{id}/workspace/snapshot` | Git snapshot → `project_versions` |
| GET    | `/projects/{id}/workspace/versions` | Version history |

## Browser Sessions

| Method | Path | Description |
|--------|------|-------------|
| GET / POST / DELETE | `/projects/{id}/browser/session` | Browser (chromium-vnc) session lifecycle.  GET returns 204 when `workspace.project_root IS NULL` (quiet polling). |

## Dev Dependencies

| Method | Path | Description |
|--------|------|-------------|
| GET    | `/projects/{id}/workspace/dev-deps` | List enabled slots |
| POST   | `/projects/{id}/workspace/dev-deps` | Ensure + start. Body: `{ service: "postgres" \| "redis" }` |
| DELETE | `/projects/{id}/workspace/dev-deps/{service}` | Stop + remove |

## Publish / Deployments

| Method | Path | Description |
|--------|------|-------------|
| POST | `/projects/{id}/publish` | Trigger publish (202) |
| GET  | `/projects/{id}/deployments?limit=N` | History |
| GET  | `/deployments/{id}` | Detail with logs |
| GET  | `/deployments/{id}/events` | SSE stream (`log` / `status` / `ready` / `failed` frames) |
| POST | `/projects/{id}/rollback` | Rollback. Body: `{ git_commit_hash }` |
| POST | `/projects/{id}/prepublish-audit` | Optional LLM review invoked by `polaris prepublish-audit --deep`. Body: `{ polaris_yaml, dockerfile, package_json_scripts }`. Returns `{ issues: [{severity: "error"\|"warning", hint, fix}] }`. Static checks (bare node-bins, YAML shape) live workspace-side in the CLI; this endpoint adds semantic review (port mismatches, missing scripts, non-idempotent migrations). Returns `{issues: []}` when `OPENAI_SECRET` is unset — audit is best-effort. |

## MCP Server (Codex-facing)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/mcp/` | Streamable-HTTP MCP protocol endpoint. Requires `Authorization: Bearer <workspace_token>`. Tools: `search_photos`, `get_all_icon_sets`, `get_icon_set`, `search_icons`, `get_icon`. |

Codex config (`infra/workspace/codex-config.toml`) points to this URL and
reads the workspace token via its `bearer_token_env_var` field.  Frontend
never calls this.

## Internal: Unsplash REST proxy

`POST /workspace/unsplash/search` is an internal REST route (session
cookie OR workspace-token) used for debugging / smoke tests.  In
production the MCP `search_photos` tool is the intended entry point.

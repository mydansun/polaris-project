# Polaris Architecture

## System Overview

```
User Browser (React 19 + Vite 7)
  ├─ Left:   Chat console (SSE-streamed session events)
  └─ Right:  Theia IDE / Chromium VNC (toggle between browser / IDE / hidden)

Frontend ↕ REST / SSE

FastAPI API (apps/api)
  ├─ Auth: email verification code + invite code + dev-login
  ├─ Projects, workspaces, sessions, versions
  ├─ Clarification request/response (blocking Codex round-trip)
  ├─ Publish pipeline + dev-dep slot management
  ├─ Redis XSTREAM job enqueue
  └─ Streamable-HTTP MCP server at /mcp (Unsplash + Iconify tools for Codex)

Worker (apps/worker, host-side)
  ├─ Consumes session jobs from Redis
  ├─ Orchestrator runs one or more AgentRuns per Session
  │     • DiscoveryAgent — LangGraph (clarifier → review → references → compiler → mood_board)
  │     • CodexAgent     — long-lived WebSocket to codex app-server in workspace
  ├─ Dynamic tools: set_project_root, focus_browser
  ├─ Fans out per-node SSE events; persists to sessions/agent_runs/events
  └─ Writes AGENTS.md + mood_board.png into the workspace container

Codex App Server (inside workspace container, port 4455)
  ├─ exec_command, apply_patch, request_user_input (container = sandbox)
  ├─ MCP clients:
  │     • playwright (stdio, talks to chromium-vnc:9223)
  │     • polaris (streamable HTTP, url → apps/api /mcp, bearer = workspace_token)
  ├─ Filesystem skills: $HOME/.agents/skills/frontend-skill/SKILL.md
  └─ Shell CLIs: polaris (publish), polaris-bg (dev servers)

Edge: Traefik v3 @ :80/:443
  ├─ Dev:     polaris-dev.xyz, ide-*.polaris-dev.xyz, browser-*.polaris-dev.xyz
  ├─ S3/MinIO: s3.polaris-dev.xyz / polaris.s3.polaris-dev.xyz (anon static/*)
  └─ Publish: <uuid>.prod.polaris-dev.xyz
```

## Session / AgentRun / Event Model

Every user message produces one **Session**.  Inside a Session the orchestrator
runs one or more **AgentRun**s in sequence; each AgentRun emits a stream of
**Event**s.  This replaces the older single-level Turn/TurnItem model and
makes multi-agent workflows (discovery → codex) a first-class DB concept.

```
Session (one per user message)
  ├─ mode: build_planned | build_direct | discover_then_build
  ├─ status: queued | running | completed | interrupted | failed
  └─ AgentRun[] (agent = discovery | codex)
        ├─ input_jsonb, output_jsonb
        ├─ status, started_at, finished_at
        └─ Event[]
              ├─ kind: codex:agent_message | codex:plan | codex:file_change
              │        | codex:command_execution | codex:reasoning
              │        | codex:mcp_tool_call | codex:dynamic_tool_call
              │        | codex:web_search | codex:error | codex:other
              │        | discovery:clarifying | discovery:references
              │        | discovery:compiled | discovery:moodboard
              └─ status (running → completed|failed), payload_jsonb
```

Session modes:

| Mode | AgentRuns | When |
|---|---|---|
| `discover_then_build` | DiscoveryAgent → CodexAgent (`plan`) | First message of a project (auto-routed by frontend) |
| `build_direct`  | Codex `default` | Frontend default for 2nd+ messages AND for the Proceed-on-plan button |
| `build_planned` | Codex `plan` | Backend default when `mode` is omitted; not currently sent by the frontend (kept for scripted callers that want a plan round on every turn) |

The frontend deliberately skips the plan/proceed handshake on iteration
turns — after the initial `discover_then_build` produces its plan, the
user approves once and subsequent messages go straight to `build_direct`
so the agent just edits code without another plan round.

## Database Schema

PostgreSQL primary store. Redis for queues + pubsub.

| Table | Key Columns |
|-------|-------------|
| `users` | id, email (unique), name, avatar_url |
| `verification_codes` | id, email, code, expires_at, used_at |
| `projects` | id, user_id, name, slug, codex_thread_id |
| `workspaces` | id, project_id, repo_path, project_root, workspace_token, ide_status |
| `sessions` | id, project_id, workspace_id, sequence, user_message, mode, status, final_message |
| `agent_runs` | id, session_id, agent, input_jsonb, output_jsonb, status |
| `events` | id, run_id, sequence, external_id, kind, status, payload_jsonb |
| `clarifications` | id, request_id, session_id, run_id, status, questions_jsonb, answers_jsonb |
| `design_intents` | id, project_id, session_id, intent_jsonb, compiled_brief, pinterest_refs_jsonb, pinterest_queries_jsonb, mood_board_url, status |
| `unsplash_images` | id, photo_id, size, s3_key, content_type (dedupe cache for Unsplash MCP) |
| `browser_sessions` | id, project_id, workspace_id, status, vnc_url |
| `deployments` | id, project_id, image_tag, domain, status, build_log, smoke_log |
| `workspace_dep_services` | id, workspace_id, service, container_name, status |

## Discovery Agent (packages/design-intent)

LangGraph pipeline that turns a vague user message into a structured design
brief + mood board reference.  Owned by the worker's `DiscoveryAgent`, which
adapts it to the generic `Agent` interface and emits SSE events on node
transitions.

```
     ┌──── clarifier_step ⇄ clarifier_ask (interrupts for user answers) ────┐
START→│                          ↓                                              │
     │  review_step (LLM quality gate; rejects → back to clarifier)           │
     │                          ↓                                              │
     │  pinterest (fetch candidates + batched LLM scorer, pick 1)             │
     │                          ↓                                              │
     │  compiler (multimodal gpt-5.4: sees chosen image + intent → brief)     │
     │                          ↓                                              │
     └ mood_board_step (gpt-image-1 images.edit with Pinterest ref → PNG) ────┘
                                 ↓
                               END
```

After the graph returns the worker:
1. Persists the 18-key `DesignIntent` + brief into `design_intents`.
2. Uploads the mood board PNG to S3 (`static/images/moodboard/<uuid>.png`)
   and writes it into the workspace container at `/home/workspace/mood_board.png`.
3. Renders `AGENTS.md` (brief + mood board absolute path + "this is a mood
   reference, not a page screenshot") into `$CODEX_HOME/AGENTS.md`.
4. Hands control to `CodexAgent` for the build run.

## Codex Integration

Codex app-server runs inside the workspace container as a supervisord process.
The worker's `CodexAgent` connects over WebSocket via `PolarisCodexSession`
(`packages/agent-core`) and drives a single JSON-RPC thread per project.

Per-turn bindings (rebuilt each run for current conn/redis/session handles):

| Binding | Purpose |
|---|---|
| `dynamic_tool_handler` | Handles `set_project_root`, `focus_browser` tool calls |
| `user_input_handler`   | Blocks Codex's `request_user_input` on a Redis pubsub answer |

MCP servers available inside every Codex session:

| Server | Transport | Hosted where |
|---|---|---|
| `playwright` | stdio (npx child process) | Inside workspace container |
| `polaris`    | streamable HTTP + bearer  | **apps/api `/mcp`** — Unsplash + Iconify tools |

The `polaris` MCP's bearer is the per-workspace token that `services/compose.py`
injects into the workspace as `POLARIS_WORKSPACE_TOKEN`.  Codex config
(`infra/workspace/codex-config.toml`) reads it via `bearer_token_env_var`.

## Codex Agent Tool Layer

| Type | Tools |
|------|-------|
| Codex native | `exec_command`, `apply_patch`, `request_user_input` |
| Shell CLIs | `polaris-bg` (dev servers), `polaris publish/scaffold-publish/dev-up` |
| Dynamic (Polaris) | `set_project_root` (IDE reveal + git init), `focus_browser` (auto-switches right pane to VNC) |
| MCP — playwright | Full browser control, points at `http://chromium-vnc:9223` |
| MCP — polaris    | `search_photos` (Unsplash → S3), `get_all_icon_sets`, `get_icon_set`, `search_icons`, `get_icon` |
| Skills | `$HOME/.agents/skills/frontend-skill/SKILL.md` (design discipline guide) |

## MCP Server (/mcp)

`apps/api/src/polaris_api/mcp_app.py` mounts a FastMCP streamable-HTTP
endpoint at `/mcp`.  Auth is bearer-token (workspace token) via a Starlette
ASGI middleware.

| Tool | Backs onto |
|---|---|
| `search_photos(query, per_page, orientation?, color?, content_filter?)` | Unsplash API → rehosts to S3 under `static/images/up/*`, dedupes via `unsplash_images` |
| `get_all_icon_sets` / `get_icon_set(set)` / `search_icons(query, limit?, start?, prefix?)` / `get_icon(set, icon)` | api.iconify.design (keyless, stateless passthrough + framework snippets) |

Secrets (UNSPLASH_ACCESS_KEY, S3_*) stay platform-side; the workspace
container only knows its own workspace token.

## Requirement Clarification

Used by BOTH discovery (LangGraph's `clarifier_ask` via `interrupt()`) and
Codex (`request_user_input`).  Both paths land on the same frontend card.

1. Agent emits structured questions → `POST /clarify/request` persists +
   publishes `clarification_requested` SSE.
2. Worker blocks on `clarification_channel:<run_id>` Redis pubsub.
3. Frontend renders `ClarificationCard` inline in chat.
4. User submits → `POST /clarify/response` publishes on the channel.
5. Agent unblocks with answers.

Questions use non-technical language.  Visual-direction choices are
still industry-tailored by the clarifier's system prompt, but the
5-color **primary color palette is LLM-generated per project** via a
dedicated graph node:

- Clarifier LLM emits a `propose_color_palette(industry,
  visual_direction, audience, language)` tool call
- `palette_step` (new LangGraph node, see `nodes/clarifier.py`) runs a
  color-theorist system prompt against `compiler_model` (flagship),
  parses the response as `[{id, label, swatch}]` × 5 with hex-regex
  validation
- On any parse / LLM failure, falls back to a neutral default palette
  so the clarifier loop never deadlocks
- Returned options flow straight into the next `ask_questions` call as
  the color question's choices — frontend's `ClarificationCard`
  up-sizes the swatch chips when every choice carries a `swatch` hex

## IDE (packages/ide)

Custom Theia build with only Explorer, Search, and Editor, shipped as
`polaris/ide:latest`.  Target: `browser` (Node backend, no Electron).
Playwright smoke tests run inside the Docker build — the build fails if
tests fail.

## Workspace Design

- **Empty-workspace invariant**: starts empty, scaffolders run first
- **IDE**: Theia on port 3000, workspace dir via CLI arg
- **Dev deps**: Independent docker containers (postgres/redis) on workspace network
- **Welcome page**: nginx sidecar at `http://welcome/`
- **Container tools**: Node 24, Python 3 + venv, git, curl, wget, unzip, zip, jq, ripgrep, build-essential, Codex CLI, Playwright MCP
- **Filesystem skills**: `/home/workspace/.agents/skills/frontend-skill/SKILL.md` (OpenAI's upstream frontend-skill verbatim)
- **Generated assets**: `/home/workspace/mood_board.png` (discovery output)

## Publish Pipeline

Triggered by Codex in chat via the `polaris publish` CLI.

**Scaffold**: `polaris scaffold-publish` (no `--stack`) prints a menu
of the five stacks — `spa` (Vite / Astro / CRA → nginx multi-stage),
`node` (long-running Node server), `python`, `static`, `custom` — plus
the marker-detected recommendation.  The CLI writes template files
only when invoked with `--stack=<choice>`.  Platform-side
`auto_scaffold_if_missing` is the fallback when a user clicks publish
without running the CLI first; it uses the same detection logic
(reads `package.json` dependencies for a `vite` key to distinguish
`spa` from `node`).

**Audit**: `polaris prepublish-audit` runs static rules against
`polaris.yaml::start` (flags bare framework binaries like `next` /
`vite` / `tsc` that would exit 127 because the runtime PATH doesn't
include `node_modules/.bin`).  The `--deep` flag additionally calls
the platform's `POST /projects/{id}/prepublish-audit` endpoint for an
LLM review of semantic mismatches (port disagreements, missing
scripts, non-idempotent migrations).

**Pipeline** (`apps/api/src/polaris_api/services/publish.py`):

1. **Git archive** → unpack commit into a tmp dir.
2. **Sanitize** — `sanitize_prod_compose` strips any host-published
   `ports:` from the user's `compose.prod.yml` (Traefik owns host
   80/443; any host publish from the user compose would collide).
   Removals are appended to `build_log` so the user sees what was
   touched.
3. **Docker build** → `<registry>/polaris/<project>:<short-hash>`.
4. **Secrets** materialize to `.data/projects/<uuid>/secrets.env`
   (`$`-escaped, stable across publishes so DB volumes keep working).
5. **Smoke** — stand up `compose.prod.yml` + a `compose.preview.yml`
   override on an isolated network, probe the publish service with a
   disposable `curlimages/curl` container.  On failure the finally
   block dumps `docker logs --tail 200 <service>-1` into `smoke_log`
   **before** `compose down -v` — the SSE stream surfaces that to the
   workspace's `polaris publish` stdout so Codex sees the real crash
   reason (e.g. `sh: 1: next: not found`) instead of just the opaque
   curl error.
6. **Push** the image to the local registry.
7. **Promote** — write the prod override (`compose.polaris.yml`) with
   the Traefik labels + `traefik-public` network + materialized
   secrets, `compose up`.

Rollback reuses cached images in the local registry.

**Templates** live at `infra/publish-templates/{spa,node,python,static}/`
and are COPY'd into the workspace image at `/opt/polaris-publish-templates/`
so the in-container CLI can read them directly.  The `node` runner stage
sets `ENV PATH=/app/node_modules/.bin:$PATH` so the common "bare `next`"
footgun doesn't reach prod in template-derived Dockerfiles.

## Session Interrupt

Clicking Stop triggers a five-point flow; all five are needed because
UI responsiveness and actual worker cooperation are separate concerns:

1. **Frontend** `App.tsx::handleInterrupt` → `POST /sessions/{id}/interrupt`;
   merges the returned `SessionResponse` into local state (optimistic
   flip to `interrupted`).
2. **API route** publishes `{kind: "interrupt"}` on
   `session_control_channel(id)`, flips `sessions.status = "interrupted"`
   in the DB, and publishes a **terminal** `session_completed(status=interrupted)`
   frame on `session_events_channel(id)` so any SSE subscriber flips
   immediately (the worker catches up and re-finalises; the duplicate
   terminal frame is idempotent — the frontend closes the EventSource
   on the first terminal frame).
3. **Worker** `_consume_session_control` forwards the interrupt to the
   currently running agent via `agent.handle_control(event)`.
   `CodexAgent.handle_control` sends `turn/interrupt` over the Codex
   WebSocket; `DiscoveryAgent.handle_control` cancels the in-flight
   `run_design_intent` asyncio task.
4. **Agent return paths** — both map their interrupted state to
   `RunOutcome(status="interrupted")` (CodexAgent's earlier bug where
   `"interrupted"` fell through to `"completed"` was fixed).
5. **Orchestrator** treats `outcome.status == "interrupted"` as
   terminal (new branch alongside the existing `"failed"`), calls
   `_finalize_session(status="interrupted")`, and polls
   `sessions.status` before each agent in the loop so an API-initiated
   interrupt arriving between agents also short-circuits.

## Concurrency Quota

Two Redis sorted-set tokens gate session creation to cap OpenAI /
gpt-image-1 spend:

| Key | Score | Member |
|---|---|---|
| `polaris:runs:global` | `now + TTL` | `session_id` |
| `polaris:runs:user:<user_id>` | `now + TTL` | `session_id` |

`POST /projects/{id}/sessions` acquires both atomically via a Lua
script (each call: `ZREMRANGEBYSCORE` expired → `ZCARD` check limit →
`ZADD`; user-bucket rejection rolls back the global slot it just
took).  Worker's orchestrator releases in its `finally`, regardless of
outcome; TTL is a crash-recovery backstop.  On rejection the API
returns `HTTP 429 {detail: {reason, limit}}`; the frontend surfaces
this via `QuotaDialog`.  Defaults: 6 global / 2 per-user / 1800s TTL
(env `POLARIS_MAX_{GLOBAL,USER}_RUNS` / `POLARIS_RUN_QUOTA_TTL_SECONDS`).

## Networking

| Network | Purpose |
|---------|---------|
| `polaris-internal` | Platform infra ↔ per-workspace composes |
| `traefik-public`   | Workspace + chromium + MinIO + published apps (Traefik label discovery) |
| `<compose>_default` | Per-workspace isolation |

## Security

- Container = sandbox (`sandbox_mode = "danger-full-access"`)
- Per-workspace network isolation, UID 1000 (non-root)
- Selkies VNC hardening (audio/sharing/gamepad disabled)
- `X-Polaris-Workspace-Token` header for in-container CLI auth + MCP bearer
- Traefik sole ingress
- OpenAI / Unsplash / S3 credentials never reach the workspace container

## Timeout & Liveness

Codex WebSocket `is_alive()` probe every 30s.  No idle timeout.  Total
wall-clock cap 900s per turn (env `POLARIS_CODEX_TURN_TIMEOUT_SECONDS`).
Discovery's LangGraph has its own internal round cap (3 ask rounds +
2 review rejections + `tool_choice="any"` no-prose guard).

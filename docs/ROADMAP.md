# Roadmap

## Current State

The platform works end-to-end: sign in → create project → chat with agent →
first message auto-runs **discovery** (LangGraph clarifier + references +
compiler + mood-board) → Codex takes over, plans, scaffolds, codes, previews
in browser → publish to `<uuid>.prod.polaris-dev.xyz`.

## Verified Milestones

### Session / Multi-agent
- **Session / AgentRun / Event model** — replaces single-level Turn/TurnItem;
  one Session per user message, 1+ AgentRuns per Session (discovery → codex).
  Frontend + backend are now session-native end-to-end; the old `turn_id`
  wire alias has been removed.
- **Three session modes** — `discover_then_build` (first message of a
  project; auto-routed by frontend), `build_direct` (frontend default for
  2nd+ messages AND for the Proceed-on-plan button), `build_planned`
  (backend default when `mode` is omitted; not sent by the frontend).
- **Full-chain interrupt** — Stop button sends `POST
  /sessions/{id}/interrupt`; API publishes the terminal SSE frame (UI
  flips instantly) + signals the control channel; worker's
  `_consume_session_control` forwards to the active agent
  (`CodexAgent.handle_control` → `turn/interrupt` WS;
  `DiscoveryAgent.handle_control` → task cancel); outcome routes to
  `_finalize_session(status="interrupted")`.
- **Concurrency quota** — Redis sorted-set tokens cap `POST /sessions`
  (`POLARIS_MAX_GLOBAL_RUNS=6`, `POLARIS_MAX_USER_RUNS=2`), acquired
  synchronously + released in the worker's orchestrator finally.
  HTTP 429 → frontend `QuotaDialog`.
- **Discovery agent (packages/design-intent)** — LangGraph pipeline:
  clarifier ⇄ clarifier_ask → review_step → pinterest → compiler →
  mood_board_step → END.  Per-node SSE events via a LangChain callback
  handler.
- **Pinterest references** — 6 candidates fetched, batched multimodal LLM
  scorer picks 1 (≥ threshold or max-scoring), only that one is fed to the
  compiler.  Query suffix "web design" applied mechanically.
- **Mood board generator** — gpt-image-1 `images.edit` with the Pinterest
  ref as visual reference + intent-filled prompt.  Uploaded to S3 for the
  frontend card; written to `/home/workspace/mood_board.png` + referenced
  in AGENTS.md so Codex can open on demand.
- **LLM-generated color palette** — clarifier calls a dedicated
  `propose_color_palette` tool; `palette_step` graph node produces 5
  context-tailored hex options per project (validated by regex,
  falls back to a neutral default on parse failure).  Replaces the
  earlier hardcoded 5-color palette.
- **Plan translation** — each Codex plan is rewritten into a non-technical
  "Overview" via a separate gpt-5.4 call; frontend renders a shadcn Tabs
  card (Overview / Details).

### Codex integration
- **Plan mode** — Codex plans first, turn ends, frontend shows "Proceed"
  button.  Clicking creates a `build_direct` session with a localized
  trigger message.
- **Codex `request_user_input`** — built-in tool for structured clarification.
  Worker blocks on Redis pubsub; frontend renders `ClarificationCard`;
  discovery shares the same channel.
- **Dynamic tools** — `set_project_root` (IDE reveal + git init) and
  `focus_browser` (auto-switches frontend right pane to VNC before
  playwright MCP calls).
- **MCP server (Codex-facing)** — streamable-HTTP mount at `/mcp` with
  bearer-workspace-token auth.  Tools: `search_photos` (Unsplash → S3),
  `get_all_icon_sets` / `get_icon_set` / `search_icons` / `get_icon`
  (Iconify passthrough).  Secrets stay platform-side.
- **Frontend skill** — OpenAI's upstream `frontend-skill/SKILL.md` shipped
  into every workspace at `$HOME/.agents/skills/frontend-skill/SKILL.md`.

### Platform
- **Theia IDE** — custom `packages/ide` with Explorer + Search + Editor.
  Playwright smoke tests in Docker build.
- **i18n** — react-i18next en + zh.  Auto-detect + toggle + localStorage.
- **Two-column layout** + resizable split with overlay preview.
- **Email verification + invite code auth** — Postmark delivery.
  Auto-registration behind invite.
- **WebSocket liveness** — `is_alive()` probe, no idle timeout (900s wall-clock cap).
- **Selkies VNC hardening** — audio/sharing/gamepad disabled and locked.
- **Chat features** — session pagination, noise clustering, Ctrl/Cmd+Enter,
  MCP overlay, empty message suppression, plan tabs, mood board card.
- **Publish pipeline** — `polaris` CLI, smoke test, `secrets.env` `$`-escaping.
  - Menu-driven `polaris scaffold-publish` (no `--stack` = print menu;
    explicit `--stack=<choice>` writes files).  Five stacks: `spa`
    (Vite → nginx multi-stage), `node`, `python`, `static`, `custom`.
  - Compose sanitizer strips host `ports:` from user
    `compose.prod.yml` before build (avoids 80/443 collisions with the
    platform's Traefik).
  - `prepublish-audit` has a static rule against bare node bins
    (`next` / `vite` / `tsc` not under `npm`/`npx`) and an opt-in
    `--deep` LLM review via `POST /projects/{id}/prepublish-audit`.
  - On smoke failure the pipeline captures `docker logs --tail 200`
    from the user service into `smoke_log` before `compose down -v`,
    so the real crash reason reaches Codex via the SSE stream.
  - Node template's runner stage sets
    `ENV PATH=/app/node_modules/.bin:$PATH` so bare `next` etc. work
    in template-derived Dockerfiles.
- **S3 / MinIO** — bucket + anon-readable `static/*` prefix.  Dedicated
  `*.s3.polaris-dev.xyz` cert.  Used by Unsplash MCP + mood board storage.
- **Unsplash dedupe** — `unsplash_images` table keyed on `(photo_id, size)`
  prevents re-uploads.  Objects land under `static/images/up/<uuid>.ext`
  (keeps Unsplash uploads separated from hand-placed platform assets
  at `static/images/frontend/*`).
- **Welcome cards** — when no project is selected, `ExampleProjectCards`
  shows four localized-prompt cards (golf / todo / blog / estate),
  clicking each one starts a `discover_then_build` session.

## Not Yet Implemented

1. Usage / token / cost accounting on `agent_runs.cost_jsonb` or
   `sessions.cost_jsonb`
2. Version-diff UI for `project_versions`
3. Multi-tenant auth (per-tenant `auth.json`)
4. Worker retry / dead-letter handling
5. Registry GC / image retention
6. Remote publish host
7. Traefik dashboard auth
8. Custom user domains
9. Syntax highlighting in Theia (requires `@theia/plugin-ext`)
10. Frontend display for design-intent history (re-discovery UI)
11. `generate_image` MCP tool (decorative / abstract image gen for Codex
    beyond Unsplash photos and Iconify icons)

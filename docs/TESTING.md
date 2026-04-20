# Testing

## Static Checks

```sh
apps/api/.venv/bin/python -m ruff check apps/api packages/agent-core apps/worker
apps/api/.venv/bin/pytest apps/api/tests -v
apps/worker/.venv/bin/pytest packages/design-intent -v
pnpm typecheck
```

## IDE Smoke Tests

`packages/ide` Dockerfile runs Playwright tests automatically during `make build-ide`:
- HTTP 200 (not 404), Theia shell renders, custom welcome page, Explorer expanded, no trust dialog

Local: `cd packages/ide && yarn build && yarn start &` → `yarn test`

## Quick Smoke (End-to-End)

```sh
make clear && make dev
```

Visit `https://polaris-dev.xyz/`, sign in (or "Dev Login"), send a prompt.

## Auth Flow

1. **New user**: email → `invite_required` → invite code → verification code → registered
2. **Returning user**: email → code → logged in
3. **Rate limit**: 6th request/hour → 429
4. **Language**: switch via header menu → instant, persisted in localStorage

## Multi-Agent Flow

### Discovery-then-build (first message of a project)

1. Create project → empty workspace
2. First message auto-routes to `mode: "discover_then_build"`
3. Discovery SSE progression:
   `discovery:clarifying` (with 1–3 rounds of ClarificationCard) →
   `discovery:references` → `discovery:compiled` → `discovery:moodboard`
4. `MoodBoardBody` appears in chat with the generated image
5. Codex takes over (same session, new `AgentRun`) with `plan` mode
6. Plan produced → "Proceed" button shown with Tabs (Overview / Details)
7. Click Proceed → new session with `build_direct` → Codex executes

### Build-direct (subsequent messages)

1. Send a 2nd+ message → session with `build_direct` → Codex writes
   code directly, no plan round.
2. `ClarificationCard` if Codex calls `request_user_input`.

The plan/proceed handshake only runs on the **first** message of a
project (inside `discover_then_build`) — subsequent messages skip it
so iteration is friction-free.

### Stop / interrupt

1. While a session is `running`, click the Stop button in the input
   bar.
2. Header status pill flips to "interrupted" within ~100ms (frontend
   merges the `POST /sessions/{id}/interrupt` response optimistically).
3. For Codex sessions: codex app-server receives `turn/interrupt` over
   WS; the outcome lands as `status="interrupted"`.
4. For Discovery sessions: the `run_design_intent` asyncio task is
   cancelled at its next await point; the outcome lands as
   `"interrupted"` — orchestrator short-circuits the remaining agent
   chain.

### Playwright smoke (agent side)

1. Agent calls `focus_browser` → right pane auto-switches to VNC
2. Agent calls `playwright` MCP tools → user watches live
3. MCP overlay debounces calls on the frontend

## Unsplash + Iconify MCP

```sh
# Grab a workspace token from the DB:
psql -d polaris -c "SELECT workspace_token FROM workspaces LIMIT 1;"

# Unsplash
curl -sSN -X POST "http://localhost:8000/mcp/" \
  -H "Authorization: Bearer <token>" \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"search_photos","arguments":{"query":"coffee shop interior","per_page":3}}}'

# Iconify
curl -sSN -X POST "http://localhost:8000/mcp/" \
  -H "Authorization: Bearer <token>" \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
       "params":{"name":"search_icons","arguments":{"query":"home","prefix":"lucide"}}}'
```

## Publish

```sh
# Inside the workspace, from the project root:
polaris scaffold-publish                        # prints the stack menu + detection
polaris scaffold-publish --stack=spa            # (or node / python / static / custom)
polaris prepublish-audit                        # static checks; --deep adds LLM review
git add . && git commit -m "pub"
polaris publish

# From host:
curl -I https://<uuid>.prod.polaris-dev.xyz/
```

On smoke failure, the platform captures the user service's container
logs (`docker logs --tail 200`) into `smoke_log` before tearing the
preview down — look for the "captured tail of `<svc>` container logs"
section in the live SSE output (or in the DB's `deployments.smoke_log`).

## Workspace Restart

1. Header menu → Restart workspace → shadcn Dialog confirmation
2. IDE + VNC show skeleton/loading
3. Auto-reload when ready (~10-20s)

## Verification Queries

```sql
-- Recent users / projects
SELECT id, email, name FROM users ORDER BY created_at DESC LIMIT 5;
SELECT id, name, slug FROM projects ORDER BY created_at DESC LIMIT 5;

-- Session + agent_runs + events
SELECT id, sequence, mode, status, LEFT(user_message, 60) FROM sessions ORDER BY created_at DESC LIMIT 5;
SELECT run.id, run.agent, run.status, sess.sequence
  FROM agent_runs run JOIN sessions sess ON run.session_id = sess.id
  ORDER BY run.created_at DESC LIMIT 10;
SELECT kind, status, COUNT(*) FROM events WHERE run_id = '<run-id>' GROUP BY kind, status;

-- Clarifications
SELECT request_id, status FROM clarifications ORDER BY created_at DESC LIMIT 5;

-- Design intents (discovery output)
SELECT project_id, status, mood_board_url IS NOT NULL AS has_mood_board,
       LEFT(compiled_brief, 80) FROM design_intents
  ORDER BY created_at DESC LIMIT 5;

-- Unsplash dedupe cache
SELECT photo_id, size, s3_key FROM unsplash_images ORDER BY created_at DESC LIMIT 10;

-- Deployments
SELECT id, status, LEFT(git_commit_hash, 7), domain FROM deployments ORDER BY created_at DESC LIMIT 5;
```

## Concurrency quota

```sh
# With defaults (6 global / 2 per user), open three browser tabs as
# the same user and fire a session from each; the third should pop
# the `QuotaDialog` ("用户配额不足 / you're already running 2 active
# sessions").  Seven tabs across different users → one of them hits
# the global cap.

# Inspect the sorted sets:
docker exec polaris-project-redis-1 redis-cli ZRANGEBYSCORE polaris:runs:global -inf +inf WITHSCORES
docker exec polaris-project-redis-1 redis-cli KEYS 'polaris:runs:user:*'
```

# Development

How to run Polaris on a developer's machine for coding on the platform
itself.  For a shared demo / staging instance, see
[STAGING.md](./STAGING.md).

---

## 1. Host prerequisites

| Tool | Why | Minimum |
|---|---|---|
| Linux or macOS | Dev host | Ubuntu 22.04+ / macOS 13+ |
| Docker Engine / Desktop | Per-workspace + publish containers | 24.x+ with compose v2 |
| Python | API / worker venvs | 3.12+ |
| Node.js + pnpm | Frontend + shared packages | Node 20+, pnpm via `corepack enable` |
| process-compose | Local process supervisor used by `make dev` | any |
| lsof | Port preflight in Makefile | any |
| Codex CLI + `codex login` | Workspace containers bind-mount `~/.codex/auth.json` | latest |

`make prereqs` fails fast when anything above is missing.

**Host ports used by `make dev`** (free these before starting):

| Port | What |
|---|---|
| 8000 | FastAPI |
| 5173 | Vite dev server |
| 5432 / 6379 | Postgres / Redis (127.0.0.1 only) |
| 5000 | Local Docker registry (127.0.0.1 only) |
| 80 / 443 / 8090 | Traefik (only when using the polaris-dev.xyz domain path; skip with `dev-local`).  8090 = unauthenticated dashboard. |
| 9001 | MinIO web console (0.0.0.0 — LAN visible; login via `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`) |

`make preflight-ports` aborts `make dev` if 8000 or 5173 are bound.

---

## 2. First-time setup

```bash
git clone <repo> && cd polaris-project
corepack enable
cp .env.example .env
```

Edit `.env` with the minimum viable set:

```
SESSION_SECRET=$(openssl rand -hex 48)    # any long random string
POLARIS_INVITE_CODE=dev-invite            # anything — required for new-user sign-up
POLARIS_DEV_USER_EMAIL=dev@polaris.local  # enables the "Dev Login" button + /auth/dev-login (leave empty to disable)
POLARIS_DEV_USER_NAME=Polaris Dev         # display name for the auto-provisioned dev user
OPENAI_SECRET=                            # leave empty to skip discovery agent locally
POSTMARK_SERVER_TOKEN=                    # empty → verification codes log to console
```

When Postmark is empty and OpenAI is empty:
- Sign-in still works via **Dev Login** (bypasses email verification).
- Discovery-agent–routed messages (first message of a new project, or
  any "re-discover") will fail at the compiler step.  Skip discovery
  in local work by sending your second+ message via a regular chat turn
  (goes straight to Codex).
- Everything else — per-workspace compose, IDE, chromium VNC, Codex
  sessions, publish pipeline — works without these keys.

Bootstrap venvs + pnpm install + build workspace/IDE/chromium images:

```bash
make bootstrap            # creates apps/api/.venv, apps/worker/.venv, runs pnpm install
make build-ide            # custom Theia base image (5-10 min first time)
make build-workspace      # IDE + dev toolchain + Codex CLI
make build-chromium       # chromium-vnc with CDP proxy
```

These targets are dependency-aware — re-running after `git pull` only
rebuilds what's stale.

---

## 3. Starting the stack

### 3.1 Full (recommended)

```bash
make dev
```

Chains: `bootstrap` → `preflight-ports` → `pull-images` →
`build-workspace` → `build-chromium` → `infra` (postgres / redis /
registry / traefik / minio) → `migrate` → `process-compose up` (api +
worker + web).

Open `http://localhost:5173/` and click **Dev Login**.

### 3.2 Without Docker infra

If you've already got Postgres + Redis running locally (e.g. Homebrew
services), skip the infra compose and use your own:

```bash
make dev-local            # same as `dev` minus `make infra`
```

Point `POLARIS_DATABASE_URL` / `POLARIS_REDIS_URL` in `.env` at your
host instances.

### 3.3 Running services individually

`process-compose up` drives all three, but you can run them standalone
for focused work:

```bash
make api        # uvicorn with --reload
make worker     # polaris-worker (Redis consumer)
make web        # pnpm dev:web (Vite)
```

Each waits for its own venv bootstrap; no need to run `make dev` first.

---

## 4. Local TLS (optional)

`make dev` works fine over `http://localhost:5173/` — TLS is only
needed if you want to exercise the full Traefik routing path.  For that:

```bash
# repo-local mkcert-style self-signed pair, already in ./certs/
# edit /etc/hosts so these names resolve to 127.0.0.1:
#   polaris-dev.xyz, ide-*.polaris-dev.xyz, browser-*.polaris-dev.xyz
```

Wildcards in `/etc/hosts` are painful — enumerate the handful of
`ide-<hash>.polaris-dev.xyz` / `browser-<hash>.polaris-dev.xyz` names
you're testing, or just use `http://localhost:5173/` which bypasses
Traefik entirely.

For a real DNS + Let's Encrypt setup, see [STAGING.md](./STAGING.md).

---

## 5. Tests

```bash
cd apps/api && .venv/bin/python -m pytest tests/ -v          # API + CLI audit tests
make test-worker                                             # worker orchestrator + discovery cancel
make test-design-intent                                      # LangGraph nodes + palette step
cd apps/web && pnpm exec tsc --noEmit                        # frontend type check
```

See [TESTING.md](./TESTING.md) for the full test matrix.

---

## 6. Common dev workflows

### 6.1 Reset everything

```bash
make clear                # interactive; drops ALL workspace state
make clear FORCE=1        # non-interactive
```

Wipes per-workspace containers, per-project compose state, workspace
meta, and the Postgres / Redis volumes.  **Does not** wipe built
images or the local Docker registry.

### 6.2 Stop without losing state

```bash
# In the process-compose TUI window: Ctrl+C           (stops api / worker / web)
make stop                 # halt every polaris container in place (keeps containers, volumes, .data/*, networks, images)
```

Four lifecycle levels for quick reference:

| Command | Containers | Volumes | `.data/*` | Built images |
|---|---|---|---|---|
| `make stop` | stopped | kept | kept | kept |
| `make stop-infra` | infra `down` = removed | kept | kept | kept |
| `make clear` | per-workspace `rm -f` + wipe; infra pg/redis stopped | platform pg/redis dropped | wiped | kept |
| `make down` | **all** removed (incl. traefik / minio / registry) | **all** dropped (incl. minio bind-mount) | wiped | kept |

Volumes persist through `make stop`; `make dev` resumes exactly where
you left off.  `make stop-infra` is fine too — its only downside is
that the postgres/redis/traefik/minio/registry containers get recreated
on the next `make infra`, which takes a few seconds longer than
`make stop`'s in-place restart.

### 6.3 Apply a new migration

```bash
cd apps/api && .venv/bin/alembic revision --autogenerate -m "add foo"
make migrate              # runs alembic upgrade head
```

Worker reads via the same `apps/api` venv, so no second install needed.

### 6.4 Rebuild workspace image after CLI / template changes

```bash
make build-workspace
# running workspace containers keep the old image until next session;
# `make clear` wipes them so new sessions get the new image.
```

`make build-workspace` triggers on:
- `infra/workspace/Dockerfile`
- `infra/workspace/polaris-cli/*` (the `polaris` CLI inside workspaces)
- `infra/publish-templates/*` (publish scaffolds COPY'd into `/opt/polaris-publish-templates`)

### 6.5 Logs

| What | Where |
|---|---|
| api / worker / web | `process-compose attach` (TUI with per-process tails) |
| Per-workspace container | `docker logs polaris-ws-<hash>` / `polaris-br-<hash>` |
| Traefik | `http://localhost:8090/dashboard/` + `docker logs polaris-traefik-1` |
| Publish SSE | the PublishPanel's live-log section, or `polaris publish` stdout inside a workspace |

### 6.6 Talking to the DB directly

```bash
docker exec -it polaris-project-postgres-1 psql -U root polaris
```

---

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `make dev` fails at `preflight-ports` | 8000 / 5173 already bound | `lsof -i :8000` → kill the stray process |
| `pnpm install` warns about missing `corepack` | Node < 16.10 | Upgrade Node to 20 LTS |
| `build-ide` hangs for ~5 min | First Theia build fetches + compiles yarn workspaces | Normal; subsequent builds are seconds |
| Session stays `queued` | Worker not running | Check `process-compose attach`; restart the worker process |
| IDE iframe shows "waiting for agent" forever | Codex never called `set_project_root` | Inspect `docker logs polaris-ws-<hash>` for the Codex transcript; often a scaffold crash |
| Workspace container exits with auth error | `~/.codex/auth.json` missing on host | `codex login` on the host, then `make clear && make dev` |

---

## See also

- [STAGING.md](./STAGING.md) — deploying to a controlled staging host (DNS, TLS, hardening caveats)
- [ARCHITECTURE.md](./ARCHITECTURE.md) — system design
- [API.md](./API.md) — REST + SSE endpoints
- [CONFIGURATION.md](./CONFIGURATION.md) — full environment variable reference
- [FRONTEND.md](./FRONTEND.md) — React architecture
- [TESTING.md](./TESTING.md) — verification procedures

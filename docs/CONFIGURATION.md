# Configuration

## Single Source

The repo-root `.env` is the single configuration source. `apps/api`, `apps/worker`,
and the `packages/design-intent` LangGraph all read it. `.env.example` is the
template.

```sh
cp .env.example .env   # fill in secrets
make dev               # bootstrap + start everything
```

## Environment Variables

### Datastores

```
POLARIS_DATABASE_URL=postgresql+asyncpg://root:123456@127.0.0.1:5432/polaris
POLARIS_REDIS_URL=redis://127.0.0.1:6379/0
```

### Auth

```
SESSION_SECRET=<random 32+ byte secret>
# Dev Login shortcut (GET /auth/dev-login + the "Dev Login" button on
# the login page).  When empty, the endpoint 404s AND the frontend
# hides the button (via `GET /auth/config`).  Set ONLY in local dev.
POLARIS_DEV_USER_EMAIL=dev@polaris.local
POLARIS_DEV_USER_NAME=Polaris Dev
POLARIS_INVITE_CODE=               # required for new user registration (leave empty to block all signups)
```

### Email (Postmark)

```
POSTMARK_SERVER_TOKEN=             # Postmark API token
POSTMARK_MESSAGE_STREAM=outbound   # Postmark message stream ID
POSTMARK_FROM_EMAIL=noreply@polaris.dev  # verified sender address
```

When `POSTMARK_SERVER_TOKEN` is empty, verification codes are logged to the API console instead of emailed (useful for local dev).

### Frontend

```
FRONTEND_URL=https://polaris-dev.xyz
POLARIS_CORS_ORIGINS=["https://polaris-dev.xyz"]
VITE_API_BASE_URL=/api
```

### Workspace Images

```
POLARIS_WORKSPACE_IMAGE=polaris/workspace:latest   # Theia IDE + dev toolchain + Codex
POLARIS_BROWSER_IMAGE=polaris/chromium-vnc:latest
POLARIS_POSTGRES_IMAGE=postgres:16-alpine
POLARIS_REDIS_IMAGE=redis:7-alpine
```

### URL Templates

```
POLARIS_IDE_PUBLIC_URL_TEMPLATE=https://ide-{workspaceHash}.polaris-dev.xyz
POLARIS_BROWSER_PUBLIC_URL_TEMPLATE=https://browser-{workspaceHash}.polaris-dev.xyz
```

### Worker (Codex)

```
POLARIS_CODEX_MODEL=gpt-5.4                     # main Codex model
# POLARIS_CODEX_TURN_TIMEOUT_SECONDS=900        # total wall-clock cap per turn
# POLARIS_CODEX_LIVENESS_CHECK_INTERVAL_SECONDS=30
# POLARIS_IDLE_WORKSPACE_TIMEOUT_SECONDS=3600   # scavenger stops idle workspaces
# POLARIS_CODEX_PLAN_PLAIN_MODEL=gpt-5.4        # translates Codex plans into non-technical "Overview"
```

### OpenAI

```
OPENAI_SECRET=                     # platform-side only — never reaches the workspace container
```

Used by: the LangGraph discovery agent (clarifier / review / compiler /
mood_board), the Codex plan-plain translator, and the MCP `search_photos`
tool's downstream image calls.

### S3 / MinIO (image re-hosting)

`static/*` key prefix is anonymously readable; built URLs are
`${S3_URL_BASE}/${key}`.  Credentials stay platform-side and are never
injected into workspace containers.

```
S3_ACCESS_KEY_ID=polaris
S3_SECRET_ACCESS_KEY=<random 32+ byte secret>
S3_ENDPOINT=https://s3.polaris-dev.xyz
S3_BUCKET=polaris
S3_URL_BASE=https://polaris.s3.polaris-dev.xyz

# MinIO root creds (infra container only — not used by apps/api)
MINIO_ROOT_USER=root
MINIO_ROOT_PASSWORD=<random 32+ byte secret>
```

### Unsplash MCP

```
UNSPLASH_ACCESS_KEY=               # server-side only; the workspace MCP proxies via /mcp
```

The MCP's `search_photos` tool downloads selected Unsplash images and
rehosts them to `static/images/up/*.jpg` under the S3 bucket above,
deduping by `(photo_id, size)` in the `unsplash_images` table.  Keeping
Unsplash uploads under the `up/` subprefix separates them from
hand-placed platform assets (e.g. `static/images/frontend/*`).

### Design-Intent LangGraph (packages/design-intent)

All optional; defaults cover normal operation.  The discovery agent uses
the flagship model for everything except the mini-friendly review step.

```
POLARIS_DESIGN_INTENT_MODEL=gpt-5.4                   # fallback for any role without its own override
POLARIS_DESIGN_INTENT_COMPILER_MODEL=gpt-5.4          # multimodal brief writer
POLARIS_DESIGN_INTENT_CLARIFIER_MODEL=gpt-5.4         # tool-calling clarifier
POLARIS_DESIGN_INTENT_REVIEW_MODEL=gpt-5.4-mini       # cheap JSON grading
POLARIS_DESIGN_INTENT_SCORER_MODEL=gpt-5.4-mini       # batched image match scorer
POLARIS_DESIGN_INTENT_MOOD_BOARD_IMAGE_MODEL=gpt-image-1
POLARIS_DESIGN_INTENT_MOOD_BOARD_SIZE=1536x1024

POLARIS_PINTEREST_TOOL_BASE=http://polaris-dev.xyz:9801
POLARIS_DESIGN_INTENT_MAX_ROUNDS=3
POLARIS_DESIGN_INTENT_PINTEREST_HOPS=1
POLARIS_DESIGN_INTENT_MAX_REFS=6                      # candidates sent to the batched scorer
POLARIS_DESIGN_INTENT_IMAGE_SCORE_THRESHOLD=4.0       # 0–5; first hit ≥ threshold wins, else max
```

### Publish

```
POLARIS_PUBLISH_PROJECTS_ROOT=.data/projects
POLARIS_REGISTRY_URL=127.0.0.1:5000
POLARIS_API_URL_FOR_WORKSPACE=http://host.docker.internal:8000
# POLARIS_PUBLISH_BUILD_TIMEOUT=900     # docker build wall-clock cap (s)
# POLARIS_PUBLISH_SMOKE_TIMEOUT=60      # smoke-probe window (s)
```

### Run concurrency quota

Two Redis sorted-set tokens gate `POST /projects/{id}/sessions` to cap
OpenAI / gpt-image-1 spend.  Acquired synchronously in the route,
released in the worker's orchestrator `finally` (so crashes eventually
free the slot via the TTL backstop).  See
`apps/api/src/polaris_api/services/run_quota.py`.

```
POLARIS_MAX_GLOBAL_RUNS=6              # platform-wide in-flight sessions
POLARIS_MAX_USER_RUNS=2                # per-user in-flight sessions
POLARIS_RUN_QUOTA_TTL_SECONDS=1800     # backstop TTL (sorted-set score = now + TTL)
```

Clients that hit the cap receive HTTP 429 with `{detail: {reason:
"global_quota" | "user_quota", limit: N}}`; the frontend surfaces this
via `QuotaDialog`.

### Prepublish audit (LLM `--deep`)

The workspace CLI's `polaris prepublish-audit --deep` uploads the
user's `polaris.yaml` + `Dockerfile` + `package.json::scripts` to
`POST /projects/{id}/prepublish-audit`, which runs an LLM review for
likely runtime failures (bare framework bins, port mismatches, missing
scripts, non-idempotent migrations).

```
POLARIS_AUDIT_MODEL=gpt-5.4-mini       # cheap by default; audit is text-in/text-out
```

Requires `OPENAI_SECRET` to be set; returns empty issues when the key
is absent or the call fails (audit is best-effort and never blocks
publish on its own infrastructure hiccup).

### Traefik / Domains

```
POLARIS_DOMAIN=polaris-dev.xyz
POLARIS_PROD_DOMAIN_BASE=prod.polaris-dev.xyz
POLARIS_TRAEFIK_PUBLIC_NETWORK=traefik-public
```

## TLS Certificates

Three Let's Encrypt wildcard cert pairs (dev + publish + S3 planes).
Wildcard SANs only match one label, so each needs its own cert.

```sh
sudo certbot certonly --manual --preferred-challenges dns \
  -d polaris-dev.xyz -d "*.polaris-dev.xyz"
sudo certbot certonly --manual --preferred-challenges dns \
  -d prod.polaris-dev.xyz -d "*.prod.polaris-dev.xyz"
sudo certbot certonly --manual --preferred-challenges dns \
  -d "*.s3.polaris-dev.xyz"
```

Certs land under `/etc/letsencrypt/live/<domain>/{fullchain,privkey}.pem`
and are loaded by `infra/traefik/dynamic/certs.yaml`. The traefik compose
file bind-mounts the whole `/etc/letsencrypt` tree read-only (mounting just
`live/` would break the symlinks into `archive/`).

DNS: `polaris-dev.xyz`, `*.polaris-dev.xyz`, `prod.polaris-dev.xyz`,
`*.prod.polaris-dev.xyz`, and `*.s3.polaris-dev.xyz` must all resolve to
the host running traefik.

## Codex Authentication

Codex app-server runs inside workspace containers. It reuses the host user's
`~/.codex/auth.json` via a read-write bind mount. Run `codex login` once on the host.

Sessions stored in per-workspace named volume (`polaris-ws-<hash>-codex-home`)
so `thread/resume` works across container restarts.  The volume is also
seeded by the workspace image with:
- `$CODEX_HOME/config.toml` — enables Playwright stdio MCP + Polaris HTTP MCP
- `$HOME/.agents/skills/frontend-skill/SKILL.md` — design discipline skill

## IDE (packages/ide)

Custom Theia build serving on port 3000. The `packages/ide/Dockerfile` has three stages:

1. **builder** — `yarn install` + `yarn build` (tsc → theia generate → inject custom modules → theia copy → webpack)
2. **runtime** — slim `node:22-bookworm-slim` with git + native libs
3. **test** — installs Playwright, starts Theia, runs smoke tests (HTTP 200, Explorer expanded, custom welcome). Build fails if tests fail.

Final image is clean runtime (test deps discarded).

## Chromium VNC (Selkies)

The chromium-vnc container uses Selkies with hardened configuration:
- `HARDEN_DESKTOP=true`, `HARDEN_OPENBOX=true`
- Audio, microphone, gamepad, file transfers, sharing, second screen: disabled and locked
- All sidebar panels hidden and locked
- Browser cursors enabled (`SELKIES_USE_BROWSER_CURSORS=true|locked`)
- Chrome homepage + new-tab page: `http://welcome/`

## Welcome Page

`make welcome-page` builds `packages/welcome-page/dist/`. On workspace startup,
the API copies it into per-workspace browser-config. A `welcome` nginx sidecar
serves it at `http://welcome/`.

## Secrets.env Escaping

All values written to `secrets.env` have `$` escaped as `$$` to prevent Docker
Compose variable interpolation.

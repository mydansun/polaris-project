# Polaris

Polaris is an AI full-stack application building platform for end users.
The platform turns a natural-language request into working code, real
browser-verified behavior, Git-backed versions, and one-click Docker +
Traefik-routed deployments at `<uuid>.prod.polaris-dev.xyz`.

First messages route through a **discovery agent** (LangGraph: clarifier
→ references → brief compiler → mood board generator) to produce a
design brief and a generated visual mood board before Codex takes over.
Subsequent messages run Codex directly with plan / build modes.

## Quick Start

```sh
cp .env.example .env     # fill in POSTMARK_* and POLARIS_INVITE_CODE
make dev                 # bootstrap + build images + start infra + api + worker + web
```

Visit `https://polaris-dev.xyz/` (or `http://localhost:5173/`) and sign in with your email. New users need an invite code (set via `POLARIS_INVITE_CODE` in `.env`). For local development, click "Dev Login" to skip email verification.

## Host Prerequisites

| Tool | Purpose |
|------|---------|
| **Docker** (Desktop or Engine) | Platform infra + workspace containers + publish pipeline |
| **certbot** | Let's Encrypt certs for `*.polaris-dev.xyz` + `*.prod.polaris-dev.xyz` |
| **Node.js >= 20** + **pnpm** | Frontend packages (`corepack enable`) |
| **Python >= 3.12** | `apps/api` + `apps/worker` virtualenvs |
| **process-compose** | Local dev process orchestration |
| **Codex CLI** + `codex login` | Workspace container reuses host `~/.codex/auth.json` |

> Python venvs, editable installs, and `pnpm install` are handled by `make bootstrap`.

## Make Targets

| Target | What it does |
|--------|--------------|
| `make dev` | Full dev stack: bootstrap + build images + infra + migrate + process-compose |
| `make dev-local` | Same but skips Docker infra (use existing Postgres/Redis) |
| `make bootstrap` | Create/refresh Python venvs + `pnpm install` |
| `make build-ide` | Rebuild `polaris/ide:latest` (Theia IDE base + Playwright smoke tests) |
| `make build-workspace` | Rebuild `polaris/workspace:latest` (IDE + dev toolchain + Codex) |
| `make build-chromium` | Rebuild `polaris/chromium-vnc:latest` |
| `make migrate` | Run Alembic migrations |
| `make stop` | Halt every polaris container (workspaces + published + infra) without removing anything — state preserved for a later `make dev` |
| `make stop-infra` | `down` MinIO + traefik + platform postgres/redis/registry (removes containers; volumes kept) |
| `make clear` | Drop ALL workspace state for a clean slate (rm containers + wipe volumes + `.data/*`) |
| `make welcome-page` | Rebuild the Chromium welcome-page bundle |

## Repository Shape

```
apps/
  web/           React workbench: chat + Theia IDE / Chromium VNC (two-column, i18n: en + zh)
  api/           FastAPI control plane: auth, projects, sessions, workspaces, publish, MCP
  worker/        Background session runner: Redis consumer, orchestrates discovery + Codex agents

packages/
  ide/            Custom Theia IDE (Explorer + Search + Editor only) + Dockerfile + Playwright tests
  agent-core/     PolarisCodexSession (WebSocket JSON-RPC to codex app-server)
  design-intent/  LangGraph discovery agent (clarifier / review / references / compiler / mood_board)
  ui/             Shared React primitives (shadcn/ui + Radix; Tabs used by plan card)
  shared-types/   Shared TypeScript API / SSE contracts
  welcome-page/   Static welcome page for chromium-vnc

infra/
  workspace/     polaris/workspace Dockerfile (FROM polaris/ide) + supervisord + CLIs + frontend-skill
  chromium/      polaris/chromium-vnc Dockerfile + nginx CDP proxy
  traefik/       Edge router for dev + publish + s3 planes
  minio/         S3-compatible object store (mood boards + Unsplash rehosted images)
  publish-templates/  Per-stack Dockerfile + compose + polaris.yaml scaffolds
                      (spa: Vite→nginx / node: Node server / python: FastAPI etc. / static: pre-built)
```

## Documentation

- [Development](./docs/DEVELOPMENT.md) · [中文](./docs/DEVELOPMENT.zh.md) — local dev: `make dev`, Dev Login, hot reload, troubleshooting
- [Staging](./docs/STAGING.md) · [中文](./docs/STAGING.zh.md) — single-host staging: custom domain, systemd units, firewall / hardening (NOT production-ready)
- [Architecture](./docs/ARCHITECTURE.md) — system design, data model, agent tools, publish pipeline
- [API Reference](./docs/API.md) — all REST + SSE endpoints
- [Configuration](./docs/CONFIGURATION.md) — environment variables and setup
- [Frontend](./docs/FRONTEND.md) — React architecture, components, data flow
- [Roadmap](./docs/ROADMAP.md) — current state, verified milestones, open items
- [Testing](./docs/TESTING.md) — verification procedures

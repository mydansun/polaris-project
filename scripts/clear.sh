#!/usr/bin/env bash
# Clear every piece of local Polaris state that accumulates from `make dev`.
# After this, `make dev` will bootstrap everything from scratch.
#
# Removes:
#   • all per-workspace containers (workspace + chromium-vnc)
#   • all per-workspace dev-dep containers (polaris-ws-<hash>-postgres / -redis)
#     + their persistent data volumes
#   • all per-workspace docker networks (polaris-<hash>_default)
#   • workspace repo checkouts under /tmp/polaris-workspaces
#   • workspace meta dir (~/.polaris/workspace-meta)
#   • per-run Codex homes (~/.polaris/codex-home/*) — keeps ~/.codex/auth.json
#   • the platform Postgres + Redis data volumes (wipes all DB rows + queued jobs)
#
# Does NOT:
#   • recreate postgres/redis (let `make dev` / `make infra` do that)
#   • run DB migrations (let `make dev` / `make migrate` do that)
#   • touch the traefik container (stateless — leave it running)
#   • delete ~/.codex/auth.json or the polaris/workspace image cache
#
# Run as `make clear` (invokes this script).
# Pass `FORCE=1` to skip the interactive confirmation.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

WORKSPACE_ROOT="${POLARIS_WORKSPACE_ROOT:-$REPO_ROOT/.data/workspaces}"
WORKSPACE_META_ROOT="${POLARIS_WORKSPACE_META_ROOT:-$REPO_ROOT/.data/workspace-meta}"
PUBLISH_PROJECTS_ROOT="${POLARIS_PUBLISH_PROJECTS_ROOT:-$REPO_ROOT/.data/projects}"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }

if [[ "${FORCE:-0}" != "1" ]]; then
    cat <<EOF
This will DELETE:
  - every Polaris workspace + chromium-vnc container
  - every per-workspace docker network
  - $WORKSPACE_ROOT/*
  - $WORKSPACE_META_ROOT/*
  - $PUBLISH_PROJECTS_ROOT/*
  - per-workspace codex-home volumes
  - the Postgres + Redis volumes (all projects/runs + queued jobs)

Postgres and Redis will be stopped and NOT recreated — use 'make dev' (or
'make infra') afterwards to bring them back up with empty volumes. traefik
is left running. Your ~/.codex/auth.json and polaris/workspace image stay.

EOF
    read -r -p "Continue? [y/N] " answer
    case "${answer:-}" in
        y|Y|yes|YES) ;;
        *) echo "aborted."; exit 1 ;;
    esac
fi

# ── 1. workspace + chromium-vnc + dev-dep containers ──────────────
say "Removing per-workspace containers"
docker ps -a --filter label=polaris.workspace_id -q | xargs -r docker rm -f >/dev/null || true
docker ps -a --format '{{.Names}}' \
    | grep -E '^polaris-(ws|br)-|^polaris-[a-f0-9]{24}-' \
    | xargs -r docker rm -f >/dev/null || true

# ── 1b. dev-dep persistent volumes (postgres/redis per workspace) ──
say "Removing dev-dep data volumes"
docker volume ls --format '{{.Name}}' \
    | grep -E '^polaris-ws-[a-f0-9]{24}-(postgres|redis)-data$' \
    | xargs -r docker volume rm >/dev/null 2>&1 || true
docker volume ls --format '{{.Name}}' \
    | grep -E '^polaris-ws-[a-f0-9]{24}-codex-home$' \
    | xargs -r docker volume rm >/dev/null 2>&1 || true

# ── 2. per-project docker networks ─────────────────────────────────
say "Removing per-workspace docker networks"
docker network ls --format '{{.Name}}' \
    | grep -E '^polaris-[a-f0-9]{24}_default' \
    | xargs -r docker network rm >/dev/null 2>&1 || true

# ── 3. workspace repo + meta + publish projects (.data/) ───────────
say "Wiping .data/ state"
rm -rf "${WORKSPACE_ROOT:?}"/* 2>/dev/null || true
rm -rf "${WORKSPACE_META_ROOT:?}"/* 2>/dev/null || true
rm -rf "${PUBLISH_PROJECTS_ROOT:?}"/* 2>/dev/null || true

# ── 4. drop postgres + redis volumes (stops those containers) ──────
say "Dropping Postgres + Redis volumes"
docker compose -f docker-compose.infra.yaml down postgres redis -v >/dev/null 2>&1 || true

# ── 5. final state ─────────────────────────────────────────────────
say "Cleared. Current polaris state:"
echo "  containers:"
docker ps -a --format '{{.Names}}  {{.Status}}' | grep polaris | sed 's/^/    /' || echo "    (none)"
echo "  networks:"
docker network ls --format '{{.Name}}' | grep '^polaris' | sed 's/^/    /' || echo "    (none)"
echo ""
echo "Next: 'make dev' — brings Postgres/Redis back up, runs migrations, starts api/worker/web."

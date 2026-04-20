#!/usr/bin/env bash
# Nuclear teardown: remove EVERY Polaris container, volume, network, and
# bind-mount data dir on this host.  Goes beyond `make clear` — that one
# keeps traefik / minio / registry running and keeps built images.  After
# `make down`, the only trace left is the built images themselves
# (polaris/ide, polaris/workspace, polaris/chromium-vnc) and
# ~/.codex/auth.json.
#
# Removes:
#   • every polaris container (workspace, chromium-vnc, preview, published,
#     welcome sidecar, polaris-web, traefik, minio, postgres, redis, registry)
#   • every polaris volume (pg / redis / registry data, per-workspace
#     codex-home + dev-dep volumes, minio bind-mount contents)
#   • every polaris network (per-workspace defaults, polaris-internal,
#     traefik-public if no other user)
#   • .data/workspaces/*, .data/workspace-meta/*, .data/projects/*
#   • infra/minio/data/*
#
# Does NOT:
#   • remove built images (polaris/ide, polaris/workspace, polaris/chromium-vnc)
#   • touch ~/.codex/auth.json
#   • remove the repo itself
#
# Pass FORCE=1 to skip the interactive confirmation.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

WORKSPACE_ROOT="${POLARIS_WORKSPACE_ROOT:-$REPO_ROOT/.data/workspaces}"
WORKSPACE_META_ROOT="${POLARIS_WORKSPACE_META_ROOT:-$REPO_ROOT/.data/workspace-meta}"
PUBLISH_PROJECTS_ROOT="${POLARIS_PUBLISH_PROJECTS_ROOT:-$REPO_ROOT/.data/projects}"
MINIO_DATA_ROOT="$REPO_ROOT/infra/minio/data"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }

if [[ "${FORCE:-0}" != "1" ]]; then
    cat <<EOF
This will DELETE every trace of Polaris state on this host:
  - all Polaris containers (workspace / browser / preview / published /
    traefik / minio / postgres / redis / registry / polaris-web)
  - all Polaris volumes (platform pg+redis+registry; per-workspace
    codex-home + dev-dep pg/redis; minio bind-mount contents)
  - all Polaris networks (per-workspace + polaris-internal + traefik-public)
  - $WORKSPACE_ROOT/*
  - $WORKSPACE_META_ROOT/*
  - $PUBLISH_PROJECTS_ROOT/*
  - $MINIO_DATA_ROOT/*

KEEPS: built images (polaris/ide, polaris/workspace, polaris/chromium-vnc)
       and ~/.codex/auth.json.

This is destructive.  All published sites, mood boards, DB rows, and
queued jobs will be gone.

EOF
    read -r -p "Continue? [y/N] " answer
    case "${answer:-}" in
        y|Y|yes|YES) ;;
        *) echo "aborted."; exit 1 ;;
    esac
fi

# ── 1. every polaris-prefixed container (workspace, browser, preview,
#       published, welcome sidecar, polaris-web, dev-deps) ─────────────
say "Removing per-workspace / published / preview / web containers"
docker ps -a --filter label=polaris.workspace_id -q | xargs -r docker rm -f >/dev/null 2>&1 || true
docker ps -a --format '{{.Names}}' \
    | grep -E '^polaris-(ws|br|pub|pvw|web)-?|^polaris-[a-f0-9]{24}-' \
    | xargs -r docker rm -f >/dev/null 2>&1 || true

# Web sidecar — no-op on dev hosts where it was never started.
say "Bringing down polaris-web sidecar"
docker compose -f infra/web/compose.yaml down -v >/dev/null 2>&1 || true

# ── 2. infra stacks (minio / traefik / postgres / redis / registry) ───
say "Bringing down minio (removing container + clearing bind-mount data)"
docker compose -f infra/minio/compose.yaml --env-file .env down >/dev/null 2>&1 || true

say "Bringing down traefik"
docker compose -f infra/traefik/compose.yaml down >/dev/null 2>&1 || true

say "Bringing down postgres / redis / registry (+ volumes)"
docker compose -f docker-compose.infra.yaml down -v >/dev/null 2>&1 || true

# ── 3. per-workspace volumes (dev-deps + codex-home) ──────────────────
say "Removing per-workspace volumes"
docker volume ls --format '{{.Name}}' \
    | grep -E '^polaris-ws-[a-f0-9]{24}-(postgres|redis)-data$|^polaris-ws-[a-f0-9]{24}-codex-home$' \
    | xargs -r docker volume rm >/dev/null 2>&1 || true

# ── 4. networks (per-workspace defaults + polaris-internal +
#       traefik-public if unused) ──────────────────────────────────────
say "Removing Polaris docker networks"
docker network ls --format '{{.Name}}' \
    | grep -E '^polaris-[a-f0-9]{24}_default|^polaris-internal$|^traefik-public$' \
    | xargs -r docker network rm >/dev/null 2>&1 || true

# ── 5. bind-mount data roots ──────────────────────────────────────────
say "Wiping .data/ and infra/minio/data/"
rm -rf "${WORKSPACE_ROOT:?}"/* 2>/dev/null || true
rm -rf "${WORKSPACE_META_ROOT:?}"/* 2>/dev/null || true
rm -rf "${PUBLISH_PROJECTS_ROOT:?}"/* 2>/dev/null || true
rm -rf "${MINIO_DATA_ROOT:?}"/* 2>/dev/null || true

# ── 6. final state ────────────────────────────────────────────────────
say "Down.  Current polaris state:"
echo "  containers:"
docker ps -a --format '{{.Names}}  {{.Status}}' | grep -E 'polaris|traefik|minio' | sed 's/^/    /' || echo "    (none)"
echo "  volumes:"
docker volume ls --format '{{.Name}}' | grep -E '^polaris' | sed 's/^/    /' || echo "    (none)"
echo "  networks:"
docker network ls --format '{{.Name}}' | grep -E '^polaris|^traefik-public' | sed 's/^/    /' || echo "    (none)"
echo ""
echo "Images kept: polaris/ide, polaris/workspace, polaris/chromium-vnc"
echo "Next: 'make dev' (or 'make staging') to bootstrap everything from scratch."

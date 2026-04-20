PYTHON ?= python3

# Staging-only nginx sidecar that serves apps/web/dist.  Tracked in git
# (the bind-mount path is relative, so the file is host-invariant); the
# `staging` target brings it up and `stop` / `down` bring it down.  Dev
# hosts never start it — `make dev` uses Vite via process-compose.
WEB_COMPOSE := infra/web/compose.yaml

.PHONY: prereqs bootstrap bootstrap-venvs bootstrap-node preflight-ports \
	infra stop stop-infra migrate \
	api worker web \
	dev dev-local staging start-published pull-images build-ide build-workspace welcome-page check-process-compose \
	clear down test-design-intent test-worker

# Host-side directory where published projects' compose + secrets live.
# Mirrors POLARIS_PUBLISH_PROJECTS_ROOT in apps/api (config.py); override
# for prod hosts (e.g. /srv/polaris-projects).
PUBLISH_PROJECTS_ROOT ?= $(CURDIR)/.data/projects

# ── Prerequisites (tool presence only; venv/deps handled by bootstrap) ─
prereqs:
	@echo "Checking prerequisites..."
	@command -v docker >/dev/null 2>&1 || { echo "ERROR: docker not found. Install Docker Desktop."; exit 1; }
	@docker info >/dev/null 2>&1 || { echo "ERROR: Docker daemon not running. Start Docker Desktop."; exit 1; }
	@command -v pnpm >/dev/null 2>&1 || { \
		echo "ERROR: pnpm not found. Install via one of:"; \
		echo "  corepack enable           # recommended (ships with Node >= 16.10)"; \
		echo "  npm install -g pnpm"; \
		echo "  brew install pnpm"; \
		exit 1; }
	@command -v $(PYTHON) >/dev/null 2>&1 || { echo "ERROR: $(PYTHON) not found. Install Python 3.12+."; exit 1; }
	@command -v lsof >/dev/null 2>&1 || { echo "ERROR: lsof not found (needed for preflight-ports)."; exit 1; }
	@echo "Prerequisites OK."

# ── Bootstrap: create .venv dirs, install editable packages, run pnpm install ──
bootstrap: prereqs bootstrap-venvs bootstrap-node
	@echo "Bootstrap complete."

bootstrap-venvs: apps/api/.venv/.bootstrapped apps/worker/.venv/.bootstrapped

apps/api/.venv/.bootstrapped: apps/api/pyproject.toml packages/agent-core/pyproject.toml
	@test -d apps/api/.venv || { echo "Creating apps/api/.venv..."; $(PYTHON) -m venv apps/api/.venv; }
	@echo "Installing API deps (agent-core + api, editable)..."
	@apps/api/.venv/bin/pip install --upgrade pip >/dev/null
	@apps/api/.venv/bin/pip install -e packages/agent-core -e apps/api
	@touch $@

apps/worker/.venv/.bootstrapped: apps/worker/pyproject.toml apps/api/pyproject.toml \
	packages/agent-core/pyproject.toml packages/design-intent/pyproject.toml
	@test -d apps/worker/.venv || { echo "Creating apps/worker/.venv..."; $(PYTHON) -m venv apps/worker/.venv; }
	@echo "Installing worker deps (agent-core + design-intent + api + worker, editable)..."
	@apps/worker/.venv/bin/pip install --upgrade pip >/dev/null
	@apps/worker/.venv/bin/pip install \
		-e packages/agent-core \
		-e packages/design-intent \
		-e apps/api \
		-e apps/worker
	@touch $@

bootstrap-node: node_modules/.modules.yaml

node_modules/.modules.yaml: pnpm-lock.yaml
	pnpm install

# ── Port preflight (dev / dev-local only) ─────────────────────────
preflight-ports:
	@lsof -i :8000 -P -n -t >/dev/null 2>&1 && { echo "ERROR: port 8000 already in use. Free it before starting dev stack."; exit 1; } || true
	@lsof -i :5173 -P -n -t >/dev/null 2>&1 && { echo "ERROR: port 5173 already in use. Free it before starting dev stack."; exit 1; } || true

# ── Welcome page bundle (standalone HTML + MV3 new-tab extension) ──
welcome-page: bootstrap-node
	pnpm --filter @polaris/welcome-page build

# ── Infrastructure (Postgres + Redis + traefik edge + minio) ─────
# traefik is the single ingress for dev (ide/browser subdomains, polaris-dev.xyz
# root) and prod (Phase C publish) planes. It depends on the polaris-internal
# network created by docker-compose.infra.yaml, so compose.infra runs first.
# minio joins traefik-public (created by traefik compose), so it runs last.
infra:
	docker compose -f docker-compose.infra.yaml up -d --wait
	docker network inspect polaris-internal >/dev/null 2>&1 || docker network create polaris-internal
	docker compose -f infra/traefik/compose.yaml up -d --wait
	docker compose -f infra/minio/compose.yaml --env-file .env up -d --wait

stop-infra:
	docker compose -f infra/minio/compose.yaml --env-file .env down
	docker compose -f infra/traefik/compose.yaml down
	docker compose -f docker-compose.infra.yaml down

# ── Stop every polaris container (preserves all state) ────────────
# Differs from `make clear` (rm + drop volumes + wipe .data/*) and
# `make stop-infra` (which `down`s infra = stop AND remove).  `make
# stop` halts every running polaris container in place so nothing is
# lost — DB rows, workspace code, published images, mood boards, and
# network definitions all stay.  Bring everything back with
# `make dev` / `make infra`.
#
# Note: if you're running `make dev`, the process-compose TUI for
# api/worker/web is NOT touched — Ctrl+C out of it separately.
# `make stop` only affects containers.
stop:
	@echo "Stopping per-workspace / published / preview / welcome containers..."
	@docker ps --format '{{.Names}}' \
		| grep -E '^polaris-(ws|br|pub|pvw)-|^polaris-[a-f0-9]{24}-' \
		| xargs -r docker stop >/dev/null 2>&1 || true
	@echo "Stopping polaris-web sidecar (no-op on dev hosts)..."
	@docker compose -f $(WEB_COMPOSE) stop 2>/dev/null || true
	@echo "Stopping MinIO..."
	@docker compose -f infra/minio/compose.yaml --env-file .env stop 2>/dev/null || true
	@echo "Stopping traefik..."
	@docker compose -f infra/traefik/compose.yaml stop 2>/dev/null || true
	@echo "Stopping platform postgres / redis / registry..."
	@docker compose -f docker-compose.infra.yaml stop

# ── Database migration ─────────────────────────────────────────────
migrate: apps/api/.venv/.bootstrapped
	cd apps/api && .venv/bin/alembic upgrade head

# ── Tests ──────────────────────────────────────────────────────────
test-design-intent: apps/worker/.venv/.bootstrapped
	cd packages/design-intent && ../../apps/worker/.venv/bin/pytest

test-worker: apps/worker/.venv/.bootstrapped
	cd apps/worker && .venv/bin/pytest

# ── Pull workspace Docker images ───────────────────────────────────
# Runs before `dev` / `dev-local` so first-time `ensure_workspace_runtime`
# doesn't time out while Docker is still fetching images. Idempotent:
# subsequent invocations are near-instant when images are already cached.
pull-images:
	@echo "Pre-pulling workspace runtime images (idempotent; cached pulls are fast)..."
	docker pull lscr.io/linuxserver/chromium:latest

# ── Build Theia IDE base image ─────────────────────────────────────────
# Minimal Theia with Explorer + Search + Editor. First build is slow
# (yarn install + theia build ~5-10 min); subsequent builds are cached.
build-ide: packages/ide/Dockerfile
	@echo "Building polaris/ide:latest (Theia IDE base)..."
	docker build -t polaris/ide:latest packages/ide

# ── Build workspace image (IDE base + dev toolchain + Codex) ──────────
# Docker layer cache makes re-runs near-instant when the Dockerfile and its
# inputs haven't changed. Build context is `infra/` so the Dockerfile can
# COPY in both `workspace/*` and `publish-templates/*`.
build-workspace: build-ide infra/workspace/Dockerfile
	@echo "Building polaris/workspace:latest (Theia + dev toolchain)..."
	docker build -t polaris/workspace:latest -f infra/workspace/Dockerfile infra

# ── Build custom chromium-vnc image (linuxserver/chromium + nginx CDP proxy) ──
# Chromium M111+ binds CDP to 127.0.0.1 only AND rejects non-localhost Host
# headers (DNS-rebinding protection). An nginx server block on :9223 inside
# the image proxies to :9222, rewrites the upstream Host to "localhost:9222",
# and sub_filters the webSocketDebuggerUrl in `/json` responses so Playwright
# reconnects through the proxy. See infra/chromium/cdp-proxy.conf.
build-chromium: infra/chromium/Dockerfile infra/chromium/cdp-proxy.conf
	@echo "Building polaris/chromium-vnc:latest (chromium + nginx cdp-proxy)..."
	docker build -t polaris/chromium-vnc:latest infra/chromium

# ── Individual services ────────────────────────────────────────────
api: apps/api/.venv/.bootstrapped
	cd apps/api && .venv/bin/uvicorn polaris_api.main:app --reload --host 0.0.0.0 --port 8000

worker: apps/worker/.venv/.bootstrapped
	cd apps/worker && .venv/bin/polaris-worker

web: bootstrap-node
	pnpm dev:web

# ── Full dev stack (with Docker infra) ─────────────────────────────
dev: bootstrap welcome-page preflight-ports pull-images build-workspace build-chromium infra migrate check-process-compose
	process-compose up

# ── Dev stack (using existing local Postgres + Redis) ──────────────
dev-local: bootstrap welcome-page preflight-ports pull-images build-workspace build-chromium migrate check-process-compose
	process-compose up

# ── Staging prep (supervisord-managed; NOT a running stack) ─────────
# One-shot "prep everything" for staging hosts where api / worker / web
# run under Supervisor rather than process-compose.  Builds every image,
# pre-pulls externals, brings infra up, runs migrations, builds the web
# bundle, and brings previously-published per-project stacks back up
# (they were stopped by `make stop`; `make staging` is the re-up path).
# Does NOT start api / worker / web — that's Supervisor's job.
# Safe to re-run after `git pull`; every sub-target is dep-aware or idempotent.
staging: bootstrap welcome-page pull-images build-workspace build-chromium infra migrate
	@echo "Building web bundle (apps/web/dist)..."
	@pnpm --filter @polaris/web build
	@echo "Starting polaris-web nginx sidecar..."
	docker compose -f $(WEB_COMPOSE) up -d
	@$(MAKE) --no-print-directory start-published
	@echo ""
	@echo "================================================================="
	@echo " Staging prep complete."
	@echo " Next: configure /etc/supervisor/conf.d/polaris-{api,worker}.conf"
	@echo "       (see STAGING.md §4), then:"
	@echo "         sudo supervisorctl reread && sudo supervisorctl update"
	@echo "         sudo supervisorctl start polaris-api polaris-worker"
	@echo "================================================================="

# ── Bring published per-project stacks up ─────────────────────────────
# Walks $(PUBLISH_PROJECTS_ROOT)/<uuid>/ for every dir that has both
# compose.prod.yml and compose.polaris.yml (i.e. has been published at
# least once) and runs `docker compose up -d` with the same project
# name the publish pipeline uses: polaris-pub-<24-char-hash>.
# Idempotent — already-running stacks are a no-op.  Invoked by
# `make staging`; safe to run standalone after `make stop`.
start-published:
	@if [ ! -d "$(PUBLISH_PROJECTS_ROOT)" ]; then \
		echo "No published projects dir at $(PUBLISH_PROJECTS_ROOT) — skipping."; \
		exit 0; \
	fi; \
	found=0; \
	for dir in "$(PUBLISH_PROJECTS_ROOT)"/*/; do \
		[ -d "$$dir" ] || continue; \
		[ -f "$$dir/compose.prod.yml" ] || continue; \
		[ -f "$$dir/compose.polaris.yml" ] || continue; \
		found=1; \
		uuid=$$(basename "$$dir"); \
		hash=$$(echo "$$uuid" | tr -d '-' | cut -c1-24); \
		project="polaris-pub-$$hash"; \
		echo "▶ compose up -d ($$project)"; \
		docker compose -p "$$project" \
			-f "$$dir/compose.prod.yml" \
			-f "$$dir/compose.polaris.yml" \
			up -d || echo "  ⚠ failed to start $$project (continuing)"; \
	done; \
	if [ "$$found" = "0" ]; then \
		echo "No published projects found under $(PUBLISH_PROJECTS_ROOT)."; \
	fi

# ── Clear: wipe all workspace containers, networks, meta, and volumes ──
# Postgres and Redis are stopped + their volumes dropped, but NOT recreated —
# run `make dev` (or `make infra`) afterwards to bring them back up fresh.
# Keeps traefik / minio / registry running and keeps all built images.
# Interactive by default; use `make clear FORCE=1` to skip confirmation.
clear:
	@FORCE=$(FORCE) scripts/clear.sh

# ── Down: nuclear teardown (everything `clear` does + infra stacks) ──
# Removes every Polaris container (workspace / browser / preview / published
# / traefik / minio / postgres / redis / registry / polaris-web), every
# volume (platform + per-workspace + minio bind-mount), every network, and
# all .data/* + infra/minio/data/ contents.  Keeps built images and
# ~/.codex/auth.json.  Interactive by default; `make down FORCE=1` skips.
down:
	@FORCE=$(FORCE) scripts/down.sh

check-process-compose:
	@command -v process-compose >/dev/null 2>&1 || { \
		echo "ERROR: process-compose not found."; \
		echo "Install (Linux/macOS):"; \
		echo "  sh -c \"\$$(curl --location https://raw.githubusercontent.com/F1bonacc1/process-compose/main/scripts/get-pc.sh)\" -- -d -b ~/.local/bin"; \
		echo "Or: brew install process-compose"; \
		exit 1; \
	}

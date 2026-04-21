# Polaris API container — built from the monorepo root so we can pull in
# editable workspace packages.  Source code is bind-mounted at runtime
# (see compose.dev.yaml); this image only freezes the dep graph.
#
#   docker build -t polaris/api:dev -f apps/api/Dockerfile .

FROM python:3.12-slim

# uv pulls deps fast and respects pyproject.toml directly.  Pin so build
# reproducibility doesn't drift with upstream.
COPY --from=ghcr.io/astral-sh/uv:0.11.9 /uv /uvx /usr/local/bin/

# Docker CLI + compose plugin from Docker's official slim image.  We
# can't use Debian bookworm's `docker.io` package because it only
# ships dockerd + docker-proxy (the daemon side); the actual `docker`
# CLI binary is in a separate package that's been renamed across
# releases.  Pulling from docker:cli is stable and gets us both the
# `docker` CLI and the `docker compose` v2 plugin in one shot.
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker:27-cli /usr/local/libexec/docker/cli-plugins/ /usr/local/libexec/docker/cli-plugins/

# Runtime deps:
#   git              — publish pipeline reads commit SHAs
#   curl             — health probes / image fetches in dev_deps
#   build-essential  — wheels that need a compiler at install (asyncpg)
#   chromium         — headless screenshot capture in publish pipeline
#                       (services/publish_screenshot.py).  --headless=new
#                       requires Chromium ≥ 112; bookworm ships 130+.
#   fonts-noto-cjk   — Chinese / Japanese / Korean glyphs in screenshots
#   fonts-liberation — Helvetica/Arial fallbacks for Latin sites
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        build-essential \
        chromium \
        fonts-noto-cjk \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Publish pipeline runs `git -C <host-mounted workspace>` from this
# container as root, but the workspace dirs on disk are owned by uid
# 1000 (the codex container's user).  git 2.35.2+ refuses to operate
# on a repo whose .git ownership doesn't match the running uid (CVE
# 2022-24765 mitigation).  System-wide ``safe.directory=*`` opts out
# of that check — we trust everything under our own bind-mounts.
RUN git config --system --add safe.directory '*'

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/apps/api/src:/app/packages/agent-core/src

# Copy ONLY the dep manifests first so a code edit doesn't bust the
# wheel cache.  The actual source comes in via bind-mount at runtime.
WORKDIR /app
COPY apps/api/pyproject.toml apps/api/pyproject.toml
COPY packages/agent-core/pyproject.toml packages/agent-core/pyproject.toml

# Install api + agent-core editable into a system-wide venv.  At runtime
# the bind-mount overlays /app with the live source — `pip install -e`
# means imports follow the bind-mount, no rebuild needed on code edits.
RUN uv venv /opt/venv \
    && . /opt/venv/bin/activate \
    && uv pip install -e ./packages/agent-core -e ./apps/api

ENV PATH="/opt/venv/bin:${PATH}" \
    VIRTUAL_ENV=/opt/venv

# Entrypoint re-registers the editable installs at the runtime bind-mount
# path (POLARIS_HOST_REPO_ROOT), which differs from the build-time /app
# location.  See entrypoint.sh for the why.
COPY apps/api/entrypoint.sh /usr/local/bin/polaris-api-entrypoint
RUN chmod +x /usr/local/bin/polaris-api-entrypoint
ENTRYPOINT ["/usr/local/bin/polaris-api-entrypoint"]

# uvicorn --reload-dir paths are absolute against the bind-mounted repo
# (POLARIS_HOST_REPO_ROOT == ${PWD} on the host).  Wrapper script
# expands those at runtime so the CMD stays static.
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn polaris_api.main:app --host 0.0.0.0 --port 8000 --reload --reload-dir ${POLARIS_HOST_REPO_ROOT}/apps/api/src --reload-dir ${POLARIS_HOST_REPO_ROOT}/packages/agent-core/src"]

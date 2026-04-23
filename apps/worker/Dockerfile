# Polaris worker container — drives the discovery / codex / publish
# pipelines.  Like the api, source is bind-mounted at runtime; this
# image freezes the dep graph only.
#
#   docker build -t polaris/worker:dev -f apps/worker/Dockerfile .

FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.9 /uv /uvx /usr/local/bin/

# Docker CLI + compose plugin (see api/Dockerfile for why we don't use
# bookworm's docker.io).
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker:27-cli /usr/local/libexec/docker/cli-plugins/ /usr/local/libexec/docker/cli-plugins/

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Worker runs `git init` (via ``_ensure_project_git``) inside this
# container as root, against a bind-mounted host dir owned by uid
# 1000.  Same safe.directory carve-out as the api Dockerfile — all
# workspace bind-mounts under our control are trusted.
RUN git config --system --add safe.directory '*'

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/apps/worker/src:/app/packages/agent-core/src:/app/packages/design-intent/src:/app/apps/api/src

WORKDIR /app
COPY apps/worker/pyproject.toml apps/worker/pyproject.toml
COPY apps/api/pyproject.toml apps/api/pyproject.toml
COPY packages/agent-core/pyproject.toml packages/agent-core/pyproject.toml
COPY packages/design-intent/pyproject.toml packages/design-intent/pyproject.toml

# Worker depends on agent-core, design-intent, api, and worker — install
# all four editable in dependency order.
RUN uv venv /opt/venv \
    && . /opt/venv/bin/activate \
    && uv pip install \
         -e ./packages/agent-core \
         -e ./packages/design-intent \
         -e ./apps/api \
         -e ./apps/worker

ENV PATH="/opt/venv/bin:${PATH}" \
    VIRTUAL_ENV=/opt/venv

# Entrypoint re-registers editable installs against the bind-mount path —
# see apps/api/Dockerfile + apps/worker/entrypoint.sh for rationale.
COPY apps/worker/entrypoint.sh /usr/local/bin/polaris-worker-entrypoint
RUN chmod +x /usr/local/bin/polaris-worker-entrypoint
ENTRYPOINT ["/usr/local/bin/polaris-worker-entrypoint"]

# polaris-worker is the console_scripts entry; the entrypoint above
# regenerates it pointing at the bind-mount source before exec.
# No --reload (worker has no native reload mode); restart the container
# to pick up code changes.  Compose `restart: unless-stopped` covers crashes.
CMD ["polaris-worker"]

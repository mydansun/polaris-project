# Polaris workspace runtime — Theia IDE + dev toolchain + Codex.
#
# Base: polaris/ide:latest (custom Theia with Explorer + Search + Editor)
# Layers: Node 24, Python 3, supervisor, Codex, Playwright MCP, polaris CLIs
#
# Build:  make build-workspace
# Use:    POLARIS_WORKSPACE_IMAGE=polaris/workspace:latest

FROM polaris/ide:latest

USER root

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        wget \
        gnupg \
        git \
        unzip \
        zip \
        jq \
        ripgrep \
        python3 \
        python3-venv \
        python3-pip \
        supervisor \
        build-essential \
        tzdata \
        inotify-tools; \
    curl -fsSL https://deb.nodesource.com/setup_24.x | bash -; \
    apt-get install -y --no-install-recommends nodejs; \
    corepack enable; \
    # Install Codex (@openai/codex) and Playwright MCP globally.  Pin nothing
    # — the image is rebuilt via `make build-workspace` which picks up the
    # latest.  Use a throwaway cache dir so npm's root-owned cache doesn't
    # end up at /home/workspace/.npm (that dir is the runtime user's home
    # and must stay uid-1000-writable for `npm install` inside the agent).
    npm install -g --cache /tmp/npm-root-cache --no-fund --no-audit \
        @openai/codex @playwright/mcp; \
    # `--cache` redirects _cacache but npm still writes _logs and
    # _update-notifier-last-checked to $HOME/.npm as root.  Blow the whole
    # thing away — the runtime user will recreate it with the right uid
    # on first `npm install`.
    rm -rf /tmp/npm-root-cache /root/.npm /home/workspace/.npm; \
    # Sanity: fail the build loudly if any key tool is missing.
    node -v; \
    npm -v; \
    python3 --version; \
    supervisord --version; \
    which supervisorctl; \
    which codex; \
    codex --version; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

# Build context is the `infra/` directory (see `make build-workspace`), so
# COPY paths are relative to infra/, not infra/workspace/.

# Override the default supervisord config so socket/pid/log live in /tmp
# (user-writable) and let the non-root user drop program configs into
# /etc/supervisor/conf.d.
COPY workspace/supervisord.conf /etc/supervisor/supervisord.conf

# Supervisor program for codex app-server.  Ships in the base image so every
# workspace container starts a ready-to-use JSON-RPC endpoint without the
# worker having to write it itself.
COPY workspace/codex-app-server.conf /etc/supervisor/conf.d/codex-app-server.conf

# Default Codex config.  Seeded into the codex-home named volume on first
# mount (docker populates empty named volumes from the image contents).  The
# playwright MCP reads its CDP endpoint from a side-by-side JSON config.
RUN mkdir -p /home/workspace/.codex /etc/codex /workspace \
    /home/workspace/.agents/skills
COPY workspace/codex-config.toml /home/workspace/.codex/config.toml
COPY workspace/codex-playwright.json /etc/codex/playwright.json
# Filesystem-discovered skills for the OpenAI Agents SDK / Codex.  Each
# subfolder is one skill; Codex loads SKILL.md on thread start.
COPY workspace/skills/ /home/workspace/.agents/skills/

# Both IDE bases use uid 1000.  chown by uid so it works regardless of username.
RUN chown -R 1000:1000 /etc/supervisor/conf.d /home/workspace/.codex \
    /home/workspace/.agents /workspace

# Wrapper entrypoint: starts supervisord in the background, then execs
# the IDE (auto-detects Theia or OpenVSCode Server).
COPY workspace/polaris-workspace-entrypoint.sh /usr/local/bin/polaris-workspace-entrypoint
RUN chmod 0755 /usr/local/bin/polaris-workspace-entrypoint

# polaris-bg — CLI sugar for the Codex agent to run dev servers / watchers /
# anything long-lived under supervisord, instead of fighting with `nohup`
# and `& disown` tricks that die when Codex's exec_command returns.
COPY workspace/polaris-bg /usr/local/bin/polaris-bg
RUN chmod 0755 /usr/local/bin/polaris-bg

# polaris — publish CLI the agent uses to scaffold Dockerfiles, audit for
# secrets, and kick off builds against the platform's publish worker.
COPY workspace/polaris-cli/polaris.py /usr/local/bin/polaris
RUN chmod 0755 /usr/local/bin/polaris

# Publish templates (Dockerfile / compose.prod.yml / polaris.yaml per stack).
# The `polaris scaffold-publish` command copies from here into the user repo.
COPY publish-templates /opt/polaris-publish-templates

# Switch to the non-root IDE user (uid 1000 in both bases).
USER 1000

EXPOSE 4455

ENTRYPOINT ["/usr/local/bin/polaris-workspace-entrypoint"]

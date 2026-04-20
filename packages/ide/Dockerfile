# Polaris IDE — minimal Theia (browser target: Explorer + Search + Editor).
#
# Three-stage build:
#   Stage 1 (builder)  — yarn install + compile + bundle
#   Stage 2 (runtime)  — slim image with only production artifacts
#   Stage 3 (test)     — start Theia + run Playwright smoke tests
#   Final              — clean runtime (test stage artifacts discarded)
#
# If the Playwright tests fail, the Docker build fails → `make build-ide` errors out.
#
# Build:  make build-ide
# Use:    As the base image for infra/workspace/Dockerfile

# ── Stage 1: Build ────────────────────────────────────────────────────
FROM node:22-bookworm AS builder

# Native compilation deps for Theia (native-keymap, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 make g++ pkg-config \
        libsecret-1-dev libx11-dev libxkbfile-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY package.json tsconfig.json ./
COPY src/ src/
COPY scripts/ scripts/

RUN yarn install --network-timeout 600000

# Compile custom TS/TSX → lib/, generate Theia src-gen, inject custom
# modules, copy static assets to lib/frontend, then webpack bundle.
RUN yarn build

# Verify critical outputs
RUN test -f src-gen/backend/main.js && \
    test -f lib/frontend/index.html

# Strip devDependencies for a smaller runtime copy
RUN yarn install --production && \
    yarn cache clean && \
    rm -rf /tmp/*

# ── Stage 2: Runtime ──────────────────────────────────────────────────
FROM node:22-bookworm-slim AS runtime

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git libsecret-1-0 libx11-6 libxkbfile1 && \
    rm -rf /var/lib/apt/lists/*

# Ensure a uid-1000 user exists.
RUN if id -u 1000 >/dev/null 2>&1; then \
        existing=$(getent passwd 1000 | cut -d: -f1); \
        usermod -l workspace -d /home/workspace -m "$existing" 2>/dev/null || true; \
    else \
        useradd -m -u 1000 -s /bin/bash workspace; \
    fi && \
    mkdir -p /home/workspace && chown 1000:1000 /home/workspace

WORKDIR /app
COPY --from=builder /app /app
RUN chown -R 1000:1000 /app

USER 1000
EXPOSE 3000

ENV HOME=/home/workspace

ENTRYPOINT ["node", "/app/src-gen/backend/main.js"]
CMD ["--hostname", "0.0.0.0", "--port", "3000", "/workspace"]

# ── Stage 3: Test (Playwright smoke) ──────────────────────────────────
FROM runtime AS test

USER root

# Install curl (readiness probe), Playwright + Chromium with system deps.
# --ignore-scripts skips node-gyp (no python3/gcc in slim image).
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    cd /app && npm install --ignore-scripts playwright && \
    npx playwright install --with-deps chromium && \
    rm -rf /var/lib/apt/lists/*

COPY tests/ /app/tests/

# Start Theia in background as uid 1000, wait for ready, run tests.
RUN mkdir -p /tmp/test-workspace && chown 1000:1000 /tmp/test-workspace && \
    su -s /bin/sh -c \
      'node /app/src-gen/backend/main.js --hostname 127.0.0.1 --port 3000 /tmp/test-workspace &' \
      "$(getent passwd 1000 | cut -d: -f1 || echo 1000)" && \
    timeout 30 sh -c 'until curl -sf http://127.0.0.1:3000/ >/dev/null 2>&1; do sleep 1; done' && \
    echo "Theia is up, running tests..." && \
    node /app/tests/ide.spec.mjs

# ── Final output: clean runtime without test deps ─────────────────────
# COPY from test stage forces Docker to actually run it.  The file
# itself is a no-op sentinel; it just creates the build dependency.
FROM runtime AS final
COPY --from=test /app/tests/ide.spec.mjs /dev/null

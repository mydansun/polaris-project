# Polaris web (vite dev server) — bind-mount source for HMR.
# Built from the monorepo root because pnpm workspaces share a lockfile.
#
#   docker build -t polaris/web:dev -f apps/web/Dockerfile .

FROM node:22-alpine

# pnpm via corepack ships with node:22.
RUN corepack enable && corepack prepare pnpm@latest --activate

WORKDIR /app

# Cache the install layer: copy only manifests + lockfile first.  The
# real source is bind-mounted at runtime so saves to apps/web/src/* are
# reflected in the running vite server without rebuilding.
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY apps/web/package.json apps/web/package.json
COPY packages/ui/package.json packages/ui/package.json
COPY packages/shared-types/package.json packages/shared-types/package.json
COPY packages/welcome-page/package.json packages/welcome-page/package.json

# `pnpm install --frozen-lockfile` resolves the workspace graph; the
# resulting node_modules is a pile of symlinks that the bind-mount over
# /app at runtime preserves (because we mount over the dirs that don't
# need build artifacts, not the whole tree — see compose.dev.yaml).
RUN pnpm install --frozen-lockfile

# vite dev server: --host 0.0.0.0 lets traefik on a sibling container
# reach it; HMR clientPort is set in vite.config.ts based on
# POLARIS_DOMAIN env at runtime.
EXPOSE 5173
WORKDIR /app/apps/web
CMD ["pnpm", "dev"]

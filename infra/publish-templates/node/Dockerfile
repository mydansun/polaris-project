# Polaris node publish template — multi-stage.
# Rendered into user repo by `polaris scaffold-publish` when stack=node.
# Codex may tweak build/start commands; the image layout should stay.

FROM node:22-alpine AS builder
WORKDIR /app
COPY package.json package-lock.json* pnpm-lock.yaml* yarn.lock* ./
# Try pnpm → yarn → npm in order of lockfile presence.
RUN set -eux; \
    if [ -f pnpm-lock.yaml ]; then corepack enable && pnpm install --frozen-lockfile; \
    elif [ -f yarn.lock ]; then corepack enable && yarn install --frozen-lockfile; \
    else npm ci; \
    fi
COPY . .
RUN __POLARIS_BUILD_CMD__

FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
# Put project-local binaries (next, vite, tsc, ...) on PATH so CMD can
# invoke them directly without npm/npx wrappers.  Standard Node.js
# image practice; container is single-tenant so shadowing of system
# bins isn't a meaningful attack surface (any compromised devDep can
# already run via build scripts).
ENV PATH=/app/node_modules/.bin:$PATH
COPY --from=builder /app ./
EXPOSE __POLARIS_PORT__
CMD __POLARIS_START_CMD_JSON__

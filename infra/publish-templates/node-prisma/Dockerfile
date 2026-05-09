# Polaris node-prisma publish template — multi-stage.
# Rendered into user repo by `polaris scaffold-publish` when the project
# carries a `prisma/schema.prisma`.  Differs from the plain `node`
# template by:
#   1. Copying `prisma/` BEFORE the install step so any postinstall
#      `prisma generate` hook can locate the schema (without this the
#      hook silently emits an empty client and the app crashes at
#      runtime trying to query).
#   2. Running an explicit `npx prisma generate` after install — pnpm
#      skips app-level postinstall hooks by default, and many Next.js
#      starters omit the hook altogether.  Idempotent.

FROM node:22-alpine AS builder
WORKDIR /app
COPY package.json package-lock.json* pnpm-lock.yaml* yarn.lock* ./
COPY prisma ./prisma
# Try pnpm → yarn → npm in order of lockfile presence.
RUN set -eux; \
    if [ -f pnpm-lock.yaml ]; then corepack enable && pnpm install --frozen-lockfile; \
    elif [ -f yarn.lock ]; then corepack enable && yarn install --frozen-lockfile; \
    else npm ci; \
    fi
RUN npx prisma generate
COPY . .
RUN __POLARIS_BUILD_CMD__

FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
# Put project-local binaries (next, vite, tsc, prisma, ...) on PATH so
# CMD can invoke them directly without npm/npx wrappers.  Container is
# single-tenant; shadowing of system bins isn't a meaningful surface.
ENV PATH=/app/node_modules/.bin:$PATH
COPY --from=builder /app ./
EXPOSE __POLARIS_PORT__
CMD __POLARIS_START_CMD_JSON__

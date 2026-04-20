# Polaris spa publish template — Vite / Astro / React-SPA-shaped apps.
# Node stage runs `npm run build` → static dist/.  Nginx stage serves
# dist/ with client-side routing fallback so deep-links like /settings
# refresh to /index.html instead of 404.

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

FROM nginx:1.27-alpine AS runner
# Replace the default server block with an SPA-aware one so deep-links
# (e.g. /settings) refresh to /index.html instead of 404.
RUN printf 'server {\n\
  listen 80;\n\
  server_name _;\n\
  root /usr/share/nginx/html;\n\
  index index.html;\n\
  location / {\n\
    try_files $uri $uri/ /index.html;\n\
  }\n\
}\n' > /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist /usr/share/nginx/html
EXPOSE 80

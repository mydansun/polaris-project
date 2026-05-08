# Staging Deployment

Run Polaris on a dedicated host, bound to **your own domain**, deployed
under the **UID 1000 user's home directory** so docker bind mounts line
up without chowns.

Scope of this doc:

1. Rebinding the platform to a domain other than the default
   `polaris-dev.xyz` — every `.env.stage` knob involved.
2. Operational notes specific to a shared / staging host (hardening,
   backup, failure signatures).

`./scripts/up.py stage` brings up the stage stack from
`compose.stage.yaml` (nginx-served web bundle, no `--reload` on api,
project name `polaris-stage`).  Traefik handles DNS-01 ACME for your
domain identically to dev.  Day-to-day command shape mirrors
[DEVELOPMENT.md](./DEVELOPMENT.md) — just append `stage` to every
`up.py` / `down.py` invocation.

---

## ⚠️ Not production-ready

**This project does not support production-grade deployment.**  Several
security boundaries are unexplored / unhardened:

- Traefik dashboard on `:8090` has no authentication.
- Local Docker registry at `127.0.0.1:5000` has no auth (bound to
  loopback — don't expose).
- Workspace containers mount host `~/.codex/auth.json` read-write —
  users share one Codex account.
- The platform api + worker containers drive the host's docker daemon
  (via the bind-mounted `/var/run/docker.sock`) to spawn workspace +
  published-project compose stacks.  Host-level docker access is
  root-equivalent.  Workspace containers themselves do **not** see the
  socket — `polaris dev-up postgres` etc. round-trip through the api,
  which calls docker on the host.  Traefik mounts the socket read-only
  for service discovery.
- Publish pipeline runs user-generated compose on the same host with
  only a `ports:` sanitizer.  No container-escape / noisy-neighbor
  defense beyond docker defaults.
- `POLARIS_MAX_*_RUNS` caps OpenAI cost; it is **not** a security
  boundary.
- `POLARIS_INVITE_CODE` is the only sign-up gate.  If leaked, anyone
  can spawn a workspace.

**Recommended**: controlled environments only — internal dogfood,
trusted collaborators, CI / demo hosts firewalled to known IPs.  Do
not expose Polaris to untrusted traffic.

---

## 1. Rebind to your own domain

Every domain reference in the running system is driven by
`${POLARIS_DOMAIN}` (and the `prod.` / `s3.` derivatives).  Traefik
does ACME DNS-01 against Cloudflare for all three certificate
groups, so there are no cert files to maintain by hand.

Assume your domain is `example.com`.  Three zones must resolve to the
staging host's public IP (or a LAN IP for a closed setup):

| Zone | Content |
|---|---|
| `example.com` + `*.example.com` | Platform root (web + `/api`) and per-workspace IDE / browser subdomains |
| `prod.example.com` + `*.prod.example.com` | Published user projects (`<uuid>.prod.example.com`) |
| `s3.example.com` + `*.s3.example.com` | MinIO endpoints (path-style + virtual-host bucket addressing) |

### 1.1 `.env.stage` — every field that mentions the domain

```bash
# Platform domain (used by every Traefik label via ${POLARIS_DOMAIN}
# interpolation in compose.dev.yaml + agent prompts + compose label
# rendering).
POLARIS_DOMAIN=example.com

# Publish plane — individual projects land at <uuid>.prod.example.com
POLARIS_PROD_DOMAIN_BASE=prod.example.com

# Web writes signed cookies against FRONTEND_URL; CORS must match.
FRONTEND_URL=https://example.com
POLARIS_CORS_ORIGINS=["https://example.com"]

# URL templates written to the DB per workspace; the frontend reads them.
POLARIS_IDE_PUBLIC_URL_TEMPLATE=https://ide-{workspaceHash}.example.com
POLARIS_BROWSER_PUBLIC_URL_TEMPLATE=https://browser-{workspaceHash}.example.com

# S3 / MinIO — MinIO advertises these as the public URLs.
S3_ENDPOINT=https://s3.example.com
S3_URL_BASE=https://polaris.s3.example.com

# Pinterest MCP — your own instance.
POLARIS_PINTEREST_TOOL_BASE=http://pinterest-mcp.internal:9801

# Frontend reads VITE_API_BASE_URL at build time; keep it relative so
# the bundle works behind any domain (Traefik routes /api/* → api:8000).
VITE_API_BASE_URL=/api
```

### 1.2 Cloudflare DNS-01 token

Traefik issues every cert via Cloudflare DNS-01 — no `certbot` runs on
the host, no `/etc/letsencrypt/` to mount.  Provision an API token
scoped to **DNS:Edit** on the parent zone (and `prod.` + `s3.` if
they're separate zones), and put it in `.env.stage`:

```bash
CF_API_TOKEN=<cloudflare DNS-edit token>
ACME_EMAIL=admin@example.com
```

`./scripts/up.py stage` validates the token live before bringing the
stack up.  First issue takes ~30–60s per cert; renewals run automatically.

### 1.3 Other secrets

```
SESSION_SECRET=<openssl rand -hex 48>
POLARIS_INVITE_CODE=<any string>                   # sign-up gate
# Leave these TWO EMPTY on staging.  Filling them in turns on a
# one-click dev-login that bypasses email verification — when unset,
# /auth/dev-login 404s and the "Dev Login" button is hidden.
POLARIS_DEV_USER_EMAIL=
POLARIS_DEV_USER_NAME=
OPENAI_SECRET=sk-...                               # required for discovery / clarifier / mood board
POSTMARK_SERVER_TOKEN=<postmark token>             # required; otherwise verification codes log to api stdout
POSTMARK_MESSAGE_STREAM=outbound
POSTMARK_FROM_EMAIL=noreply@example.com
S3_ACCESS_KEY_ID=polaris
S3_SECRET_ACCESS_KEY=<openssl rand -hex 32>        # MinIO root password too — wizard reuses this
S3_BUCKET=polaris
POLARIS_MAX_GLOBAL_RUNS=6
POLARIS_MAX_USER_RUNS=2
POLARIS_CODEX_TURN_TIMEOUT_SECONDS=900
```

Full reference: [CONFIGURATION.md](./CONFIGURATION.md).

---

## 2. Deploy layout: run as the UID 1000 user

Deploy under the **UID 1000 user's home directory**.  Two reasons:

- **`/opt/` is root-owned** by default.  The platform needs to
  read-write `.data/` (published project state, certs, project
  archives) and per-workspace meta under the repo root — root-owned
  paths just add friction.
- **Workspace + IDE container images run as UID 1000** (see
  `infra/workspace/Dockerfile` `USER 1000` + `packages/ide/Dockerfile`
  `USER 1000`).  Anything those containers bind-mount from the host
  (the workspace volume, `~/.codex/auth.json`, mood board writes)
  lines up permission-wise without a chown dance when the host path
  is owned by host UID 1000.

On most cloud VMs the first interactive user is already UID 1000
(Ubuntu's `ubuntu`, Debian's `admin`, Fedora's `fedora`).  No new
account is needed — verify and reuse it:

```bash
id -u                                              # must print 1000
groups | grep -qw docker && echo "docker group OK"
# If not in docker group: sudo usermod -aG docker $USER && exec newgrp docker
```

As that user, clone + configure the repo:

```bash
cd ~
git clone <repo> polaris-2
cd polaris-2
cp .env.example .env.stage                         # fill in per §1
chmod 600 .env.stage                               # secrets live here
codex login                                        # workspace containers bind-mount ~/.codex/auth.json
```

---

## 3. Bring it up

```bash
./scripts/up.py stage          # interactive wizard the first time; rebuilds the stage stack
./scripts/build.py             # build polaris/{ide,workspace,chromium-vnc}:latest workspace images
docker compose -f compose.stage.yaml exec api alembic upgrade head
```

`./scripts/up.py stage --non-interactive` is the CI / scripted path —
fails fast on any missing required env, never prompts.

Health checks once Traefik has issued its certs:

```bash
curl https://example.com/api/health   # {service: "polaris-api", status: "ok"}
curl https://example.com/api/ready    # {database: "ok", redis: "ok"}
```

---

## 4. Upgrade flow

```bash
cd ~/polaris-2
git pull
./scripts/build.py             # rebuild only stale workspace images
./scripts/up.py stage          # rebuild + restart api/worker/web containers
docker compose -f compose.stage.yaml exec api alembic upgrade head
```

`./scripts/up.py stage` re-runs `docker compose up -d --build`, which
recreates only services whose image or config changed.  Bind-mounted
source under `apps/*` and `packages/*` is picked up live without an
image rebuild for api / worker — but stage's api CMD has `--reload`
removed, so code edits need an explicit `docker compose -f
compose.stage.yaml restart api`.  The web bundle is fully baked into
`polaris/web:stage`, so frontend edits require a `docker compose -f
compose.stage.yaml build web` + `up -d`.

Workspace runtime image rebuilds don't affect already-running user
containers; they keep the old image until the next session.  To flush
every running session: `./scripts/down.py stage --clear` before
`./scripts/up.py stage`.

---

## 5. Backup and restore

Back up to an off-host location (another host, S3, whatever your
environment allows).  A staging host is a single point of failure.

### Postgres

```bash
docker compose -f compose.stage.yaml exec -T postgres \
  pg_dump -U root -d polaris > ~/backups/polaris-$(date +%F).sql
# restore:
docker compose -f compose.stage.yaml exec -T postgres psql -U root -d polaris \
  < ~/backups/polaris-<date>.sql
```

### MinIO

MinIO data lives in a bind-mount at `infra/minio/data/` (owned by the
MinIO container's UID).  Snapshot via a throwaway container so the
running MinIO doesn't have to stop:

```bash
docker run --rm \
  -v $HOME/polaris-2/infra/minio/data:/data:ro \
  -v $HOME/backups:/out \
  alpine tar -czf /out/minio-$(date +%F).tgz -C /data .
```

### Published project state

Each published project owns `~/polaris-2/.data/projects/<uuid>/`:

- `archives/<short-hash>.tar.gz` — frozen source per version
- `secrets.env` — per-project DB credentials + session secret
- `compose.prod.yml` + `compose.polaris.yml` — materialised compose

Back up the whole `.data/projects/` tree.  On restore, already-running
prod containers keep running (images cached locally + in the registry);
the first new `compose up` for each project after restore reads back
the restored state.

### Redis

Transient — skip.  Lost state means in-flight sessions don't resume;
new sessions work.

### Cron (optional)

```bash
mkdir -p ~/backups
(crontab -l 2>/dev/null; cat <<'EOF'
0 3 * * * docker compose -f $HOME/polaris-2/compose.stage.yaml exec -T postgres pg_dump -U root -d polaris > ~/backups/polaris-$(date +\%F).sql
10 3 * * * docker run --rm -v $HOME/polaris-2/infra/minio/data:/data:ro -v $HOME/backups:/out alpine tar -czf /out/minio-$(date +\%F).tgz -C /data .
30 3 * * * find ~/backups -mtime +14 -delete
EOF
) | crontab -
```

(Running `docker exec` / `docker run` from user cron works because
the deploy user is in the `docker` group.)

---

## 6. Operations

### 6.1 Logs

| Source | Location |
|---|---|
| api | `docker compose -f compose.stage.yaml logs api -f` |
| worker | `docker compose -f compose.stage.yaml logs worker -f` |
| web | `docker compose -f compose.stage.yaml logs web -f` |
| Per-workspace container | `docker logs polaris-ws-<hash>` / `polaris-br-<hash>` |
| Per-published container | `docker logs polaris-pub-<projid>-web-1` |
| Publish pipeline | DB `deployments.build_log` / `smoke_log`; streamed via SSE to `GET /deployments/{id}/events` and to `polaris publish` stdout inside the workspace |
| Traefik | `docker logs polaris-traefik-1` + `http://<host>:8090/dashboard/` |

### 6.2 Common failure signatures

| Symptom | Likely cause | Where to look |
|---|---|---|
| api / worker container restart-looping | Bad `.env.stage` / missing secret | `docker compose -f compose.stage.yaml logs api` (captures startup traceback) |
| Traefik 404 on platform root | Compose label rule didn't pick up `${POLARIS_DOMAIN}` change | `./scripts/down.py stage && ./scripts/up.py stage` after `.env.stage` edits — labels are baked at create time |
| Traefik 404 on `ide-*.example.com` | Workspace container died or never joined `polaris-shared` | `docker logs polaris-ws-<hash>` |
| Session stuck in `queued` | Worker crashed | `docker compose -f compose.stage.yaml ps worker` + tail logs |
| Publish `smoke probe never succeeded` | User container crash during startup; real cause is in the web container logs (auto-captured into `smoke_log`) | PublishPanel live log → "captured tail of `<svc>` container logs" section |
| Users pile at "queued" | `POLARIS_MAX_GLOBAL_RUNS` hit | Bump in `.env.stage`, then `./scripts/up.py stage` to recreate api / worker |

### 6.3 Clean slate

```bash
./scripts/down.py stage --clear         # drop platform pg/redis + workspace state (interactive)
./scripts/down.py stage --clear --force # non-interactive
```

`--clear` keeps built images and the local docker registry.  For a
full wipe (every Polaris container + every volume + every bind-mount
data dir, keeping only `~/.codex/auth.json`):

```bash
./scripts/down.py stage --nuclear
```

### 6.4 Stopping without losing state

```bash
./scripts/down.py stage
```

Removes containers, keeps every named volume + the `.data/` tree.
The next `./scripts/up.py stage` recreates from existing volumes in seconds.

---

## 7. Hardening checklist

Before pointing real users at the staging host:

- **Enable the host firewall.  Only open 80 and 443 inbound.** Every
  other port the platform binds (8090 Traefik dashboard, 5000 local
  registry, 5432 Postgres) is either unauthenticated, admin-only, or
  only safe for loopback.

  With ufw:

  ```bash
  sudo ufw default deny incoming
  sudo ufw default allow outgoing
  sudo ufw allow 22/tcp              # SSH — scope to your admin IPs in prod
  sudo ufw allow 80/tcp
  sudo ufw allow 443/tcp
  sudo ufw enable
  sudo ufw status verbose
  ```

  Equivalent with `firewalld`:

  ```bash
  sudo firewall-cmd --set-default-zone=public
  sudo firewall-cmd --permanent --add-service=http
  sudo firewall-cmd --permanent --add-service=https
  sudo firewall-cmd --permanent --add-service=ssh
  sudo firewall-cmd --reload
  ```

  Cloud provider security groups should mirror the same policy at the
  infrastructure layer (belt + suspenders).

- `chmod 600 ~/polaris-2/.env.stage`.  It carries every credential.
  Also `chmod 700 ~` on the deploy user's home so other local users
  can't read across.

- Rotate `POLARIS_INVITE_CODE` any time you think it leaked.  Empty
  value blocks all new sign-ups as a kill switch.

- Cron a daily Postgres dump + MinIO snapshot to off-host storage.

- Monitor the Traefik dashboard + `docker stats` for runaway
  per-workspace containers.  Tune `POLARIS_MAX_*_RUNS` to your budget.

- Treat the invite code as an admin credential.

What this checklist does **not** cover (and why staging is the
recommended ceiling — see the top-of-doc warning): container escape
defense, per-workspace resource quotas, per-user Codex credentials,
authenticated docker registry, Traefik dashboard auth, tenant network
isolation.  Those are open design items.

---

## See also

- [DEVELOPMENT.md](./DEVELOPMENT.md) — local dev (`./scripts/up.py`, Dev Login, hot reload)
- [ARCHITECTURE.md](./ARCHITECTURE.md) — system design, data model, publish pipeline
- [API.md](./API.md) — REST + SSE endpoints
- [CONFIGURATION.md](./CONFIGURATION.md) — full environment variable reference
- [FRONTEND.md](./FRONTEND.md) — React architecture
- [TESTING.md](./TESTING.md) — verification procedures
- `infra/traefik/README.md` — Traefik specifics (routing, cert layout)

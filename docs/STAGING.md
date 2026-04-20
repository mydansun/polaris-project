# Staging Deployment

Run Polaris on a dedicated host, bound to **your own domain**, with
api / worker / web supervised **outside** the `make dev` /
`process-compose` loop (Supervisor, auto-restart, no `--reload`),
deployed under the **UID 1000 user's home directory** so Docker bind
mounts line up without chowns.

Scope of this doc:

1. Rebinding the platform to a domain other than the default
   `polaris-dev.xyz` — all the env knobs + DNS / TLS shape.
2. Running api / worker / web as non-dev long-lived services under
   Supervisor, owned by the host's UID 1000 user.
3. Operational notes specific to a shared / staging host.

For local dev (`make dev`, Dev Login, hot reload, self-signed certs)
see [DEVELOPMENT.md](./DEVELOPMENT.md).

---

## ⚠️ Not production-ready

**This project does not support production-grade deployment.**  Several
security boundaries are unexplored / unhardened:

- Traefik dashboard on `:8090` has no authentication.
- Local Docker registry at `127.0.0.1:5000` has no auth (bound to
  loopback — don't expose).
- Workspace containers mount host `~/.codex/auth.json` read-write —
  users share one Codex account.
- The platform api + worker processes (host-supervised by Supervisor)
  drive the host's Docker daemon directly to spawn workspace +
  published-project compose stacks.  Host-level Docker access is
  root-equivalent.  Workspace containers themselves do **not** have
  `/var/run/docker.sock` mounted — `polaris dev-up postgres` etc. go
  through the platform API, which calls Docker on the host.  Traefik
  mounts docker.sock read-only for service discovery only.
- Publish pipeline runs user-generated compose on the same host with
  only a `ports:` sanitizer.  No container-escape / noisy-neighbor
  defense beyond Docker defaults.
- `POLARIS_MAX_*_RUNS` caps OpenAI cost; it is **not** a security
  boundary.
- `POLARIS_INVITE_CODE` is the only sign-up gate.  If leaked, anyone
  can spawn a workspace.

**Recommended**: controlled environments only — internal dogfood,
trusted collaborators, CI / demo hosts firewalled to known IPs.  Do
not expose Polaris to untrusted traffic.

---

## 1. Rebind to your own domain

Assume your domain is `example.com`.  Four zones must resolve to the
staging host's public IP (or a LAN IP for a closed setup):

| Zone | Content |
|---|---|
| `example.com` | platform root (web + `/api`) |
| `*.example.com` | per-workspace IDE / browser subdomains |
| `prod.example.com` + `*.prod.example.com` | published user projects |
| `*.s3.example.com` + `s3.example.com` | MinIO endpoints |

### 1.1 `.env` — every field that mentions the domain

```bash
# Platform domain (used in agent prompts + compose label rendering)
POLARIS_DOMAIN=example.com

# Publish plane — individual projects land at <uuid>.prod.example.com
POLARIS_PROD_DOMAIN_BASE=prod.example.com

# The web UI writes signed cookies against FRONTEND_URL; CORS must match
FRONTEND_URL=https://example.com
POLARIS_CORS_ORIGINS=["https://example.com"]

# URL templates written to the DB per workspace — the frontend reads them
POLARIS_IDE_PUBLIC_URL_TEMPLATE=https://ide-{workspaceHash}.example.com
POLARIS_BROWSER_PUBLIC_URL_TEMPLATE=https://browser-{workspaceHash}.example.com

# S3 / MinIO — the MinIO container advertises these as public URLs
S3_ENDPOINT=https://s3.example.com
S3_URL_BASE=https://polaris.s3.example.com

# Pinterest MCP — your own instance or whatever you use
POLARIS_PINTEREST_TOOL_BASE=http://pinterest-mcp.internal:9801

# Frontend build reads this at compile time; keep it relative so web
# works behind any domain (Traefik routes /api/* → host:8000)
VITE_API_BASE_URL=/api
```

### 1.2 Let's Encrypt certs

Three cert pairs — wildcard SANs only match one label, so each
publish / platform / S3 zone needs its own:

```bash
sudo certbot certonly --manual --preferred-challenges dns \
  -d example.com -d "*.example.com"
sudo certbot certonly --manual --preferred-challenges dns \
  -d prod.example.com -d "*.prod.example.com"
sudo certbot certonly --manual --preferred-challenges dns \
  -d "*.s3.example.com"
```

### 1.3 Hard-coded domain references outside `.env`

A handful of configs ship with the dev-default domain baked in as a
string — they are **not** driven by `.env` and must be edited directly
when you rebind.

**Traefik certs** — `infra/traefik/dynamic/certs.yaml`:

```yaml
tls:
  certificates:
    - certFile: /etc/letsencrypt/live/example.com/fullchain.pem
      keyFile:  /etc/letsencrypt/live/example.com/privkey.pem
    - certFile: /etc/letsencrypt/live/prod.example.com/fullchain.pem
      keyFile:  /etc/letsencrypt/live/prod.example.com/privkey.pem
    - certFile: /etc/letsencrypt/live/s3.example.com/fullchain.pem
      keyFile:  /etc/letsencrypt/live/s3.example.com/privkey.pem
  stores:
    default:
      defaultCertificate:
        certFile: /etc/letsencrypt/live/example.com/fullchain.pem
        keyFile:  /etc/letsencrypt/live/example.com/privkey.pem
```

**Traefik route rules** — `infra/traefik/dynamic/main-site.yaml` has
three Host rules plus a www→apex redirect middleware.  Every one of
these must be retargeted:

- `main-api` router — `Host(\`example.com\`) && PathPrefix(\`/api/\`)`
- `main-web` router — `Host(\`example.com\`)` (routes everything non-`/api` to the nginx sidecar, service upstream `http://polaris-web:8080` via docker DNS, see §4.3)
- `main-www-redirect` router — `Host(\`www.example.com\`)`
- `redirect-www-to-apex` middleware — update **both** the `regex:` and
  the `replacement:` fields; the `\\.` escape must survive

**MinIO** — `infra/minio/compose.yaml` carries two string-level refs
that no env var overrides:

- `environment.MINIO_DOMAIN: s3.example.com` — enables MinIO's own
  virtual-host bucket routing.  Without this, `<bucket>.s3.example.com`
  requests reach MinIO but it treats the hostname as a literal bucket
  name and 400s.
- `traefik.http.routers.minio.rule=HostRegexp(\`^(.+\\.)?s3\\.example\\.com$$\`)`
  — one router matches both path-style (`s3.example.com/<bucket>/<key>`)
  and virtual-host (`<bucket>.s3.example.com`).  The `\\.` escapes
  survive the YAML and reach Traefik as `\.`; do not collapse them.

### 1.4 Other secrets (unchanged from `.env.example`)

```
SESSION_SECRET=<openssl rand -hex 48>
POLARIS_INVITE_CODE=<any string>                   # sign-up gate
# Leave these TWO EMPTY on staging — they enable a one-click dev-login
# shortcut that bypasses email verification.  When unset the route
# /auth/dev-login 404s and the "Dev Login" button is hidden.
POLARIS_DEV_USER_EMAIL=
POLARIS_DEV_USER_NAME=
OPENAI_SECRET=sk-...                               # required for discovery / clarifier / mood board
POSTMARK_SERVER_TOKEN=<postmark token>             # required; otherwise verification codes fall back to stdout
POSTMARK_MESSAGE_STREAM=outbound
POSTMARK_FROM_EMAIL=noreply@example.com
MINIO_ROOT_USER=root
MINIO_ROOT_PASSWORD=<openssl rand -hex 16>
S3_ACCESS_KEY_ID=polaris
S3_SECRET_ACCESS_KEY=<same as MINIO_ROOT_PASSWORD>
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
  read-write `.data/` (published project state), `apps/api/.venv/`,
  generated images / bundles, and per-workspace meta under the repo
  root — root-owned path creates unnecessary friction.
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

As that user, clone + configure the repo.  This doc uses
`$HOME/polaris-project` as the canonical path; absolute-path configs
later substitute `/home/ubuntu/polaris-project` — **swap `ubuntu` for
your UID 1000 user's name** in those places.

```bash
cd ~
git clone <repo> polaris-project
cd polaris-project
corepack enable
cp .env.example .env                               # fill in per §1 + §1.4
chmod 600 .env                                     # secrets live here
```

Also make sure `~/.codex/auth.json` is present (run `codex login` as
this user once).  Workspace containers bind-mount this file; missing
it means every Codex session fails on start.

---

## 3. One-time install

As your UID 1000 user, from the repo root:

```bash
make staging
```

Chains: `bootstrap` → `welcome-page` → `pull-images` → `build-ide` →
`build-workspace` → `build-chromium` → `infra` (postgres / redis /
registry / traefik / minio, with `--wait` so postgres is healthy
before the next step) → `migrate` (alembic upgrade head) →
`pnpm --filter @polaris/web build` (emits `apps/web/dist/`) → brings
up the nginx web sidecar (§4.3).

First run takes ~10 min (mostly Theia IDE build).  Re-runs after
`git pull` are dep-aware — only stale images rebuild.

**`make staging` does NOT start api / worker** — that's Supervisor's
job (§4.1, §4.2).  The nginx web sidecar **is** (re-)started by
`make staging`.  Do not run `make dev` on a staging host; it launches
a foreground `process-compose` TUI meant for interactive dev.

**Image rebuild triggers** (only when you pull new code):

- `polaris/ide` — whenever `packages/ide/Dockerfile` or its `yarn.lock` changes
- `polaris/workspace` — whenever `infra/workspace/Dockerfile`, the workspace CLI (`infra/workspace/polaris-cli/`), or any `infra/publish-templates/` file changes
- `polaris/chromium-vnc` — whenever `infra/chromium/Dockerfile` or `cdp-proxy.conf` changes

Makefile targets are dependency-aware — re-running rebuilds only
what's stale.

---

## 4. Run api / worker / web under Supervisor

Supervisor (`apt-get install supervisor` on Debian/Ubuntu;
`dnf install supervisor` on Fedora/RHEL) is a simple process supervisor
that runs as root, launches child processes as configured users, and
handles restart-on-crash + log rotation out of the box.  This replaces
`process-compose up` for staging.

### 4.1 API

`/etc/supervisor/conf.d/polaris-api.conf` (create as root):

```ini
[program:polaris-api]
command=/home/ubuntu/polaris-project/apps/api/.venv/bin/uvicorn
    polaris_api.main:app
    --host 0.0.0.0 --port 8000
    --workers 2
    --proxy-headers --forwarded-allow-ips=*
directory=/home/ubuntu/polaris-project/apps/api
user=ubuntu
autostart=true
autorestart=true
startsecs=5
startretries=10
stopwaitsecs=30
stopsignal=TERM
redirect_stderr=true
stdout_logfile=/var/log/supervisor/polaris-api.log
stdout_logfile_maxbytes=50MB
stdout_logfile_backups=5
```

No `environment=` directive needed — `apps/api/src/polaris_api/config.py`
reads `.env` directly via `pydantic-settings`, anchored to the repo
root.  The same is true for the worker.  `--workers 2` is safe
because the API is stateless; bump if CPU allows.  No `--reload`.

### 4.2 Worker

`/etc/supervisor/conf.d/polaris-worker.conf`:

```ini
[program:polaris-worker]
command=/home/ubuntu/polaris-project/apps/worker/.venv/bin/polaris-worker
directory=/home/ubuntu/polaris-project/apps/worker
user=ubuntu
autostart=true
autorestart=true
startsecs=10
startretries=10
stopwaitsecs=60
stopsignal=TERM
redirect_stderr=true
stdout_logfile=/var/log/supervisor/polaris-worker.log
stdout_logfile_maxbytes=50MB
stdout_logfile_backups=5
```

The deploy user needs:
- Membership in the `docker` group (checked in §2) — the worker spawns
  workspace + publish containers via the host Docker daemon.
- Read access to its own `~/.codex/auth.json` — workspace containers
  bind-mount it.

### 4.3 Web — nginx sidecar serving `apps/web/dist/`

Unlike dev (`pnpm dev:web`, hot-reload Vite), staging serves a built
bundle.  `make staging` already built `apps/web/dist/` in §3; the §5
upgrade flow rebuilds it on every release.  A small nginx container
serves that directory read-only.

The sidecar is defined at `infra/web/compose.yaml` (tracked — the
bind-mount path is relative to the compose file, so the same file
works on every host):

```yaml
# infra/web/compose.yaml (excerpt)
services:
  polaris-web:
    image: nginxinc/nginx-unprivileged:1.27-alpine
    container_name: polaris-web
    user: "1000:1000"
    restart: unless-stopped
    volumes:
      - ../../apps/web/dist:/usr/share/nginx/html:ro
    networks:
      - traefik-public

networks:
  traefik-public:
    name: traefik-public
    external: true
```

Why the non-obvious shape:

- **Relative bind-mount** (`../../apps/web/dist`).  Compose resolves
  volume paths relative to the compose file's directory, so the same
  file works whether the repo lives at `/home/ubuntu/polaris-project`,
  `/home/sun/polaris-prod`, or anywhere else — no per-host edits, no
  per-host gitignore.
- **`nginxinc/nginx-unprivileged`** listens on `:8080` and never needs
  root.  The Traefik `main-web` service upstream reflects that.
- **`user: "1000:1000"`** matches the host UID that owns
  `apps/web/dist/`.  The default `nginx` UID (101) cannot traverse a
  UID-1000-owned home directory even when files inside are
  world-readable — you'd otherwise need `chmod o+x /home/<user>`.
- **No `ports:` binding.** The container is reachable *only* from the
  `traefik-public` docker network — Traefik resolves `polaris-web` via
  docker's embedded DNS.  No host port → no accidental LAN/internet
  exposure, no dependency on `ufw-docker` policy.

Lifecycle is wired into the Makefile + `scripts/down.sh`:

- `make staging` — `docker compose -f infra/web/compose.yaml up -d`
  after the web bundle build (§3).
- `make stop` — stops the sidecar alongside every other polaris
  container, preserving state.  No-op on dev hosts where it was
  never started.
- `make down` (`scripts/down.sh`) — brings the sidecar down with `-v`
  as part of the nuclear teardown.

Traefik routing is already in `infra/traefik/dynamic/main-site.yaml`
— router `main-web` points to `http://polaris-web:8080` (docker DNS
on `traefik-public`), `main-api` to `http://host.docker.internal:8000`
(host-gateway, since api runs on the host under Supervisor).  No
extra dynamic config is needed unless you rebind to a non-default
domain (§1.3).

### 4.4 Load + start

```bash
sudo supervisorctl reread                          # parse new conf files
sudo supervisorctl update                          # start newly-defined programs
sudo supervisorctl status polaris-api polaris-worker
```

Check health endpoints:

```bash
curl https://example.com/api/health                # {service: "polaris-api", status: "ok"}
curl https://example.com/api/ready                 # {database: "ok", redis: "ok"}
```

Per-program lifecycle:

```bash
sudo supervisorctl restart polaris-api
sudo supervisorctl stop polaris-worker
sudo supervisorctl tail -f polaris-api             # live stdout tail
sudo supervisorctl tail -f polaris-api stderr
```

---

## 5. Upgrade flow

```bash
# As the deploy user (UID 1000), from the repo root:
cd ~/polaris-project
git pull
make bootstrap                             # reinstall venvs if pyproject changed
make build-workspace                       # rebuild workspace image if infra/workspace/ or publish-templates/ changed
pnpm --filter @polaris/web build           # rebuild frontend bundle
make migrate                               # alembic upgrade head (idempotent)

# As root (or via sudo):
sudo supervisorctl restart polaris-api polaris-worker
# As the deploy user (docker group membership lets compose work without sudo):
docker compose -f infra/web/compose.yaml restart polaris-web
```

**Workspace image rebuild** doesn't affect already-running user
containers — they keep the old image until the next session.  To flush
everyone's state: `make clear` before restarting services.

---

## 6. Backup and restore

Back up to an off-host location (another host, S3, whatever your
environment allows).  A staging host is still a single point of failure.

### Postgres

```bash
docker exec polaris-project-postgres-1 \
  pg_dump -U root -d polaris > /home/ubuntu/backups/polaris-$(date +%F).sql
# restore:
docker exec -i polaris-project-postgres-1 psql -U root -d polaris \
  < /home/ubuntu/backups/polaris-<date>.sql
```

### MinIO

MinIO data is a bind mount at `infra/minio/data/` (not a named
volume), owned by the MinIO container's UID.  Back it up with a tar
snapshot via a throwaway container so the running MinIO doesn't need
to stop:

```bash
docker run --rm \
  -v /home/ubuntu/polaris-project/infra/minio/data:/data:ro \
  -v /home/ubuntu/backups:/out \
  alpine tar -czf /out/minio-$(date +%F).tgz -C /data .
```

### Published project state

Each published project owns `~/polaris-project/.data/projects/<uuid>/`:

- `archives/<short-hash>.tar.gz` — frozen source per version
- `secrets.env` — per-project DB credentials + session secret
- `compose.prod.yml` + `compose.polaris.yml` — materialized compose

Back up the whole `.data/projects/` tree.  On restore, already-running
prod containers keep running (images cached locally + in the registry);
the first new `compose up` for each project after restore reads back
the restored state.

### Redis

Transient — skip.  Lost state = in-flight sessions don't resume; new
sessions work.

### Cron (optional)

A user-level cron on the deploy user is enough:

```bash
mkdir -p ~/backups
(crontab -l 2>/dev/null; cat <<'EOF'
0 3 * * * docker exec polaris-project-postgres-1 pg_dump -U root -d polaris > ~/backups/polaris-$(date +\%F).sql
10 3 * * * docker run --rm -v $HOME/polaris-project/infra/minio/data:/data:ro -v ~/backups:/out alpine tar -czf /out/minio-$(date +\%F).tgz -C /data .
30 3 * * * find ~/backups -mtime +14 -delete
EOF
) | crontab -
```

(Running `docker exec` / `docker run` from user cron works because
the deploy user is in the `docker` group.)

---

## 7. Operations

### 7.1 Logs

| Source | Location |
|---|---|
| api | `sudo supervisorctl tail -f polaris-api` or `/var/log/supervisor/polaris-api.log` |
| worker | `sudo supervisorctl tail -f polaris-worker` or `/var/log/supervisor/polaris-worker.log` |
| web (nginx sidecar) | `docker logs polaris-web` |
| per-workspace container | `docker logs polaris-ws-<hash>` / `polaris-br-<hash>` |
| per-published container | `docker logs polaris-pub-<projid>-web-1` |
| Publish pipeline | DB `deployments.build_log` / `smoke_log`; streamed via SSE to `GET /deployments/{id}/events` and to `polaris publish` stdout inside the workspace |
| Traefik | `docker logs polaris-traefik-1` + `http://<host>:8090/dashboard/` |

`redirect_stderr=true` in each program config means stdout + stderr
are merged into the single `stdout_logfile` above.  Supervisor rotates
each log at 50 MB × 5 backups.

### 7.2 Common failure signatures

| Symptom | Likely cause | Where to look |
|---|---|---|
| `polaris-api` / `polaris-worker` in `FATAL` or restart-looping | Bad `.env` / missing secret | `sudo supervisorctl tail polaris-api` (captures startup traceback) |
| Traefik 404 on platform root | Dynamic config not reloaded (file provider watches `/etc/traefik/dynamic/`) | Touch a file in that dir; Traefik reloads within ~1s |
| Traefik 404 on `ide-*.example.com` | Workspace container died or never joined `polaris-internal` | `docker logs polaris-ws-<hash>` |
| Session stuck in `queued` | Worker crashed | `sudo supervisorctl status polaris-worker` + tail log |
| Publish `smoke probe never succeeded` | User container crash during startup; real cause is in the web container logs (auto-captured into `smoke_log`) | PublishPanel live log → "captured tail of `<svc>` container logs" section |
| Users pile at "queued" | `POLARIS_MAX_GLOBAL_RUNS` hit | Bump in `.env` + `sudo supervisorctl restart polaris-api polaris-worker` |

### 7.3 Clean slate

```bash
make clear              # drops workspace state + platform pg/redis (interactive)
make clear FORCE=1      # non-interactive
```

**`make clear` keeps**: traefik, MinIO, registry, built images.  For a
full wipe (every Polaris container + every volume + every bind-mount
data dir, keeping only built images + `~/.codex/auth.json`):

```bash
make down               # truly nuclear; interactive
make down FORCE=1       # non-interactive
```

Stop Supervisor-managed api / worker first
(`sudo supervisorctl stop polaris-api polaris-worker`) before running
`make down`, otherwise they'll thrash trying to reconnect to a
disappearing Postgres.

### 7.4 Stopping without losing state

```bash
sudo supervisorctl stop polaris-api polaris-worker
make stop                                               # every polaris container incl. nginx sidecar (workspaces, published, infra)
```

`make stop` halts containers without removing them — the nginx web
sidecar (when `infra/web/compose.yaml` is present), per-workspace /
published / preview containers, MinIO, traefik, and platform
postgres-redis-registry all stop in place.  Volumes, DB rows,
published project state (`.data/projects/<uuid>/`), and network
definitions stay intact.  Re-run the boot sequence (`make infra` +
`sudo supervisorctl start ...` + `docker compose -f
infra/web/compose.yaml start`, or just `make staging` which chains
the same up-steps) to resume without re-creating anything.  If you'd
rather let compose recreate containers on resume, `make stop-infra`
does `down` (stop + remove) on MinIO / traefik / platform
postgres-redis-registry — volumes still survive.

---

## 8. Hardening checklist

Before pointing real users at the staging host:

- **Enable the host firewall.  Only open 80 and 443 inbound.** Every
  other port the platform binds (8090 Traefik dashboard, 9001 MinIO
  console, 5000 local registry, 5432 / 6379 Postgres / Redis, 8000 /
  5173 internal API / Vite) is either unauthenticated, admin-only, or
  only safe for loopback.  With
  ufw:

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

- `chmod 600 ~/polaris-project/.env`.  It carries every credential.
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

- [DEVELOPMENT.md](./DEVELOPMENT.md) — local dev (`make dev`, Dev Login, hot reload)
- [ARCHITECTURE.md](./ARCHITECTURE.md) — system design, data model, publish pipeline
- [API.md](./API.md) — REST + SSE endpoints
- [CONFIGURATION.md](./CONFIGURATION.md) — full environment variable reference
- [FRONTEND.md](./FRONTEND.md) — React architecture
- [TESTING.md](./TESTING.md) — verification procedures
- `infra/traefik/README.md` — Traefik specifics (routing, cert layout)

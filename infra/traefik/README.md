# Polaris edge (traefik)

Traefik v3 is the single ingress for both dev and prod planes:

| host pattern                        | plane | target                                                       |
|-------------------------------------|-------|--------------------------------------------------------------|
| `polaris-dev.xyz` `/`               | dev   | host-bound vite dev server (:5173), file provider            |
| `polaris-dev.xyz` `/api/*`          | dev   | host-bound FastAPI (:8000), prefix-stripped, file provider   |
| `ide-<24hex>.polaris-dev.xyz`       | dev   | workspace container `polaris-ws-<24hex>:3000`, docker labels |
| `browser-<24hex>.polaris-dev.xyz`   | dev   | chromium container `polaris-br-<24hex>:3000`, docker labels  |
| `<uuid>.prod.polaris-dev.xyz`       | prod  | published user compose stacks, docker labels (Phase C)       |
| `s3.polaris-dev.xyz` + `*.s3.*`     | infra | minio container :9000, docker labels (`infra/minio/`)        |

## One-time setup

```bash
# 1. Issue two cert pairs with certbot (DNS-01 for the wildcards). The two
#    domains need separate certs because wildcard SANs only match one label.
sudo certbot certonly --manual --preferred-challenges dns \
  -d polaris-dev.xyz -d "*.polaris-dev.xyz"
sudo certbot certonly --manual --preferred-challenges dns \
  -d prod.polaris-dev.xyz -d "*.prod.polaris-dev.xyz"

# 2. Confirm certs landed at the paths dynamic/certs.yaml expects:
sudo ls /etc/letsencrypt/live/polaris-dev.xyz/
# cert.pem  chain.pem  fullchain.pem  privkey.pem
sudo ls /etc/letsencrypt/live/prod.polaris-dev.xyz/
# cert.pem  chain.pem  fullchain.pem  privkey.pem

# 3. Make sure polaris-dev.xyz, *.polaris-dev.xyz, prod.polaris-dev.xyz and
#    *.prod.polaris-dev.xyz all resolve to this host (public DNS A records,
#    or /etc/hosts for a local-only setup).

# 4. Bring traefik up. `make infra` (repo root) runs this alongside the
#    platform postgres/redis. The compose file mounts /etc/letsencrypt
#    read-only (the whole tree, because live/*.pem are symlinks into
#    archive/).
docker compose -f infra/traefik/compose.yaml up -d
```

Certbot renewal (`certbot renew`) rewrites the archive files atomically;
traefik's file provider picks up the change without a restart.

Dashboard: <http://localhost:8090/dashboard/> (LAN-visible, no auth — demo only).

## How containers advertise themselves

The docker provider watches `/var/run/docker.sock`. Any container joined
to the `traefik-public` network with `traefik.enable=true` plus router
labels is auto-discovered within ~1 second. Per-workspace compose stacks
(`apps/api/.../services/compose.py`) and per-project publish stacks
(Phase C) both use this convention.

No central route file to edit — containers carry their own routing intent.

## The two external networks

- **`polaris-internal`** — created by `docker-compose.infra.yaml`, shared
  with per-workspace compose projects. Pre-existing; traefik joins it
  so it can reach workspace/chromium container names for dev routing.
- **`traefik-public`** — created by this compose file. Published user
  projects (Phase C) will join it as `external: true` so their traefik
  labels land on a network traefik watches.

Dev containers currently join `polaris-internal` only (nothing published
yet). Once Phase C lands, published containers join `traefik-public`.

## Retiring the old platform nginx

The nginx service in `docker-compose.infra.yaml` is removed in the same
commit that introduces this config. Its config is archived at
`infra/nginx/` for reference but no longer loaded at runtime.

---
name: polaris-dev-deps
description: Use when the project needs a database or cache (Postgres, Redis) — auth, sessions, persistent state, caching, queues. Spins up a per-workspace sidecar container so the app has a real DB to talk to during development. NOT for production storage — published apps get their own DB via the publish stack.
---

# Polaris dev-deps

The platform runs the DB; you just ask for it.

## When to use

The user wants users/auth, persistent records, sessions, jobs, caching, or anything that survives a process restart. Don't roll your own `docker run`, don't use SQLite as a stand-in — call `polaris dev-up`.

## Happy path

```bash
polaris dev-up postgres   # or: polaris dev-up redis
```

The CLI prints the connection env to **stdout** (does not write `.env`). Read it, finish scaffolding the app (`create-next-app .`, `npm create vite@latest .`, `django-admin startproject`, etc.), THEN write the env into the scaffolded project's `.env`.

Postgres is reachable at `postgres:5432` (user=`app`, db=`app`). Redis at `redis:6379`. Both hostnames resolve only inside the workspace network — `localhost` will not work from your app container.

## Common pitfalls

- **Writing `.env` before scaffolding.** Most JS scaffolders refuse to run in a non-empty directory. `dev-up` deliberately does not touch `.env` for this reason; you write it after the scaffolder finishes.
- **Hardcoding `localhost:5432`.** Use the hostname `postgres` (or `redis`). Localhost only works from the host machine, not from inside the workspace container running your app.
- **Calling it twice.** `dev-up` is idempotent — re-running just reports the existing container. Use `polaris dev-list` to see what's already up, `polaris dev-down <service>` to remove.

## More

`polaris dev-up --help`, `polaris dev-list`, `polaris dev-down --help` cover the rest.

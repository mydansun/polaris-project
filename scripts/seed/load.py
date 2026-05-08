"""Seed loader — reads a snapshot dir and materializes it locally.

Snapshot shape (produced by the one-shot prod extractor, see commit
history; the extractor itself is not committed since it's not reusable):

    seed-data/<date>/
      manifest.json         — list of projects + per-project payloads
      projects/<uuid>/
        compose.prod.yml    — for reference, NOT loaded into .data/
        compose.polaris.yml — same
      minio/
        static/images/moodboard/<uuid>.png

What's loaded:
  - users:        a `seeded` dev user is found-or-created (POLARIS_DEV_USER_EMAIL)
  - projects:     INSERT, user_id rebound to the seeded dev user,
                  codex_thread_id NULL, original ids preserved
  - workspaces:   stub row per project (status='archived', repo_path
                  pointing at a real empty dir under
                  .data/workspaces/seeded-<uuid>/) — required so that
                  sessions FK resolves
  - sessions:     stub row per project (status='completed', sequence=0,
                  metadata_jsonb={"seeded": true}) — required so that
                  design_intents FK resolves
  - design_intents: real compiled_brief / mood_board_url / pinterest_queries
                  + stubbed intent_jsonb={} + pinterest_refs_jsonb=[]
                  (we deliberately didn't extract refs — too large with
                  base64 image data)
  - deployments:  metadata only (status, domain, created_at, ready_at,
                  image_tag, git_commit_hash); error/build_log/smoke_log
                  NOT carried (token-leak risk)
  - MinIO:        each PNG uploaded at its original key, ACL public-read

Domain replacement:
  Snapshot stores the prod domain replaced by the literal sentinel
  ``__POLARIS_DOMAIN__``.  Loader substitutes the current
  ``Settings.domain`` everywhere on its way into the DB.

Idempotency:
  Re-running with an already-loaded project_id is a no-op unless
  ``--force`` is passed (which deletes the existing project row and
  cascades).  ``reset`` mode wipes every project that has the
  ``polaris.seeded=true`` marker in its session metadata.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import asyncpg
import boto3
from botocore.client import Config as BotoConfig

# scripts/lib is on sys.path via tests/conftest or the entry CLI.
from lib import paths
from lib.db import asyncpg_url
from lib.env_io import read as read_env

PLACEHOLDER = "__POLARIS_DOMAIN__"
SEED_MARKER = {"seeded": True}


@dataclass
class LoadStats:
    inserted: int = 0
    skipped: int = 0
    minio_uploaded: int = 0
    minio_failed: int = 0


# ── env helpers ────────────────────────────────────────────────────────


def _env() -> dict[str, str]:
    """Combined view of repo .env + process env (process wins)."""
    e = read_env(paths.env_file())
    e.update({k: v for k, v in os.environ.items() if k in (
        "POLARIS_DOMAIN", "POLARIS_DEV_USER_EMAIL", "POLARIS_DEV_USER_NAME",
        "POLARIS_DATABASE_URL", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY",
        "S3_BUCKET",
    ) and v})
    return e


def _db_url(env: dict[str, str]) -> str:
    """asyncpg postgres URL — defers to ``lib.db.asyncpg_url`` which
    handles the random-host-port discovery via ``docker port``.  ``env``
    is the merged repo .env + process env; we only honour
    ``POLARIS_DATABASE_URL`` (process env wins) here so callers can
    override during tests."""
    if override := env.get("POLARIS_DATABASE_URL"):
        os.environ.setdefault("POLARIS_DATABASE_URL", override)
    return asyncpg_url()


def _to_dt(value: Any) -> datetime | None:
    """Parse manifest's ISO timestamp strings back to datetime.  Pass
    through ``None`` unchanged.  asyncpg refuses str timestamps."""
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        # fromisoformat handles both ``2026-04-22T20:05:01.654917+00:00``
        # and ``...Z`` (after replacing).
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise TypeError(f"unexpected timestamp type: {type(value).__name__}")


def _replace_placeholder(value: Any, domain: str) -> Any:
    if isinstance(value, str):
        return value.replace(PLACEHOLDER, domain)
    if isinstance(value, list):
        return [_replace_placeholder(v, domain) for v in value]
    if isinstance(value, dict):
        return {k: _replace_placeholder(v, domain) for k, v in value.items()}
    return value


# ── DB ops ─────────────────────────────────────────────────────────────


async def _ensure_dev_user(conn: asyncpg.Connection, env: dict[str, str]) -> uuid.UUID:
    email = env.get("POLARIS_DEV_USER_EMAIL", "dev@polaris.local")
    name = env.get("POLARIS_DEV_USER_NAME", "Polaris Dev")
    row = await conn.fetchrow("SELECT id FROM users WHERE email = $1", email)
    if row:
        return row["id"]
    new_id = uuid.uuid4()
    # `users` table has email_verified_at column on recent migrations;
    # use minimal valid set.  If other NOT NULL cols appear later, add
    # them here.
    await conn.execute(
        """
        INSERT INTO users (id, email, name, created_at)
        VALUES ($1, $2, $3, NOW())
        """,
        new_id,
        email,
        name,
    )
    print(f"  created dev user {email} (id={new_id})", file=sys.stderr)
    return new_id


async def _project_exists(conn: asyncpg.Connection, project_id: uuid.UUID) -> bool:
    row = await conn.fetchrow("SELECT 1 FROM projects WHERE id = $1", project_id)
    return row is not None


async def _delete_project_cascade(conn: asyncpg.Connection, project_id: uuid.UUID) -> None:
    """Manual delete dance because four FK constraints don't cascade:

       browser_sessions_workspace_id_fkey  (browser_sessions → workspaces)
       sessions_workspace_id_fkey          (sessions → workspaces)
       workspaces_project_id_fkey          (workspaces → projects)
       project_versions_project_id_fkey    (project_versions → projects)

    Order:
      1. browser_sessions for this project (also clears workspace refs)
      2. Sessions (cascades design_intents via session_id ON DELETE CASCADE)
      3. project_versions (own NO ACTION FK to projects; published projects
         have at least one row here that would otherwise block step 5)
      4. Workspaces (now safe — no session / browser-session refs)
      5. Project (cascades deployments / clarifications via their own
         ON DELETE CASCADE)
    """
    await conn.execute(
        "DELETE FROM browser_sessions WHERE project_id = $1", project_id
    )
    await conn.execute("DELETE FROM sessions WHERE project_id = $1", project_id)
    await conn.execute(
        "DELETE FROM project_versions WHERE project_id = $1", project_id
    )
    await conn.execute("DELETE FROM workspaces WHERE project_id = $1", project_id)
    await conn.execute("DELETE FROM projects WHERE id = $1", project_id)


async def _insert_project(
    conn: asyncpg.Connection, payload: dict, dev_user_id: uuid.UUID, domain: str
) -> None:
    p = _replace_placeholder(payload["project"], domain)
    await conn.execute(
        """
        INSERT INTO projects
            (id, user_id, name, slug, description, stack_template, status,
             codex_thread_id, created_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, NULL, $8, $9)
        """,
        uuid.UUID(p["id"]),
        dev_user_id,
        p["name"],
        p["slug"],
        p.get("description"),
        p.get("stack_template", "spa"),
        p.get("status", "active"),
        _to_dt(p["created_at"]),
        _to_dt(p["updated_at"]),
    )


async def _insert_stub_workspace(
    conn: asyncpg.Connection, project_id: uuid.UUID
) -> uuid.UUID:
    """Empty placeholder so design_intents.session_id FK can land.
    repo_path points at a real (empty) dir on disk so any code that
    `os.path.exists()` checks it doesn't choke."""
    ws_id = uuid.uuid4()
    repo_path = paths.REPO_ROOT / ".data" / "workspaces" / f"seeded-{project_id}"
    repo_path.mkdir(parents=True, exist_ok=True)
    await conn.execute(
        """
        INSERT INTO workspaces
            (id, project_id, repo_path, current_branch, status,
             compose_profile, ide_status, created_at, updated_at)
        VALUES ($1, $2, $3, 'main', 'archived', 'default',
                'not_configured', NOW(), NOW())
        """,
        ws_id,
        project_id,
        str(repo_path),
    )
    return ws_id


async def _insert_stub_session(
    conn: asyncpg.Connection, project_id: uuid.UUID, workspace_id: uuid.UUID
) -> uuid.UUID:
    """Stub session whose ONLY purpose is to satisfy the
    ``design_intents.session_id`` FK.  ``mode='discover_then_build'`` is
    the broadest of the current SessionMode literals — anything older
    that prod sessions might have had (``'discovery'``, etc.) breaks the
    Pydantic response validator on ``GET /projects/:id/sessions`` (the
    frontend loads this on every project detail open)."""
    sid = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO sessions
            (id, project_id, workspace_id, sequence, user_message, mode,
             status, metadata_jsonb, cost_jsonb, created_at)
        VALUES ($1, $2, $3, 0, '(seeded)', 'discover_then_build',
                'completed', $4::jsonb, '{}'::jsonb, NOW())
        """,
        sid,
        project_id,
        workspace_id,
        json.dumps(SEED_MARKER),
    )
    return sid


async def _insert_design_intent(
    conn: asyncpg.Connection,
    project_id: uuid.UUID,
    session_id: uuid.UUID,
    payload: dict | None,
    domain: str,
) -> None:
    if not payload:
        return
    payload = _replace_placeholder(payload, domain)
    await conn.execute(
        """
        INSERT INTO design_intents
            (project_id, session_id, intent_jsonb, compiled_brief,
             pinterest_refs_jsonb, pinterest_queries_jsonb,
             mood_board_url, status, created_at)
        VALUES ($1, $2, '{}'::jsonb, $3, '[]'::jsonb, $4::jsonb, $5,
                'active', NOW())
        """,
        project_id,
        session_id,
        payload.get("compiled_brief", ""),
        json.dumps(payload.get("pinterest_queries_jsonb") or []),
        payload.get("mood_board_url"),
    )


async def _insert_deployment(
    conn: asyncpg.Connection, project_id: uuid.UUID, payload: dict | None, domain: str
) -> None:
    if not payload:
        return
    payload = _replace_placeholder(payload, domain)
    await conn.execute(
        """
        INSERT INTO deployments
            (id, project_id, project_version_id, git_commit_hash,
             image_tag, domain, status, created_at, ready_at,
             screenshot_url)
        VALUES ($1, $2, NULL, $3, $4, $5, $6, $7, $8, $9)
        """,
        uuid.UUID(payload["id"]),
        project_id,
        payload.get("git_commit_hash"),
        payload.get("image_tag"),
        payload.get("domain"),
        payload.get("status", "ready"),
        _to_dt(payload["created_at"]),
        _to_dt(payload.get("ready_at")),
        # screenshot_url is rewritten through _replace_placeholder
        # above, so the persisted value already has the active domain
        # baked in.  None when the snapshot didn't capture one.
        payload.get("screenshot_url"),
    )


# ── MinIO ──────────────────────────────────────────────────────────────


def _minio_client(env: dict[str, str]) -> Any:
    """boto3 S3 client pointing at the local MinIO via traefik (TLS).

    We use ``https://s3.<POLARIS_DOMAIN>`` as endpoint and
    virtual-host addressing — exactly what services/s3.py does in the
    api.  Same TLS cert chain (acquired by traefik via CF DNS-01)."""
    domain = env.get("POLARIS_DOMAIN", "").strip()
    if not domain:
        raise RuntimeError("POLARIS_DOMAIN must be set in .env to load seed data.")
    endpoint = f"https://s3.{domain}"
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=env.get("S3_ACCESS_KEY_ID", ""),
        aws_secret_access_key=env.get("S3_SECRET_ACCESS_KEY", ""),
        config=BotoConfig(signature_version="s3v4"),
    )


def _upload_minio(
    s3: Any, bucket: str, key: str, src: Path, stats: LoadStats
) -> None:
    if not src.exists():
        print(f"    miss local file: {src}", file=sys.stderr)
        stats.minio_failed += 1
        return
    try:
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=src.read_bytes(),
            ContentType="image/png",
            ACL="public-read",
        )
        stats.minio_uploaded += 1
    except Exception as e:  # noqa: BLE001
        print(f"    minio upload failed for {key}: {e!r}", file=sys.stderr)
        stats.minio_failed += 1


# ── orchestration ──────────────────────────────────────────────────────


PROD_REPO_ROOT_LITERAL = "/home/polaris/opt/polaris-project"


def _copy_compose_files(
    source: Path, project_id: uuid.UUID, *, domain: str
) -> int:
    """Copy compose.prod.yml + compose.polaris.yml from the snapshot
    into ``.data/projects/<uuid>/`` so the (later) import step can
    ``docker compose up`` the published stack without re-extracting.

    Three rewrites at copy time:
      * prod-absolute paths → dev repo root (so env_file / bind-mounts
        land where they actually exist)
      * ``__POLARIS_DOMAIN__`` placeholder → the configured domain
        (this lives on traefik Host(...) labels)
      * Generate a synthetic ``secrets.env`` with deterministic-but-
        unique POSTGRES_* values so the postgres healthcheck has
        something to authenticate against.  Real prod secrets are NOT
        imported; any user-triggered republish later regenerates them
        via ``services/publish.py::materialize_secrets``.
    """
    src_dir = source / "projects" / str(project_id)
    dst_dir = paths.REPO_ROOT / ".data" / "projects" / str(project_id)
    if not src_dir.exists():
        return 0
    dst_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for fname in ("compose.prod.yml", "compose.polaris.yml"):
        src = src_dir / fname
        if not src.exists():
            continue
        text = src.read_text(encoding="utf-8")
        text = text.replace(PROD_REPO_ROOT_LITERAL, str(paths.REPO_ROOT))
        text = text.replace(PLACEHOLDER, domain)
        (dst_dir / fname).write_text(text, encoding="utf-8")
        n += 1
    # Synthetic secrets.env: deterministic per-project so repeated loads
    # produce the same POSTGRES_* values (postgres data volume keeps
    # working across re-imports).  Always rewritten — the only thing in
    # this file is seed-derived; real user-triggered publishes
    # regenerate it via services/publish.py::materialize_secrets.
    import hashlib

    seed = hashlib.sha256(str(project_id).encode()).hexdigest()[:24]
    (dst_dir / "secrets.env").write_text(
        "POSTGRES_USER=app\n"
        "POSTGRES_PASSWORD=" + seed + "\n"
        "POSTGRES_DB=app\n"
        "DATABASE_URL=postgres://app:" + seed + "@postgres:5432/app\n",
        encoding="utf-8",
    )
    return n


async def load(source: Path, *, force: bool = False) -> LoadStats:
    manifest_path = source / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"no manifest at {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    env = _env()
    domain = env.get("POLARIS_DOMAIN", "").strip()
    if not domain:
        raise RuntimeError("POLARIS_DOMAIN must be set in .env to load seed data.")
    print(
        f"loading {len(manifest['projects'])} projects → domain={domain!r}",
        file=sys.stderr,
    )

    stats = LoadStats()
    s3 = _minio_client(env)
    bucket = env.get("S3_BUCKET", "polaris")

    conn = await asyncpg.connect(_db_url(env))
    try:
        dev_user_id = await _ensure_dev_user(conn, env)
        for entry in manifest["projects"]:
            pid = uuid.UUID(entry["project"]["id"])
            slug = entry["project"]["slug"]

            if await _project_exists(conn, pid):
                if not force:
                    print(f"  skip   {slug} ({pid}) — already loaded", file=sys.stderr)
                    stats.skipped += 1
                    continue
                print(f"  delete {slug} ({pid}) — --force", file=sys.stderr)
                await _delete_project_cascade(conn, pid)

            async with conn.transaction():
                await _insert_project(conn, entry, dev_user_id, domain)
                ws_id = await _insert_stub_workspace(conn, pid)
                ses_id = await _insert_stub_session(conn, pid, ws_id)
                await _insert_design_intent(conn, pid, ses_id, entry["design_intent"], domain)
                await _insert_deployment(conn, pid, entry["deployment"], domain)
            n_compose = _copy_compose_files(source, pid, domain=domain)
            print(f"  insert {slug} ({pid}) [+{n_compose} compose files]", file=sys.stderr)
            stats.inserted += 1

            for key in entry.get("minio_keys", []):
                src = source / "minio" / key
                _upload_minio(s3, bucket, key, src, stats)
    finally:
        await conn.close()

    print(
        f"\n done.  inserted {stats.inserted}, skipped {stats.skipped}, "
        f"minio uploaded {stats.minio_uploaded} (failed {stats.minio_failed})",
        file=sys.stderr,
    )
    return stats


async def reset() -> int:
    """Wipe every project whose stub session carries the seeded marker."""
    env = _env()
    conn = await asyncpg.connect(_db_url(env))
    try:
        rows = await conn.fetch(
            """
            SELECT DISTINCT p.id, p.slug
              FROM projects p
              JOIN sessions s ON s.project_id = p.id
             WHERE s.metadata_jsonb @> '{"seeded": true}'::jsonb
            """
        )
        if not rows:
            print("  no seeded projects found", file=sys.stderr)
            return 0
        for r in rows:
            print(f"  delete {r['slug']} ({r['id']})", file=sys.stderr)
            await _delete_project_cascade(conn, r["id"])

        # Also clean up the seeded workspace dirs.
        for r in rows:
            d = paths.REPO_ROOT / ".data" / "workspaces" / f"seeded-{r['id']}"
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
        print(f"  removed {len(rows)} seeded projects", file=sys.stderr)
        return len(rows)
    finally:
        await conn.close()

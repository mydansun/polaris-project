"""Seed importer: take the seed-data/import/ snapshot and upgrade the
seed-only project rows into FULL projects on dev — workspace files in
place, archives copied, image built+pushed, published stack running
behind traefik with a real cert.

``load.py`` sets up DB metadata + mood boards / cards (enough to render
HomePage).  This step makes ``<uuid>.<prod-base>`` URLs serve real
content.

What we do per project:
  1. Untar ``workspaces/<wsid>.tar.gz`` into the dev workspace path
     (``settings.workspace_root / dev_user_id / project_slug``) and
     update the workspace row's ``repo_path`` to point there.
  2. Copy ``archives/<project_id>/*.tar.gz`` into
     ``settings.publish_projects_root / <project_id>/archives/``.
  3. Pick the most-recent archive, untar to a temp dir, run the same
     ``docker build`` + ``docker push`` the publish pipeline does
     (we just borrow the helpers, no API round-trip needed).
  4. Reuse ``compose.prod.yml`` + ``compose.polaris.yml`` already on
     disk (from ``load.py``).  Run ``docker compose up -d`` with the
     project's published-name (``polaris-pub-<24-char-hash>``).

All steps best-effort per project — a failure on one doesn't abort
the others; a summary is printed at the end.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path
from typing import Any

import asyncpg

from lib import paths
from lib.db import asyncpg_url


import re

REGISTRY_URL = "127.0.0.1:5000"


def _expected_image_tag(project_id: str, projects_root: Path) -> str | None:
    """Parse the image: line out of compose.polaris.yml so we know
    EXACTLY which sha to build.  Compose pinned the image at publish
    time; we have to reproduce that exact tag, not just any."""
    polaris_yml = projects_root / project_id / "compose.polaris.yml"
    if not polaris_yml.exists():
        return None
    text = polaris_yml.read_text(encoding="utf-8")
    m = re.search(r"image:\s*([^\s'\"]+)", text)
    return m.group(1) if m else None


def _archive_tag(archive: Path) -> str:
    """Strip the .tar.gz to get the bare commit hash (.stem only
    strips one extension, leaving the .tar)."""
    name = archive.name
    if name.endswith(".tar.gz"):
        return name[: -len(".tar.gz")]
    return archive.stem


def _slug_for(conn: asyncpg.Connection, project_id: str) -> str:
    """Tiny helper — fetch the slug for use as the workspace dir name."""
    raise NotImplementedError  # async; see _async_slug_for


async def _async_slug_for(conn: asyncpg.Connection, project_id: str) -> str:
    row = await conn.fetchrow(
        "SELECT slug FROM projects WHERE id=$1::uuid", project_id
    )
    return row["slug"]


async def _dev_user_id(conn: asyncpg.Connection) -> str:
    row = await conn.fetchrow(
        "SELECT id FROM users WHERE email = 'dev@polaris.local' LIMIT 1"
    )
    if not row:
        raise RuntimeError("dev user not found — run scripts/seed.py load first")
    return str(row["id"])


def _untar_workspace(tar_path: Path, dest_dir: Path) -> None:
    """Extract workspace tarball into ``dest_dir`` (which is created
    fresh — pre-existing contents are wiped first to keep the import
    deterministic)."""
    if dest_dir.exists():
        # Use sudo-equivalent via docker run since some files inside
        # may be owned by root after a previous workspace container
        # touched them.
        subprocess.run(  # noqa: S603, S607
            ["docker", "run", "--rm", "--user", "0",
             "-v", f"{dest_dir.parent}:/x", "alpine",
             "rm", "-rf", f"/x/{dest_dir.name}"],
            check=False,
        )
    dest_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "r:gz") as tf:
        tf.extractall(dest_dir)


def _copy_archives(src_dir: Path, dest_dir: Path) -> int:
    """Copy *.tar.gz archive snapshots.  Returns count copied."""
    if not src_dir.exists():
        return 0
    dest_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for src in src_dir.glob("*.tar.gz"):
        shutil.copy2(src, dest_dir / src.name)
        count += 1
    return count


async def _docker_build_and_push(
    archive: Path, image: str, *, log_prefix: str
) -> None:
    """Untar ``archive`` to a temp dir, ``docker build`` it, push to
    the local registry.  Prints concise progress lines."""
    with tempfile.TemporaryDirectory(prefix="polaris-import-build-") as tmp:
        tmp_path = Path(tmp)
        with tarfile.open(archive, "r:gz") as tf:
            tf.extractall(tmp_path)
        # Some archives have a single top-level dir; flatten if so.
        contents = list(tmp_path.iterdir())
        if len(contents) == 1 and contents[0].is_dir():
            ctx = contents[0]
        else:
            ctx = tmp_path
        if not (ctx / "Dockerfile").exists():
            # Look one level deeper
            for sub in ctx.iterdir():
                if sub.is_dir() and (sub / "Dockerfile").exists():
                    ctx = sub
                    break
        if not (ctx / "Dockerfile").exists():
            raise RuntimeError(f"no Dockerfile under {ctx}")

        print(f"  {log_prefix} build {image} (context={ctx.name})", file=sys.stderr)
        proc = await asyncio.create_subprocess_exec(
            "docker", "build", "--pull", "-t", image, str(ctx),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            tail = stdout.decode(errors="replace").splitlines()[-15:]
            raise RuntimeError(
                f"docker build failed (exit {proc.returncode}):\n"
                + "\n".join(tail)
            )
        print(f"  {log_prefix} push  {image}", file=sys.stderr)
        proc = await asyncio.create_subprocess_exec(
            "docker", "push", image,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"docker push failed (exit {proc.returncode}): "
                f"{stderr.decode(errors='replace')[:300]}"
            )


def _compose_project_for_publish(project_id: str) -> str:
    """Mirror apps/api/src/polaris_api/services/publish.py
    ``compose_project_name``."""
    return "polaris-pub-" + project_id.replace("-", "")[:24]


async def _start_published_stack(project_id: str, projects_root: Path) -> None:
    """`docker compose up -d` the per-project compose stack."""
    project_dir = projects_root / project_id
    compose_prod = project_dir / "compose.prod.yml"
    compose_polaris = project_dir / "compose.polaris.yml"
    if not compose_prod.exists() or not compose_polaris.exists():
        raise RuntimeError(f"compose files missing under {project_dir}")
    # secrets.env may not exist (we skip it during seed) — touch a
    # placeholder so the env_file: directive in compose.prod.yml doesn't
    # blow up.  Real secrets get materialized via the publish flow on
    # actual user-triggered publishes; for the import we accept default
    # postgres credentials baked into the image.
    secrets = project_dir / "secrets.env"
    if not secrets.exists():
        secrets.write_text("# placeholder — see services/publish.py materialize_secrets\n")
    project_name = _compose_project_for_publish(project_id)
    proc = await asyncio.create_subprocess_exec(
        "docker", "compose",
        "-p", project_name,
        "-f", str(compose_prod),
        "-f", str(compose_polaris),
        "up", "-d",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"compose up failed (exit {proc.returncode}):\n"
            + out.decode(errors="replace")[-600:]
        )


async def import_projects(snapshot: Path, *, force: bool = False) -> int:
    """Drive the full per-project import.  Returns 0 on success."""
    workspaces_meta = json.loads(
        (snapshot / "workspaces.json").read_text(encoding="utf-8")
    )
    repo_root = paths.REPO_ROOT
    workspace_root = repo_root / ".data" / "workspaces"
    projects_root = repo_root / ".data" / "projects"

    conn = await asyncpg.connect(asyncpg_url())
    summary: list[tuple[str, str, str]] = []
    try:
        dev_uid = await _dev_user_id(conn)
        for entry in workspaces_meta:
            project_id = entry["project_id"]
            wsid = entry["workspace_id"]
            slug = await _async_slug_for(conn, project_id)
            label = f"{slug:24}"
            print(f"\n▶ {label} ({project_id})", file=sys.stderr)

            # 1. Untar workspace into dev path
            dev_repo_path = workspace_root / dev_uid / slug
            tar_path = snapshot / entry["tarball"]
            try:
                _untar_workspace(tar_path, dev_repo_path)
                print(f"  ✓ workspace untarred → {dev_repo_path}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001
                summary.append((slug, "workspace", f"FAIL: {exc!r}"))
                continue

            # Update workspace row
            await conn.execute(
                """UPDATE workspaces SET repo_path=$1, status='ready',
                          ide_status='not_configured'
                   WHERE id=$2::uuid""",
                str(dev_repo_path), wsid,
            )

            # 2. Copy archives
            arch_src = snapshot / "archives" / project_id
            arch_dst = projects_root / project_id / "archives"
            n_arch = _copy_archives(arch_src, arch_dst)
            print(f"  ✓ {n_arch} archive(s) copied", file=sys.stderr)
            if n_arch == 0:
                summary.append((slug, "build", "SKIP: no archives"))
                continue

            # 3. docker build + push.  We MUST use the exact image tag
            # the saved compose.polaris.yml expects — that's the sha
            # of the source archive used at publish time.  Find that
            # archive specifically; no fallbacks (an unrelated archive
            # would build a misleading image with the right tag).
            expected = _expected_image_tag(project_id, projects_root)
            if not expected:
                summary.append((slug, "build", "FAIL: no image tag in compose.polaris.yml"))
                continue
            expected_sha = expected.rsplit(":", 1)[1]
            target_archive = arch_dst / f"{expected_sha}.tar.gz"
            if not target_archive.exists():
                summary.append((
                    slug, "build",
                    f"FAIL: archive {expected_sha}.tar.gz not in seed",
                ))
                continue
            try:
                await _docker_build_and_push(target_archive, expected, log_prefix="✓")
            except Exception as exc:  # noqa: BLE001
                summary.append((slug, "build", f"FAIL: {exc!r}"))
                continue

            # 4. Bring up the published stack
            try:
                await _start_published_stack(project_id, projects_root)
                print(f"  ✓ published stack running", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001
                summary.append((slug, "deploy", f"FAIL: {exc!r}"))
                continue

            summary.append((slug, "done", "✓"))
    finally:
        await conn.close()

    print("\n═ Import summary ═", file=sys.stderr)
    for slug, stage, msg in summary:
        print(f"  {slug:30}  {stage:10}  {msg}", file=sys.stderr)
    failures = sum(1 for _, _, m in summary if not m.startswith("✓"))
    return 1 if failures else 0

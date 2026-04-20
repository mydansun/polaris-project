#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""down.py — stop the dev stack + sweep dynamic containers.

Three destruction levels:
  (no flag)    docker compose down (preserves all volumes + .data)
  --clear      down -v + wipe .data/{workspaces,workspace-meta,projects}
  --nuclear    --clear + remove built platform images

Dynamic containers (polaris-ws-*, polaris-pub-*, etc.) live OUTSIDE
compose.dev.yaml — they're spawned by api at runtime via docker socket.
We sweep them by label `polaris.runtime` plus name-pattern fallback for
anything pre-label.

Confirmation prompts are interactive by default; --force skips them.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from lib import docker_ops, paths  # noqa: E402


def _confirm(msg: str, *, force: bool) -> bool:
    if force:
        return True
    try:
        ans = input(f"{msg} [y/N] ").strip().lower()
    except EOFError:
        return False
    return ans == "y"


def _sweep_runtime_containers() -> int:
    names = docker_ops.list_polaris_runtime_containers()
    if not names:
        print("  (no dynamic containers found)")
        return 0
    print(f"  stopping {len(names)} dynamic container(s):")
    for n in names:
        print(f"    - {n}")
    subprocess.run(  # noqa: S603, S607
        ["docker", "rm", "-f", *names],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return len(names)


def _wipe_data_dirs() -> None:
    targets = [
        paths.REPO_ROOT / ".data" / "workspaces",
        paths.REPO_ROOT / ".data" / "workspace-meta",
        paths.REPO_ROOT / ".data" / "projects",
    ]
    for t in targets:
        if t.exists():
            print(f"  rm -rf {t}")
            shutil.rmtree(t, ignore_errors=True)


def _remove_built_images() -> None:
    tags = ["polaris/api:dev", "polaris/worker:dev", "polaris/web:dev",
            "polaris/ide:latest", "polaris/workspace:latest",
            "polaris/chromium-vnc:latest"]
    print(f"  removing platform images: {len(tags)}")
    for t in tags:
        if docker_ops.image_exists(t):
            subprocess.run(  # noqa: S603, S607
                ["docker", "rmi", "-f", t],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Stop the polaris dev stack + sweep dynamic containers."
    )
    ap.add_argument(
        "--clear",
        action="store_true",
        help="ALSO drop volumes + wipe .data/{workspaces,workspace-meta,projects}",
    )
    ap.add_argument(
        "--nuclear",
        action="store_true",
        help="--clear + remove built platform images",
    )
    ap.add_argument("--force", action="store_true", help="skip confirmation prompts")
    args = ap.parse_args()

    if args.nuclear:
        args.clear = True

    if not docker_ops.docker_daemon_up():
        print("⚠ docker daemon not reachable — nothing to stop", file=sys.stderr)
        return 0

    cf = paths.compose_file()

    print("▶ docker compose down")
    if args.clear:
        if not _confirm(
            "  this will DROP postgres/redis/registry/minio/traefik volumes",
            force=args.force,
        ):
            print("aborted", file=sys.stderr)
            return 130
        docker_ops.compose(cf, "down", "-v", check=False)
    else:
        docker_ops.compose(cf, "down", check=False)

    print("\n▶ sweeping runtime containers (workspace / browser / publish)")
    swept = _sweep_runtime_containers()

    if args.clear:
        print("\n▶ wiping .data/")
        _wipe_data_dirs()

    if args.nuclear:
        if not _confirm(
            "  this will REMOVE built images (polaris/api,worker,web,workspace,ide,chromium-vnc)",
            force=args.force,
        ):
            print("aborted (data already wiped, but images kept)", file=sys.stderr)
            return 0
        print("\n▶ removing built platform images")
        _remove_built_images()

    print(f"\n✓ done.  swept {swept} runtime container(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

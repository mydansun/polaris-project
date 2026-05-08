#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""down.py — stop the polaris stack + sweep dynamic containers.

Two modes (mirror up.py):
  ./scripts/down.py             # interactive prompt for dev | stage
  ./scripts/down.py dev         # explicit
  ./scripts/down.py stage       # explicit

Three destruction levels:
  (no flag)    docker compose down (preserves all volumes + .data)
  --clear      down -v + wipe .data/{workspaces,workspace-meta,projects}
  --nuclear    --clear + remove built platform images

Dynamic containers (polaris-ws-*, polaris-pub-*, etc.) live OUTSIDE
compose.<mode>.yaml — they're spawned by api at runtime via docker
socket.  We sweep them by label `polaris.runtime` plus name-pattern
fallback for anything pre-label.

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


def _remove_built_images(mode: str) -> None:
    # Per-mode platform images, plus the workspace runtime trio that
    # both modes share (those don't carry a mode tag).
    tags = [
        f"polaris/api:{mode}",
        f"polaris/worker:{mode}",
        f"polaris/web:{mode}",
        "polaris/ide:latest",
        "polaris/workspace:latest",
        "polaris/chromium-vnc:latest",
    ]
    print(f"  removing platform images: {len(tags)}")
    for t in tags:
        if docker_ops.image_exists(t):
            subprocess.run(  # noqa: S603, S607
                ["docker", "rmi", "-f", t],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


def _resolve_mode(cli_mode: str | None) -> str:
    """Mirror up.py: explicit > interactive prompt > dev fallback."""
    if cli_mode is not None:
        return cli_mode
    try:
        import questionary  # type: ignore
        answer = questionary.select(
            "Which mode?",
            choices=list(paths.MODES),
            default="dev",
        ).ask()
        if answer is None:
            print("aborted", file=sys.stderr)
            raise SystemExit(130)
        return answer
    except ImportError:
        ans = input("which mode? [dev/stage] (dev): ").strip().lower()
        if ans not in paths.MODES:
            ans = "dev"
        return ans


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Stop the polaris stack + sweep dynamic containers."
    )
    ap.add_argument(
        "mode",
        nargs="?",
        choices=paths.MODES,
        default=None,
        help="dev | stage (prompted if omitted)",
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

    mode = _resolve_mode(args.mode)
    print(f"→ mode: {mode}", file=sys.stderr)

    if not docker_ops.docker_daemon_up():
        print("⚠ docker daemon not reachable — nothing to stop", file=sys.stderr)
        return 0

    cf = paths.compose_file(mode)

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
            f"  this will REMOVE built images (polaris/api:{mode},worker:{mode},web:{mode},workspace,ide,chromium-vnc)",
            force=args.force,
        ):
            print("aborted (data already wiped, but images kept)", file=sys.stderr)
            return 0
        print("\n▶ removing built platform images")
        _remove_built_images(mode)

    print(f"\n✓ done.  swept {swept} runtime container(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

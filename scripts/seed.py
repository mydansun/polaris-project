#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "asyncpg>=0.29",
#     "boto3>=1.34",
# ]
# ///
"""seed.py — load a snapshot directory into the local stack, or reset.

Usage:
    ./scripts/seed.py load --source seed-data/2026-05-05/
    ./scripts/seed.py load --force                   # auto-find newest dir + replace existing
    ./scripts/seed.py reset
    ./scripts/seed.py import                         # bring up real published stacks

``load`` inserts projects + stub workspace/session + design_intents +
deployments + uploads mood_board PNGs to the local MinIO.  Idempotent.
``reset`` wipes any project whose stub session is tagged
``polaris.seeded=true`` in metadata_jsonb.
``import`` picks up where ``load`` stops: untars workspace files, copies
archives, builds + pushes images, and brings up the published compose
stacks so ``<uuid>.<prod-base>`` URLs serve real content.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from seed import load as seed_load  # noqa: E402


def _newest_seed_dir() -> Path | None:
    root = _SCRIPTS.parent / "seed-data"
    if not root.exists():
        return None
    candidates = sorted([p for p in root.iterdir() if p.is_dir()], reverse=True)
    return candidates[0] if candidates else None


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed the local stack from a snapshot.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_load = sub.add_parser("load", help="load a snapshot")
    p_load.add_argument("--source", type=Path, help="path to seed-data/<date>/ (default: newest)")
    p_load.add_argument("--force", action="store_true", help="replace already-loaded projects")

    sub.add_parser("reset", help="wipe seeded projects")

    p_import = sub.add_parser(
        "import",
        help="install workspace files + rebuild images + run published stacks",
    )
    p_import.add_argument(
        "--source", type=Path, default=Path("seed-data/import"),
        help="path to the import snapshot dir",
    )

    args = ap.parse_args()

    if args.cmd == "load":
        source = args.source or _newest_seed_dir()
        if source is None:
            print("no seed-data/<date>/ directory found", file=sys.stderr)
            return 2
        if not source.exists():
            print(f"source not found: {source}", file=sys.stderr)
            return 2
        asyncio.run(seed_load.load(source, force=args.force))
        return 0

    if args.cmd == "reset":
        asyncio.run(seed_load.reset())
        return 0

    if args.cmd == "import":
        from seed.importer import import_projects

        src = args.source
        if not src.is_absolute():
            src = (_SCRIPTS.parent / src).resolve()
        if not src.exists():
            print(f"snapshot not found: {src}", file=sys.stderr)
            return 2
        return asyncio.run(import_projects(src))

    return 1


if __name__ == "__main__":
    sys.exit(main())

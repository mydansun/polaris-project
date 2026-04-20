#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""build.py — build the platform images workspaces are spawned from.

Three images:
  polaris/ide:latest         → Theia IDE base
  polaris/workspace:latest   → IDE base + dev toolchain + Codex
  polaris/chromium-vnc:latest → custom chromium with CDP nginx proxy

Idempotency:
  Each image carries a `polaris.built-at` LABEL with the mtime of its
  Dockerfile (and key context files).  Re-runs skip images whose
  Dockerfile / context hasn't changed since the last build.

Flags:
  --force            rebuild everything regardless of mtime
  --push REGISTRY    after build, tag + push to <registry>/polaris/<name>
  --only NAME        only build the named image (ide / workspace / chromium-vnc)
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Allow importing scripts/lib/ when invoked from anywhere.
_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from lib import docker_ops  # noqa: E402
from lib.paths import REPO_ROOT  # noqa: E402


@dataclass(frozen=True)
class ImageSpec:
    name: str       # short logical name (used by --only)
    tag: str        # image tag in local docker daemon
    dockerfile: Path
    context: Path
    # Files / dirs whose mtime feeds the "needs rebuild?" check.  Should
    # cover the whole effective build context (so editing any one of them
    # triggers rebuild).
    inputs: list[Path]


def _images() -> list[ImageSpec]:
    return [
        ImageSpec(
            name="ide",
            tag="polaris/ide:latest",
            dockerfile=REPO_ROOT / "packages" / "ide" / "Dockerfile",
            context=REPO_ROOT / "packages" / "ide",
            inputs=[REPO_ROOT / "packages" / "ide"],
        ),
        ImageSpec(
            name="workspace",
            tag="polaris/workspace:latest",
            dockerfile=REPO_ROOT / "infra" / "workspace" / "Dockerfile",
            context=REPO_ROOT / "infra",
            inputs=[
                REPO_ROOT / "infra" / "workspace",
                REPO_ROOT / "infra" / "publish-templates",
            ],
        ),
        ImageSpec(
            name="chromium-vnc",
            tag="polaris/chromium-vnc:latest",
            dockerfile=REPO_ROOT / "infra" / "chromium" / "Dockerfile",
            context=REPO_ROOT / "infra" / "chromium",
            inputs=[REPO_ROOT / "infra" / "chromium"],
        ),
    ]


def _max_mtime(paths: list[Path]) -> float:
    """Largest mtime across paths (recursing into directories).  Returns 0
    if no files found (treat as 'must rebuild')."""
    best = 0.0
    for p in paths:
        if not p.exists():
            continue
        if p.is_file():
            best = max(best, p.stat().st_mtime)
        else:
            for f in p.rglob("*"):
                if f.is_file():
                    best = max(best, f.stat().st_mtime)
    return best


def _image_built_at(tag: str) -> float:
    """Read our `polaris.built-at` label from the image, 0 if missing."""
    p = subprocess.run(  # noqa: S603, S607
        [
            "docker",
            "image",
            "inspect",
            "--format",
            "{{ index .Config.Labels \"polaris.built-at\" }}",
            tag,
        ],
        capture_output=True,
        text=True,
    )
    if p.returncode != 0:
        return 0.0
    raw = p.stdout.strip()
    try:
        return float(raw)
    except ValueError:
        return 0.0


def _needs_rebuild(spec: ImageSpec) -> tuple[bool, str]:
    if not docker_ops.image_exists(spec.tag):
        return True, "image absent"
    src_mtime = _max_mtime([spec.dockerfile, *spec.inputs])
    img_mtime = _image_built_at(spec.tag)
    if src_mtime == 0:
        return True, "no source mtime found (rebuild forced)"
    if img_mtime == 0:
        return True, "image lacks polaris.built-at label"
    if src_mtime > img_mtime:
        return True, f"source newer ({src_mtime:.0f} > {img_mtime:.0f})"
    return False, f"up-to-date (source {src_mtime:.0f} ≤ image {img_mtime:.0f})"


def _build_one(spec: ImageSpec) -> None:
    src_mtime = _max_mtime([spec.dockerfile, *spec.inputs])
    print(f"▶ building {spec.tag}", flush=True)
    cmd = [
        "docker",
        "build",
        "-t",
        spec.tag,
        "--label",
        f"polaris.built-at={src_mtime:.0f}",
        "-f",
        str(spec.dockerfile),
        str(spec.context),
    ]
    subprocess.run(cmd, check=True)  # noqa: S603


def _push(spec: ImageSpec, registry: str) -> None:
    target = f"{registry.rstrip('/')}/{spec.tag}"
    print(f"▶ tagging + pushing {target}", flush=True)
    subprocess.run(["docker", "tag", spec.tag, target], check=True)  # noqa: S603, S607
    subprocess.run(["docker", "push", target], check=True)  # noqa: S603, S607


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Build polaris/{ide,workspace,chromium-vnc} images."
    )
    ap.add_argument("--force", action="store_true", help="rebuild even if up-to-date")
    ap.add_argument("--push", metavar="REGISTRY", help="tag + push to a registry")
    ap.add_argument(
        "--only",
        metavar="NAME",
        help="only build one (ide / workspace / chromium-vnc)",
    )
    args = ap.parse_args()

    if not docker_ops.docker_daemon_up():
        print("docker daemon not reachable — start docker first", file=sys.stderr)
        return 1

    images = _images()
    if args.only:
        images = [i for i in images if i.name == args.only]
        if not images:
            print(f"no image named {args.only!r}", file=sys.stderr)
            return 2

    skipped = 0
    built: list[ImageSpec] = []
    for spec in images:
        needs, reason = _needs_rebuild(spec)
        if not needs and not args.force:
            print(f"⏭  {spec.tag} — {reason}")
            skipped += 1
            continue
        _build_one(spec)
        built.append(spec)

    if args.push and built:
        for spec in built:
            _push(spec, args.push)

    print(f"\n{len(built)} built, {skipped} skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["questionary>=2.0", "httpx>=0.27"]
# ///
"""up.py — configure (if needed) + start the polaris dev stack.

First-run flow:
  1. Detect missing required env in .env.
  2. Run the questionary wizard (lib.wizard.run_wizard).
  3. Validate live tokens (CF, OpenAI, Pinterest).
  4. Auto-generate SESSION_SECRET if blank.
  5. docker-daemon + dockerfile-mtime preflight.
  6. docker compose -f compose.dev.yaml up -d.
  7. Wait for service healthchecks.
  8. Print "open https://${POLARIS_DOMAIN}".

Re-runs:
  --reconfigure   force the wizard even if .env looks complete
  --non-interactive  pull values from existing env, fail on any missing
                  required field (CI mode).
  --skip-build    don't auto-trigger build.py even if Dockerfiles changed
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from lib import docker_ops, env_io, paths, spec, wizard  # noqa: E402


# ── pre-flight ───────────────────────────────────────────────────────────


def _check_docker() -> None:
    if not docker_ops.docker_daemon_up():
        print(
            "✗ docker daemon not reachable\n"
            "  start docker (e.g. `systemctl start docker` or open Docker Desktop) "
            "and retry",
            file=sys.stderr,
        )
        raise SystemExit(1)


def _ensure_codex_auth(env: dict[str, str]) -> str:
    """Resolve + validate the host-side codex auth path.

    The file is bind-mounted into every workspace at
    ``/home/workspace/.codex/auth.json``, and codex-app-server reads
    auth from it on every turn.  Empty / malformed files cause every
    Codex turn to fail with ``401 Missing bearer or basic authentication``
    on ``/v1/responses`` — a confusing failure mode that surfaces only
    once the user has already gotten through clarification + Pinterest +
    mood board.  Validate up-front instead.

    A valid auth.json has at least one of:
      * ``OPENAI_API_KEY`` — non-empty string (API-key mode), or
      * ``tokens`` — non-empty dict (ChatGPT OAuth mode written by
        ``codex login``).

    Failure modes are HARD by default; set ``POLARIS_SKIP_CODEX_AUTH_CHECK=1``
    to demote them to warnings (useful when you intend to launch codex
    inside a workspace and let it OAuth from there — rare in dev).

    Returns the resolved absolute path so up.py can re-export it as
    POLARIS_HOST_CODEX_AUTH_PATH for the docker compose subprocess.
    """
    raw = env.get("POLARIS_HOST_CODEX_AUTH_PATH", "").strip()
    if not raw:
        raw = "~/.codex/auth.json"
    path = Path(os.path.expanduser(raw)).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    pre_existed = path.exists()
    path.touch(exist_ok=True)

    skip_check = env.get("POLARIS_SKIP_CODEX_AUTH_CHECK", "").strip() in (
        "1",
        "true",
        "True",
        "yes",
    )

    def _emit(level: str, msg: str) -> None:
        if skip_check:
            print(f"⚠ {msg} (POLARIS_SKIP_CODEX_AUTH_CHECK=1, continuing)", file=sys.stderr)
        else:
            print(f"✗ {msg}", file=sys.stderr)
            print(
                "  Fix one of:\n"
                "    1. Run `codex login` on the host (writes OAuth tokens), or\n"
                "    2. Write `{\"OPENAI_API_KEY\": \"sk-...\"}` to the file directly, or\n"
                "    3. Set POLARIS_SKIP_CODEX_AUTH_CHECK=1 in .env to bypass this check\n"
                "       (workspaces will fail every codex turn with 401 until auth is set).",
                file=sys.stderr,
            )
            raise SystemExit(2)

    if not pre_existed:
        _emit("error", f"codex auth file did not exist; created empty stub at {path}")
        return str(path)
    if path.stat().st_size == 0:
        _emit("error", f"codex auth file is empty: {path}")
        return str(path)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _emit("error", f"codex auth file is not valid JSON: {path} — {exc}")
        return str(path)
    if not isinstance(data, dict):
        _emit("error", f"codex auth file is not a JSON object: {path}")
        return str(path)

    has_api_key = isinstance(data.get("OPENAI_API_KEY"), str) and bool(
        data["OPENAI_API_KEY"].strip()
    )
    has_oauth = isinstance(data.get("tokens"), dict) and bool(data["tokens"])
    if not (has_api_key or has_oauth):
        _emit(
            "error",
            f"codex auth file has neither OPENAI_API_KEY nor tokens: {path}",
        )
        return str(path)

    mode = "API-key" if has_api_key else "ChatGPT OAuth"
    print(f"✓ codex auth ok ({mode}, {path})", file=sys.stderr)
    return str(path)


# ── auto-build ───────────────────────────────────────────────────────────


def _maybe_build(skip: bool) -> None:
    if skip:
        return
    # Defer-import build.py via subprocess to keep its PEP 723 deps
    # isolated (we don't pull in build.py at module-import time).
    print("▶ scripts/build.py (idempotent — skips up-to-date images)", flush=True)
    rc = subprocess.run(  # noqa: S603, S607
        [sys.executable, str(_SCRIPTS / "build.py")], check=False
    ).returncode
    if rc != 0:
        # build.py can fail e.g. during initial Theia build; don't abort
        # the whole `up` since the platform itself doesn't depend on
        # those images at startup (only at workspace-spawn time).
        print(
            f"⚠ build.py exited {rc} — platform images (api/worker/web) "
            "will still build via compose.",
            file=sys.stderr,
        )


# ── compose up + wait ────────────────────────────────────────────────────


def _compose_up() -> None:
    cf = paths.compose_file()
    print(f"▶ docker compose -f {cf.name} up -d --build", flush=True)
    docker_ops.compose(cf, "up", "-d", "--build")


def _wait_healthy(timeout: float = 90.0) -> bool:
    """Poll `docker compose ps` until all services with healthchecks are
    reporting `healthy` (or the timeout fires)."""
    cf = paths.compose_file()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        p = docker_ops.compose(
            cf,
            "ps",
            "--format",
            "{{.Service}} {{.Status}}",
            check=False,
            env_extra={},
        )
        # `docker compose ps` doesn't return a nice machine format in older
        # versions; fall back to plain inspection.
        # If anything is "starting"/"unhealthy", keep waiting.
        bad = False
        for line in (p.stdout or "").splitlines():
            if "unhealthy" in line or "starting" in line:
                bad = True
                break
        if not bad:
            return True
        time.sleep(2)
    return False


# ── main ────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description="Configure + start polaris dev stack.")
    ap.add_argument("--reconfigure", action="store_true", help="force the wizard")
    ap.add_argument(
        "--non-interactive",
        action="store_true",
        help="don't prompt; fail if any required key is missing in env",
    )
    ap.add_argument(
        "--skip-build",
        action="store_true",
        help="don't auto-run build.py for workspace/ide/chromium images",
    )
    args = ap.parse_args()

    _check_docker()

    env_path = paths.env_file()
    current = env_io.read(env_path)
    missing = [k for k in spec.required_keys(current) if not current.get(k, "").strip()]

    if args.reconfigure or missing:
        print("→ launching configuration wizard", file=sys.stderr)
        current = wizard.run_wizard(
            env_path=env_path,
            only_missing=not args.reconfigure,
            interactive=not args.non_interactive,
        )

    codex_auth_path = _ensure_codex_auth(current)
    _maybe_build(args.skip_build)

    # PWD is what the api/worker containers see as POLARIS_HOST_REPO_ROOT.
    # Force it to the repo root regardless of where the user invoked us.
    os.chdir(paths.REPO_ROOT)
    os.environ["PWD"] = str(paths.REPO_ROOT)
    # Make sure docker compose interpolates the resolved (touched) codex
    # auth path even when it wasn't explicitly set in .env.
    os.environ["POLARIS_HOST_CODEX_AUTH_PATH"] = codex_auth_path

    _compose_up()
    print("▶ waiting for healthchecks (≤ 90s) …", flush=True)
    if not _wait_healthy():
        print(
            "⚠ some services didn't become healthy within 90s.  Check "
            "`docker compose -f compose.dev.yaml ps` and logs.",
            file=sys.stderr,
        )

    domain = current.get("POLARIS_DOMAIN", "").strip()
    if not domain:
        # Should be unreachable — wizard + non-interactive both ensure
        # POLARIS_DOMAIN is set before we get here.  Surface loudly
        # rather than print a misleading https:// URL.
        raise RuntimeError("POLARIS_DOMAIN missing after wizard — refusing to print bogus URLs.")
    print(f"\n✓ stack up.")
    print(f"   web   →  https://{domain}")
    print(f"   vnc   →  https://vnc.{domain}    (chromium auto-loaded with {domain})")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

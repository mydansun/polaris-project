#!/usr/bin/env python3
"""
polaris — workspace-side CLI for Codex / the user to interact with the Polaris
publish pipeline from inside the workspace container.

Commands:
  polaris scaffold-publish             print stack menu + auto-detection; writes nothing
  polaris scaffold-publish --stack=X   drop Dockerfile / compose.prod.yml / polaris.yaml for stack X
  polaris prepublish-audit             scan tracked files for secrets + size issues; exit non-zero on FAIL
  polaris publish [--dry-run]          build + smoke + promote; streams platform log back via SSE
  polaris rollback <commit-hash>       redeploy an older image tag by short commit hash
  polaris status                       show current deployment state

Talks to the platform API at $POLARIS_API_URL (default http://host.docker.internal:8000).
Authenticates with $POLARIS_WORKSPACE_TOKEN via the X-Polaris-Workspace-Token header.
The platform injects both env vars + $POLARIS_PROJECT_ID into the workspace container.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

TEMPLATES_ROOT = Path("/opt/polaris-publish-templates")

# ── .gitignore-tracked files that should NEVER end up in a publish ─────────
SECRET_FILENAME_PATTERNS = [
    re.compile(r"(^|/)\.env$"),
    re.compile(r"(^|/)\.env\.(?!example$)[^/]+$"),
    re.compile(r"\.pem$"),
    re.compile(r"\.key$"),
    re.compile(r"\.p12$"),
    re.compile(r"\.pfx$"),
    re.compile(r"(^|/)id_rsa(\.pub)?$"),
    re.compile(r"(^|/)credentials[^/]*$"),
    re.compile(r"\.sqlite3?$"),
    re.compile(r"(^|/)node_modules/"),
]

# ── Basic secret-in-content regexes (low-recall by design, catch obvious cases) ─
SECRET_CONTENT_PATTERNS = [
    (re.compile(rb"AKIA[0-9A-Z]{16}"), "AWS access key id"),
    (re.compile(rb"ghp_[A-Za-z0-9]{30,}"), "GitHub personal token"),
    (re.compile(rb"gho_[A-Za-z0-9]{30,}"), "GitHub OAuth token"),
    (re.compile(rb"xox[baprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
    (re.compile(rb"-----BEGIN (?:RSA|DSA|EC|OPENSSH) PRIVATE KEY-----"), "private key block"),
]

MAX_TRACKED_FILE_BYTES = 10 * 1024 * 1024  # 10 MB

# Framework/tooling binaries that live in node_modules/.bin.  The prod
# container's default PATH doesn't include node_modules/.bin, so invoking
# these names directly from the start command results in `sh: 1: <bin>:
# not found` (exit 127) at runtime.  Always wrap with npm / npx / an
# explicit path.
_NODE_BIN_NAMES = frozenset({
    "next", "vite", "tsc", "astro", "remix", "nuxt", "nest",
    "serve", "http-server", "webpack", "rollup", "esbuild",
    "nodemon", "ts-node", "tsx", "concurrently", "pm2",
})


def _check_bare_node_bins(start_cmd: str) -> list[str]:
    """Scan the polaris.yaml::start shell command for bare invocations of
    known node_modules/.bin binaries.  Returns human-readable error
    lines; empty list means the start command is PATH-safe.

    Heuristic, not a full shell parser — catches the common
    `a && b && c` / `env=x bin args` shapes we see in practice:

    - ``npm start``, ``npm run foo``, ``pnpm exec vite`` → pass (wrapper
      commands add node_modules/.bin to the subprocess PATH)
    - ``npx next ...`` → pass
    - ``./node_modules/.bin/next`` / absolute path → pass
    - Leading ``KEY=value`` env assignments → skipped
    - Bare ``next start ...`` / ``tsx foo.ts`` → fail
    """
    if not start_cmd.strip():
        return []
    flat = re.sub(r"[&|;]+", " ", start_cmd)
    toks = flat.split()
    errors: list[str] = []
    i = 0
    while i < len(toks):
        tok = toks[i]
        if "=" in tok and not tok.startswith("-"):
            # Env-var assignment prefix (NODE_ENV=production ...).  Skip.
            i += 1
            continue
        if tok in ("npm", "pnpm", "yarn") and i + 1 < len(toks):
            # `npm start`, `npm run foo`, `pnpm exec vite`.  Skip wrapper
            # + its subcommand token.
            i += 2
            continue
        if tok == "npx":
            # `npx <bin> ...`.  Skip npx + its bin token.
            i += 2
            continue
        if tok.startswith("./") or tok.startswith("/"):
            # Explicit path — user opted in knowingly.
            i += 1
            continue
        if tok in _NODE_BIN_NAMES:
            errors.append(
                f"  start command invokes '{tok}' directly — prod PATH doesn't\n"
                f"    include node_modules/.bin, so the container will exit 127.\n"
                f"    fix: use 'npm start' (or 'npm run <script>') or 'npx {tok} ...'"
            )
        i += 1
    return errors


def _llm_deep_audit(repo: Path) -> list[str]:
    """Upload polaris.yaml + Dockerfile + package.json::scripts to the
    platform's ``POST /projects/{id}/prepublish-audit`` endpoint and
    translate the returned {severity, hint, fix} issues into failure
    lines (for severity=error) or informational lines printed but NOT
    blocking (for severity=warning).  Best-effort — if the endpoint
    errors out or the LLM fails, we return an empty list (audit stays
    advisory, doesn't gatekeep on infrastructure hiccups)."""
    try:
        import yaml  # noqa: F401  (kept consistent with _audit_polaris_yaml)
    except ImportError:
        return []
    polaris_yaml = (repo / "polaris.yaml").read_text() if (repo / "polaris.yaml").is_file() else ""
    dockerfile = (repo / "Dockerfile").read_text() if (repo / "Dockerfile").is_file() else ""
    scripts: dict[str, str] = {}
    pkg_path = repo / "package.json"
    if pkg_path.is_file():
        try:
            pkg = json.loads(pkg_path.read_text())
            raw_scripts = pkg.get("scripts") or {}
            if isinstance(raw_scripts, dict):
                scripts = {str(k): str(v) for k, v in raw_scripts.items()}
        except Exception:  # noqa: BLE001
            pass
    if not polaris_yaml and not dockerfile and not scripts:
        return []

    try:
        resp = api_post(
            f"/projects/{project_id()}/prepublish-audit",
            {
                "polaris_yaml": polaris_yaml,
                "dockerfile": dockerfile,
                "package_json_scripts": scripts,
            },
            timeout=60,
        )
    except SystemExit:
        # api_post calls _fail on HTTP error; deep audit is best-effort
        # so we swallow and return empty rather than kill the whole
        # prepublish-audit.
        _info("  (deep audit: platform call failed, skipping)")
        return []

    issues = resp.get("issues") or []
    errors: list[str] = []
    for issue in issues:
        sev = issue.get("severity")
        hint = issue.get("hint") or ""
        fix = issue.get("fix") or ""
        if sev == "error":
            line = f"  [deep] {hint}"
            if fix:
                line += f"\n    fix: {fix}"
            errors.append(line)
        else:
            # Warnings go to stdout but don't block the publish preflight.
            _info(f"  [deep warn] {hint}" + (f"  (fix: {fix})" if fix else ""))
    return errors


def _audit_polaris_yaml(repo: Path) -> list[str]:
    """Scan polaris.yaml for known misconfigurations that would make the
    prod container fail at runtime.  Currently: bare node_modules bins in
    the start command for node / spa stacks."""
    manifest_path = repo / "polaris.yaml"
    if not manifest_path.is_file():
        # Nothing to audit; publish path has its own missing-manifest handling.
        return []
    try:
        import yaml  # PyYAML ships in the workspace image
    except ImportError:
        return []
    try:
        manifest = yaml.safe_load(manifest_path.read_text()) or {}
    except Exception as exc:  # noqa: BLE001
        return [f"  polaris.yaml is not valid YAML: {exc}"]
    out: list[str] = []
    stack = str(manifest.get("stack") or "")
    start = str(manifest.get("start") or "")
    if stack in ("node", "spa"):
        out.extend(_check_bare_node_bins(start))
    return out


STACK_DESCRIPTIONS: dict[str, str] = {
    "spa":    "Static SPA builder — Vite / Astro / CRA → nginx. For client-side React/Vue/Svelte that compiles to dist/.",
    "node":   "Long-running Node server — Express, Next.js SSR, Fastify. For apps that listen on a port.",
    "python": "Python web server — FastAPI, Django, Flask. For WSGI/ASGI apps.",
    "static": "Pre-built HTML/CSS/JS served as-is. For sites where NOTHING needs to build — just upload files.",
    "custom": "No template. You author your own Dockerfile, compose.prod.yml, and polaris.yaml.",
}

STACK_DETECT_REASONS: dict[str, str] = {
    "spa":    'package.json has "vite" in (dev)dependencies',
    "node":   "package.json present (no vite dep found)",
    "python": "requirements.txt or pyproject.toml present",
    "static": "index.html present, no package.json",
    "custom": "no recognized marker files",
}


# ── Output helpers ─────────────────────────────────────────────────────────

def _stderr(msg: str) -> None:
    sys.stderr.write(msg + "\n")


def _info(msg: str) -> None:
    sys.stdout.write(msg + "\n")


def _fail(msg: str, code: int = 1) -> None:
    _stderr(f"✗ {msg}")
    sys.exit(code)


# ── Repo discovery ─────────────────────────────────────────────────────────

def repo_root() -> Path:
    """Resolve the git repo root. Error out if we're not inside one."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], stderr=subprocess.DEVNULL
        )
    except subprocess.CalledProcessError:
        _fail("not inside a git repository (run `git init` in your project root first)")
    return Path(out.decode().strip())


def current_short_hash(cwd: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short=12", "HEAD"], cwd=cwd, stderr=subprocess.DEVNULL
        )
    except subprocess.CalledProcessError:
        _fail("HEAD has no commits yet — run `git add . && git commit` first")
    return out.decode().strip()


# ── scaffold-publish ───────────────────────────────────────────────────────

def detect_stack(repo: Path) -> str:
    """Pick the recommended publish stack for this repo.

    Vite-shaped SPAs (React/Vue/Svelte that compile to dist/) are
    distinguished from a plain Node server by peeking at the
    ``package.json`` dependencies for a ``vite`` key.  Anything else
    falls back to the marker-file heuristics.  The auto-pick is only a
    hint — the caller is expected to show the full menu and let the
    agent / user override via ``--stack``.
    """
    pkg = repo / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
        deps = {
            **(data.get("dependencies") or {}),
            **(data.get("devDependencies") or {}),
        }
        if "vite" in deps:
            return "spa"
        return "node"
    if (repo / "requirements.txt").exists() or (repo / "pyproject.toml").exists():
        return "python"
    if (repo / "index.html").exists():
        return "static"
    return "custom"


def render_template_text(text: str, replacements: dict[str, str]) -> str:
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


def cmd_scaffold_publish(args: argparse.Namespace) -> None:
    repo = repo_root()
    detected = detect_stack(repo)

    # Menu mode: no --stack means "show me the choices".  Codex reads
    # this output, picks a stack based on actual project intent (which
    # it knows better than our marker heuristic), and re-invokes with
    # --stack=<choice>.  No files written.
    if args.stack is None:
        _info("Available publish stacks:")
        for name, desc in STACK_DESCRIPTIONS.items():
            _info(f"  {name:<7} — {desc}")
        _info("")
        _info(f"Auto-detected for this repo: {detected}")
        _info(f"  (reason: {STACK_DETECT_REASONS[detected]})")
        _info("")
        _info("Pick one and re-run:")
        _info(f"  polaris scaffold-publish --stack={detected}     # recommended")
        _info("  polaris scaffold-publish --stack=<other>")
        _info("")
        _info(
            "Add --force to overwrite existing Dockerfile / compose.prod.yml / "
            "polaris.yaml."
        )
        return

    stack = args.stack
    tpl_dir = TEMPLATES_ROOT / stack
    if not tpl_dir.is_dir():
        _fail(f"no template for stack={stack} (looked in {tpl_dir})")

    service = args.service or "web"
    default_port = {"spa": 80, "node": 3000, "python": 8000, "static": 80}.get(stack, 80)
    port = str(args.port or default_port)

    # Reasonable defaults Codex can edit afterwards.
    default_build = {
        "spa": "npm run build",
        "node": "npm run build",
        "python": "",
        "static": "",
    }.get(stack, "")
    default_start = {
        "spa": "",  # nginx CMD comes from the base image
        "node": "npm start",
        "python": "python -m uvicorn app:app --host 0.0.0.0 --port " + port,
        "static": "",
    }.get(stack, "")

    build_cmd = args.build or default_build
    start_cmd = args.start or default_start

    replacements = {
        "__POLARIS_SERVICE__": service,
        "__POLARIS_PORT__": port,
        "__POLARIS_BUILD_CMD__": build_cmd or "true",  # Dockerfile RUN can't be empty
        "__POLARIS_START_CMD__": start_cmd,
        "__POLARIS_START_CMD_JSON__": json.dumps(start_cmd.split()) if start_cmd else '["true"]',
    }

    targets = ["Dockerfile", "compose.prod.yml", "polaris.yaml"]
    for name in targets:
        src = tpl_dir / name
        if not src.is_file():
            continue
        dst = repo / name
        if dst.exists() and not args.force:
            _info(f"  skip {name} (exists; pass --force to overwrite)")
            continue
        dst.write_text(render_template_text(src.read_text(), replacements))
        _info(f"  write {name}")

    _info(f"✓ scaffolded stack={stack} (service={service}, port={port})")
    _info("  Review the files above, tweak as needed, then `git add . && git commit`.")


# ── prepublish-audit ───────────────────────────────────────────────────────

def tracked_files(repo: Path) -> list[str]:
    out = subprocess.check_output(["git", "ls-files", "-z"], cwd=repo)
    return [p for p in out.decode().split("\0") if p]


def cmd_prepublish_audit(args: argparse.Namespace) -> None:
    repo = repo_root()
    failures: list[str] = []

    files = tracked_files(repo)

    # 1. Filename blacklist
    for path in files:
        for pat in SECRET_FILENAME_PATTERNS:
            if pat.search(path):
                failures.append(
                    f"  tracked secret-looking file: {path}  (pattern: {pat.pattern})\n"
                    f"    fix: git rm --cached {path!r} && echo {path!r} >> .gitignore"
                )
                break

    # 2. Size ceiling
    for path in files:
        full = repo / path
        try:
            size = full.stat().st_size
        except FileNotFoundError:
            continue
        if size > MAX_TRACKED_FILE_BYTES:
            failures.append(
                f"  oversized tracked file: {path}  ({size // (1024 * 1024)} MB > 10 MB)\n"
                f"    fix: git rm --cached {path!r} && echo {path!r} >> .gitignore"
            )

    # 3. Content scan (only on small-ish text files to keep it fast)
    for path in files:
        full = repo / path
        if not full.is_file():
            continue
        try:
            size = full.stat().st_size
        except FileNotFoundError:
            continue
        if size > 512 * 1024:  # skip >512 KB files for content scan
            continue
        try:
            data = full.read_bytes()
        except OSError:
            continue
        for pat, label in SECRET_CONTENT_PATTERNS:
            m = pat.search(data)
            if m:
                line_no = data[: m.start()].count(b"\n") + 1
                failures.append(
                    f"  possible {label} in {path}:{line_no}\n"
                    f"    fix: scrub the value, rotate the real credential, then re-commit"
                )
                break

    # 4. polaris.yaml / start-command sanity (known runtime traps)
    failures.extend(_audit_polaris_yaml(repo))

    # 5. Optional: LLM-assisted deep review — gated behind --deep so the
    # default `prepublish-audit` stays offline and instant.
    if getattr(args, "deep", False):
        failures.extend(_llm_deep_audit(repo))

    if failures:
        _stderr("✗ prepublish audit failed:\n")
        for f in failures:
            _stderr(f)
            _stderr("")
        sys.exit(1)

    _info(f"✓ audit passed ({len(files)} tracked files, no secrets detected)")


# ── HTTP helper (talks to platform API) ────────────────────────────────────

def api_base() -> str:
    return os.environ.get("POLARIS_API_URL", "http://host.docker.internal:8000").rstrip("/")


def api_headers() -> dict[str, str]:
    token = os.environ.get("POLARIS_WORKSPACE_TOKEN", "")
    if not token:
        _fail(
            "POLARIS_WORKSPACE_TOKEN not set — this CLI is meant to run inside an "
            "polaris workspace container. If you're debugging outside, export the "
            "token from the workspace row in the DB."
        )
    return {
        "Content-Type": "application/json",
        "X-Polaris-Workspace-Token": token,
    }


def project_id() -> str:
    pid = os.environ.get("POLARIS_PROJECT_ID", "")
    if not pid:
        _fail("POLARIS_PROJECT_ID not set (injected by the platform at container start)")
    return pid


def api_post(path: str, body: dict[str, Any], timeout: float = 30) -> dict[str, Any]:
    url = api_base() + path
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST", headers=api_headers()
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode(errors="replace")
        _fail(f"API {path} → HTTP {exc.code}: {payload}")
    except urllib.error.URLError as exc:
        _fail(f"API {path} unreachable: {exc}")


def api_get(path: str, timeout: float = 15) -> Any:
    url = api_base() + path
    req = urllib.request.Request(url, headers=api_headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode(errors="replace")
        _fail(f"API {path} → HTTP {exc.code}: {payload}")
    except urllib.error.URLError as exc:
        _fail(f"API {path} unreachable: {exc}")


def api_delete(path: str, timeout: float = 30) -> None:
    url = api_base() + path
    req = urllib.request.Request(url, method="DELETE", headers=api_headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode(errors="replace")
        _fail(f"API {path} → HTTP {exc.code}: {payload}")
    except urllib.error.URLError as exc:
        _fail(f"API {path} unreachable: {exc}")


def api_stream_sse(path: str, on_event: callable) -> None:
    """Consume a Server-Sent Events stream line by line; call `on_event(json_data)`."""
    url = api_base() + path
    req = urllib.request.Request(url, headers=api_headers())
    with urllib.request.urlopen(req, timeout=None) as resp:
        for raw in resp:
            line = raw.decode(errors="replace").rstrip("\n")
            if not line or line.startswith(":"):
                continue
            if line.startswith("data:"):
                data = line[5:].strip()
                if data:
                    try:
                        on_event(json.loads(data))
                    except json.JSONDecodeError:
                        pass


# ── publish ────────────────────────────────────────────────────────────────

def cmd_publish(args: argparse.Namespace) -> None:
    _info("Running prepublish-audit...")
    audit = subprocess.run([sys.argv[0], "prepublish-audit"])
    if audit.returncode != 0:
        _fail("audit failed — fix the issues above and re-run.")

    resp = api_post(
        f"/projects/{project_id()}/publish",
        {"dry_run": args.dry_run},
    )
    deployment_id = resp["id"]
    _info(f"✓ queued deployment {deployment_id[:8]} (dry_run={args.dry_run})")
    _info("  streaming build log...")

    def on_event(ev: dict[str, Any]) -> None:
        kind = ev.get("type")
        if kind == "log":
            sys.stdout.write(ev.get("data", ""))
            sys.stdout.flush()
        elif kind == "status":
            _info(f"  → status={ev.get('status')}")
        elif kind == "ready":
            _info(f"✓ {ev.get('domain')}")
        elif kind == "failed":
            _info(f"✗ failed: {ev.get('error')}")

    try:
        api_stream_sse(f"/deployments/{deployment_id}/events", on_event)
    except KeyboardInterrupt:
        _info("\n(detached — deployment continues in background)")


# ── rollback ───────────────────────────────────────────────────────────────

def cmd_rollback(args: argparse.Namespace) -> None:
    resp = api_post(
        f"/projects/{project_id()}/rollback",
        {"git_commit_hash": args.commit},
    )
    _info(f"✓ rollback queued → deployment {resp['id'][:8]}")


# ── dev-up / dev-down / dev-list ──────────────────────────────────────────

# Human-friendly descriptions of what each service provides in dev.
_DEV_SERVICE_BLURBS = {
    "postgres": "Postgres 16 accessible at postgres:5432 (user=app, db=app).",
    "redis": "Redis 7 accessible at redis:6379 (no auth).",
}


def _merge_env_file(env_path: Path, additions: dict[str, str]) -> list[str]:
    """Append any missing keys in `additions` to .env at env_path.
    Never overwrites existing keys. Returns the list of actually-added keys."""
    env_path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines: list[str] = []
    existing_keys: set[str] = set()
    if env_path.exists():
        existing_lines = env_path.read_text().splitlines()
        for line in existing_lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key:
                    existing_keys.add(key)

    added: list[str] = []
    for key, value in additions.items():
        if key in existing_keys:
            continue
        added.append(key)

    if not added:
        return []

    if not existing_lines:
        header = [
            "# Polaris dev-up generated — do not commit.",
            "# This file is gitignored by the baseline .gitignore.",
        ]
        new_lines = header + [""] + [f"{k}={additions[k]}" for k in added]
    else:
        # Append section marker + new lines; never touch existing content.
        new_lines = list(existing_lines)
        if new_lines and new_lines[-1] != "":
            new_lines.append("")
        new_lines.append("# Polaris dev-up — appended")
        new_lines.extend(f"{k}={additions[k]}" for k in added)

    env_path.write_text("\n".join(new_lines) + "\n")
    return added


def _project_env_path() -> Path:
    """Find the project root (git root) and return its .env path.
    Falls back to the current working directory if not in a git repo yet
    (e.g. immediately after `polaris dev-up` and before Codex scaffolds)."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
        )
        return Path(out.decode().strip()) / ".env"
    except subprocess.CalledProcessError:
        return Path.cwd() / ".env"


def cmd_dev_up(args: argparse.Namespace) -> None:
    if args.service is None:
        cmd_dev_list(args)
        return
    resp = api_post(
        f"/projects/{project_id()}/workspace/dev-deps",
        {"service": args.service},
        timeout=120,  # container start + healthcheck ~20-60s
    )
    service = resp["service"]
    container = resp["container_name"]
    status = resp["status"]
    env: dict[str, str] = resp.get("env_jsonb") or {}

    # Print connection credentials but do NOT write .env automatically.
    # Reason: `polaris dev-up` often runs BEFORE scaffolding (e.g. before
    # `create-next-app .`).  Writing .env to the workspace root at this
    # point makes the directory non-empty and breaks in-place scaffolders.
    # The agent reads these values from stdout and writes .env itself at
    # the right time and place (after scaffolding, into the project root).
    _info(f"✓ {service} is up")
    _info(f"  container: {container} ({status})")
    blurb = _DEV_SERVICE_BLURBS.get(service)
    if blurb:
        _info(f"  {blurb}")
    if env:
        _info("  connection env (write these to your project .env AFTER scaffolding):")
        for k, v in env.items():
            _info(f"    {k}={v}")


def cmd_dev_down(args: argparse.Namespace) -> None:
    api_delete(f"/projects/{project_id()}/workspace/dev-deps/{args.service}")
    _info(f"✓ {args.service} stopped and removed")
    _info(f"  (the DATABASE_URL / REDIS_URL lines in .env are left alone; clean them up manually if you care)")


def cmd_dev_list(_args: argparse.Namespace) -> None:
    rows = api_get(f"/projects/{project_id()}/workspace/dev-deps")
    if not rows:
        _info("(no dev deps enabled)")
        _info("  run `polaris dev-up postgres` or `polaris dev-up redis` to add one.")
        return
    for r in rows:
        env = r.get("env_jsonb") or {}
        env_display = ", ".join(f"{k}={v}" for k, v in env.items()) or "(none)"
        _info(f"  {r['service']:<10}  {r['status']:<10}  {r['container_name']}")
        _info(f"              env: {env_display}")


# ── status ─────────────────────────────────────────────────────────────────

def cmd_status(_args: argparse.Namespace) -> None:
    url = api_base() + f"/projects/{project_id()}/deployments?limit=5"
    req = urllib.request.Request(url, headers=api_headers())
    with urllib.request.urlopen(req, timeout=15) as resp:
        items = json.loads(resp.read().decode())
    if not items:
        _info("(no deployments yet)")
        return
    for d in items:
        hash_short = (d.get("git_commit_hash") or "")[:7] or "—"
        domain = d.get("domain") or "—"
        _info(f"  {d['status']:<11}  {hash_short}  {domain}  {d['created_at']}")


# ── Entry point ────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(prog="polaris", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_scaffold = sub.add_parser(
        "scaffold-publish",
        help="print stack menu (no --stack) or write template for the chosen stack",
    )
    p_scaffold.add_argument(
        "--stack",
        choices=["spa", "node", "python", "static", "custom"],
        help="If omitted, prints the stack menu + detection and exits without writing.",
    )
    p_scaffold.add_argument("--service", help="service name (default: web)")
    p_scaffold.add_argument("--port", type=int, help="service port")
    p_scaffold.add_argument("--build", help="build command")
    p_scaffold.add_argument("--start", help="start command")
    p_scaffold.add_argument("--force", action="store_true", help="overwrite existing files")
    p_scaffold.set_defaults(func=cmd_scaffold_publish)

    p_audit = sub.add_parser("prepublish-audit", help="scan for secrets + size issues")
    p_audit.add_argument(
        "--deep",
        action="store_true",
        help="additionally run a platform-side LLM review of polaris.yaml + "
        "Dockerfile + package.json scripts for likely runtime failures",
    )
    p_audit.set_defaults(func=cmd_prepublish_audit)

    p_pub = sub.add_parser("publish", help="build + smoke + deploy current commit")
    p_pub.add_argument("--dry-run", action="store_true", help="build + smoke only, don't promote")
    p_pub.set_defaults(func=cmd_publish)

    p_roll = sub.add_parser("rollback", help="redeploy an older commit by short hash")
    p_roll.add_argument("commit", help="short git commit hash of the target deployment")
    p_roll.set_defaults(func=cmd_rollback)

    p_stat = sub.add_parser("status", help="last few deployments")
    p_stat.set_defaults(func=cmd_status)

    p_devup = sub.add_parser(
        "dev-up",
        help="start a dev-time dependency container (postgres / redis) "
        "alongside the workspace; writes connection env to .env",
    )
    p_devup.add_argument(
        "service", nargs="?", choices=["postgres", "redis"],
        help="which service to start; omit to list currently enabled deps",
    )
    p_devup.set_defaults(func=cmd_dev_up)

    p_devdown = sub.add_parser(
        "dev-down", help="stop + remove a dev-time dependency container and drop its volume"
    )
    p_devdown.add_argument("service", choices=["postgres", "redis"])
    p_devdown.set_defaults(func=cmd_dev_down)

    p_devlist = sub.add_parser(
        "dev-list", help="show currently enabled dev-time dependencies",
    )
    p_devlist.set_defaults(func=cmd_dev_list)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

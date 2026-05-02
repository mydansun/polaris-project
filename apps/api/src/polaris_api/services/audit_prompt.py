"""LLM-assisted prepublish audit — system prompt + response parser.

Invoked from ``routes/audit.py`` when the workspace CLI calls
``polaris prepublish-audit --deep``.  The LLM reads the user's
``polaris.yaml`` + ``Dockerfile`` + ``package.json::scripts`` and returns
a list of concrete runtime failures it predicts (not style preferences).

Kept deliberately short + opinionated.  Static rules in the CLI
(``_check_bare_node_bins``) already cover the most common trap; this
pass picks up semantic mismatches a regex can't see — e.g. ``port`` in
polaris.yaml disagreeing with the port the start cmd actually binds,
migration commands that aren't idempotent in prod, build output dir
mismatched with what start serves.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


AUDIT_SYSTEM_PROMPT = """You are a deployment-correctness reviewer for Node
/ Python web apps deployed via Docker + docker-compose behind Traefik.

Given the user's polaris.yaml, Dockerfile, and package.json scripts,
enumerate concrete runtime failures (the prod container would exit
non-zero or the HTTP probe would never succeed) or misconfigurations.

ONLY report things that WILL fail in production — not style nits, not
"you could improve ...".  Each issue is JSON:

  {"severity": "error"|"warning", "hint": "<one-line explanation>", "fix": "<one-line action>"}

Use `severity: "error"` for things that will deterministically break
publish; `severity: "warning"` for likely-but-not-certain problems.

Categories to watch (non-exhaustive):
- Bare framework binaries invoked from start without npm/npx wrapper
  (next / vite / tsc / astro / tsx ...) — the prod PATH does not
  include node_modules/.bin unless the Dockerfile explicitly adds it.
- polaris.yaml `port` disagreeing with the port the start command
  actually binds to.
- Start command references a package.json script that does not exist.
- Start command uses a migration tool that's NOT idempotent (e.g.
  `prisma migrate dev`) — prod must use `prisma migrate deploy` or
  `prisma db push`.
- Build step produces `dist/` but start serves `build/` or vice versa.
- Dockerfile CMD and polaris.yaml::start command diverge silently.

Return ONLY a JSON array.  Empty array = no issues found.  No code
fence, no preamble, no trailing prose."""


def parse_audit_response(raw: str) -> list[dict]:
    """Extract the JSON array from the model output.  Tolerates an
    optional ```json code fence (models often add one despite instructions).
    Drops items missing required keys or with invalid `severity`.
    Returns [] on any parse failure — audit is best-effort and must
    never 500 the endpoint."""
    stripped = raw.strip()
    if stripped.startswith("```json"):
        stripped = stripped[len("```json"):].strip()
    elif stripped.startswith("```"):
        stripped = stripped[3:].strip()
    if stripped.endswith("```"):
        stripped = stripped[:-3].strip()
    try:
        parsed = json.loads(stripped)
    except Exception:  # noqa: BLE001
        logger.warning("audit: LLM output was not JSON (first 120 chars: %r)", raw[:120])
        return []
    if not isinstance(parsed, list):
        return []
    valid: list[dict] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        sev = item.get("severity")
        hint = item.get("hint")
        if sev not in ("error", "warning") or not isinstance(hint, str) or not hint.strip():
            continue
        valid.append(
            {
                "severity": sev,
                "hint": hint.strip(),
                "fix": str(item.get("fix") or "").strip(),
            }
        )
    return valid


# Extract a compact string representation of the user's polaris.yaml +
# Dockerfile + package.json scripts for the LLM's human message.
def format_audit_inputs(
    polaris_yaml: str, dockerfile: str, package_json_scripts: dict[str, str]
) -> str:
    scripts_pretty = (
        json.dumps(package_json_scripts, indent=2, sort_keys=True)
        if package_json_scripts
        else "(none)"
    )
    return (
        "polaris.yaml:\n```yaml\n"
        f"{polaris_yaml.strip() or '(empty)'}\n"
        "```\n\n"
        "Dockerfile:\n```dockerfile\n"
        f"{dockerfile.strip() or '(empty)'}\n"
        "```\n\n"
        "package.json scripts:\n```json\n"
        f"{scripts_pretty}\n"
        "```"
    )


# Re-export a regex for truncating oversized inputs (safety) — caller
# clamps input sizes before sending to the LLM.
MAX_FIELD_CHARS = 16_000


def clamp(text: str, limit: int = MAX_FIELD_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"

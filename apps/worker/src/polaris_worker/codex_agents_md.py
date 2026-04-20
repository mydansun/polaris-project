"""Render + write design-intent AGENTS.md into the workspace container's
``$CODEX_HOME`` (global scope).

Codex discovers AGENTS.md globally before project-scope, so this location
is read on every turn regardless of CWD.  The codex-home is a per-workspace
named docker volume (see ``infra/workspace/codex-app-server.conf``), so we
use ``docker exec -i`` + stdin to write it — no bind mount needed.

``CodexAgent`` calls ``write_codex_home_agents_md`` right before
``session.run_turn``, so the first thing Codex reads as its plan round
starts is the freshly-rendered brief.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
from typing import Any
from uuid import UUID

import asyncpg

logger = logging.getLogger(__name__)

BEGIN_MARKER = "<!-- polaris:design-intent:begin -->"
END_MARKER = "<!-- polaris:design-intent:end -->"

_CODEX_HOME_AGENTS_PATH = "/home/workspace/.codex/AGENTS.md"
MOOD_BOARD_CONTAINER_PATH = "/home/workspace/mood_board.png"


def _workspace_container_name(workspace_id: UUID) -> str:
    """Mirror of ``runner._workspace_container_name`` — keep the naming rule
    in exactly one place by cross-import when CodexAgent is ready."""
    hash_id = str(workspace_id).replace("-", "")[:24]
    return f"polaris-ws-{hash_id}"


def _format_kv_list(d: dict[str, Any]) -> str:
    lines: list[str] = []
    for key, value in d.items():
        if value is None or value == [] or value == {}:
            continue
        if isinstance(value, (list, dict)):
            rendered = (
                "```json\n" + json.dumps(value, ensure_ascii=False, indent=2) + "\n```"
            )
            lines.append(f"- **{key}**:\n{rendered}")
        else:
            lines.append(f"- **{key}**: {value}")
    return "\n".join(lines) if lines else "- _(none)_"


def render_design_intent_markdown(brief: dict[str, Any]) -> str:
    """Render the design-intent block (wrapped in begin/end markers).

    ``brief`` is the persisted shape — keys ``intent``, ``brief``,
    ``pinterest_queries``.  ``pinterest_refs`` is deliberately NOT rendered:
    image URLs and base64 image bytes must not leak into AGENTS.md per the
    product rule that Codex should read design guidance as text only —
    the visual direction has already been translated into the compiled
    brief's prose by the multimodal compiler node.
    """
    intent = brief.get("intent") or {}
    compiled_brief = brief.get("brief") or ""
    queries = brief.get("pinterest_queries") or []
    mood_board_url = brief.get("mood_board_url")

    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    queries_md = ", ".join(f"`{q}`" for q in queries) if queries else "_(none)_"

    mood_board_section = (
        "## Mood board\n\n"
        f"A generated mood board image is saved at `{MOOD_BOARD_CONTAINER_PATH}` "
        "(absolute path inside this workspace).  If you need visual detail, "
        "open / view that file — it's a designer's mood board collage.\n\n"
        "**It is an atmosphere reference only — NOT a screenshot of the "
        "page you are building.**  Use it to inform palette, material "
        "feel, typography character, and composition rhythm.  Do **not** "
        "attempt to recreate the collage layout, tiles, or specific "
        "imagery in the generated UI.  The page you build is an ordinary "
        "web page whose visual system is inspired by this mood, not a "
        "copy of the board.\n\n"
        if mood_board_url else ""
    )

    return (
        f"{BEGIN_MARKER}\n"
        f"# Design Intent\n\n"
        f"_Managed by Polaris — updated {now}. Edit via re-discover, not by hand._\n\n"
        f"## Intent fields\n\n"
        f"{_format_kv_list(intent)}\n\n"
        f"## Reference queries\n\n"
        f"{queries_md}\n\n"
        f"{mood_board_section}"
        f"## Full compiled brief\n\n"
        f"{compiled_brief.strip()}\n"
        f"{END_MARKER}\n"
    )


async def write_codex_home_agents_md(
    *, container: str, content: str
) -> None:
    """Stream ``content`` into ``$CODEX_HOME/AGENTS.md`` inside ``container``.

    Uses ``docker exec -i`` so we don't need a shared host tempfile.
    Raises ``RuntimeError`` on non-zero exit — caller decides whether to
    swallow (logging) or propagate (fail the run).
    """
    cmd = (
        "mkdir -p /home/workspace/.codex && "
        f"cat > {_CODEX_HOME_AGENTS_PATH}"
    )
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "exec",
        "-i",
        container,
        "sh",
        "-c",
        cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate(content.encode("utf-8"))
    if proc.returncode != 0:
        raise RuntimeError(
            f"docker exec write AGENTS.md to {container} failed "
            f"(exit={proc.returncode}): {stderr.decode(errors='replace')}"
        )
    logger.info("wrote design-intent AGENTS.md to %s:%s", container, _CODEX_HOME_AGENTS_PATH)


async def write_mood_board_to_workspace(
    *, container: str, image_b64: str
) -> None:
    """Write a base64-encoded PNG into the workspace container at
    ``$HOME/mood_board.png`` so Codex's ``turn/start`` can reference it
    as a ``localImage`` input without round-tripping through S3.

    Input is base64 text piped via stdin to ``base64 -d`` inside the
    container — sidesteps any binary-in-stdin edge cases with docker's
    stream handling.

    Raises ``RuntimeError`` on non-zero exit; caller logs and degrades
    (no mood_board input on subsequent turns until rewritten)."""
    cmd = (
        "mkdir -p /home/workspace && "
        f"base64 -d > {MOOD_BOARD_CONTAINER_PATH}"
    )
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "exec",
        "-i",
        container,
        "sh",
        "-c",
        cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate(image_b64.encode("ascii"))
    if proc.returncode != 0:
        raise RuntimeError(
            f"docker exec write mood_board to {container} failed "
            f"(exit={proc.returncode}): {stderr.decode(errors='replace')}"
        )
    logger.info(
        "wrote mood_board to %s:%s (%d b64 chars)",
        container,
        MOOD_BOARD_CONTAINER_PATH,
        len(image_b64),
    )


async def load_active_design_intent(
    conn: asyncpg.Connection,
    conn_lock: Any,
    project_id: UUID,
) -> dict[str, Any] | None:
    """Fetch the active design-intent row shaped for ``render_design_intent_markdown``.

    Returns ``None`` if the project has never run discovery or if the
    current intent has been superseded.
    """
    async with conn_lock:
        row = await conn.fetchrow(
            "SELECT intent_jsonb, compiled_brief, pinterest_refs_jsonb, "
            "pinterest_queries_jsonb, mood_board_url FROM design_intents "
            "WHERE project_id=$1 AND status='active' LIMIT 1",
            project_id,
        )
    if row is None:
        return None

    def _maybe_load(v: Any) -> Any:
        return json.loads(v) if isinstance(v, str) else v

    return {
        "intent": _maybe_load(row["intent_jsonb"]) or {},
        "brief": row["compiled_brief"] or "",
        "pinterest_refs": _maybe_load(row["pinterest_refs_jsonb"]) or [],
        "pinterest_queries": _maybe_load(row["pinterest_queries_jsonb"]) or [],
        "mood_board_url": row["mood_board_url"],
    }
